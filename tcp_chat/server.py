"""
tcp_chat/server.py
==================
A multi-client TCP chat server using Python's socket + threading modules.

HOW IT WORKS (big picture):
  1. The server creates ONE "listening" socket and binds it to a port.
  2. It blocks on accept(), waiting for clients to connect.
  3. Each time a client connects, accept() returns a NEW, dedicated socket
     for that client. The server assigns that client a numeric id and a
     generic name, spawns a thread to handle it, then immediately goes back
     to waiting for the next connection.
  4. Each thread reads messages from its client in a loop and calls
     broadcast() to forward the message to every other connected client.

CLIENT IDENTITY / METADATA:
  Every connection gets a small metadata record -- id, generic name, remote
  address and connection time -- stored in `clients`.  That record is what the
  server prints on join, what prefixes every chat line ("[Kite #3] hello"),
  what the /ROSTER control message carries, and what lets each client list
  who else is in the room via the /clients command.

FILE TRANSFER:
  A client announces a transfer with a "/FILE <name> <bytes> [to=<selector>]"
  header.  The server resolves the selector against the live roster *before*
  forwarding anything: it either replies /SEND (the sender then streams the
  body, which is relayed only to the verified audience) or /ERR (nothing is
  transmitted).  That is what makes a private transfer private.

VIVA POINT -- Why a new socket per client?
  The listening socket's only job is to accept connection requests (the TCP
  handshake). Once the handshake is done, the OS hands you a fresh socket
  with its own send/receive buffers for that specific TCP connection.
  This is why you can have N clients on the same port number -- each gets
  its own (server_ip, server_port, client_ip, client_port) 4-tuple.

VIVA POINT -- Thread-safety:
  Python lists are not thread-safe for compound operations. If two threads
  simultaneously mutate `clients`, its internal state can be corrupted. We
  protect every access with a Lock. A Lock is a mutual-exclusion primitive:
  only one thread can hold it at a time. "with clients_lock:" does the
  acquire/release for us.
"""

import argparse
import os
import socket
import sys
import threading
import time
from itertools import count

# Make the project root importable regardless of the launch directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common_info import (
    addr_str,
    ctrl,
    describe,
    describe_client,
    format_file_header,
    format_roster_table,
    format_time,
    is_probably_text,
    normalize_target,
    parse_ctrl,
    parse_file_opts,
    pick_name,
    render_roster,
    render_room_notice,
    render_target_label,
    resolve_target,
    target_token,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = '127.0.0.1'   # localhost -- only accepts connections from this machine
PORT = 65432          # arbitrary high port; anything > 1024 needs no root/admin

BOUND_HOST = HOST     # the interface actually bound, for the /HELLO banner
CLIENT_IDS = count(1) # monotonic source of client ids

# ---------------------------------------------------------------------------
# Shared state -- the list of currently connected clients
#
# Each entry is a metadata record:
#   {'id': 3, 'name': 'Kite', 'socket': <socket>, 'addr': (ip, port),
#    'joined_at': <epoch seconds>}
# ---------------------------------------------------------------------------
clients = []                 # list of client records
clients_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Socket helpers
# ---------------------------------------------------------------------------
def _snapshot():
    """A shallow copy of the client list, taken under the lock."""
    with clients_lock:
        return list(clients)


def _send_to(record, payload):
    """Send a payload to one client.  Returns False if that client is gone."""
    try:
        record['socket'].sendall(payload)
        return True
    except OSError:
        return False


def _recipients(sender_socket):
    """Every client record except the one identified by `sender_socket`."""
    return [r for r in _snapshot() if r['socket'] is not sender_socket]


def broadcast(message, sender_socket):
    """
    Iterate over all connected clients and forward `message` to each one,
    skipping the client who sent it (so you don't echo back to the sender).

    Args:
        message       : the raw bytes received from the sender
        sender_socket : the socket of the client who sent the message
    """
    for record in _snapshot():
        if record['socket'] is not sender_socket:
            try:
                # socket.sendall() is important here:
                #
                # VIVA POINT -- Why sendall(), not send()?
                #   TCP is a STREAM protocol -- it has no concept of
                #   "message boundaries". socket.send() may send only
                #   PART of the data if the kernel's send buffer is full.
                #   socket.sendall() loops internally until every byte is
                #   delivered to the kernel buffer (or raises an exception).
                _send_to(record, message)
            except OSError:
                # If sending fails, the client is probably gone.
                # handle_client() will detect the disconnect cleanly on its
                # next recv() call.
                pass


def broadcast_roster():
    """
    Push a fresh /ROSTER to everybody after a join or a leave.

    Marked why=update so clients stay silent: a roster nobody asked for is noise.
    """
    records = _snapshot()
    for record in records:
        _send_to(record, render_roster(records, record['id'], why='update') + b'\n')


# ---------------------------------------------------------------------------
# Control messages
# ---------------------------------------------------------------------------
def _send_hello(record):
    """Tell a freshly accepted client who it is."""
    payload = ctrl(
        "HELLO",
        f"id={record['id']}",
        f"name={record['name']}",
        f"addr={addr_str(record['addr'])}",
        "protocol=TCP",
        f"server={addr_str((BOUND_HOST, PORT))}",
        f"joined={format_time(record['joined_at'])}",
    )
    _send_to(record, payload + b'\n')


def _handle_client_command(record, data):
    """
    Handle a client -> server control message.

    Returns True if the message was consumed and must not be treated as chat.
    """
    tag, _fields = parse_ctrl(data)

    if tag == 'CLIENTS':
        records = _snapshot()
        _send_to(record, render_roster(records, record['id'], why='ask') + b'\n')
        # The client gets names and ids; the full connection detail is printed
        # here, on the server console, where it belongs.
        print(f"[CLIENTS] {describe(record)} requested the roster.")
        print(format_roster_table(records, record['id']))
        return True

    if tag == 'WHOAMI':
        records = _snapshot()
        _send_to(record, render_roster(records, record['id'], why='ask') + b'\n')
        print(f"[CLIENTS] {describe(record)} asked who it is.")
        print(format_roster_table(records, record['id']))
        return True

    return False


# ---------------------------------------------------------------------------
# File transfer framing
# ---------------------------------------------------------------------------
def _resolve_recipients(record, filename, filesize, spec):
    """
    Work out who a /FILE header may reach, and tell the sender the verdict.

    Returns (recipients, label, error) where `recipients` is the list of client
    records that will get the file (empty when `error` is set) and `label` is the
    resolved target ('all', or 'Kite#3').

    The verdict is sent BEFORE the header is forwarded, so a sender that has not
    been told /SEND yet has provably not put a single body byte on the wire.
    """
    everyone = _snapshot()
    others = _recipients(record['socket'])
    target, error = resolve_target(everyone, record['id'], normalize_target(spec),
                                   len(others))

    if error is not None:
        code, arg = error
        print(f"[FILE] {describe(record)} asked to send '{filename}' to "
              f"{arg or '(nobody)'} -- REFUSED ({code}). Nothing was transmitted.")
        _send_to(record, ctrl(
            "ERR", "kind=file", f"code={code}", f"arg={arg}", f"name={filename}",
        ) + b'\n')
        return [], 'all', error

    recipients = [target] if target is not None else others
    label = target_token(target)
    print(f"[FILE] {describe(record)} wants to send '{filename}' ({filesize} bytes) "
          f"to {render_target_label(label)} -- VERIFIED, {len(recipients)} recipient(s).")

    _send_to(record, ctrl(
        "SEND", "kind=file", "status=accepted", f"name={filename}",
        f"bytes={filesize}", f"target={label}", f"recipients={len(recipients)}",
    ) + b'\n')
    return recipients, label, None


def _forward_file(record, data):

    """
    Handle an incoming "/FILE <name> <filesize> [to=<selector>]\n" header.

    The recipient selector is resolved and checked first.  Only if it names
    exactly one connected client (or the sender asked for a broadcast) does the
    header reach anybody; the body is then relayed strictly to that audience.

    Returns True if the header was handled (so the caller can `continue` instead
    of treating the payload as chat text).
    """
    header_end = data.find(b'\n')
    if header_end == -1:
        return False

    initial_data = data[header_end + 1:]

    parts = data[:header_end].decode(errors='ignore').strip().split(' ')
    if len(parts) < 3:
        return False
    filename = parts[1]
    try:
        filesize = int(parts[2])
    except ValueError:
        return False

    options = parse_file_opts(parts[3:])
    recipients, label, error = _resolve_recipients(record, filename, filesize,
                                                   options.get('to'))

    if error is not None:
        # Refused: no header and no body byte go to anybody.
        #
        # VIVA POINT -- why nothing is drained here:
        #   The sender is waiting for this verdict before it writes a single body
        #   byte, so there is provably no payload in flight to throw away.  And we
        #   must NOT go looking for some: TCP cannot tell "leftover file bytes"
        #   apart from "the next message", so a read here would happily swallow
        #   the sender's next /FILE header or chat line and silently break the
        #   session.  (A client that streams a body without asking first is
        #   violating the protocol; its bytes are caught by the unframed-binary
        #   check in handle_client and suppressed rather than relayed.)
        return True

    # 1. Forward the header to the verified audience only. It carries `from=` so
    #    the receiver knows who is sending it, and `to=` so the capture on the
    #    wire shows a private transfer was the one that happened.
    header = (format_file_header(filename, filesize, record, label) + '\n').encode()
    for recipient in recipients:
        _send_to(recipient, header)

    def relay(chunk, _sender_socket):
        for recipient in recipients:
            _send_to(recipient, chunk)

    # 2. Forward any file bytes that arrived in the same recv() as the header.
    if initial_data:
        relay(initial_data, record['socket'])

    # 3. Strict byte-counting loop for the rest. This is what stops the server
    #    from corrupting binary file data by prepending "[Kite #3] " text to it.
    import file_transfer
    forwarded = file_transfer.forward_file(
        record['socket'],
        max(0, filesize - len(initial_data)),
        relay,
        record['socket'],
    )

    delivered = forwarded + len(initial_data)
    if delivered < filesize:
        status = 'incomplete'
        print(f"[FILE] {describe(record)} disconnected mid-transfer "
              f"({delivered}/{filesize} bytes relayed).")
    else:
        status = 'ok'
        print(f"[FILE] {describe(record)} finished sending '{filename}' "
              f"({filesize} bytes relayed to {render_target_label(label)}).")

    # 4. Acknowledge the upload back to the SENDER.
    _send_to(record, ctrl(
        "ACK", "kind=file", f"status={status}", f"name={filename}",
        f"bytes={filesize}", f"recipients={len(recipients)}", f"target={label}",
    ) + b'\n')

    return True


def _announce_join(record):
    """Tell the room that somebody arrived -- by name only."""
    broadcast((render_room_notice(record, 'joined') + '\n').encode(),
              record['socket'])


def _announce_leave(record):
    """Tell the room that somebody left -- by name only."""
    broadcast((render_room_notice(record, 'left') + '\n').encode(),
              record['socket'])


# ---------------------------------------------------------------------------
# handle_client() -- runs in its own thread, one per connected client
# ---------------------------------------------------------------------------
def handle_client(client_socket, addr):
    """
    Dedicated per-client loop: read messages and broadcast them.

    Args:
        client_socket : the socket object for THIS specific client
        addr          : (ip_string, port_int) of the remote client
    """
    # Register the client and give it a generic name. Everything happens under
    # the lock so ids and names can never be handed out twice.
    with clients_lock:
        client_id = next(CLIENT_IDS)
        record = {
            'id': client_id,
            'name': pick_name(client_id, {c['name'] for c in clients}),
            'socket': client_socket,
            'addr': addr,
            'joined_at': time.time(),
        }
        clients.append(record)

    print(f"[NEW CONNECTION] {describe(record)} connected.")
    print(format_roster_table(_snapshot()))
    print()

    # Hand the client its own identity first -- it renders its banner from this.
    _send_hello(record)
    broadcast_roster()
    _announce_join(record)

    # -----------------------------------------------------------------------
    # Main receive loop for this client
    # -----------------------------------------------------------------------
    while True:
        try:
            # recv(BUFFER_SIZE) blocks until:
            #   a) up to BUFFER_SIZE bytes of data arrive, OR
            #   b) the connection is closed (returns b''), OR
            #   c) an error occurs (raises OSError)
            #
            # VIVA POINT -- Partial reads on TCP:
            #   Even with recv(4096), you might get LESS than 4096 bytes if
            #   the sender's data didn't fill the buffer yet. For a simple
            #   line-based chat we get away with this because each "message"
            #   is short. For file transfer we MUST loop until we've received
            #   exactly as many bytes as expected.
            data = client_socket.recv(4096)

            if not data:
                # recv() returning empty bytes means the client sent a TCP FIN
                # packet -- it closed its end of the connection gracefully.
                #
                # VIVA POINT -- TCP FIN / connection teardown:
                #   When a client calls socket.close(), the OS sends a FIN
                #   segment. The server's recv() returns b'' to signal EOF.
                print(f"[DISCONNECT] {describe(record)} disconnected (graceful close / FIN).")
                break

            # --- File transfer header -------------------------------------
            if data.startswith(b'/FILE ') and _forward_file(record, data):
                continue

            # --- Other control messages (/clients, /whoami) ----------------
            if data.startswith(b'/') and _handle_client_command(record, data):
                continue

            # --- Binary that arrived with no recognisable header ------------
            # Never print it and never prefix it: doing either would put file
            # bytes on the console or corrupt the stream for other clients.
            if not is_probably_text(data):
                print(f"[BINARY] {describe(record)} sent {len(data)} unframed byte(s); "
                      f"suppressed (not displayed, not relayed).")
                continue

            # Prefix with the client's name and id, then forward.
            tagged = f"[{record['name']} #{record['id']}] ".encode() + data
            print(f"[MSG] {tagged.decode(errors='replace').strip()}")
            broadcast(tagged, client_socket)

        except OSError as e:
            # An abrupt disconnect (e.g., client process killed) raises an
            # OSError instead of returning b''. We handle both cases by
            # breaking out of the loop.
            print(f"[ERROR] {describe(record)} connection error: {e}")
            break

    # -----------------------------------------------------------------------
    # Cleanup -- runs once the loop exits (on disconnect or error)
    # -----------------------------------------------------------------------
    with clients_lock:
        clients[:] = [r for r in clients if r['socket'] is not client_socket]

    _announce_leave(record)
    broadcast_roster()
    client_socket.close()
    print(f"[CLOSED] Socket for {describe(record)} closed.")


# ---------------------------------------------------------------------------
# start_server() -- creates the listening socket and the accept() loop
# ---------------------------------------------------------------------------
def start_server(host=HOST):
    """
    Bind to host:PORT, listen for incoming connections, and for each one
    spawn a daemon thread running handle_client().
    """
    global BOUND_HOST
    BOUND_HOST = host

    # socket.AF_INET  -> IPv4 address family
    # socket.SOCK_STREAM -> TCP (reliable, ordered, stream-based)
    #   Compare: SOCK_DGRAM would give us UDP (udp_chat/)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # SO_REUSEADDR lets us re-bind to the same port immediately after the
    # server is restarted, without waiting for the OS TIME_WAIT period to expire.
    #
    # VIVA POINT -- TIME_WAIT:
    #   After a TCP connection closes, the OS keeps the port in TIME_WAIT for
    #   ~2xMSL (Maximum Segment Lifetime, typically 60s) to absorb any delayed
    #   packets. Without SO_REUSEADDR, restarting the server quickly raises
    #   "Address already in use". This flag bypasses that restriction.
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Bind: associate this socket with a local (IP, port) pair.
    # '127.0.0.1' means we only accept connections from the local machine.
    # Use '0.0.0.0' to accept from any network interface.
    server_socket.bind((host, PORT))

    # listen(backlog): mark the socket as passive (listening).
    # backlog=5 -> the OS will queue up to 5 not-yet-accept()ed connections.
    server_socket.listen(5)

    if host == '0.0.0.0':
        print(f"[LISTENING] Server is up on 0.0.0.0:{PORT}")
        print("  (Listening on all interfaces. Reachable on your LAN.)")
        try:
            print(f"  (Likely LAN IP: {socket.gethostbyname(socket.gethostname())})")
        except Exception:
            pass
    else:
        print(f"[LISTENING] Server is up on {host}:{PORT}")
    print("  New clients are assigned an automatic id and a generic name (Falcon, "
          "Otter, Kite, ...). No username needed.")

    # -----------------------------------------------------------------------
    # Accept loop -- runs forever in the main thread
    # -----------------------------------------------------------------------
    while True:
        # accept() BLOCKS here until a client completes the TCP 3-way handshake.
        # It returns a new socket object + the client's (ip, port).
        #
        # VIVA POINT -- The TCP 3-way handshake:
        #   1. Client sends SYN  (sequence number = X)
        #   2. Server replies SYN-ACK  (seq = Y, ack = X+1)
        #   3. Client replies ACK  (ack = Y+1)
        #   After step 3, the connection is ESTABLISHED and accept() returns.
        client_socket, addr = server_socket.accept()

        thread = threading.Thread(
            target=handle_client,
            args=(client_socket, addr),
            daemon=True
        )
        thread.start()
        # handle_client() prints the assigned id/name as soon as it registers.
        print(f"[ACTIVE CONNECTIONS] {threading.active_count() - 1}")
        # We subtract 1 because threading.active_count() includes the main thread.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="TCP Chat Server")
    parser.add_argument('--host', type=str, default=HOST,
                        help="Interface to listen on (default: 127.0.0.1, use 0.0.0.0 for LAN)")
    args = parser.parse_args()
    start_server(args.host)

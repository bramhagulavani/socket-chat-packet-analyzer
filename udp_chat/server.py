"""
udp_chat/server.py
==================
A multi-client UDP chat server.

HOW IT WORKS (big picture):
  1. UDP is CONNECTIONLESS. The server does not call accept() or spawn a
     new socket per client. It has exactly ONE socket.
  2. Every time a datagram (packet) arrives, recvfrom() gives us the data
     AND the source address (ip, port) of who sent it.
  3. We maintain a "registry" (a dict) of all addresses we've seen, keyed by
     (ip, port) and holding the same metadata the TCP server keeps: client id,
     generic name and join time.
  4. To broadcast, we simply loop through the registry and sendto() every
     address except the sender's.

CLIENT IDENTITY / METADATA:
  A client's very first datagram -- an empty "join beacon" -- is what registers
  it.  The server immediately answers that one client with a /HELLO control
  message carrying its assigned id and generic name (Falcon, Otter, Kite, ...),
  then pushes a /ROSTER to everybody so all clients know who is in the room.

FILE TRANSFER ACKNOWLEDGEMENT:
  UDP has no FIN and no handshake, so "the upload is over" has to be said out
  loud.  The sender emits a /FILEEND datagram when it stops; the server answers
  with an /ACK reporting how many chunks actually made it onto the wire.  If
  the simulator dropped chunks, the ACK comes back status=lossy with the count.

  The same connectionless design applies to choosing a recipient.  The optional
  `to=` selector on the /FILE header is resolved before the header is reflected:
  the sender is told /SEND (and then sends its chunks) or /ERR (and then sends
  nothing at all).  The audience is pinned in the transfer state so no chunk of a
  private file can be reflected to the rest of the room.

VIVA POINT -- TCP vs UDP Server Architecture:
  In TCP, the server needed a new thread for every client because calling
  recv() on a client's dedicated socket would block.
  In UDP, all clients send datagrams to the SAME server port. The OS queues
  them up. A single thread looping over recvfrom() can handle hundreds of
  clients sequentially because it's just popping discrete packets off a single
  queue!
"""

import argparse
import os
import socket
import sys
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
HOST = '127.0.0.1'
PORT = 55001

BOUND_HOST = HOST      # the interface actually bound, for the /HELLO banner
CLIENT_IDS = count(1)  # monotonic source of client ids

# ---------------------------------------------------------------------------
# Shared state
#
# `clients` maps (ip, port) -> metadata record:
#   {'id': 3, 'name': 'Kite', 'addr': (ip, port), 'joined_at': <epoch>}
#
#   `transfers` maps (ip, port) -> in-flight file transfer bookkeeping, so the
#   server knows when to stop waiting for chunks and acknowledge the upload.  It
#   also pins the *audience*: `addresses` is the list of clients the /FILE header
#   was verified for, and it is the only place those chunks are ever reflected to.
# ---------------------------------------------------------------------------
clients = {}
transfers = {}


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------
def _records():
    return list(clients.values())


def _send_to(server_socket, addr, payload):
    try:
        server_socket.sendto(payload, addr)
    except OSError as e:
        print(f"[ERROR] Could not send to {addr_str(addr)}: {e}")


def _broadcast(server_socket, payload, sender_addr):
    """Send a payload to every registered client except the sender."""
    for addr in clients:
        if addr != sender_addr:
            _send_to(server_socket, addr, payload)


def _broadcast_roster(server_socket):
    """
    Push a fresh /ROSTER to every registered client.

    Marked why=update so clients stay silent: a roster nobody asked for is noise.
    """
    records = _records()
    for addr, record in clients.items():
        _send_to(server_socket, addr,
                 render_roster(records, record['id'], why='update'))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def _register(server_socket, addr):
    """Assign an id + generic name to a newly seen address and announce it."""
    client_id = next(CLIENT_IDS)
    record = {
        'id': client_id,
        'name': pick_name(client_id, {r['name'] for r in clients.values()}),
        'addr': addr,
        'joined_at': time.time(),
    }
    clients[addr] = record

    print(f"[NEW CLIENT] {describe(record)} registered from {addr_str(addr)}.")
    print(format_roster_table(_records()))
    print()

    # Tell the newcomer who it is, then tell everybody who else is here.
    _send_to(server_socket, addr, ctrl(
        "HELLO",
        f"id={record['id']}",
        f"name={record['name']}",
        f"addr={addr_str(addr)}",
        "protocol=UDP",
        f"server={addr_str((BOUND_HOST, PORT))}",
        f"joined={format_time(record['joined_at'])}",
    ))
    _send_to(server_socket, addr,
             render_roster(_records(), record['id'], why='update'))

    # Clients are told a name, not an address.
    _broadcast(server_socket,
               (render_room_notice(record, 'joined') + '\n').encode(), addr)
    _broadcast_roster(server_socket)


# ---------------------------------------------------------------------------
# Control messages
# ---------------------------------------------------------------------------
def _handle_client_command(server_socket, addr, data):
    """
    Handle a client -> server control message.

    Returns True if the message was consumed and must not be reflected as chat.
    """
    tag, _fields = parse_ctrl(data)

    if tag in ('CLIENTS', 'WHOAMI'):
        record = clients.get(addr)
        viewer = record['id'] if record else None
        records = _records()
        _send_to(server_socket, addr, render_roster(records, viewer, why='ask'))
        if record:
            # The client gets names and ids; the connection detail is printed
            # here, on the server console, where it belongs.
            print(f"[CLIENTS] {describe(record)} requested the roster.")
            print(format_roster_table(records, viewer))
        return True

    return False


# ---------------------------------------------------------------------------
# File transfer bookkeeping
# ---------------------------------------------------------------------------
def _begin_transfer(server_socket, addr, data):
    """
    Handle an incoming '/FILE <name> <total_chunks> [to=<selector>]' header.

    This datagram is the transfer announcement.  The recipient selector is
    resolved here, before any chunk exists, and the sender is told the verdict
    (/SEND or /ERR) before the header goes out.  The verified audience is stored
    with the transfer so every later chunk is reflected to exactly those
    addresses -- which is what keeps a private file private.
    """
    parts = data.split(b' ')
    if len(parts) < 3:
        return

    filename = parts[1].decode(errors='ignore')
    try:
        total_chunks = int(parts[2])
    except ValueError:
        return

    options = parse_file_opts(t.decode('ascii', 'replace') for t in parts[3:])

    sender = clients.get(addr)
    who = describe(sender) if sender else addr_str(addr)
    others = [a for a in clients if a != addr]
    target, error = resolve_target(_records(), sender['id'] if sender else None,
                                   normalize_target(options.get('to')), len(others))

    if error is not None:
        code, arg = error
        print(f"[FILE] {who} asked to send '{filename}' to {arg or '(nobody)'} "
              f"-- REFUSED ({code}). Nothing was transmitted.")
        # No transfer state is created, so if chunks do arrive anyway they are
        # dropped by _forward_chunk instead of being reflected to the room.
        _send_to(server_socket, addr, ctrl(
            "ERR", "kind=file", f"code={code}", f"arg={arg}", f"name={filename}",
        ))
        return

    recipients = [target['addr']] if target is not None else others
    label = target_token(target)

    transfers[addr] = {
        'name': filename,
        'total': total_chunks,
        'forwarded': 0,
        'recipients': len(recipients),
        'addresses': list(recipients),
        'target': label,
    }

    print(f"[FILE] {who} wants to send '{filename}' ({total_chunks} chunks) to "
          f"{render_target_label(label)} -- VERIFIED, {len(recipients)} recipient(s).")

    # 1. The verdict goes to the sender alone...
    _send_to(server_socket, addr, ctrl(
        "SEND", "kind=file", "status=accepted", f"name={filename}",
        f"chunks={total_chunks}", f"target={label}", f"recipients={len(recipients)}",
    ))

    # 2. ...and only then does the announcement reach the verified audience.
    header = format_file_header(filename, total_chunks, sender, label).encode()
    for dest in recipients:
        _send_to(server_socket, dest, header)


def _forward_chunk(server_socket, addr, data):
    """
    Reflect one '/CHUNK <seq> <bytes>' datagram to that transfer's recipients.

    VIVA POINT -- Why is the audience pinned instead of re-broadcasting?
      Reflecting to "everybody except the sender" would hand a private file to
      the entire room on every single chunk.  The address list captured when the
      /FILE header was verified is the only audience allowed to see the bytes.
    """
    state = transfers.get(addr)
    if state is None:
        # No transfer was accepted from this sender, so there is no audience
        # entitled to these bytes.
        print(f"[FILE] {addr_str(addr)} sent a chunk with no accepted transfer; "
              f"dropped (not reflected to anybody).")
        return

    for dest in state['addresses']:
        _send_to(server_socket, dest, data)

    state['forwarded'] += 1

    # If the loss simulator swallowed chunks, this threshold is never reached
    # and the transfer is finalised by the /FILEEND datagram instead.
    if state['forwarded'] >= state['total']:
        _finish_transfer(server_socket, addr)


def _finish_transfer(server_socket, addr):
    """Acknowledge a completed upload back to the sender."""
    state = transfers.pop(addr, None)
    if state is None:
        print(f"[FILE] {addr_str(addr)} sent /FILEEND with no accepted transfer; "
              f"ignored.")
        return

    missing = max(0, state['total'] - state['forwarded'])
    status = 'ok' if missing == 0 else 'lossy'

    record = clients.get(addr)
    who = describe(record) if record else addr_str(addr)
    label = state['target']
    audience = render_target_label(label)

    if status == 'ok':
        print(f"[FILE] {who} finished '{state['name']}' -- all {state['total']} "
              f"chunks reflected to {audience} ({state['recipients']} client(s)).")
    else:
        print(f"[FILE] {who} finished '{state['name']}' -- only "
              f"{state['forwarded']}/{state['total']} chunks reached the wire. "
              f"{missing} chunk(s) were lost and UDP has no mechanism to ask for "
              f"them back.")

    _send_to(server_socket, addr, ctrl(
        "ACK", "kind=file", f"status={status}", f"name={state['name']}",
        f"chunks={state['total']}", f"forwarded={state['forwarded']}",
        f"missing={missing}", f"recipients={state['recipients']}",
        f"target={label}",
    ))


# ---------------------------------------------------------------------------
# start_server() -- one socket, one thread, endless recvfrom() loop
# ---------------------------------------------------------------------------
def start_server(host=HOST):
    global BOUND_HOST
    BOUND_HOST = host

    # socket.SOCK_DGRAM -> UDP (Unreliable, connectionless, datagram-based)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # UDP only needs bind(). No listen() or accept().
    server_socket.bind((host, PORT))

    if host == '0.0.0.0':
        print(f"[LISTENING] UDP Server is up on 0.0.0.0:{PORT}")
        print("  (Listening on all interfaces. Reachable on your LAN.)")
        try:
            print(f"  (Likely LAN IP: {socket.gethostbyname(socket.gethostname())})")
        except Exception:
            pass
    else:
        print(f"[LISTENING] UDP Server is up on {host}:{PORT}")
    print("  New clients are assigned an automatic id and a generic name (Falcon, "
          "Otter, Kite, ...) the first time they send a datagram.")

    while True:
        try:
            # recvfrom() returns the payload AND the sender's address.
            # We use 8192 to ensure we can fit our 4096-byte chunks + headers.
            data, addr = server_socket.recvfrom(8192)

        except KeyboardInterrupt:
            print("\n[SHUTTING DOWN] Server closed.")
            break
        except OSError as e:
            print(f"[ERROR] Socket error: {e}")
            continue

        # A client's first datagram of any kind registers it. An empty datagram
        # is our explicit "join beacon" and carries nothing to reflect.
        if addr not in clients:
            _register(server_socket, addr)
        if not data:
            continue

        # ------------------------------------------------------------------
        # Dispatch by application-layer frame
        # ------------------------------------------------------------------
        if data.startswith(b'/FILE '):
            # Header format: "/FILE <​filename> <total_chunks>"
            _begin_transfer(server_socket, addr, data)

        elif data.startswith(b'/CHUNK '):
            # Chunk format: "/CHUNK <seq_num> <binary_data>"
            _forward_chunk(server_socket, addr, data)

        elif data.startswith(b'/FILEEND'):
            # The sender says it is done. This is UDP's stand-in for TCP's FIN,
            # and it is what makes the acknowledgement possible at all.
            _finish_transfer(server_socket, addr)

        elif data.startswith(b'/'):
            if not _handle_client_command(server_socket, addr, data):
                print(f"[NOTE] Ignoring unknown control message from {addr_str(addr)}.")

        elif is_probably_text(data):
            # Chat message -- reflect it verbatim to everyone else.
            _broadcast(server_socket, data, addr)

        else:
            # Binary with no recognisable frame. Dropping it is correct: UDP has
            # no stream to resynchronise on, and guessing would corrupt a file.
            print(f"[BINARY] {addr_str(addr)} sent {len(data)} unframed byte(s); "
                  f"dropped (no stream to resynchronise on).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="UDP Chat Server")
    parser.add_argument('--host', type=str, default=HOST,
                        help="Interface to listen on (default: 127.0.0.1, use 0.0.0.0 for LAN)")
    args = parser.parse_args()
    start_server(args.host)

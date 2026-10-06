"""
tcp_chat/client.py
==================
A TCP chat client with a simple CLI interface.

HOW IT WORKS (big picture):
  The client has TWO jobs happening simultaneously:
    1. READ from the keyboard and send typed messages to the server.
    2. READ from the socket and print incoming messages from other clients.

  These two jobs would block each other if done sequentially, so we split
  them across two threads:
    - Main thread  -> handles user keyboard input (blocking input() calls)
    - Receive thread -> handles incoming server messages (blocking recv() calls)

IDENTITY:
  The server assigns every connection a client id and a generic name (Falcon,
  Otter, Kite, ...) and sends it back in a /HELLO control message.  All this
  client prints for it is one line -- "<name> (<id>) connected - TCP chat" -- and
  then an input line to type on.  Commands are never listed here; /help is one
  keystroke away.  Anything the server knows about a connection that the client
  does not need (addresses, join times) is not sent to the client at all.

FILE TRANSFER:
  File bytes are never echoed to the terminal.  The /FILE header switches the
  receive thread into a strict byte-counting loop that writes straight to disk,
  and any unframed binary payload is counted and dropped rather than printed.

  The sender picks the recipient in the same command as the path
  ("/sendfile notes.txt to=#2").  That selector rides out on the header, and this
  client waits for the server's /SEND or /ERR before writing a single body byte --
  so it always knows, before and after, exactly which client is receiving the
  file.

VIVA POINT -- Why two threads?
  Both input() and socket.recv() are BLOCKING calls. If you called them in
  sequence, you'd have to finish reading from the socket before you could
  type, or vice-versa. Threads let both operations run concurrently so the
  UI stays responsive while messages arrive.
"""

import argparse
import os
import socket
import sys
import threading

# Make the project root importable regardless of the launch directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common_info import (
    FileVerdict,
    addr_str,
    ctrl,
    format_ack,
    format_roster_names,
    is_probably_text,
    parse_ctrl,
    parse_file_opts,
    parse_roster_peers,
    parse_send_command,
    render_connected,
    render_help,
    render_target_label,
)

# ---------------------------------------------------------------------------
# Configuration -- must match server.py
# ---------------------------------------------------------------------------
HOST = '127.0.0.1'
PORT = 65432

# Identity assigned by the server.  Populated from the /HELLO control message.
ME = {'id': None, 'name': None, 'addr': None, 'joined': None}

# Verdict queue for outgoing files.  The receive thread drops the server's /SEND
# or /ERR answer in here; the send loop (the thread blocked on input()) picks it
# up.  Nothing else reads the socket, so this hand-off is what makes "verify the
# recipient, then send" possible at all.
VERDICT = FileVerdict()


# ---------------------------------------------------------------------------
# The input line
#
# The one thing the user looks for: a named prompt they type after.  Exactly one
# prompt is drawn per command, and it is redrawn after anything else is printed,
# so the cursor is always somewhere the user can type.
# ---------------------------------------------------------------------------
PROMPT_VISIBLE = False


def prompt_text():
    return f"{ME['name'] or '...'} > "


def show_prompt():
    global PROMPT_VISIBLE
    if ME['name'] is None:
        return          # nothing to call the user yet; wait for the /HELLO
    if not PROMPT_VISIBLE:
        sys.stdout.write(prompt_text())
        sys.stdout.flush()
        PROMPT_VISIBLE = True


def _incoming_prefix():
    """
    Start an incoming message on its own line if it would land on the input line.

    If the prompt is on screen, the message would otherwise be printed straight
    after it and look like something the user had entered.  We deliberately do NOT
    redraw the prompt here: the main thread draws exactly one per command, so
    redrawing on every arriving message would leave prompts stacked up.
    """
    global PROMPT_VISIBLE
    on_prompt = PROMPT_VISIBLE
    PROMPT_VISIBLE = False
    return '\n' if on_prompt else ''


def say(text, prompt=False):
    """
    Print a client-side notice.

    `prompt=True` re-draws the input line afterwards, which is what we want after
    a reply to something the user asked for (/clients, /help, an /ACK): the answer
    ends the turn, so the next thing they do is type.
    """
    print(f"{_incoming_prefix()}{text}", flush=True)
    if prompt:
        show_prompt()


def _print_connected():
    """The one line printed when the server has named us and we can chat."""
    say(render_connected(ME['name'], ME['id'], 'TCP'))
    # Now that we have a name, this is the moment to show where to type.
    show_prompt()


# ---------------------------------------------------------------------------
# Incoming payload handling
# ---------------------------------------------------------------------------
def _echo(data):
    """Print an incoming payload unless it is raw file content."""
    if not data:
        return
    if not is_probably_text(data):
        say("[FILE DATA] {0} byte(s) written to disk, not shown.".format(len(data)))
        return
    sys.stdout.write(_incoming_prefix())
    print(data.decode(errors='replace'), end='', flush=True)


def _handle_control(line, server_host, server_port):
    """
    Render a server -> client control message.

    Returns True if `line` was a control message and has been fully handled.
    """
    tag, fields = parse_ctrl(line)
    if tag is None:
        return False

    if tag == 'HELLO':
        ME['id'] = fields.get('id')
        ME['name'] = fields.get('name')
        ME['addr'] = fields.get('addr')
        ME['joined'] = fields.get('joined')
        # The server has registered us and named us: we can chat from here on.
        _print_connected()
        return True

    if tag == 'ROSTER':
        # Background updates stay silent -- otherwise every join would dump a
        # roster nobody asked for.  Only /clients prints the list.
        if fields.get('why') == 'ask':
            peers = parse_roster_peers(fields)
            say(format_roster_names(peers, fields.get('you')), prompt=True)
        return True

    if tag == 'ACK':
        say(format_ack(fields), prompt=True)
        return True

    if tag in ('SEND', 'ERR'):
        # A verdict on an outgoing file. Park it for the send loop instead of
        # printing here: the thread that asked for the transfer is the one that
        # has to decide whether to stream the body or give up.
        VERDICT.resolve(tag, fields)
        return True

    return False


def _enter_file_mode(sock, data):
    """
    Handle an incoming "/FILE <name> <filesize> [to=...] [from=...]\n" header.

    Returns True if the header was recognised (so the caller must not also try
    to print the payload).
    """
    header_end = data.find(b'\n')
    if header_end == -1:
        return False

    header = data[:header_end].decode(errors='ignore')
    initial_data = data[header_end + 1:]

    parts = header.split(' ')
    if len(parts) < 3:
        return False
    try:
        filesize = int(parts[2])
    except ValueError:
        return False

    # Extra fields are informational: who it came from, and whether it was
    # addressed to this client alone.
    options = parse_file_opts(parts[3:])
    sender = render_target_label(options['from']) if options.get('from') else None
    private = options.get('to', 'all') not in ('all', '-', '')

    # Switch from "chat mode" to "file receiving mode". The receiver writes
    # bytes straight to disk -- it never decodes or prints them.
    import file_transfer
    file_transfer.receive_file(sock, parts[1], filesize, initial_data,
                               sender=sender, private=private)
    show_prompt()
    return True


def _dispatch(sock, data, server_host, server_port):
    """
    Route one chunk of bytes from the server to the right handler.

    VIVA POINT -- one recv() is NOT one message:
      TCP is a byte stream, so the kernel is free to hand us several server
      messages in a single recv() -- and it usually does, because the server
      sends /HELLO, /ROSTER and the "has joined" notice back to back when we
      connect.  Handling only the first line would dump the rest on the screen as
      raw text and, worse, would swallow the /SEND verdict for a file we are
      waiting on.  So the chunk is split into lines and each one is routed
      separately.  File bytes never come through here: /FILE is handled above by
      byte count, not by line.
    """
    if data.startswith(b'/FILE ') and _enter_file_mode(sock, data):
        return

    if data.startswith(b'/'):
        for line in data.splitlines(keepends=True):
            # The server prefixes every chat line with "[Name #n] ", so a line
            # that starts with '/' is always a control message.
            stripped = line.rstrip(b'\r\n')
            if stripped.startswith(b'/') and _handle_control(
                    stripped, server_host, server_port):
                continue
            _echo(line)
        return

    _echo(data)


# ---------------------------------------------------------------------------
# receive_messages() -- runs in a background thread
# ---------------------------------------------------------------------------
def receive_messages(sock, server_host, server_port):
    """
    Continuously read messages arriving from the server and print them.
    This runs in a daemon thread so it dies when the main thread exits.
    """
    while True:
        try:
            data = sock.recv(4096)

            if not data:
                # Server closed the connection -- recv() returns b''.
                # This mirrors the same b'' sentinel on the server side.
                say("[SERVER CLOSED] The server has shut down.")
                sys.stdout.flush()
                break

            _dispatch(sock, data, server_host, server_port)

        except OSError:
            # Connection was reset or closed abruptly.
            say("[DISCONNECTED] Lost connection to server.")
            break


# ---------------------------------------------------------------------------
# start_client() -- connects and runs the send loop in the main thread
# ---------------------------------------------------------------------------
def start_client(host=HOST, port=PORT):
    """
    Connect to the server, start the receive thread, then loop reading
    keyboard input and sending it to the server.
    """
    global PROMPT_VISIBLE

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # connect() triggers the TCP 3-way handshake with the server.
    # It BLOCKS until the handshake completes (or raises OSError on failure).
    #
    # VIVA POINT -- connect() vs bind():
    #   The CLIENT does NOT need to call bind(). The OS automatically assigns
    #   an ephemeral (temporary) source port (e.g., 49152-65535) for the
    #   client side of the connection. Only servers that must be reachable at
    #   a known port call bind() explicitly.
    sock.connect((host, port))

    print(f"Connecting to {addr_str((host, port))}...", flush=True)

    # Start the receive thread BEFORE entering the send loop, so the connect line
    # is printed as soon as the server assigns us a name.
    recv_thread = threading.Thread(
        target=receive_messages,
        args=(sock, host, port),
        daemon=True
    )
    recv_thread.start()

    # -----------------------------------------------------------------------
    # Send loop -- runs in the main thread
    # -----------------------------------------------------------------------
    # input() blocks until the user presses Enter, giving us one line at a time.
    # Meanwhile the receive thread prints incoming messages in the background,
    # each on its own line, and puts the input line back after a reply.
    try:
        while True:
            # Exactly one prompt per command: nothing is drawn if it is already
            # on screen, and nothing is drawn before the server has named us.
            show_prompt()
            message = input()
            PROMPT_VISIBLE = False       # the user's Enter consumed the prompt

            if message.lower() == '/quit':
                # User wants to exit. Break out of the loop.
                print("[EXITING] Closing connection...")
                break

            if message.strip() in ('/help', '/?'):
                say(render_help(), prompt=True)
                continue

            # -----------------------------------------------------------------
            # VIVA POINT -- File Transfer Command
            #
            #   /sendfile <path>                 -> the whole room
            #   /sendfile <path> to=#2           -> client #2 and nobody else
            #   /sendfile <path> to=@Otter       -> same, by name
            #
            # The recipient is part of the command, so it is decided BEFORE the
            # transfer starts.  send_file() announces the header and waits for
            # the server to verify that recipient; only then are the bytes sent.
            # -----------------------------------------------------------------
            filepath, target, usage_error = parse_send_command(message)
            if usage_error:
                say(f"[ERROR] {usage_error}", prompt=True)
                continue
            if filepath:
                import file_transfer
                sent = file_transfer.send_file(sock, filepath, target=target,
                                              verdict=VERDICT)
                show_prompt()
                if sent:
                    say("Sent. Waiting for the server to confirm.")
                else:
                    say("Cancelled - nothing was sent. "
                        "Check the recipient with /clients.")
                continue

            # Show who else is in the room. The server answers with /ROSTER.
            if message.strip() in ('/clients', '/who', '/whoami'):
                sock.sendall(ctrl("CLIENTS") + b'\n')
                continue

            if message:
                # Append '\n' as a simple message delimiter so the server
                # (and other clients) can separate messages when printed.
                #
                # VIVA POINT -- Message framing:
                #   TCP is a byte stream with no built-in message boundaries.
                #   Here we use newline ('\n') as a delimiter, which works for
                #   simple text chat. For binary protocols (e.g., file transfer)
                #   you'd typically prefix each message with a fixed-size header
                #   containing the payload length, then read exactly that many
                #   bytes -- this is called "length-prefixed framing".
                sock.sendall((message + '\n').encode())

    except KeyboardInterrupt:
        # Ctrl+C in the terminal -- treat it as a graceful quit.
        print("\n[EXITING] KeyboardInterrupt -- closing connection.")
    except EOFError:
        # stdin closed (piped input ran out, or the terminal window went away).
        print("\n[EXITING] stdin closed -- closing connection.")

    finally:
        # Closing the socket sends a TCP FIN to the server, which causes the
        # server's recv() to return b'' and triggers its cleanup routine.
        sock.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="TCP Chat Client")
    parser.add_argument('--server-ip', type=str, default=HOST,
                        help="Server IP address (default: 127.0.0.1)")
    parser.add_argument('--port', type=int, default=PORT,
                        help=f"Server port (default: {PORT})")
    args = parser.parse_args()

    start_client(args.server_ip, args.port)

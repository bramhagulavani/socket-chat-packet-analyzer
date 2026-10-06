"""
udp_chat/client.py
==================
A UDP chat client.

HOW IT WORKS:
  1. We create a UDP socket. No connect() is required!
  2. To "join", we just send an empty dummy packet to the server so it can
     learn our IP and Port and add us to its registry. The server answers that
     one datagram with a /HELLO control message telling us the client id and
     generic name it assigned us.
  3. Like TCP, we run two threads: one for input(), one for recvfrom().

IDENTITY:
  Everything the /HELLO message produces is one line -- "<name> (<id>) connected
  - UDP chat" -- followed by an input line to type on.  Commands are never listed
  here; /help is one keystroke away.  The client is never told anything about the
  server's connections beyond a name and an id.

FILE TRANSFER:
  File bytes are never echoed. Chunks are written straight to disk at their
  sequence offset, unframed binary is counted and dropped, and the sender waits
  for the server's /ACK to learn whether every chunk actually made it.

  The sender picks the recipient in the same command as the path
  ("/sendfile notes.txt to=#2").  That selector rides out on the /FILE header,
  and this client waits for the server's /SEND or /ERR before sending a single
  chunk -- so it always knows, before and after, exactly which client receives
  the file.

VIVA POINT -- "Connecting" in UDP
  UDP is connectionless. The server doesn't know we exist until we send
  it a datagram. We send a hidden initialization message so the server
  adds our (ip, port) to its broadcast registry.
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

HOST = '127.0.0.1'
PORT = 55001

# Identity assigned by the server.  Populated from the /HELLO control message.
ME = {'id': None, 'name': None, 'addr': None, 'joined': None}

# Verdict queue for outgoing files.  The receive thread drops the server's /SEND
# or /ERR answer in here; the send loop (the thread blocked on input()) picks it
# up.  Nothing else reads the socket, so this hand-off is what makes "verify the
# recipient, then send" possible at all.
VERDICT = FileVerdict()

# Orphaned chunks (we joined after a /FILE header already went out) are counted
# rather than printed one warning per chunk.
ORPHANED_CHUNKS = 0


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


# ---------------------------------------------------------------------------
# Incoming payload handling
# ---------------------------------------------------------------------------
def _echo(data):
    """Print an incoming payload unless it is raw file content."""
    global ORPHANED_CHUNKS
    if not data:
        return
    if not is_probably_text(data):
        say("[FILE DATA] {0} byte(s) written to disk, not shown.".format(len(data)))
        return
    sys.stdout.write(_incoming_prefix())
    print(data.decode(errors='replace'), end='', flush=True)


def _handle_control(data, server_addr):
    """
    Render a server -> client control message.

    Returns True if `data` was a control message and has been fully handled.
    """
    global ORPHANED_CHUNKS

    tag, fields = parse_ctrl(data)
    if tag is None:
        return False

    if tag == 'HELLO':
        ME['id'] = fields.get('id')
        ME['name'] = fields.get('name')
        ME['addr'] = fields.get('addr')
        ME['joined'] = fields.get('joined')
        # The server has registered us and named us: we can chat from here on.
        say(render_connected(ME['name'], ME['id'], 'UDP'))
        show_prompt()
        return True

    if tag == 'ROSTER':
        # Background updates stay silent -- otherwise every join would dump a
        # roster nobody asked for.  Only /clients prints the list.
        if fields.get('why') == 'ask':
            peers = parse_roster_peers(fields)
            text = format_roster_names(peers, fields.get('you'))
            if ORPHANED_CHUNKS:
                text += (f"\n  ({ORPHANED_CHUNKS} file chunk(s) ignored - that "
                         f"transfer had already started)")
            say(text, prompt=True)
        return True

    if tag == 'ACK':
        say(format_ack(fields), prompt=True)
        return True

    if tag in ('SEND', 'ERR'):
        # A verdict on an outgoing file. Park it for the send loop instead of
        # printing here: the thread that asked for the transfer is the one that
        # has to decide whether to send its chunks or give up.
        VERDICT.resolve(tag, fields)
        return True

    return False


def _handle_interleaved(data, server_addr):
    """
    Handle a datagram that turned up while we were busy writing a file.

    In UDP, chat messages and /ROSTER updates share the same datagram queue as
    the file chunks, so anything that is not a chunk has to be routed back
    through the normal dispatcher instead of being treated as an error.
    """
    if data.startswith(b'/') and _handle_control(data, server_addr):
        return
    _echo(data)


def _dispatch(sock, data, server_addr):
    """Route one datagram from the server to the right handler."""
    global ORPHANED_CHUNKS

    # Check if this is the start of a file transfer
    if data.startswith(b'/FILE '):
        # Parse: "/FILE filename total_chunks [to=...] [from=...]"
        parts = data.split(b' ')
        if len(parts) >= 3:
            try:
                total_chunks = int(parts[2])
            except ValueError:
                total_chunks = None

            if total_chunks is not None:
                filename = parts[1].decode(errors='ignore')

                # Extra fields say who it came from and whether it was addressed
                # to this client alone.
                options = parse_file_opts(t.decode('ascii', 'replace')
                                          for t in parts[3:])
                sender = (render_target_label(options['from'])
                          if options.get('from') else None)
                private = options.get('to', 'all') not in ('all', '-', '')

                # Hand off to the blocking file receiver logic
                import udp_chat.file_transfer as ft
                ft.receive_file(sock, filename, total_chunks,
                                on_other=lambda payload: _handle_interleaved(payload, server_addr),
                                sender=sender, private=private)
                show_prompt()
                return

    # Check if it's an orphaned chunk (e.g., we joined late and missed /FILE)
    if data.startswith(b'/CHUNK '):
        ORPHANED_CHUNKS += 1
        return

    # Otherwise, it's a control message or normal chat.
    if data.startswith(b'/') and _handle_control(data, server_addr):
        return

    _echo(data)


# ---------------------------------------------------------------------------
# receive_messages() -- runs in a background thread
# ---------------------------------------------------------------------------
def receive_messages(sock, server_addr):
    while True:
        try:
            # We must use a buffer big enough to hold file chunks + headers (8192)
            data, _ = sock.recvfrom(8192)
            _dispatch(sock, data, server_addr)

        except OSError:
            say("[EXITING] Socket closed.")
            break


# ---------------------------------------------------------------------------
# start_client() -- registers with the server and runs the send loop
# ---------------------------------------------------------------------------
def start_client(loss_rate=0.0, host=HOST, port=PORT):
    global PROMPT_VISIBLE

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    server_addr = (host, port)

    print(f"Joining {addr_str(server_addr)}"
          f"{f' (loss {loss_rate * 100:.0f}%)' if loss_rate else ''}...",
          flush=True)

    # VIVA POINT -- "Connecting" in UDP
    # UDP is connectionless. The server doesn't know we exist until we send
    # it a datagram. We send a hidden initialization message so the server
    # adds our (ip, port) to its broadcast registry.
    sock.sendto(b'', server_addr)

    # Start the receive thread
    recv_thread = threading.Thread(target=receive_messages, args=(sock, server_addr), daemon=True)
    recv_thread.start()

    try:
        while True:
            # Exactly one prompt per command: nothing is drawn if it is already
            # on screen, and nothing is drawn before the server has named us.
            show_prompt()
            message = input()
            PROMPT_VISIBLE = False       # the user's Enter consumed the prompt

            if message.lower() == '/quit':
                print("[EXITING] Shutting down...")
                break

            if message.strip() in ('/help', '/?'):
                say(render_help(), prompt=True)
                continue

            # -----------------------------------------------------------------
            # File Transfer Command
            #
            #   /sendfile <path>                 -> the whole room
            #   /sendfile <path> to=#2           -> client #2 and nobody else
            #   /sendfile <path> to=@Otter       -> same, by name
            #
            # The recipient is part of the command, so it is decided BEFORE the
            # transfer starts.  send_file() announces the header and waits for
            # the server to verify that recipient; only then are chunks sent.
            # -----------------------------------------------------------------
            filepath, target, usage_error = parse_send_command(message)
            if usage_error:
                say(f"[ERROR] {usage_error}", prompt=True)
                continue
            if filepath:
                import udp_chat.file_transfer as ft
                sent = ft.send_file(sock, server_addr, filepath, loss_rate,
                                    target=target, verdict=VERDICT)
                show_prompt()
                if sent:
                    say("Sent. Waiting for the server to confirm.")
                else:
                    say("Cancelled - nothing was sent. "
                        "Check the recipient with /clients.")
                continue

            # Show who else is in the room. The server answers with /ROSTER.
            if message.strip() in ('/clients', '/who', '/whoami'):
                sock.sendto(ctrl("CLIENTS"), server_addr)
                continue

            if message:
                # Normal chat: format and send as a single datagram
                sock.sendto(f"{message}\n".encode(), server_addr)

    except KeyboardInterrupt:
        print("\n[EXITING] KeyboardInterrupt")
    except EOFError:
        print("\n[EXITING] stdin closed")

    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="UDP Chat Client")
    parser.add_argument('--loss-rate', type=float, default=0.0,
                        help='Probability of intentionally dropping a file chunk (0.0 to 1.0)')
    parser.add_argument('--server-ip', type=str, default=HOST,
                        help="Server IP address (default: 127.0.0.1)")
    parser.add_argument('--port', type=int, default=PORT,
                        help=f"Server port (default: {PORT})")
    args = parser.parse_args()

    start_client(args.loss_rate, args.server_ip, args.port)

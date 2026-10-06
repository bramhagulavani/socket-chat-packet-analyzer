"""
tcp_chat/file_transfer.py
=========================
Shared logic for sending and receiving files over an existing TCP socket.

VIVA POINT -- Why does this work reliably without custom retries?
  TCP is a "reliable, ordered byte stream".
  - Reliable: If a packet is lost, the sender's OS will automatically retransmit it.
  - Ordered: The receiver's OS reassembles packets in the exact sequence they were sent.
  Because we are using TCP, we don't need to write sequence numbers, checksums, or
  acknowledgments in our Python code. We simply push bytes into the socket on the
  sending side, and read them on the receiving side. We TRUST the OS TCP stack to
  deliver exactly the bytes we sent, in order.

VIVA POINT -- The Framing Problem:
  How do we send a file and chat messages on the same socket without them mixing?
  We use "framing". We define a special text header for files:
      "/FILE  filename filesize [to=<selector>] [from=<Name>#<id>]\n"
  When the receiver sees this, it stops treating incoming bytes as text chat, and
  switches to a strict byte-counting loop (receive_file) until exactly `filesize`
  bytes are read.

ANNOUNCE, VERIFY, THEN SEND:
  send_file() writes the header and stops.  The header carries the recipient the
  user picked (if any), so the server can check it before a single body byte is
  written.  Only after the server replies /SEND does the body go out; a refusal
  (/ERR) means the file never leaves this machine.

TERMINAL SAFETY:
  File content is never decoded or printed.  send_file() reports only counts, and
  receive_file() writes bytes straight to disk while showing a progress counter.
"""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common_info import (
    format_file_header,
    format_send_error,
    format_send_verdict,
)


def send_file(sock, filepath, target=None, verdict=None, timeout=5.0):
    """
    Reads a file from disk and streams it over the socket.

    Args:
        target  : the recipient selector the user typed after `to=` ('#2',
                  '@Kite', ...), or None to reach the whole room.  It is sent in
                  the header so the server can verify it FIRST.
        verdict : shared FileVerdict queue used to pick up the server's /SEND or
                  /ERR answer.  When given, no body byte is written until the
                  server has confirmed the recipient.

    Returns True if the file was sent, False if it was refused (nothing was sent).

    Nothing about the file's contents is printed -- only the name, the size and
    a chunk counter, so a multi-megabyte transfer cannot flood the terminal.
    """
    if not os.path.isfile(filepath):
        print(f"[ERROR] File '{filepath}' does not exist.")
        return False

    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)

    # 1. Send the framing header -- the announcement, carrying the chosen
    #    recipient. The '\n' is our delimiter.
    sock.sendall((format_file_header(filename, filesize, target=target) + '\n').encode())

    # 2. Wait for the server's verdict. Nothing is streamed until it says yes.
    if verdict is not None:
        who = target or 'the room'
        print(f"[SENDING] '{filename}' ({filesize} bytes) to {who} "
              f"- waiting for the server to check the recipient...", flush=True)
        arrived, tag, fields = verdict.take(filename, timeout)

        if not arrived:
            print(f"[NOT SENT] '{filename}' - the server did not answer within "
                  f"{timeout:.0f}s. Nothing was transmitted.")
            return False

        if tag == 'ERR':
            print(f"[NOT SENT] {format_send_error(fields)}")
            return False

        # The sender now knows exactly who is about to receive the file.
        print(f"[VERIFIED] {format_send_verdict(fields)}")

    # 3. Stream the file data in chunks.
    # We use 4KB chunks so we don't load huge files into RAM all at once.
    print(f"[UPLOADING] {filename} ({filesize} bytes)...", flush=True)
    chunks = 0
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(4096)
            if not chunk:
                break
            sock.sendall(chunk)
            chunks += 1
            # Report every 64 chunks (256KB) so the terminal stays responsive.
            if chunks % 64 == 0:
                print(f"\r[UPLOADING] {filename}: {chunks * 4096}/{filesize} bytes...",
                      end='', flush=True)
    print(f"\r[UPLOAD COMPLETE] {filename}: {filesize} bytes in {chunks} chunk(s) sent.")
    return True


def receive_file(sock, filename, filesize, initial_data, sender=None, private=False):
    """
    Reads exactly `filesize` bytes from the socket and saves them to disk.

    Args:
        initial_data: Any file data that was accidentally read during the
                      socket.recv() call that grabbed the header.
        sender      : 'Otter#2' attribution taken from the header's `from=` field.
        private     : True when the header said this file was addressed to this
                      one client, so the receiver can see that it is the only
                      recipient.
    """
    bytes_received = len(initial_data)

    # Save with a prefix to avoid overwriting local files
    save_path = f"downloaded_{filename}"

    origin = f" from {sender}" if sender else ""
    privacy = " - for you only" if private else ""
    print(f"\n[RECEIVING] {filename}{origin} -> {save_path} "
          f"({filesize} bytes){privacy}", flush=True)

    with open(save_path, 'wb') as f:
        # Write any overflow data that arrived with the header
        if initial_data:
            f.write(initial_data)

        # VIVA POINT -- Partial Reads handling:
        # We MUST loop and count bytes. We cannot just call recv() and assume
        # it returns exactly what the sender sent. TCP guarantees delivery, but
        # it does NOT guarantee that one send() equals one recv().
        while bytes_received < filesize:
            # Only ask for the remaining bytes, up to a max of 4096.
            # If we asked for 4096 blindly, we might accidentally read the NEXT
            # chat message that was sent immediately after the file!
            chunk_size = min(4096, filesize - bytes_received)
            chunk = sock.recv(chunk_size)

            if not chunk:
                # Connection dropped mid-transfer
                print("\n[ERROR] Connection closed during file transfer.")
                break

            # Note: the bytes go straight to disk. They are never decoded,
            # echoed, or logged -- only the counter below is shown.
            f.write(chunk)
            bytes_received += len(chunk)

            # Print progress bar on the same line using carriage return (\r)
            progress = (bytes_received / filesize) * 100
            sys.stdout.write(f"\r[DOWNLOADING] {bytes_received}/{filesize} bytes "
                             f"({progress:.0f}%)   ")
            sys.stdout.flush()

    if filesize > 0 and bytes_received < filesize:
        print(f"\n[INCOMPLETE] Only {bytes_received}/{filesize} bytes arrived.")
    else:
        print(f"\n[SAVED] {save_path} ({bytes_received} bytes)")


def forward_file(sock, remaining_bytes, broadcast_func, sender_socket):
    """
    Used by the SERVER. Reads exactly `remaining_bytes` from the sending client
    and broadcasts them immediately to other clients as raw binary data.

    Returns the number of bytes actually relayed, so the server can tell whether
    the sender finished or vanished mid-transfer (and acknowledge accordingly).
    """
    forwarded = 0
    while remaining_bytes > 0:
        chunk_size = min(4096, remaining_bytes)
        chunk = sock.recv(chunk_size)
        if not chunk:
            break

        # Broadcast the binary chunk exactly as received (no "[Name #3] " prefix!)
        broadcast_func(chunk, sender_socket)
        forwarded += len(chunk)
        remaining_bytes -= len(chunk)

    return forwarded

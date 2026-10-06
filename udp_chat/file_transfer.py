"""
udp_chat/file_transfer.py
=========================
UDP File Transfer Logic

VIVA POINT -- UDP File Transfer Limitations (Failure Modes):
  1. No guaranteed delivery: If the network drops a datagram (router buffer full,
     noise on Wi-Fi), it is gone forever. This implementation will hang waiting
     for that specific chunk!
  2. No ordering guarantee: If chunk 2 takes a faster route than chunk 1, it will
     arrive first. Our receiver does not care about arrival order -- it seeks to
     the right offset -- but missing chunks still leave permanent holes.
  3. No flow control: send_file() blasts packets as fast as Python can read the
     disk. It will easily overwhelm the receiver's socket buffer, causing the OS
     to drop datagrams at the destination.

THE END-OF-TRANSFER PROBLEM:
  TCP knows a stream ended because the peer sends a FIN. UDP has no such notion,
  and the receiver cannot tell "that was the last chunk" from "the last chunk is
  still in flight".  So the sender says it explicitly with a /FILEEND datagram,
  and the server replies with an /ACK reporting how many chunks actually made
  it onto the wire.  That /ACK is what tells the sender its upload is complete.

CHOOSING A RECIPIENT BEFORE SENDING:
  The /FILE header carries the optional `to=` selector, so the recipient is
  chosen up front.  send_file() sends that header and then WAITS for the
  server's /SEND or /ERR.  On /ERR it sends no chunks at all -- which is the
  whole point of verifying before the transfer rather than after it.

TERMINAL SAFETY:
  File content is never decoded or printed -- only chunk numbers and byte counts.
"""

import os
import random
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common_info import (
    ctrl,
    format_file_header,
    format_send_error,
    format_send_verdict,
)


def send_file(sock, server_addr, filepath, loss_rate=0.0, target=None,
              verdict=None, timeout=3.0):
    """
    Chunks a file and blasts it over UDP to the server, optionally dropping chunks.

    Args:
        target  : the recipient selector the user typed after `to=` ('#2',
                  '@Kite', ...), or None to reach the whole room.  It travels in
                  the /FILE header so the server can verify it before any chunk.
        verdict : shared FileVerdict queue used to collect the server's answer.
                  When given, no chunk is sent unless the server accepted.

    Returns True if the file was sent, False if it was refused (nothing was sent).

    Nothing about the file's contents is printed -- only the chunk counter, so a
    multi-megabyte transfer cannot flood the terminal.
    """
    if not os.path.isfile(filepath):
        print(f"[ERROR] File '{filepath}' does not exist.")
        return False

    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)

    chunk_size = 4096
    # Calculate total chunks needed (ceiling division)
    total_chunks = (filesize + chunk_size - 1) // chunk_size

    # 1. Announce the transfer, carrying the chosen recipient, then wait for the
    #    server to confirm that recipient before a single chunk is sent.
    sock.sendto(format_file_header(filename, total_chunks, target=target).encode(),
                server_addr)

    if verdict is not None:
        who = target or 'the room'
        print(f"[SENDING] '{filename}' ({total_chunks} chunks) to {who} "
              f"- waiting for the server to check the recipient...", flush=True)
        arrived, tag, fields = verdict.take(filename, timeout)

        if not arrived:
            print(f"[NOT SENT] '{filename}' - the server did not answer within "
                  f"{timeout:.0f}s. No chunks were transmitted.")
            return False

        if tag == 'ERR':
            print(f"[NOT SENT] {format_send_error(fields)}")
            return False

        print(f"[VERIFIED] {format_send_verdict(fields)}")

    print(f"[UPLOADING] {filename} ({total_chunks} chunks)...", flush=True)

    sent = 0
    dropped = 0

    # 2. Send chunks stamped with a sequence number
    with open(filepath, 'rb') as f:
        for seq_num in range(total_chunks):
            chunk_data = f.read(chunk_size)

            # SIMULATOR: Deliberate Packet Loss
            if random.random() < loss_rate:
                dropped += 1
                print(f"[SIMULATOR] Dropped chunk {seq_num} intentionally.",
                      flush=True)
                continue

            # We prefix the binary payload with: "/CHUNK <seq_num> "
            packet = f"/CHUNK {seq_num} ".encode() + chunk_data

            # sendto() delivers a single discrete datagram
            sock.sendto(packet, server_addr)
            sent += 1

    print(f"[UPLOAD COMPLETE] {filename}: {sent}/{total_chunks} chunks handed to "
          f"the network ({dropped} dropped by the simulator).")

    # 3. Say explicitly that the upload is over. TCP would send a FIN here; UDP
    #    has to carry the equivalent in the payload, and it is what allows the
    #    server to send the /ACK that closes the loop.
    sock.sendto(ctrl("FILEEND", f"name={filename}", f"chunks={total_chunks}",
                     f"sent={sent}"), server_addr)
    return True


def receive_file(sock, filename, total_chunks, on_other=None, sender=None,
                 private=False):
    """
    A "dumb" UDP receiver that writes chunks to disk as they arrive.
    It does not acknowledge packets or request retransmissions, so any dropped
    packets will result in missing chunks (holes) in the final file.

    Args:
        on_other: optional callback invoked for any datagram that is not a file
                  chunk. Chat messages and /ROSTER updates share the same
                  datagram queue, so they must be routed back to the client.
        sender  : 'Otter#2' attribution taken from the header's `from=` field.
        private : True when the header said this file was addressed to this one
                  client, so the receiver can see that it is the only recipient.
    """
    origin = f" from {sender}" if sender else ""
    privacy = " - for you only" if private else ""
    print(f"\n[RECEIVING] {filename}{origin} -> downloaded_{filename} "
          f"({total_chunks} chunks){privacy}", flush=True)

    save_path = f"downloaded_{filename}"
    received_seqs = set()

    # We set a timeout. If the sender finishes (or drops the last packets),
    # we can't wait forever because we don't know when to stop if chunks are lost.
    sock.settimeout(3.0)

    with open(save_path, 'wb') as f:
        try:
            while len(received_seqs) < total_chunks:
                # We must use a buffer larger than 4096 to fit our /CHUNK header!
                data, addr = sock.recvfrom(8192)

                if data.startswith(b'/CHUNK '):
                    # Find the space after the sequence number
                    space_idx = data.find(b' ', 7)
                    if space_idx == -1:
                        continue

                    try:
                        seq_num = int(data[7:space_idx])
                    except ValueError:
                        continue
                    chunk_data = data[space_idx + 1:]

                    # VIVA POINT -- Handling Out-of-Order Delivery
                    # Instead of expecting chunk N, we use f.seek() to place the
                    # chunk exactly where it belongs in the file. This perfectly
                    # handles out-of-order packets! But missing packets leave holes.
                    f.seek(seq_num * 4096)
                    # Note: the bytes go straight to disk. They are never decoded,
                    # echoed or logged -- only the counter below is shown.
                    f.write(chunk_data)

                    received_seqs.add(seq_num)

                    # Print progress
                    progress = (len(received_seqs) / total_chunks) * 100
                    sys.stdout.write(f"\r[DOWNLOADING] {len(received_seqs)}/{total_chunks} "
                                     f"chunks ({progress:.0f}%)   ")
                    sys.stdout.flush()
                elif on_other is not None:
                    # Control traffic (roster updates, ACKs) shares the same
                    # datagram queue as the chunks. Hand it back to the client
                    # rather than treating it as a protocol error.
                    on_other(data)
                else:
                    print(f"\n[ERROR] Received non-chunk data during file transfer: "
                          f"{data[:20]!r}...")

        except socket.timeout:
            print("\n[TIMEOUT] No chunks received for 3 seconds. Transfer ended.")

    # Reset timeout so normal chat can block indefinitely again
    sock.settimeout(None)

    # Summary of missing data
    missing_seqs = set(range(total_chunks)) - received_seqs
    if missing_seqs:
        print(f"\n[INCOMPLETE] {len(missing_seqs)}/{total_chunks} chunks lost "
              f"-> {save_path} has holes. UDP cannot ask for them back.")
        missing_list = sorted(missing_seqs)
        if len(missing_list) > 20:
            print(f"  missing: {missing_list[:20]} ... and "
                  f"{len(missing_list) - 20} more")
        else:
            print(f"  missing: {missing_list}")
    else:
        print(f"\n[SAVED] {save_path} ({total_chunks} chunks, 100%)")

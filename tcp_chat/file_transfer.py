"""
tcp_chat/file_transfer.py
=========================
Shared logic for sending and receiving files over an existing TCP socket.

VIVA POINT — Why does this work reliably without custom retries?
  TCP is a "reliable, ordered byte stream".
  - Reliable: If a packet is lost, the sender's OS will automatically retransmit it.
  - Ordered: The receiver's OS reassembles packets in the exact sequence they were sent.
  Because we are using TCP, we don't need to write sequence numbers, checksums, or 
  acknowledgments in our Python code. We simply push bytes into the socket on the 
  sending side, and read them on the receiving side. We TRUST the OS TCP stack to 
  deliver exactly the bytes we sent, in order.
  
VIVA POINT — The Framing Problem:
  How do we send a file and chat messages on the same socket without them mixing?
  We use "framing". We define a special text header for files:
      "/FILE <filename> <filesize>\n"
  When the receiver sees this, it stops treating incoming bytes as text chat, and 
  switches to a strict byte-counting loop (receive_file) until exactly `filesize` 
  bytes are read.
"""

import os
import socket
import sys

def send_file(sock: socket.socket, filepath: str) -> None:
    """
    Reads a file from disk and sends it over the socket.
    """
    if not os.path.exists(filepath):
        print(f"[ERROR] File '{filepath}' does not exist.")
        return

    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)

    # 1. Send the framing header. The '\n' is our delimiter.
    header = f"/FILE {filename} {filesize}\n"
    sock.sendall(header.encode())

    # 2. Send the file data in chunks.
    # We use 4KB chunks so we don't load huge files into RAM all at once.
    print(f"[UPLOADING] {filename} ({filesize} bytes)...")
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(4096)
            if not chunk:
                break
            sock.sendall(chunk)
    print(f"[UPLOAD COMPLETE] {filename} sent.")


def receive_file(sock: socket.socket, filename: str, filesize: int, initial_data: bytes) -> None:
    """
    Reads exactly `filesize` bytes from the socket and saves them to disk.
    
    Args:
        initial_data: Any file data that was accidentally read during the 
                      socket.recv() call that grabbed the header.
    """
    bytes_received = len(initial_data)
    
    # Save with a prefix to avoid overwriting local files
    save_path = f"downloaded_{filename}"
    
    with open(save_path, 'wb') as f:
        # Write any overflow data that arrived with the header
        if initial_data:
            f.write(initial_data)

        # VIVA POINT — Partial Reads handling:
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
                
            f.write(chunk)
            bytes_received += len(chunk)
            
            # Print progress bar on the same line using carriage return (\r)
            progress = (bytes_received / filesize) * 100
            sys.stdout.write(f"\r[DOWNLOADING] {filename}: {bytes_received}/{filesize} bytes ({progress:.1f}%)   ")
            sys.stdout.flush()
            
    print(f"\n[DOWNLOAD COMPLETE] Saved as {save_path}")


def forward_file(sock: socket.socket, remaining_bytes: int, broadcast_func, sender_socket: socket.socket) -> None:
    """
    Used by the SERVER. Reads exactly `remaining_bytes` from the sending client
    and broadcasts them immediately to other clients as raw binary data.
    """
    while remaining_bytes > 0:
        chunk_size = min(4096, remaining_bytes)
        chunk = sock.recv(chunk_size)
        if not chunk:
            break
        
        # Broadcast the binary chunk exactly as received (no [IP:PORT] text prefix!)
        broadcast_func(chunk, sender_socket)
        remaining_bytes -= len(chunk)

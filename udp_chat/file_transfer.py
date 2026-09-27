"""
udp_chat/file_transfer.py
=========================
UDP File Transfer Logic

VIVA POINT — UDP File Transfer Limitations (Failure Modes):
  1. No guaranteed delivery: If the network drops a datagram (router buffer full, 
     noise on Wi-Fi), it is gone forever. This implementation will hang waiting 
     for that specific chunk!
  2. No ordering guarantee: If chunk 2 takes a faster route than chunk 1, it will 
     arrive first. Our simple loop strictly expects chunk 1, so it will print an 
     error and drop chunk 2, breaking the file!
  3. No flow control: send_file() blasts packets as fast as Python can read the 
     disk. It will easily overwhelm the receiver's socket buffer, causing the OS 
     to drop datagrams at the destination.
"""

import os
import socket
import sys
import random

def send_file(sock: socket.socket, server_addr: tuple, filepath: str, loss_rate: float = 0.0) -> None:
    """
    Chunks a file and blasts it over UDP to the server, optionally dropping chunks.
    """
    if not os.path.exists(filepath):
        print(f"[ERROR] File '{filepath}' does not exist.")
        return

    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    
    chunk_size = 4096
    # Calculate total chunks needed (ceiling division)
    total_chunks = (filesize + chunk_size - 1) // chunk_size

    # 1. Send metadata header
    header = f"/FILE {filename} {total_chunks}"
    sock.sendto(header.encode(), server_addr)
    
    print(f"[UPLOADING] {filename} ({total_chunks} chunks)...")

    # 2. Send chunks stamped with a sequence number
    with open(filepath, 'rb') as f:
        for seq_num in range(total_chunks):
            chunk_data = f.read(chunk_size)
            
            # SIMULATOR: Deliberate Packet Loss
            if random.random() < loss_rate:
                print(f"[SIMULATOR] Dropped chunk {seq_num} intentionally.")
                continue
            
            # We prefix the binary payload with: "/CHUNK <seq_num> "
            packet = f"/CHUNK {seq_num} ".encode() + chunk_data
            
            # sendto() delivers a single discrete datagram
            sock.sendto(packet, server_addr)

    print(f"[UPLOAD COMPLETE] {filename} sent to server queue.")


def receive_file(sock: socket.socket, filename: str, total_chunks: int) -> None:
    """
    A "dumb" UDP receiver that writes chunks to disk as they arrive.
    It does not acknowledge packets or request retransmissions, so any dropped
    packets will result in missing chunks (holes) in the final file.
    """
    print(f"\n[INCOMING FILE] {filename} ({total_chunks} chunks expected)")
    
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
                    if space_idx != -1:
                        seq_str = data[7:space_idx]
                        seq_num = int(seq_str)
                        chunk_data = data[space_idx + 1:]
                        
                        # VIVA POINT — Handling Out-of-Order Delivery
                        # Instead of expecting chunk N, we use f.seek() to place the
                        # chunk exactly where it belongs in the file. This perfectly
                        # handles out-of-order packets! But missing packets leave holes.
                        f.seek(seq_num * 4096)
                        f.write(chunk_data)
                        
                        received_seqs.add(seq_num)
                        
                        # Print progress
                        progress = (len(received_seqs) / total_chunks) * 100
                        sys.stdout.write(f"\r[DOWNLOADING] {len(received_seqs)}/{total_chunks} chunks ({progress:.1f}%)   ")
                        sys.stdout.flush()
                else:
                    # In UDP, interleaved chat messages arrive in the SAME datagram queue!
                    print(f"\n[ERROR] Received non-chunk data during file transfer: {data[:20]}...")
                    
        except socket.timeout:
            print("\n[TIMEOUT] No chunks received for 3 seconds. Transfer ended.")
            
    # Reset timeout so normal chat can block indefinitely again
    sock.settimeout(None)
    
    # Summary of missing data
    missing_seqs = set(range(total_chunks)) - received_seqs
    if missing_seqs:
        missing_pct = (len(missing_seqs) / total_chunks) * 100
        print(f"\n[TRANSFER INCOMPLETE] Missing {len(missing_seqs)} chunks ({missing_pct:.1f}%)")
        
        missing_list = sorted(list(missing_seqs))
        if len(missing_list) > 20:
            print(f"Missing sequence numbers: {missing_list[:20]} ... and {len(missing_list)-20} more.")
        else:
            print(f"Missing sequence numbers: {missing_list}")
    else:
        print(f"\n[DOWNLOAD COMPLETE] 100% received. Saved as {save_path}")

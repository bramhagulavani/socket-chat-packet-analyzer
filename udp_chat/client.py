"""
udp_chat/client.py
==================
A UDP chat client.

HOW IT WORKS:
  1. We create a UDP socket. No connect() is required!
  2. To "join", we just send an empty dummy packet to the server so it can 
     learn our IP and Port and add us to its registry.
  3. Like TCP, we run two threads: one for input(), one for recvfrom().
"""

import socket
import threading

HOST = '127.0.0.1'
PORT = 55001
SERVER_ADDR = (HOST, PORT)

def receive_messages(sock: socket.socket) -> None:
    while True:
        try:
            # We must use a buffer big enough to hold file chunks + headers (8192)
            data, _ = sock.recvfrom(8192)

            # Check if this is the start of a file transfer
            if data.startswith(b'/FILE '):
                # Parse: "/FILE filename total_chunks"
                parts = data.split(b' ', 2)
                if len(parts) == 3:
                    filename = parts[1].decode(errors='ignore')
                    total_chunks = int(parts[2])
                    
                    # Hand off to the blocking file receiver logic
                    import udp_chat.file_transfer as ft
                    ft.receive_file(sock, filename, total_chunks)
                    continue

            # Check if it's an orphaned chunk (e.g., we joined late and missed /FILE)
            if data.startswith(b'/CHUNK '):
                print("\n[WARNING] Received orphaned file chunk. Ignoring.")
                continue

            # Otherwise, it's normal chat.
            print(data.decode(errors='replace'), end='', flush=True)

        except OSError:
            print("\n[EXITING] Socket closed.")
            break

def start_client(loss_rate: float = 0.0, host=HOST, port=PORT) -> None:
    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    server_addr = (host, port)

    # VIVA POINT — "Connecting" in UDP
    # UDP is connectionless. The server doesn't know we exist until we send 
    # it a datagram. We send a hidden initialization message so the server 
    # adds our (ip, port) to its broadcast registry.
    sock.sendto(b'', server_addr)
    print(f"[JOINED] Listening for messages on UDP... (Loss rate: {loss_rate})")

    # Start the receive thread
    recv_thread = threading.Thread(target=receive_messages, args=(sock,), daemon=True)
    recv_thread.start()

    try:
        while True:
            message = input()
            
            if message.lower() == '/quit':
                print("[EXITING] Shutting down...")
                break

            if message.startswith('/sendfile '):
                parts = message.split(' ', 1)
                if len(parts) == 2:
                    filepath = parts[1]
                    import udp_chat.file_transfer as ft
                    ft.send_file(sock, server_addr, filepath, loss_rate)
                else:
                    print("[ERROR] Usage: /sendfile <path>")
                continue

            if message:
                # Normal chat: format and send as a single datagram
                full_message = f"{message}\n".encode()
                sock.sendto(full_message, server_addr)

    except KeyboardInterrupt:
        print("\n[EXITING] KeyboardInterrupt")

    finally:
        sock.close()

import argparse

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="UDP Chat Client")
    parser.add_argument('--loss-rate', type=float, default=0.0, 
                        help='Probability of intentionally dropping a file chunk (0.0 to 1.0)')
    parser.add_argument('--server-ip', type=str, default=HOST, help="Server IP address (default: 127.0.0.1)")
    parser.add_argument('--port', type=int, default=PORT, help=f"Server port (default: {PORT})")
    args = parser.parse_args()
    
    start_client(args.loss_rate, args.server_ip, args.port)

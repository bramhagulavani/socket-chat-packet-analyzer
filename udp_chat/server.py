"""
udp_chat/server.py
==================
A multi-client UDP chat server.

HOW IT WORKS (big picture):
  1. UDP is CONNECTIONLESS. The server does not call accept() or spawn a 
     new socket per client. It has exactly ONE socket.
  2. Every time a datagram (packet) arrives, recvfrom() gives us the data 
     AND the source address (ip, port) of who sent it.
  3. We maintain a "registry" (a Python set) of all addresses we've seen.
  4. To broadcast, we simply loop through the registry and sendto() every 
     address except the sender's.

VIVA POINT — TCP vs UDP Server Architecture:
  In TCP, the server needed a new thread for every client because calling 
  recv() on a client's dedicated socket would block. 
  In UDP, all clients send datagrams to the SAME server port. The OS queues 
  them up. A single thread looping over recvfrom() can handle hundreds of 
  clients sequentially because it's just popping discrete packets off a single queue!
"""

import socket

HOST = '127.0.0.1'
PORT = 55001

# ---------------------------------------------------------------------------
# Shared state — The Registry
# ---------------------------------------------------------------------------
# We don't need a Lock here because the server is completely single-threaded!
clients = set()

def start_server(host=HOST) -> None:
    # socket.SOCK_DGRAM -> UDP (Unreliable, connectionless, datagram-based)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # UDP only needs bind(). No listen() or accept().
    server_socket.bind((host, PORT))
    
    if host == '0.0.0.0':
        print(f"[LISTENING] UDP Server is up on 0.0.0.0:{PORT}")
        print("  (Listening on all interfaces. Reachable on your LAN.)")
        try:
            print(f"  (Likely LAN IP: {socket.gethostbyname(socket.gethostname())})")
        except: pass
    else:
        print(f"[LISTENING] UDP Server is up on {host}:{PORT}")

    while True:
        try:
            # recvfrom() returns the payload AND the sender's address.
            # We use 8192 to ensure we can fit our 4096-byte chunks + headers.
            data, addr = server_socket.recvfrom(8192)

            # If this is the first time we've seen this address, add them!
            if addr not in clients:
                print(f"[NEW CLIENT] {addr} registered.")
                clients.add(addr)
                
                # Announce the new client
                announce_msg = f"[SERVER] {addr} has joined the chat.\n".encode()
                for client_addr in clients:
                    if client_addr != addr:
                        server_socket.sendto(announce_msg, client_addr)

            # If it's a chat message (not a file header or chunk), we might want 
            # to prefix it for the console. But to keep it simple and not corrupt 
            # binary file chunks, we just act as a "dumb reflector".
            # Whatever a client sends, we echo it to everyone else exactly as-is.
            # (The client handles prefixing their own chat messages).

            # Broadcast the datagram to all other registered clients
            for client_addr in clients:
                if client_addr != addr:
                    server_socket.sendto(data, client_addr)

        except KeyboardInterrupt:
            print("\n[SHUTTING DOWN] Server closed.")
            break
        except OSError as e:
            print(f"[ERROR] Socket error: {e}")

import argparse

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="UDP Chat Server")
    parser.add_argument('--host', type=str, default=HOST, help="Interface to listen on (default: 127.0.0.1, use 0.0.0.0 for LAN)")
    args = parser.parse_args()
    start_server(args.host)

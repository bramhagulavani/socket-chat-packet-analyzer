"""
tcp_chat/server.py
==================
A multi-client TCP chat server using Python's socket + threading modules.

HOW IT WORKS (big picture):
  1. The server creates ONE "listening" socket and binds it to a port.
  2. It blocks on accept(), waiting for clients to connect.
  3. Each time a client connects, accept() returns a NEW, dedicated socket
     for that client. The server spawns a thread to handle that client,
     then immediately goes back to waiting for the next connection.
  4. Each thread reads messages from its client in a loop and calls
     broadcast() to forward the message to every other connected client.

VIVA POINT — Why a new socket per client?
  The listening socket's only job is to accept connection requests (the TCP
  handshake). Once the handshake is done, the OS hands you a fresh socket
  with its own send/receive buffers for that specific TCP connection.
  This is why you can have N clients on the same port number — each gets
  its own (server_ip, server_port, client_ip, client_port) 4-tuple.
"""

import socket
import threading

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = '127.0.0.1'   # localhost — only accepts connections from this machine
PORT = 65432          # arbitrary high port; anything > 1024 needs no root/admin

# ---------------------------------------------------------------------------
# Shared state — the list of currently connected client sockets
# ---------------------------------------------------------------------------
# Every thread that handles a client needs to read and modify this list
# (add itself on connect, remove itself on disconnect, iterate to broadcast).
#
# VIVA POINT — Thread-safety:
#   Python lists are not thread-safe for compound operations. If two threads
#   simultaneously call clients.append() or clients.remove(), the internal
#   state of the list can be corrupted. We protect every access with a Lock.
#   A Lock is a mutual-exclusion primitive: only one thread can hold it at a
#   time. Any other thread calling lock.acquire() will BLOCK until the holder
#   calls lock.release(). The "with lock:" syntax does acquire/release for us.

clients = []          # list of (socket, address) tuples for connected clients
clients_lock = threading.Lock()   # guards the 'clients' list


# ---------------------------------------------------------------------------
# broadcast() — send a message to every client except the sender
# ---------------------------------------------------------------------------
def broadcast(message: bytes, sender_socket: socket.socket) -> None:
    """
    Iterate over all connected clients and forward 'message' to each one,
    skipping the client who sent it (so you don't echo back to the sender).

    Args:
        message       : the raw bytes received from the sender
        sender_socket : the socket of the client who sent the message
                        (we skip this one)
    """
    # Acquire the lock before reading the list — another thread might be
    # adding/removing clients at the same moment.
    with clients_lock:
        for client_socket, addr in clients:
            if client_socket is not sender_socket:   # don't echo to sender
                try:
                    # socket.sendall() is important here:
                    #
                    # VIVA POINT — Why sendall(), not send()?
                    #   TCP is a STREAM protocol — it has no concept of
                    #   "message boundaries". socket.send() may send only
                    #   PART of the data if the kernel's send buffer is full.
                    #   socket.sendall() loops internally until every byte is
                    #   delivered to the kernel buffer (or raises an exception).
                    #   Always use sendall() unless you have a specific reason.
                    client_socket.sendall(message)
                except OSError:
                    # If sending fails, the client is probably gone.
                    # We'll let handle_client() detect the disconnect
                    # cleanly on its next recv() call.
                    pass


# ---------------------------------------------------------------------------
# handle_client() — runs in its own thread, one per connected client
# ---------------------------------------------------------------------------
def handle_client(client_socket: socket.socket, addr: tuple) -> None:
    """
    Dedicated per-client loop: read messages and broadcast them.
    This function is the 'target' for each client-handling thread.

    Args:
        client_socket : the socket object for THIS specific client
        addr          : (ip_string, port_int) of the remote client
    """
    print(f"[NEW CONNECTION] {addr} connected.")

    # Send a welcome message to the newly connected client.
    # encode() converts str -> bytes; TCP sockets work with bytes, not strings.
    client_socket.sendall(f"Welcome! You are connected as {addr}\n".encode())

    # Announce the new arrival to everyone else.
    broadcast(f"[SERVER] {addr} has joined the chat.\n".encode(), client_socket)

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
            # VIVA POINT — Partial reads on TCP:
            #   Even with recv(4096), you might get LESS than 4096 bytes if
            #   the sender's data didn't fill the buffer yet. For a simple
            #   line-based chat we get away with this because each "message"
            #   is short. For file transfer (later module) we MUST loop until
            #   we've received exactly as many bytes as expected.
            data = client_socket.recv(4096)

            if not data:
                # recv() returning empty bytes means the client sent a TCP FIN
                # packet — it closed its end of the connection gracefully.
                #
                # VIVA POINT — TCP FIN / connection teardown:
                #   When a client calls socket.close(), the OS sends a FIN
                #   segment. The server's recv() returns b'' to signal EOF.
                #   This triggers our cleanup below (the break).
                print(f"[DISCONNECT] {addr} disconnected (graceful close).")
                break

            # ---------------------------------------------------------------
            # VIVA POINT — Intercepting the File Transfer Header
            # ---------------------------------------------------------------
            # We check if the packet starts with our special framing header.
            # If so, we know the NEXT `filesize` bytes are binary data, not text!
            if data.startswith(b'/FILE '):
                header_end = data.find(b'\n')
                if header_end != -1:
                    header = data[:header_end + 1]
                    initial_data = data[header_end + 1:]
                    
                    header_str = header.decode(errors='ignore').strip()
                    parts = header_str.split(' ')
                    if len(parts) == 3:
                        filesize = int(parts[2])
                        
                        # 1. Forward the header so receivers switch to file-mode
                        broadcast(header, client_socket)
                        
                        # 2. Forward any overflow file data already read
                        if initial_data:
                            broadcast(initial_data, client_socket)
                            
                        # 3. Enter a strict byte-counting loop to forward the rest.
                        # This prevents the server from corrupting binary file chunks
                        # by accidentally prepending "[IP:PORT] " text to them!
                        import file_transfer
                        file_transfer.forward_file(
                            client_socket, 
                            filesize - len(initial_data), 
                            broadcast, 
                            client_socket
                        )
                        continue

            # Prefix the message with the sender's address for clarity.
            tagged_message = f"[{addr[0]}:{addr[1]}] ".encode() + data
            print(f"[MSG] {tagged_message.decode(errors='replace').strip()}")

            # Forward the message to all other clients.
            broadcast(tagged_message, client_socket)

        except OSError as e:
            # An abrupt disconnect (e.g., client process killed) raises an
            # OSError instead of returning b''. We handle both cases by
            # breaking out of the loop.
            print(f"[ERROR] {addr} connection error: {e}")
            break

    # -----------------------------------------------------------------------
    # Cleanup — runs once the loop exits (on disconnect or error)
    # -----------------------------------------------------------------------
    # Remove this client from the shared list.
    with clients_lock:
        clients[:] = [(s, a) for s, a in clients if s is not client_socket]

    # Let the remaining clients know someone left.
    broadcast(f"[SERVER] {addr} has left the chat.\n".encode(), client_socket)

    # Close our end of the socket to release OS resources.
    # This sends a FIN to the client (if not already sent).
    client_socket.close()
    print(f"[CLOSED] Socket for {addr} closed.")


# ---------------------------------------------------------------------------
# start_server() — creates the listening socket and the accept() loop
# ---------------------------------------------------------------------------
def start_server(host=HOST) -> None:
    """
    Bind to host:PORT, listen for incoming connections, and for each one
    spawn a daemon thread running handle_client().
    """
    # socket.AF_INET  -> IPv4 address family
    # socket.SOCK_STREAM -> TCP (reliable, ordered, stream-based)
    #   Compare: SOCK_DGRAM would give us UDP (later module)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # SO_REUSEADDR lets us re-bind to the same port immediately after the
    # server is restarted, without waiting for the OS TIME_WAIT period to expire.
    #
    # VIVA POINT — TIME_WAIT:
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
    # Connections beyond the backlog are refused by the OS automatically.
    server_socket.listen(5)
    
    if host == '0.0.0.0':
        print(f"[LISTENING] Server is up on 0.0.0.0:{PORT}")
        print("  (Listening on all interfaces. Reachable on your LAN.)")
        try:
            print(f"  (Likely LAN IP: {socket.gethostbyname(socket.gethostname())})")
        except: pass
    else:
        print(f"[LISTENING] Server is up on {host}:{PORT}")

    # -----------------------------------------------------------------------
    # Accept loop — runs forever in the main thread
    # -----------------------------------------------------------------------
    while True:
        # accept() BLOCKS here until a client completes the TCP 3-way handshake.
        # It returns a new socket object + the client's (ip, port).
        #
        # VIVA POINT — The TCP 3-way handshake:
        #   1. Client sends SYN  (sequence number = X)
        #   2. Server replies SYN-ACK  (seq = Y, ack = X+1)
        #   3. Client replies ACK  (ack = Y+1)
        #   After step 3, the connection is ESTABLISHED and accept() returns.
        client_socket, addr = server_socket.accept()

        # Add the new client to the shared list (protected by the lock).
        with clients_lock:
            clients.append((client_socket, addr))

        # Spawn a new thread to handle this client.
        # target=handle_client -> the function the thread will run
        # args=(client_socket, addr) -> arguments passed to that function
        # daemon=True -> the thread will be killed automatically when the
        #               main thread exits (so Ctrl+C shuts everything down).
        thread = threading.Thread(
            target=handle_client,
            args=(client_socket, addr),
            daemon=True
        )
        thread.start()
        print(f"[ACTIVE CONNECTIONS] {threading.active_count() - 1}")
        # We subtract 1 because threading.active_count() includes the main thread.


import argparse

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="TCP Chat Server")
    parser.add_argument('--host', type=str, default=HOST, help="Interface to listen on (default: 127.0.0.1, use 0.0.0.0 for LAN)")
    args = parser.parse_args()
    start_server(args.host)

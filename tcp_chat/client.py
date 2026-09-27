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
    - Main thread  → handles user keyboard input (blocking input() calls)
    - Receive thread → handles incoming server messages (blocking recv() calls)

VIVA POINT — Why two threads?
  Both input() and socket.recv() are BLOCKING calls. If you called them in
  sequence, you'd have to finish reading from the socket before you could
  type, or vice-versa. Threads let both operations run concurrently so the
  UI stays responsive while messages arrive.
"""

import socket
import threading

# ---------------------------------------------------------------------------
# Configuration — must match server.py
# ---------------------------------------------------------------------------
HOST = '127.0.0.1'
PORT = 65432

# ---------------------------------------------------------------------------
# receive_messages() — runs in a background thread
# ---------------------------------------------------------------------------
def receive_messages(sock: socket.socket) -> None:
    """
    Continuously read messages arriving from the server and print them.
    This runs in a daemon thread so it dies when the main thread exits.

    Args:
        sock : the connected socket to the server
    """
    while True:
        try:
            # Block here until data arrives from the server.
            # The server calls sendall() so we expect the full message
            # in one recv() for short chat lines (see VIVA POINT in server.py
            # about partial reads for why this only works for short messages).
            data = sock.recv(4096)

            if not data:
                # Server closed the connection — recv() returns b''.
                # This mirrors the same b'' sentinel on the server side.
                print("\n[SERVER CLOSED] The server has shut down.")
                break

            # ---------------------------------------------------------------
            # VIVA POINT — Intercepting the File Transfer Header
            # ---------------------------------------------------------------
            if data.startswith(b'/FILE '):
                header_end = data.find(b'\n')
                if header_end != -1:
                    header = data[:header_end].decode(errors='ignore')
                    initial_data = data[header_end + 1:]
                    
                    parts = header.split(' ')
                    if len(parts) == 3:
                        filename = parts[1]
                        filesize = int(parts[2])
                        
                        # Switch from "chat mode" to "file receiving mode"
                        import file_transfer
                        file_transfer.receive_file(sock, filename, filesize, initial_data)
                        continue

            # Decode bytes -> str and print. errors='replace' prevents a
            # UnicodeDecodeError from crashing the thread if the bytes aren't
            # valid UTF-8 (defensive coding, doesn't add complexity).
            print(data.decode(errors='replace'), end='', flush=True)

        except OSError:
            # Connection was reset or closed abruptly.
            print("\n[DISCONNECTED] Lost connection to server.")
            break


# ---------------------------------------------------------------------------
# start_client() — connects and runs the send loop in the main thread
# ---------------------------------------------------------------------------
def start_client() -> None:
    """
    Connect to the server, start the receive thread, then loop reading
    keyboard input and sending it to the server.
    """
    # Create a TCP socket (same family/type as the server).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # connect() triggers the TCP 3-way handshake with the server.
    # It BLOCKS until the handshake completes (or raises OSError on failure).
    #
    # VIVA POINT — connect() vs bind():
    #   The CLIENT does NOT need to call bind(). The OS automatically assigns
    #   an ephemeral (temporary) source port (e.g., 49152-65535) for the
    #   client side of the connection. Only servers that must be reachable at
    #   a known port call bind() explicitly.
    sock.connect((HOST, PORT))
    print(f"[CONNECTED] Connected to {HOST}:{PORT}")

    # Start the receive thread BEFORE entering the send loop, so we can
    # immediately see the server's welcome message.
    recv_thread = threading.Thread(
        target=receive_messages,
        args=(sock,),
        daemon=True   # dies automatically when main thread exits
    )
    recv_thread.start()

    # -----------------------------------------------------------------------
    # Send loop — runs in the main thread
    # -----------------------------------------------------------------------
    # input() blocks until the user presses Enter, giving us one line at a time.
    # Meanwhile, the receive thread prints incoming messages in the background.
    try:
        while True:
            message = input()   # blocking: waits for the user to type + Enter

            if message.lower() == '/quit':
                # User wants to exit. Break out of the loop.
                # The finally block below will close the socket.
                print("[EXITING] Closing connection...")
                break

            # ---------------------------------------------------------------
            # VIVA POINT — File Transfer Command
            # ---------------------------------------------------------------
            if message.startswith('/sendfile '):
                parts = message.split(' ', 1)
                if len(parts) == 2:
                    filepath = parts[1]
                    import file_transfer
                    file_transfer.send_file(sock, filepath)
                else:
                    print("[ERROR] Usage: /sendfile <path>")
                continue

            if message:   # don't send empty messages
                # Append '\n' as a simple message delimiter so the server
                # (and other clients) can separate messages when printed.
                #
                # VIVA POINT — Message framing:
                #   TCP is a byte stream with no built-in message boundaries.
                #   Here we use newline ('\n') as a delimiter, which works for
                #   simple text chat. For binary protocols (e.g., file transfer)
                #   you'd typically prefix each message with a fixed-size header
                #   containing the payload length, then read exactly that many
                #   bytes — this is called "length-prefixed framing".
                sock.sendall((message + '\n').encode())

    except KeyboardInterrupt:
        # Ctrl+C in the terminal — treat it as a graceful quit.
        print("\n[EXITING] KeyboardInterrupt — closing connection.")

    finally:
        # Closing the socket sends a TCP FIN to the server, which causes the
        # server's recv() to return b'' and triggers its cleanup routine.
        sock.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    start_client()

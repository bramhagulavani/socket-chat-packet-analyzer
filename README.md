# Socket Chat & Packet Analyzer

A socket-based multi-client chat and file-transfer system demonstrating the differences between TCP and UDP networking protocols. Built for academic demonstration and packet-level analysis.

## Project Structure

- `tcp_chat/`: TCP implementation featuring reliable, ordered byte-stream delivery.
- `udp_chat/`: UDP implementation featuring connectionless, datagram-based delivery with an application-layer packet loss simulator.
- `analyzer/`: *(Pending)* Packet capture and analysis tools to inspect TCP handshakes, retransmissions, and UDP gaps.

## Setup Instructions

1. Ensure you have Python 3.8+ installed.
2. Clone this repository.
3. No external dependencies are required for the chat servers/clients (uses Python's standard `socket` and `threading` libraries).

## How to Run the Demos

### 1. TCP Chat & File Transfer
The TCP version guarantees reliable, ordered delivery implicitly through the OS TCP stack.

**Start the Server:**
```powershell
python tcp_chat/server.py
```

**Start Clients (open multiple terminals):**
```powershell
python tcp_chat/client.py
```

**Commands:**
- Type any text to chat.
- Type `/sendfile <path_to_file>` to send a file to all connected clients.
- Type `/quit` to disconnect gracefully.

### 2. UDP Chat & File Transfer
The UDP version is connectionless and offers no reliability guarantees. The server acts as a simple datagram reflector, keeping a registry of known client addresses.

**Start the Server:**
```powershell
python udp_chat/server.py
```

**Start Clients:**
```powershell
python udp_chat/client.py
```

### 3. UDP Packet Loss Simulation
To visibly demonstrate what happens when network packets are dropped without TCP's reliability (and why simulating this in TCP requires OS-level interference), use the UDP loss simulator.

**Start the Server:**
```powershell
python udp_chat/server.py
```

**Start a Receiver Client:**
```powershell
python udp_chat/client.py
```

**Start a Sender Client with 10% simulated loss:**
```powershell
python udp_chat/client.py --loss-rate 0.1
```

Send a file from the sender client. You will see deliberate drops logged on the sender side (`[SIMULATOR] Dropped chunk X intentionally`). On the receiver side, after a 3-second timeout, it will summarize the missing sequence numbers (holes in the file).

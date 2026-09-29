# Socket Chat & Packet Analyzer

A socket-based multi-client chat and file-transfer system demonstrating the differences between TCP and UDP networking protocols. Built for academic demonstration and packet-level analysis.

## Project Structure

- `tcp_chat/`: TCP implementation featuring reliable, ordered byte-stream delivery.
- `udp_chat/`: UDP implementation featuring connectionless, datagram-based delivery with an application-layer packet loss simulator.
- `analyzer/`: Packet capture and analysis tools to inspect TCP handshakes, retransmissions, and UDP gaps.

## Setup Instructions

1. Ensure you have Python 3.8+ installed.
2. Clone this repository.
3. No external dependencies are required for the chat servers/clients (uses Python's standard `socket` and `threading` libraries).
4. For the **analyzer**, install the dependencies listed in `requirements.txt`:
   ```powershell
   pip install -r requirements.txt
   ```

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

### 4. Packet Analysis
The `analyzer` module parses packet captures (`.pcapng` files) captured from Wireshark.

**Analyze a TCP transfer (handshakes, teardown, retransmissions):**
```powershell
python analyzer/analyze.py captures/tcp_demo.pcapng
```

**Analyze a UDP transfer (missing sequence numbers, out of order):**
```powershell
python analyzer/analyze_udp.py captures/udp_loss_demo.pcapng --port 55001
```

**Generate a Comparative Report:**
Creates a Markdown report comparing TCP and UDP behaviors under loss.
```powershell
python analyzer/report.py --tcp captures/tcp_demo.pcapng --udp captures/udp_loss_demo.pcapng --tcp-loss captures/tcp_loss_demo.pcapng --expected-drops drops.txt --out report.md
```

*Note on LAN/Wi-Fi Captures:* When running on two different devices over Wi-Fi, you must capture on your Wi-Fi interface instead of the "Adapter for loopback traffic". The display filters remain the same (`tcp.port == 65432` or `udp.port == 55001`). Note that on a real network, the UDP loopback reflection (where the server broadcasts the chunk back to the sender) won't appear on the sender's Wi-Fi capture as a duplicate destination packet; it would appear as an incoming packet from the server. The `analyze_udp.py` script filters strictly by `dport == 55001` (upload traffic), so its deduplication logic works perfectly unchanged for both loopback and Wi-Fi captures.

## Known Limitations

- **Loopback Traffic Only:** The current server and client configurations are explicitly hardcoded for `127.0.0.1` (localhost).
- **Sender-Side Loss Simulation Only:** The UDP loss simulator only drops packets at the application layer *before* they hit the network stack, meaning they will not show up in a Wireshark capture of the loopback adapter.
- **No Packet Reordering Observed:** Due to the nature of the OS loopback adapter, packets are typically processed instantaneously in-order. True out-of-order delivery usually requires a complex physical network topology to observe reliably.

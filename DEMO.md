# Networking Demonstration Steps

## Two-Device Demo (LAN/Wi-Fi)

To demonstrate the networking behaviors on a real physical network, you can run the server and clients on separate devices connected to the same Wi-Fi router.

### Step 1: Server Setup (Device A)
1. Find the server's IP address by opening PowerShell and typing `ipconfig`. Look for the "IPv4 Address" under your Wi-Fi adapter (e.g., `192.168.1.5`).
2. Open Windows Defender Firewall -> "Advanced Settings" -> "Inbound Rules" -> "New Rule". Allow both TCP port `65432` and UDP port `55001`.
3. Start the TCP and UDP servers bound to all interfaces:
   ```powershell
   python tcp_chat/server.py --host 0.0.0.0
   python udp_chat/server.py --host 0.0.0.0
   ```

### Step 2: Client Verification (Device B)
1. On the second device, use the diagnostic script to ensure the firewall is open:
   ```powershell
   python scripts/check_connection.py 192.168.1.5 65432
   ```
2. If it says `[SUCCESS]`, proceed. If it times out, check the server's firewall.

### Step 3: Run the Transfers and Capture Packets
1. On **Device A (Server)**, open Wireshark and start capturing on your **Wi-Fi adapter** (not the loopback adapter).
2. On **Device B (Client)**, start the clients using the server's IP:
   ```powershell
   # Start TCP Client
   python tcp_chat/client.py --server-ip 192.168.1.5
   
   # Start UDP Client (with 10% simulated loss)
   python udp_chat/client.py --server-ip 192.168.1.5 --loss-rate 0.1
   ```
3. Type `/sendfile path_to_file` in both clients to perform the transfers.
4. Stop the Wireshark capture and save the files (e.g., `tcp_wifi.pcapng` and `udp_wifi.pcapng`).

### Step 4: Analysis
1. Run the report generator on the new Wi-Fi captures:
   ```powershell
   python analyzer/report.py --tcp tcp_wifi.pcapng --udp udp_wifi.pcapng --out wifi_report.md
   ```

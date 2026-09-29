# SETUP.md

Setup and systematic run guide for **Socket Chat & Packet Analyzer**
(ML3001 — Computer Network Technology, VIT Pune)

---

## 1. Prerequisites

- Python 3.8+ (tested on 3.14.3)
- Git
- [Wireshark](https://www.wireshark.org/) with Npcap
  - During install, enable **"Support loopback traffic"**
- `scapy` (only required for the `analyzer/` module)

```powershell
python --version
pip install scapy
```

---

## 2. Project Structure

```
socket-chat-packet-analyzer/
├── tcp_chat/
│   ├── server.py
│   ├── client.py
│   └── file_transfer.py
├── udp_chat/
│   ├── server.py
│   ├── client.py
│   ├── file_transfer.py
│   └── loss_simulator.py
├── analyzer/
│   ├── analyze.py          # TCP: handshake, teardown, retransmissions
│   ├── analyze_udp.py      # UDP: sequence gaps, duplicates, reordering
│   └── report.py           # Combined TCP vs UDP comparison report
├── tests/                  # Integrity tests (SHA-256 verification)
├── scripts/
│   ├── run_demo.ps1
│   └── check_connection.py
├── captures/                # Saved Wireshark .pcapng files
├── results/                  # Recorded run outputs (hashes, drop logs)
├── docs/
│   ├── PROJECT_REPORT.md
│   └── screenshots/
├── DEMO.md
├── VIVA_NOTES.md
├── README.md
└── requirements.txt
```

---

## 3. One-Time Setup

```powershell
git clone <repo-url>
cd socket-chat-packet-analyzer
mkdir captures, results

# Create a fixed test file used across every run for fair comparisons
fsutil file createnew testfile.bin 204800
certutil -hashfile testfile.bin SHA256
```
Note the hash — every received copy of this file must match it exactly.

---

## 4. Systematic Run Order

Run phases **in order**. Do not move to the next phase until the current one gives a result you've recorded in `results/`.

### Phase 1 — TCP chat + file transfer
```powershell
python tcp_chat/server.py           # Terminal 1
python tcp_chat/client.py           # Terminal 2
python tcp_chat/client.py           # Terminal 3
```
Check: two-way broadcast, `/sendfile testfile.bin` (verify hash on receiver), clean `/quit`.

### Phase 2 — UDP chat + file transfer (no loss)
```powershell
python udp_chat/server.py
python udp_chat/client.py           # receiver
python udp_chat/client.py           # sender
```

### Phase 3 — UDP loss simulation
```powershell
python udp_chat/client.py --loss-rate 0.1
```
Log `[SIMULATOR] Dropped chunk X` lines to `results/drops.txt`. Repeat at `0.05` and `0.2`.

### Phase 4 — Packet capture (Wireshark)
- Capture on **loopback adapter** (`127.0.0.1` runs)
- TCP filter: `tcp port 65432` → save as `captures/tcp_demo.pcapng`
- UDP filter: `udp port <udp_port>` → save as `captures/udp_loss_demo.pcapng`

### Phase 5 — Analyzers and report
```powershell
python analyzer/analyze.py captures/tcp_demo.pcapng
python analyzer/analyze_udp.py captures/udp_loss_demo.pcapng --port <udp_port> --expected-drops results/drops.txt
python analyzer/report.py --tcp captures/tcp_demo.pcapng --udp captures/udp_loss_demo.pcapng --expected-drops results/drops.txt --out report.md
```
Cross-check handshake packet number, received chunk count, and missing-chunk list against Wireshark directly.

### Phase 6 — Automated tests
```powershell
python -m unittest discover tests -v
```

### Phase 7 — Two-laptop Wi-Fi run (optional, do last)
```powershell
# Server laptop
ipconfig
python tcp_chat/server.py --host 0.0.0.0

# Client laptop
python scripts/check_connection.py <server-ip> 65432
python tcp_chat/client.py --server-ip <server-ip>
```
Use a phone hotspot if college/campus Wi-Fi blocks device-to-device traffic. Capture on the **Wi-Fi adapter**, not loopback.

### Phase 8 — Commit and rehearse
```powershell
git add .
git commit -m "..."
git tag v1.0
```
Then run through `DEMO.md` twice, out loud, with a timer.

---

## 5. Rules Throughout

- Use the **same test file** for every comparison run.
- **Start Wireshark before** each transfer, never after.
- Record every result in `results/` as you go — the final report is built from these, not from memory.
- Stop and fix any phase that fails before continuing.

---

## 6. Known Limitations

- Loopback runs show no natural packet loss or reordering — TCP retransmissions require either OS-level loss injection (e.g. Clumsy) or a real Wi-Fi run.
- Loss simulation happens only on the UDP sender side, in application code.
- UDP server reflects datagrams, so loopback captures may show each chunk twice (client→server, server→receiver).

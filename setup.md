# setup.md

Setup and run guide for **Socket Chat & Packet Analyzer**
(ML3001 — Computer Network Technology, VIT Pune)

This file is the "how do I run and demo this" document. `README.md` is the short
overview; this one is the step-by-step.

---

## 1. What the project does

Two chat servers (one TCP, one UDP) that let several clients talk and exchange
files, plus tools that analyse the captured packets to show the difference
between the two protocols.

---

## 2. Requirements

- Python 3.8+ (verified on 3.11)
- Git
- [Wireshark](https://www.wireshark.net/) with Npcap
  - during install, tick **"Support loopback traffic"** — the local demos run on
    `127.0.0.1`
- `scapy` and `matplotlib` — only for the `analyzer/` module

```powershell
python --version
pip install -r requirements.txt
```

The chat servers and clients themselves use only the standard library
(`socket`, `threading`), so they work without any install.

---

## 3. Project layout

```
Project/
├── common_info.py          Shared protocol: control messages, client naming,
│                           who a file may be sent to, all text output
├── tcp_chat/
│   ├── server.py           One thread per client; relays chat and files
│   ├── client.py           Chat CLI (input line, /clients, /sendfile)
│   └── file_transfer.py    Announce → verify → send, and receive to disk
├── udp_chat/
│   ├── server.py           One socket, one loop; registry of client addresses
│   ├── client.py           Chat CLI, same commands as TCP
│   └── file_transfer.py    Chunking, sequence numbers, loss simulator
├── analyzer/
│   ├── analyze.py          TCP: handshake, teardown, retransmissions
│   ├── analyze_udp.py      UDP: sequence gaps, duplicates, reordering
│   └── report.py           Combined TCP vs UDP comparison report
├── scripts/
│   └── check_connection.py Port/firewall check before a two-device run
├── tests/
│   └── test_targeted_send.py  Recipient selection, messages, data abstraction
├── captures/               Saved .pcapng files
├── results/                Recorded hashes, drop logs, generated reports
├── setup.md                This file
└── README.md               Short overview
```

---

## 4. One-time setup

```powershell
git clone <repo-url>
cd Project
mkdir captures, results -ErrorAction SilentlyContinue

# Create the fixed test file used for every run, so runs stay comparable
fsutil file createnew testfile.bin 204800
certutil -hashfile testfile.bin SHA256
```

Write the hash down. Every received copy of `testfile.bin` must match it.

---

## 5. Run order

Run the phases in order. Do not move on until the current one gives a result you
have written down in `results/`.

### Phase 1 — TCP chat and file transfer

```powershell
python tcp_chat/server.py           # Terminal 1 — the server
python tcp_chat/client.py           # Terminal 2 — client #1
python tcp_chat/client.py           # Terminal 3 — client #2
```

Each client prints one line and then an input line to type on:

```
Otter (#2) connected - TCP chat
Otter > 
```

Check:
- typing text in one client appears in the other
- `/clients` lists both names
- `/sendfile testfile.bin` arrives at the other client; compare the hash
- `/sendfile testfile.bin to=#2` reaches client #2 only — the other client sees
  nothing, and no file appears on its disk
- `/sendfile testfile.bin to=#99` is refused with `[NOT SENT]` and nothing is sent
- `/quit` disconnects cleanly

### Phase 2 — UDP chat and file transfer, no loss

```powershell
python udp_chat/server.py
python udp_chat/client.py           # receiver
python udp_chat/client.py           # sender
```

Same checks as Phase 1, plus compare: the UDP server reflects datagrams instead of
relaying a stream, so there is no ordering or delivery guarantee to rely on.

### Phase 3 — UDP loss simulation

```powershell
python udp_chat/server.py
python udp_chat/client.py                              # receiver
python udp_chat/client.py --loss-rate 0.1              # sender
```

Send a file from the sender. It logs `[SIMULATOR] Dropped chunk N` for each chunk
it withholds. On the receiver, the transfer ends after a 3-second timeout and
prints the missing sequence numbers — those chunks are gone for good, because UDP
has no acknowledgement and no retransmission.

Save those drop lines to `results/drops.txt`, then repeat at `0.05` and `0.2`.

### Phase 4 — Packet capture

1. Open Wireshark and start capturing **before** the transfer, on the loopback
   adapter (`Adapter for loopback traffic`).
2. Run Phase 1 again. Save as `captures/tcp_demo.pcapng`.
3. Run Phase 3 again. Save as `captures/udp_loss_demo.pcapng`.
   - Display filter: `tcp port 65432` or `udp port 55001`

### Phase 5 — Analysis and report

```powershell
python analyzer/analyze.py captures/tcp_demo.pcapng
python analyzer/analyze_udp.py captures/udp_loss_demo.pcapng --port 55001 `
        --expected-drops results/drops.txt
python analyzer/report.py --tcp captures/tcp_demo.pcapng `
        --udp captures/udp_loss_demo.pcapng `
        --expected-drops results/drops.txt --out results/report.md
```

Cross-check the numbers against Wireshark: handshake packet count, chunk count
received, retransmissions, and the missing-chunk list.

### Phase 6 — Tests

```powershell
python -m unittest discover tests -v
```

Should pass without any network setup — it covers recipient selection, the
messages the clients print, and the rule that connection detail stays on the
server.

### Phase 7 — Two-device Wi-Fi run (optional, do last)

The loopback adapter never shows natural loss or reordering, so a real network is
the only way to observe those.

**On the server device:**
1. `ipconfig` → note the IPv4 address on the Wi-Fi adapter (e.g. `192.168.1.5`).
2. Windows Defender Firewall → Advanced Settings → Inbound Rules → New Rule →
   allow **TCP 65432** and **UDP 55001**.
3. Listen on all interfaces:
   ```powershell
   python tcp_chat/server.py --host 0.0.0.0
   python udp_chat/server.py --host 0.0.0.0
   ```

**On the second device:**
```powershell
python scripts/check_connection.py 192.168.1.5 65432
python tcp_chat/client.py --server-ip 192.168.1.5
python udp_chat/client.py --server-ip 192.168.1.5 --loss-rate 0.1
```

`check_connection.py` must print `[SUCCESS]` before you continue; a timeout means
the firewall is still closed. A phone hotspot works better than campus Wi-Fi,
which often blocks device-to-device traffic.

Capture on the **Wi-Fi adapter** on the server device, not the loopback adapter.

### Phase 8 — Rehearse

```powershell
git add .
git commit -m "..."
git tag v1.0
```

Then run the whole demo once more end to end, out loud, with a timer.

---

## 6. Rules for a fair comparison

- Use the **same** `testfile.bin` for every run.
- Start Wireshark **before** each transfer, never after.
- Write every result into `results/` as you go — the final report is built from
  those files, not from memory.

---

## 7. Known limitations

- The demos are configured for `127.0.0.1` unless you pass `--host 0.0.0.0` and
  `--server-ip`.
- The loss simulator drops packets in application code, before they reach the
  network stack, so they will **not** appear in a capture of the loopback adapter.
- Loopback packets are processed instantaneously and in order, so natural
  reordering will not appear. Use the Wi-Fi run (Phase 7) for that.
- The UDP server reflects each chunk, so a loopback capture shows it twice
  (client→server, server→receiver).

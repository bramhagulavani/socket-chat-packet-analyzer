# Socket Chat & Packet Analyzer

Multi-client chat and file transfer over **TCP and UDP**, with packet-analysis
tools that show the difference between them. Built for academic demonstration
(ML3001 — Computer Network Technology, VIT Pune).

See **[setup.md](setup.md)** for the step-by-step run and demo order.

---

## Quick start

```powershell
# Terminal 1
python tcp_chat/server.py

# Terminal 2 and 3
python tcp_chat/client.py
```

Each client prints one line and then an input line to type on:

```
Otter (#2) connected - TCP chat
Otter > hello everyone
```

Same three commands with `udp_chat/server.py` and `udp_chat/client.py`.

Only the standard library is needed for the servers and clients. The analyzer
needs the packages in `requirements.txt`.

---

## Client commands

| Type this | What happens |
|---|---|
| `<any text>` | Sends a message to everyone else |
| `/clients` | Lists who is in the room (names and ids) |
| `/sendfile <path>` | Sends a file to everyone |
| `/sendfile <path> to=#2` | Sends a file to **client #2 only** |
| `/sendfile <path> to=@Otter` | Same, by name |
| `/help` | Lists these commands |
| `/quit` | Disconnects |

---

## Sending a file to one specific client

The recipient is part of the command, so it is chosen *before* the transfer
starts, and the server checks it before any byte of the file is sent:

1. The client sends `/FILE <name> <size> to=<selector>` and stops.
2. The server resolves the selector against the roster and replies:
   - one connected client → `/SEND`
   - unknown id/name, an ambiguous name, yourself, or nobody else → `/ERR`
3. The file body is streamed **only** after `/SEND`.

```
Otter > /sendfile secret.txt to=#2
[SENDING] 'secret.txt' (2816 bytes) to #2 - waiting for the server to check the recipient...
[VERIFIED] File 'secret.txt' will be delivered to @Falcon (#1) only
[UPLOADING] secret.txt (2816 bytes)...
[UPLOAD COMPLETE] secret.txt: 2816 bytes in 1 chunk(s) sent.
Otter >
File 'secret.txt' was sent successfully [2816 bytes] -> 1 other client(s) -> @Falcon (#1) only
```

On the receiving client:

```
[RECEIVING] secret.txt from @Otter (#2) -> downloaded_secret.txt (2816 bytes) - for you only
[DOWNLOADING] 2816/2816 bytes (100%)
[SAVED] downloaded_secret.txt (2816 bytes)
```

A refused transfer sends nothing to anybody:

```
Otter > /sendfile secret.txt to=#99
[NOT SENT] File 'secret.txt' was NOT sent: no connected client matches '99'.
           Type /clients to see who is in the room.
Cancelled - nothing was sent. Check the recipient with /clients.
```

The server pins the verified audience for the whole transfer, so no chunk of a
private file can reach the rest of the room. Works the same way in both TCP and
UDP; in UDP the audience is pinned per-transfer because chunks are reflected
individually.

---

## Who sees what (data abstraction)

The **server** is the only component that sees connection detail. Its console
keeps the full table — id, name, remote address, join time, uptime — and prints
it on every connect and roster request:

```
[NEW CONNECTION] #2 Otter (127.0.0.1:51241) connected.
  ID   NAME       ADDRESS               JOINED    UPTIME
  #1   Falcon     127.0.0.1:51240       21:03:20  42s
  #2   Otter      127.0.0.1:51241       21:03:21  41s
```

A client is told only what it needs: its own name, the name and id of every other
client (so it can pick one with `to=`), and that somebody joined or left — as a
name. Addresses, ports, join times and uptimes are never sent to a client, not
even inside the `/ROSTER` payload.

---

## Packet analysis

Parse captures taken from Wireshark.

```powershell
python analyzer/analyze.py captures/tcp_demo.pcapng
python analyzer/analyze_udp.py captures/udp_loss_demo.pcapng --port 55001
python analyzer/report.py --tcp captures/tcp_demo.pcapng `
        --udp captures/udp_loss_demo.pcapng --out results/report.md
```

- `analyze.py` — TCP handshake, teardown, retransmissions
- `analyze_udp.py` — UDP sequence gaps, duplicates, out-of-order chunks
- `report.py` — a Markdown report comparing the two, with charts

Filters: `tcp port 65432` for TCP, `udp port 55001` for UDP.

---

## Tests

```powershell
python -m unittest discover tests -v
```

No network needed. Covers recipient selection, the text the clients print, and
the rule that connection detail never leaves the server.

---

## Layout

```
common_info.py   Shared protocol: control messages, naming, `to=` resolution, output
tcp_chat/        TCP implementation
udp_chat/        UDP implementation + packet-loss simulator
analyzer/        Capture analysis and report generation
scripts/         check_connection.py — firewall/port check for a two-device run
tests/           Unit tests
captures/        Saved .pcapng files
results/         Recorded hashes, drop logs, generated reports
```

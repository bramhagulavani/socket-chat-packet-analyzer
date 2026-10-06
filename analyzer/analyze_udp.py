import argparse
import sys
import re

# The scapy library is used to read and dissect PCAP files.
try:
    from scapy.all import rdpcap, UDP, IP, Raw
except ImportError:
    print("Error: 'scapy' is not installed. Please install it using 'pip install scapy'.")
    sys.exit(1)

def parse_expected_drops(filepath):
    """
    Parses a text file containing the simulator's output to extract the intentionally dropped chunks.
    Looks for lines like: "[SIMULATOR] Dropped chunk X intentionally."
    """
    drops = set()
    try:
        with open(filepath, 'r') as f:
            for line in f:
                # Use regex to find the chunk number
                match = re.search(r'Dropped chunk (\d+)', line)
                if match:
                    drops.add(int(match.group(1)))
    except FileNotFoundError:
        print(f"[ERROR] Could not find drops file '{filepath}'.")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Failed to read drops file: {e}")
        sys.exit(1)
    return drops

def analyze_udp(pcap_path, server_port, expected_drops_path=None):
    print(f"--- Analyzing UDP PCAP: {pcap_path} ---")
    try:
        packets = rdpcap(pcap_path)
    except Exception as e:
        print(f"Error reading PCAP file: {e}")
        return

    total_chunks = None
    received_seqs = set()
    highest_seq_seen = -1
    
    out_of_order = []
    duplicates = []

    print("\n--- Packet Stream ---")
    
    # Iterate through all packets in the capture (1-based index to match Wireshark)
    for i, pkt in enumerate(packets, start=1):
        if UDP not in pkt or Raw not in pkt:
            continue
            
        udp_layer = pkt[UDP]
        
        # VIVA POINT — Deduplication / Direction Isolation
        # The UDP server blindly broadcasts received chunks to all connected clients. 
        # If we analyzed the entire loopback interface without filtering, we would 
        # see every chunk twice: once uploading (Client->Server) and once reflecting 
        # (Server->Client).
        # We explicitly filter for `dport == server_port` so we only analyze 
        # the initial upload stream from the sender. This naturally eliminates 
        # the server's reflections, preventing false 'duplicate' detections.
        if udp_layer.dport != server_port:
            continue
            
        # Extract the application layer payload from the UDP datagram
        payload = bytes(pkt[Raw].load)
        
        # Check if this packet is the file metadata header
        if payload.startswith(b'/FILE '):
            # Header format: "/FILE <​filename> <total_chunks> [to=<Name>#<id>] [from=<Name>#<id>]"
            # The optional trailing fields identify the sender and the single
            # recipient, so we split on single spaces and read the count from
            # field 3 rather than lumping the rest of the line into it.
            parts = payload.split(b' ')
            if len(parts) >= 3:
                try:
                    total_chunks = int(parts[2])
                except ValueError:
                    total_chunks = None
                if total_chunks is not None:
                    options = parts[3:]
                    audience = 'the whole room'
                    for option in options:
                        if option.startswith(b'to='):
                            name, _, ident = option[3:].decode('ascii', 'replace').partition('#')
                            audience = f"{name} (#{ident}) only" if ident else name
                    print(f"Packet {i} [{pkt.time:.6f}]: [FILE HEADER] Expected total "
                          f"chunks: {total_chunks} -> {audience}")

                    
        # Check if this packet is a file chunk
        elif payload.startswith(b'/CHUNK '):
            # Chunk format: "/CHUNK <seq_num> <binary_data>"
            space_idx = payload.find(b' ', 7)
            if space_idx != -1:
                seq_str = payload[7:space_idx]
                try:
                    seq_num = int(seq_str)
                except ValueError:
                    continue
                
                # Detect Duplicates
                if seq_num in received_seqs:
                    duplicates.append(seq_num)
                    print(f"Packet {i} [{pkt.time:.6f}]: [DUPLICATE] Sequence {seq_num}")
                else:
                    received_seqs.add(seq_num)
                    
                    # Detect Out-of-Order Delivery
                    # If we receive sequence 5, but we previously saw sequence 8,
                    # it means sequence 5 took a slower path and arrived late.
                    if seq_num < highest_seq_seen:
                        out_of_order.append(seq_num)
                        print(f"Packet {i} [{pkt.time:.6f}]: [OUT OF ORDER] Sequence {seq_num} (Highest was {highest_seq_seen})")
                    else:
                        highest_seq_seen = seq_num
                        # Uncomment the line below if you want to see every single chunk print out
                        # print(f"Packet {i} [{pkt.time:.6f}]: [CHUNK] Sequence {seq_num}")

    print("\n--- Analysis Summary ---")
    if total_chunks is None:
        print("[WARNING] Could not find the /FILE header. Total expected chunks is unknown.")
        # Fallback if header is missing: guess based on the highest sequence number seen
        total_chunks = highest_seq_seen + 1 if highest_seq_seen >= 0 else 0
    else:
        print(f"Total Expected Chunks: {total_chunks}")
        
    print(f"Total Received Chunks: {len(received_seqs)}")
    
    missing_seqs = set(range(total_chunks)) - received_seqs
    missing_list = sorted(list(missing_seqs))
    
    loss_pct = (len(missing_seqs) / total_chunks * 100) if total_chunks > 0 else 0.0
        
    print(f"Missing Chunks: {len(missing_seqs)} ({loss_pct:.2f}% loss)")
    
    if missing_seqs:
        print(f"Missing Sequence Numbers: {missing_list}")
        
        # VIVA POINT — UDP Lack of Retransmission
        print("\n[VIVA] Retransmission Check:")
        print("Confirmed NO RETRANSMISSION. The missing sequence numbers never appeared")
        print("later in the capture. Unlike TCP, UDP has no acknowledgment mechanism to")
        print("detect the gap, and no built-in protocol to request the missing data.")
        print("The holes in the file remain unfilled.")
    else:
        print("Missing Sequence Numbers: None")

    print("\n[VIVA] Anomaly Check:")
    if out_of_order:
        print(f"Out-of-Order Deliveries: {len(out_of_order)} detected.")
    else:
        print("Out-of-Order Deliveries: none detected")
        
    if duplicates:
        print(f"Duplicate Chunks: {len(duplicates)} detected.")
    else:
        print("Duplicate Chunks: none detected")

    expected_drops_list = None
    # Optional: Cross-check against the application's text output of deliberately dropped chunks
    if expected_drops_path:
        print(f"\n--- Cross-Checking with Simulator Output ({expected_drops_path}) ---")
        expected_drops = parse_expected_drops(expected_drops_path)
        expected_drops_list = list(expected_drops)
        print(f"Drops reported by simulator: {sorted(list(expected_drops))}")
        
        if expected_drops == missing_seqs:
            print("Result: [MATCH] The missing packets exactly match the simulator's dropped chunks!")
        else:
            print("Result: [MISMATCH] The missing packets on the wire differ from the simulator's drops.")
            extra_missing = missing_seqs - expected_drops
            missing_but_seen = expected_drops - missing_seqs
            if extra_missing:
                print(f"  - Missing on wire, but NOT dropped by app (real network loss!): {sorted(list(extra_missing))}")
            if missing_but_seen:
                print(f"  - Dropped by app, but found on wire?! (Bug in simulator logic): {sorted(list(missing_but_seen))}")

    return {
        "total_chunks": total_chunks,
        "received_chunks": len(received_seqs),
        "missing_chunks": missing_list,
        "loss_pct": loss_pct,
        "out_of_order": out_of_order,
        "duplicates": duplicates,
        "expected_drops": expected_drops_list
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UDP Packet Loss Analyzer")
    parser.add_argument('pcap_file', help="Path to the PCAP file")
    parser.add_argument('--port', type=int, required=True, help="Server port to filter for upload traffic")
    parser.add_argument('--expected-drops', type=str, help="Path to text file containing simulator drops")
    
    args = parser.parse_args()
    analyze_udp(args.pcap_file, args.port, args.expected_drops)

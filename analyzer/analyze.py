import sys
from collections import defaultdict

# The scapy library is used to read and dissect PCAP files.
# It can parse the layers of each packet (e.g., IP, TCP) to access their fields.
try:
    from scapy.all import rdpcap, TCP, IP
except ImportError:
    print("Error: 'scapy' is not installed. Please install it using 'pip install scapy'.")
    sys.exit(1)

def get_connection_id(ip_src, port_src, ip_dst, port_dst):
    """
    A TCP connection is uniquely identified by a 4-tuple: 
    (Source IP, Source Port, Destination IP, Destination Port).
    Sorting the ports ensures we get the same ID regardless of the packet's direction.
    """
    if port_src < port_dst:
        return (ip_src, port_src, ip_dst, port_dst)
    else:
        return (ip_dst, port_dst, ip_src, port_src)

def analyze_pcap(filepath):
    print(f"--- Analyzing PCAP file: {filepath} ---")
    try:
        # Load the entire pcap file into memory for analysis. 
        # (Fine for this use case, but live capture/large files would use sniffer/generators)
        packets = rdpcap(filepath)
    except FileNotFoundError:
        print(f"Error: Could not find the file '{filepath}'.")
        return
    except Exception as e:
        print(f"Error reading PCAP file: {e}")
        return

    # Dictionary to hold data for each unique connection found in the capture.
    connections = defaultdict(lambda: {
        'client_port': None,
        'total_packets': 0,
        'total_payload_bytes': 0,
        'handshake': {'SYN': None, 'SYN_ACK': None, 'ACK': None},
        'teardown_type': None,
        'seen_seqs': set(), # Tracks (source_port, sequence_number, payload_length)
        'retransmissions': []
    })

    server_port = 65432

    # Iterate through all packets in the capture.
    # We use enumerate with start=1 so the packet numbers match Wireshark's 1-based indexing.
    for i, pkt in enumerate(packets, start=1):
        # We only care about TCP/IP packets for this analysis.
        if IP not in pkt or TCP not in pkt:
            continue
            
        ip_layer = pkt[IP]
        tcp_layer = pkt[TCP]
        
        # Filter for localhost traffic on our specific server port.
        if (ip_layer.src != "127.0.0.1" and ip_layer.dst != "127.0.0.1"):
            continue
        if (tcp_layer.sport != server_port and tcp_layer.dport != server_port):
            continue
            
        conn_id = get_connection_id(ip_layer.src, tcp_layer.sport, ip_layer.dst, tcp_layer.dport)
        conn = connections[conn_id]
        
        # The client port is whichever port is NOT the server port (65432).
        if conn['client_port'] is None:
            conn['client_port'] = tcp_layer.sport if tcp_layer.sport != server_port else tcp_layer.dport
            
        conn['total_packets'] += 1
        
        # TCP payload is anything in the TCP segment beyond the TCP header.
        payload_len = len(tcp_layer.payload)
        conn['total_payload_bytes'] += payload_len
        
        # TCP Flags: 
        # S (SYN)  : Synchronize sequence numbers (initiate connection).
        # A (ACK)  : Acknowledgment field is significant.
        # F (FIN)  : No more data from sender (teardown request).
        # R (RST)  : Reset the connection (abrupt teardown).
        # P (PSH)  : Push function (send data to application immediately).
        flags = str(tcp_layer.flags)
        
        # 1. Detect Handshake (SYN, SYN-ACK, ACK)
        # SYN: Sent by client. 'S' flag is set, 'A' is not.
        if flags == 'S':
            conn['handshake']['SYN'] = i
            print(f"Packet {i} [{pkt.time:.6f}]: [SYN] Client ({tcp_layer.sport}) -> Server ({tcp_layer.dport})")
            print(f"  Seq: {tcp_layer.seq}, Ack: {tcp_layer.ack}")
            
        # SYN-ACK: Sent by server. Both 'S' and 'A' flags are set.
        elif flags == 'SA':
            conn['handshake']['SYN_ACK'] = i
            print(f"Packet {i} [{pkt.time:.6f}]: [SYN-ACK] Server ({tcp_layer.sport}) -> Client ({tcp_layer.dport})")
            print(f"  Seq: {tcp_layer.seq}, Ack: {tcp_layer.ack}")
            
        # ACK (Handshake completion): Sent by client. 'A' flag is set, no 'S' or 'F' or 'R'.
        # We check if SYN and SYN-ACK were already seen to uniquely identify the handshake ACK
        # vs a regular data ACK.
        elif 'A' in flags and 'S' not in flags and 'F' not in flags and 'R' not in flags:
            if conn['handshake']['SYN'] and conn['handshake']['SYN_ACK'] and not conn['handshake']['ACK']:
                # The final ACK of the 3-way handshake comes from the client.
                if tcp_layer.sport == conn['client_port']:
                    conn['handshake']['ACK'] = i
                    print(f"Packet {i} [{pkt.time:.6f}]: [ACK] Client ({tcp_layer.sport}) -> Server ({tcp_layer.dport}) (Handshake Complete)")
                    print(f"  Seq: {tcp_layer.seq}, Ack: {tcp_layer.ack}")
                    
        # 2. Detect Teardown (FIN or RST)
        if 'F' in flags:
            if conn['teardown_type'] != 'RST': # RST represents an abrupt close, it overrides normal FIN teardown
                conn['teardown_type'] = 'FIN/ACK'
            print(f"Packet {i} [{pkt.time:.6f}]: [FIN] Port {tcp_layer.sport} -> {tcp_layer.dport} (Teardown)")
            print(f"  Seq: {tcp_layer.seq}, Ack: {tcp_layer.ack}")
            
        if 'R' in flags:
            conn['teardown_type'] = 'RST'
            print(f"Packet {i} [{pkt.time:.6f}]: [RST] Port {tcp_layer.sport} -> {tcp_layer.dport} (Abrupt Teardown)")
            print(f"  Seq: {tcp_layer.seq}, Ack: {tcp_layer.ack}")

        # 3. Detect Retransmissions
        # A retransmission happens when the same sequence number carrying >0 byte payload
        # is sent more than once from the same source port. (This ignores pure ACKs that 
        # don't carry payload but might share sequence numbers).
        if payload_len > 0:
            seq_id = (tcp_layer.sport, tcp_layer.seq, payload_len)
            if seq_id in conn['seen_seqs']:
                conn['retransmissions'].append(i)
                print(f"Packet {i} [{pkt.time:.6f}]: [RETRANSMISSION] Port {tcp_layer.sport} -> {tcp_layer.dport}")
                print(f"  Seq: {tcp_layer.seq}, Payload: {payload_len} bytes")
            else:
                conn['seen_seqs'].add(seq_id)

    # 4. Print Summary per Connection
    print("\n--- Connection Summaries ---")
    if not connections:
        print("No TCP connections found on localhost:65432.")
        return

    for conn_id, conn in connections.items():
        client_port = conn['client_port']
        print(f"\nConnection: Client Port {client_port} <-> Server Port {server_port}")
        print(f"  - Total Packets: {conn['total_packets']}")
        print(f"  - Total Payload: {conn['total_payload_bytes']} bytes")
        
        # A full 3-way handshake means SYN, SYN-ACK, and ACK were all observed
        handshake_complete = all(conn['handshake'].values())
        print(f"  - Handshake Found: {'Yes' if handshake_complete else 'No'}")
        
        teardown = conn['teardown_type'] if conn['teardown_type'] else 'None detected'
        print(f"  - Teardown Type: {teardown}")
        
        if conn['retransmissions']:
            print(f"  - Retransmissions: {len(conn['retransmissions'])} detected at packet(s) {', '.join(map(str, conn['retransmissions']))}")
        else:
            print("  - Retransmissions: No retransmissions detected")

    return dict(connections)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python analyze.py <path_to_pcap_file>")
        sys.exit(1)
        
    pcap_path = sys.argv[1]
    analyze_pcap(pcap_path)

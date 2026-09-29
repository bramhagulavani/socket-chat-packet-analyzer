import argparse
import sys
import os
import contextlib

# Ensure we can import the analyzer modules from the same directory
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from analyze import analyze_pcap
from analyze_udp import analyze_udp

def generate_report(args):
    # Suppress stdout while running the underlying analyzers to keep CLI quiet
    # (as their original CLI behavior of printing must remain intact when run directly)
    with open(os.devnull, 'w') as devnull:
        with contextlib.redirect_stdout(devnull):
            tcp_res_raw = analyze_pcap(args.tcp)
            # Hardcoding UDP port 55001 based on default client configuration
            udp_res = analyze_udp(args.udp, 55001, args.expected_drops)
            
            tcp_loss_res_raw = None
            if args.tcp_loss:
                tcp_loss_res_raw = analyze_pcap(args.tcp_loss)
                
    # tcp_res_raw is a dict keyed by connection ID. We expect 1 main connection.
    tcp_res = list(tcp_res_raw.values())[0] if tcp_res_raw else None
    tcp_loss_res = list(tcp_loss_res_raw.values())[0] if tcp_loss_res_raw else None

    # Chart generation (optional, only if matplotlib is installed)
    chart_filename = None
    try:
        import matplotlib.pyplot as plt
        if udp_res and udp_res.get('total_chunks'):
            plt.figure(figsize=(6, 4))
            labels = ['Expected', 'Received', 'Missing']
            values = [
                udp_res['total_chunks'], 
                udp_res['received_chunks'], 
                len(udp_res['missing_chunks'])
            ]
            colors = ['#4CAF50', '#2196F3', '#F44336']
            plt.bar(labels, values, color=colors)
            plt.title('UDP File Transfer Chunk Delivery')
            plt.ylabel('Number of Chunks')
            
            chart_filename = 'udp_chart.png'
            plt.savefig(chart_filename)
            plt.close()
    except ImportError:
        print("Note: matplotlib not installed. Skipping bar chart generation.")

    print(f"Generating report at: {args.out}")
    with open(args.out, 'w') as f:
        f.write("# TCP vs UDP: Packet Analysis Comparison Report\n\n")
        
        # Section A: Test Setup
        f.write("## 1. Test Setup\n")
        f.write(f"- **TCP Capture:** `{args.tcp}`\n")
        f.write(f"- **UDP Capture:** `{args.udp}`\n")
        if args.tcp_loss:
            f.write(f"- **TCP Loss Capture:** `{args.tcp_loss}`\n")
        else:
            f.write("- **TCP Loss Capture:** not captured\n")
            
        if args.expected_drops:
            f.write(f"- **Drops file:** `{args.expected_drops}`\n")
            
        tcp_port_str = f"Client {tcp_res['client_port']} <-> Server 65432" if tcp_res else "not captured"
        udp_port_str = "Server Port 55001"
        f.write(f"- **Ports:** TCP ({tcp_port_str}), UDP ({udp_port_str})\n")
        
        tcp_bytes = f"{tcp_res['total_payload_bytes']} bytes" if tcp_res else "not captured"
        f.write(f"- **TCP Payload Size:** {tcp_bytes}\n")
        
        if udp_res and udp_res.get('expected_drops') is not None and udp_res.get('total_chunks'):
            sim_loss = len(udp_res['expected_drops']) / udp_res['total_chunks'] * 100
            f.write(f"- **Simulated UDP Loss Rate:** {sim_loss:.2f}%\n")
        else:
            f.write("- **Simulated UDP Loss Rate:** not captured\n")

        # Section B: TCP Results
        f.write("\n## 2. TCP Results\n")
        if tcp_res:
            hs = tcp_res['handshake']
            f.write(f"- **Handshake Packets:** SYN (Packet {hs.get('SYN', 'not captured')}), "
                    f"SYN-ACK (Packet {hs.get('SYN_ACK', 'not captured')}), "
                    f"ACK (Packet {hs.get('ACK', 'not captured')})\n")
            f.write(f"- **Teardown Type:** {tcp_res['teardown_type'] or 'None detected'}\n")
            retrans = tcp_res['retransmissions']
            f.write(f"- **Retransmissions:** {len(retrans)} detected\n")
            f.write("- **Delivery Outcome:** Complete (reliable byte stream)\n")
        else:
            f.write("- not captured\n")
            
        # Section C: UDP Results
        f.write("\n## 3. UDP Results\n")
        if udp_res:
            f.write(f"- **Chunks Expected:** {udp_res['total_chunks'] if udp_res['total_chunks'] is not None else 'not captured'}\n")
            f.write(f"- **Chunks Received:** {udp_res['received_chunks']}\n")
            f.write(f"- **Chunks Missing:** {len(udp_res['missing_chunks'])}\n")
            f.write(f"- **Loss Percentage:** {udp_res['loss_pct']:.2f}%\n")
            f.write(f"- **Retransmission of missing chunks:** No (UDP is stateless)\n")
            
            if udp_res['expected_drops'] is not None:
                if set(udp_res['expected_drops']) == set(udp_res['missing_chunks']):
                    f.write("- **Drops Check:** MATCH (Wire loss perfectly matches simulator drops)\n")
                else:
                    f.write("- **Drops Check:** MISMATCH vs simulator log\n")
            else:
                f.write("- **Drops Check:** not captured\n")
                
            if chart_filename:
                f.write(f"\n![UDP Chunk Delivery]({chart_filename})\n")
        else:
            f.write("- not captured\n")
            
        # Section D: TCP Behavior Under Loss
        if args.tcp_loss:
            f.write("\n## 4. TCP Behavior Under OS-Level Loss\n")
            if tcp_loss_res:
                retrans = tcp_loss_res['retransmissions']
                f.write(f"- **TCP Retransmissions Events:** {len(retrans)} detected at packets {retrans}\n")
                f.write("- **Delivery Outcome:** The file still arrived completely despite dropped packets on the wire. TCP successfully tracked the missing sequence numbers and retransmitted them until acknowledged.\n")
            else:
                f.write("- not captured\n")
                
        # Section E: Side-by-Side Comparison
        f.write("\n## 5. Side-by-Side Comparison\n")
        f.write("| Feature | TCP | UDP |\n")
        f.write("|---------|-----|-----|\n")
        f.write("| **Connection Setup** | 3-Way Handshake (SYN, SYN-ACK, ACK) | Connectionless (None) |\n")
        f.write("| **Reliability** | Guaranteed (Acknowledges data) | Best Effort (Fire and forget) |\n")
        f.write("| **Ordering** | In-order delivery guaranteed | Out-of-order possible |\n")
        f.write("| **Behavior under loss** | Retransmits missing segments automatically | Ignores holes, data is lost forever |\n")
        f.write("| **Overhead** | High (Headers, state tracking) | Low (Minimal header, stateless) |\n")
        f.write("| **Delivery completeness** | 100% | Vulnerable to network loss |\n")

        # Section F: Conclusion
        f.write("\n## 6. What this shows (Unit III Conclusion)\n")
        f.write("This analysis concretely demonstrates the foundational differences between TCP and UDP as taught in Unit III. ")
        f.write("TCP operates as a stateful, reliable byte-stream protocol. By establishing a connection via the 3-way handshake and ")
        f.write("tracking sequence numbers, TCP can transparently recover from OS-level packet loss through automatic retransmissions, ")
        f.write("guaranteeing full delivery. In contrast, UDP operates as a stateless datagram protocol. When application-level chunks ")
        f.write("are dropped on the wire, UDP has no mechanism to detect the gap or request retransmission. Consequently, the data is lost permanently, ")
        f.write("proving that applications requiring reliability must either use TCP or implement their own complex tracking logic over UDP.\n")
        
    print("Done!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TCP vs UDP Packet Analysis Report Generator")
    parser.add_argument('--tcp', required=True, help="Path to TCP pcap file")
    parser.add_argument('--udp', required=True, help="Path to UDP pcap file")
    parser.add_argument('--tcp-loss', help="Path to TCP loss pcap file (optional)")
    parser.add_argument('--expected-drops', help="Path to text file containing UDP simulator drops (optional)")
    parser.add_argument('--out', required=True, help="Path to output Markdown report")
    
    args = parser.parse_args()
    generate_report(args)

import argparse
import socket
import sys

def check_connection(ip, port):
    print(f"Testing TCP connection to {ip}:{port}...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)  # 3-second timeout is plenty for a local LAN
    
    try:
        s.connect((ip, port))
        print("[SUCCESS] Connection established! The server is reachable.")
        s.close()
        sys.exit(0)
    except socket.timeout:
        print("[FAILURE] Connection timed out.")
        print("  Diagnosis: The packets are being dropped by the network.")
        print("  - Is the server's Windows Defender Firewall blocking the port?")
        print("  - Are both devices on the exact same Wi-Fi network?")
        print("  - Does the router have 'Client Isolation' (or 'AP Isolation') turned on?")
        sys.exit(1)
    except ConnectionRefusedError:
        print("[FAILURE] Connection refused.")
        print("  Diagnosis: The server machine was reached, but no process is listening.")
        print("  - Did you start the server script?")
        print("  - Did you start the server with '--host 0.0.0.0'?")
        print("  - Are you using the correct port?")
        sys.exit(1)
    except Exception as e:
        print(f"[FAILURE] Unknown error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Check TCP connection to a server")
    parser.add_argument('ip', type=str, help="Server IP address")
    parser.add_argument('port', type=int, help="Server port")
    args = parser.parse_args()
    
    check_connection(args.ip, args.port)

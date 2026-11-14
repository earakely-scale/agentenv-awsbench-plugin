"""Wait until HOST:PORT accepts a connection: python3 wait_port.py HOST PORT SECONDS (exit 1 on timeout)."""
import socket
import sys
import time

host, port, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
deadline = time.monotonic() + seconds
while time.monotonic() < deadline:
    try:
        socket.create_connection((host, port), 1).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.5)
sys.exit(f"nothing answered on {host}:{port} in {seconds:.0f} s")

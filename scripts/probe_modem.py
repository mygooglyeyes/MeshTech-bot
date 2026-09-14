#!/usr/bin/env python3
"""One-shot raw-socket probe: NOISE_REQ + STATUS_REQ against cleanmodem.

Proves the radio path is alive: a REAL noise floor (not the -105.0
default) and live counters. Prints values only - no secrets.
Usage: python3 probe_modem.py <token_file> [port]
"""
import socket
import struct
import sys

sys.path.insert(0, "/opt/meshtech-bot")
from cleanmodem import frames  # noqa: E402

token_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/cleanmodem/observer.token"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5055

with open(token_path, encoding="utf-8") as handle:
    token = handle.readline().strip()

sock = socket.create_connection(("127.0.0.1", port), timeout=5)
sock.sendall(token.encode("utf-8"))
answer = sock.recv(1)
if answer != b"\x01":
    print("AUTH FAILED:", answer)
    sys.exit(1)


def exchange(cmd):
    sock.sendall(frames.build_frame(cmd))
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        parsed = frames.parse_frame(buf)
        if parsed is not None:
            return parsed
    return None


cmd, payload, _ = exchange(frames.CMD_NOISE_REQ)
noise = struct.unpack("<h", payload)[0] / 10.0
print(f"noise floor: {noise} dBm   (real radio reading if > -130)")

cmd, payload, _ = exchange(frames.CMD_STATUS_REQ)
(uptime, rx, tx, crc, last_rssi, last_snr_x10, noise_x10, _max, state) = \
    struct.unpack(frames.STATUS_RESP_FMT, payload)
print(f"status: uptime={uptime}s rx={rx} tx={tx} crc={crc} "
      f"last_rssi={last_rssi} snr={last_snr_x10 / 10} "
      f"noise={noise_x10 / 10} rx_state={'RX' if state == 1 else 'NOT RX'}")
sock.close()

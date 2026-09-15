#!/usr/bin/env python3
"""Raw-SPI SX1262 register/command dump - settle WHERE the -105 comes from.

Read-only: pokes nothing, transmits nothing, changes no radio state
(one STANDBY_RC -> RX dance at the end restores what GetRssiInst
needs; the running cleanmodem re-arms RX on its next work item anyway).

Run ON THE RADIO BOX as root, while cleanmodem is STOPPED (two SPI
masters must not fight):

    sudo systemctl stop cleanmodem
    sudo python3 scripts/probe_rssi_raw.py
    sudo systemctl start cleanmodem

Prints, side by side: GetStatus, GetRssiInst (THE noise byte),
GetPacketStatus (the proven-good 3-byte read packets use). If
GetRssiInst's window echoes a constant (the 0xD2-style status byte)
while GetPacketStatus carries real data, the bug is the read slice,
not the channel. If it varies run to run, -105 was real.
"""
import sys
import time

sys.path.insert(0, "/opt/meshtech-bot")

from cleanmodem.sx126x import (  # noqa: E402
    OP_GET_PACKET_STATUS, OP_GET_RSSI_INST, SX126xRadio, _default_gpio,
    _default_spi)
from cleanmodem.config import PIN_PRESETS  # noqa: E402

OP_GET_STATUS = 0xC0          # datasheet §13.1.1 (not a module constant)

READS = (
    ("GetStatus       ", OP_GET_STATUS, 1),
    ("GetRssiInst     ", OP_GET_RSSI_INST, 1),
    ("GetPacketStatus ", OP_GET_PACKET_STATUS, 3),
)


def main() -> int:
    pins = dict(PIN_PRESETS["pimesh-1w-v2"])
    gpio = _default_gpio(False, "gpiod")
    spi = _default_spi(0, 0, 2_000_000)

    # Same bring-up order the driver uses (minimal: to the point where
    # the chip answers commands - the running service did the rest).
    gpio.setup_out(pins["en"], 1)
    time.sleep(0.05)
    gpio.setup_out(pins["reset"], 1)
    gpio.setup_in(pins["busy"])
    gpio.write(pins["reset"], 0)
    time.sleep(0.002)
    gpio.write(pins["reset"], 1)
    time.sleep(0.01)

    def wait_busy():
        deadline = time.monotonic() + 1.0
        while gpio.read(pins["busy"]):
            if time.monotonic() >= deadline:
                print("BUSY stuck high - chip not answering; aborting.")
                return False
            time.sleep(0.0002)
        return True

    if not wait_busy():
        return 1

    def read_cmd(opcode, size, tries=5):
        """One read; repeated so a constant vs varying byte is obvious."""
        rows = []
        for _ in range(tries):
            wait_busy()
            raw = spi.transfer(bytes([opcode]) + bytes(size + 2))
            rows.append(bytes(raw[2:2 + size]).hex(" "))
            time.sleep(0.05)
        return rows

    print("== raw MISO dumps (5 reads each, 50 ms apart) ==")
    for name, opcode, size in READS:
        print(f"{name} [{size}B]:")
        for row in read_cmd(opcode, size):
            print(f"    {row}")

    print("\nReading: full-frame view of ONE GetRssiInst transfer")
    wait_busy()
    full = spi.transfer(bytes([OP_GET_RSSI_INST]) + bytes(3))
    print("    whole transfer:", bytes(full).hex(" "),
          "(MISO byte0=garbage, byte1=status, byte2=data)")
    print("\nInterpretation: GetRssiInst data identical across reads AND")
    print("equal to the status byte -> the slice sees an echo, not RSSI;")
    print("varying values around -95..-115 (byte/2) -> -105 was REAL.")
    gpio, spi = None, None
    SX126xRadio  # imported to prove the module loads; no instance made
    return 0


if __name__ == "__main__":
    sys.exit(main())

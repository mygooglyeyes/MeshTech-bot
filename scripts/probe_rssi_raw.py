#!/usr/bin/env python3
"""Raw-SPI SX1262 dump IN RX, via the real driver - settle the -105.

Differences from v1 (which ran against a barely-initialized chip in
STANDBY and returned 0xFF - not the running state): this one performs
the driver's FULL init, arms continuous RX exactly like the modem,
then dumps the WHOLE MISO window of each read command while the chip
is actually receiving - the exact condition _hw_noise runs under.

Read-only: pokes no config, transmits nothing. Run ON THE RADIO BOX
as root with cleanmodem STOPPED (two SPI masters must not fight):

    sudo systemctl stop cleanmodem
    sudo python3 scripts/probe_rssi_raw.py
    sudo systemctl start cleanmodem

What to look for: a REAL channel-RSSI byte sits where the value/2
lands in -90..-120 dBm (0xB4..0xF0) and wobbles between reads. A
constant byte at offset 2 with a wobbling one at offset 3 (or vice
versa) means hilltop's read slice is off by one for this command.
"""
import sys
import time

sys.path.insert(0, "/opt/meshtech-bot")

from cleanmodem.config import PIN_PRESETS  # noqa: E402
from cleanmodem.sx126x import (  # noqa: E402
    OP_GET_IRQ_STATUS, OP_GET_PACKET_STATUS, OP_GET_RSSI_INST,
    OP_GET_RX_BUFFER_STATUS, SX126xRadio)

OP_GET_STATUS = 0xC0          # datasheet §13.1.1 (not a module constant)

WINDOW = 8                    # MISO bytes captured per transfer
READS = (
    ("GetStatus       ", OP_GET_STATUS),
    ("GetRssiInst     ", OP_GET_RSSI_INST),
    ("GetPacketStatus ", OP_GET_PACKET_STATUS),
    ("GetRxBufferStat ", OP_GET_RX_BUFFER_STATUS),
    ("GetIrqStatus    ", OP_GET_IRQ_STATUS),
)
TRIES = 8


def _dbm(byte_val: int) -> str:
    return f"{byte_val / -2.0:+.1f}"


def main() -> int:
    radio = SX126xRadio(dict(PIN_PRESETS["pimesh-1w-v2"]),
                        gpio_backend="gpiod")
    if not radio._hw_begin():
        print("chip did not come up - aborting")
        return 1
    radio._hw_enter_rx()
    time.sleep(0.2)                       # let AGC settle into RX
    print("== driver in continuous RX (full init) ==")
    print(f"driver _hw_noise() says: {_dbm(0):>6} is the formula; "
          f"it returns {radio._hw_noise():+.1f} dBm\n")

    spi = radio._spi

    def wait_busy():
        deadline = time.monotonic() + 1.0
        while radio._gpio.read(radio._pins["busy"]):
            if time.monotonic() >= deadline:
                print("BUSY stuck high - aborting")
                return False
            time.sleep(0.0002)
        return True

    print(f"== whole {WINDOW + 1}-byte MISO windows x{TRIES} ==")
    for name, opcode in READS:
        print(f"{name}:")
        windows = []
        for _ in range(TRIES):
            if not wait_busy():
                return 1
            raw = spi.transfer(bytes([opcode]) + bytes(WINDOW))
            windows.append(bytes(raw))
            time.sleep(0.05)
        for w in windows[:3]:
            print("    " + bytes(w).hex(" "))
        # per-offset stability + RSSI interpretation
        for off in range(1, min(5, WINDOW)):
            vals = {w[off] for w in windows}
            sample = windows[0][off]
            note = f"  <- {_dbm(sample)} if RSSI" if vals else ""
            flag = "CONST" if len(vals) == 1 else "varies"
            print(f"    offset[{off}]: {flag} "
                  f"({', '.join(f'0x{v:02X}' for v in sorted(vals))}){note}")
        print()

    print("Interpretation: the RSSI byte is the offset that VARIES with")
    print("plausible dBm values (0xB4..0xF0 ~ -90..-120). A constant at")
    print("offset 2 while offset 3 carries the live value = the driver's")
    print("_read_cmd slice is one byte short ON THIS BOARD for this")
    print("command. All-constant = the channel truly is that quiet.")
    radio._hw_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

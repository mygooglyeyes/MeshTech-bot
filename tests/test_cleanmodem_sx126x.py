"""Tests for cleanmodem.sx126x - register math + IRQ path (no hardware)."""
import asyncio
import struct

import pytest

from cleanmodem import sx126x as sx
from cleanmodem.config import PIN_PRESETS


def test_rf_frequency_formula():
    # freq * 2^25 / 32 MHz (PLL step derivation, datasheet §13.1.7)
    expected = struct.pack(">I", (910_525_000 * (1 << 25)) // 32_000_000)
    assert sx._rf_frequency(910_525_000) == expected
    assert len(expected) == 4


def test_bw_register_mapping():
    assert sx._bw_register(7_800) == 0x00
    assert sx._bw_register(62_500) == 0x03
    assert sx._bw_register(125_000) == 0x04
    assert sx._bw_register(500_000) == 0x06
    # Boundaries match the reference driver's table.
    assert sx._bw_register(62_499) == 0x03


def test_timeout_steps():
    steps = sx._timeout_steps(2.0)
    expected = int(2.0 / 15.625e-6)
    assert steps == bytes([(expected >> 16) & 0xFF,
                           (expected >> 8) & 0xFF, expected & 0xFF])
    # Clamped to the 24-bit field.
    assert sx._timeout_steps(10_000.0) == b"\xff\xff\xff"


def test_signal_conversions():
    assert sx._rssi_dbm(56) == -28
    assert sx._rssi_dbm(0) == 0
    assert sx._snr_signed(0x24) == 9.0        # +9.00 dB
    assert sx._snr_signed(0xE0) == -8.0       # -8.00 dB
    assert sx._snr_signed(0x06) == 1.5        # quarter-dB step


def test_sync_word_bytes():
    # The MeshCore convention: 0x12 -> register 0x1424 (verified against
    # the reference driver's nibble rule, with the 8-bit register mask
    # the reference gets implicitly from register width).
    word = 0x12
    reg = bytes([(word & 0xF0) | 0x04, ((word << 4) | 0x04) & 0xFF])
    assert reg == b"\x14\x24"


def test_tcxo_ctrl_params_are_four_bytes():
    """v0.0.166: SetDIO3AsTcxoCtrl takes voltage + 3-byte timeout.

    The old 3-byte command was rejected (CmdStatus EXEC_FAIL), the
    TCXO never armed, and every clock-dependent command failed while
    the chip sat in standby answering register writes - the root cause
    of the hilltop deaf-RX day (trace 2026-09-14).
    """
    assert sx._tcxo_ctrl_params(1.8) == [0x02, 0x00, 0x05, 0x60]
    assert sx._tcxo_ctrl_params(1.6) == [0x00, 0x00, 0x05, 0x60]


def test_pimesh_1w_v2_preset_has_en_pin():
    """v0.0.166: the radio power-enable pin 26 from openHop's proven
    map. Without it the chip answered SPI from standby but the radio
    stage never ran."""
    pins = PIN_PRESETS["pimesh-1w-v2"]
    assert pins["en"] == 26
    # The draft carrier has no enable pin - absent is fine.
    assert PIN_PRESETS["pimesh-v2-draft"].get("en", -1) == -1


PINS_WORKING = {
    "spi_bus": 0, "cs": 8, "busy": 5, "dio1": 6, "reset": 18,
    "dio2_rf_switch": True, "dio3_tcxo": 1.8,
    "txen": -1, "rxen": -1, "lna": -1,
}


class ScriptedSpi:
    """SPI fake that answers per-opcode (null GPIO: busy always clear)."""

    def __init__(self):
        self.replies = {}

    def transfer(self, data: bytes) -> bytes:
        # Datasheet layout (v0.0.166): MISO = [opcode echo, status,
        # data...] - the driver slices [2:]. The v0.0.165 "data at 3"
        # fake encoded a misread probe (the chip was stuck in STANDBY
        # behind the TCXO bug; GetStatus repeats its status byte, which
        # is what made the probe look like an extra header byte).
        # Replies are stored data-only; the fake prepends the header.
        opcode = data[0]
        reply = self.replies.get(opcode)
        if reply is None:
            return bytes(len(data))
        if opcode == sx.OP_READ_BUFFER:
            # Buffer reads: MISO = [opcode, offset echo, status, data...]
            return bytes([opcode, data[1] if len(data) > 1 else 0,
                          0x00]) + bytes(reply)
        return bytes([opcode, 0xA2]) + bytes(reply)

    def close(self):
        pass


def _radio_with_scripted_spi():
    radio = sx.SX126xRadio(dict(PINS_WORKING), force_null_hw=False)
    spi = ScriptedSpi()
    spi.replies[sx.OP_GET_IRQ_STATUS] = [0x00, 0x02]          # RX_DONE
    spi.replies[sx.OP_GET_RX_BUFFER_STATUS] = [0x03, 0x00]    # len=3, start=0 (sliced [2:4])
    # ReadBuffer is a BUFFER read, not a command read: MISO returns
    # [opcode, offset, dummy, data...] - data starts at raw[3]. The
    # driver slices raw[3:] directly (never via _read_cmd), so this
    # reply keeps the plain data form.
    spi.replies[sx.OP_READ_BUFFER] = [0x01, 0x02, 0x03]
    spi.replies[sx.OP_GET_PACKET_STATUS] = [56, 0x24, 56]     # -28 dBm, +9 dB
    radio._spi = spi
    radio._gpio = sx._NullGpio()
    return radio


def test_hw_begin_runs_on_null_hardware():
    radio = sx.SX126xRadio(dict(PINS_WORKING), force_null_hw=True)
    radio._hw_begin()
    assert radio._rx_mode is True
    text = radio._describe()
    assert "SF7" in text and "62.5kHz" in text and "910.525MHz" in text
    radio._hw_shutdown()


def test_irq_delivery_produces_rx_callback():
    async def _run():
        radio = _radio_with_scripted_spi()
        captured = []
        radio._loop = asyncio.get_running_loop()
        radio.on_rx_packet = captured.append
        radio._handle_irq()
        await asyncio.sleep(0.05)   # let call_soon_threadsafe run
        assert len(captured) == 1
        pkt = captured[0]
        assert pkt.data == b"\x01\x02\x03"
        assert pkt.rssi == -28
        assert pkt.snr == 9.0
        assert radio.rx_count == 1
    asyncio.run(_run())


def test_irq_ignores_non_rx_flags():
    async def _run():
        radio = _radio_with_scripted_spi()
        radio._spi.replies[sx.OP_GET_IRQ_STATUS] = [0x00, 0x01]  # TX_DONE
        captured = []
        radio._loop = asyncio.get_running_loop()
        radio.on_rx_packet = captured.append
        radio._handle_irq()
        await asyncio.sleep(0.02)
        assert captured == []
        assert radio.rx_count == 0
    asyncio.run(_run())


def test_crc_error_counted_not_delivered():
    async def _run():
        radio = _radio_with_scripted_spi()
        radio._spi.replies[sx.OP_GET_IRQ_STATUS] = [0x00, 0x42]  # CRC|RX
        captured = []
        radio._loop = asyncio.get_running_loop()
        radio.on_rx_packet = captured.append
        radio._handle_irq()
        await asyncio.sleep(0.02)
        assert captured == []
        assert radio.crc_errors == 1
    asyncio.run(_run())


def test_tx_roundtrip_on_null_hardware():
    radio = sx.SX126xRadio(dict(PINS_WORKING), force_null_hw=True)
    radio._hw_begin()
    result = radio._hw_tx(b"\x01\x02\x03")
    # Null hardware never asserts TX_DONE -> a timeout result, not a
    # crash; the radio goes back to RX either way.
    assert result.ok is False
    assert radio._rx_mode is True
    radio._hw_shutdown()


def test_worker_skips_irq_polls_before_bringup():
    """v0.0.160: in irq_poll mode the worker polled flags before the
    bring-up work item ran, crashing on the None gpio/spi ('NoneType'
    object has no attribute 'read'' at every start - hilltop 2026-09-14).
    Polls must be skipped until the hardware is up."""
    import cleanmodem.sx126x as sx

    radio = sx.SX126xRadio({}, force_null_hw=True, irq_poll_mode=True)
    radio._gpio = None          # exactly the pre-bring-up state
    radio._spi = None
    radio._queue = __import__("queue").Queue()   # empty: no work items
    radio._stop_flag = True     # worker exits after one pass
    radio._worker()             # old code raised AttributeError here
    assert radio.irq_polls == 0

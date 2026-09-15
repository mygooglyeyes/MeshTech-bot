"""LoRaRF cross-check: pin the opcode table + TCXO params to the proven driver.

The hilltop EXEC_FAIL traces (2026-09-14) proved every clocked command
fails with our opcode table: SetDIO3AsTcxoCtrl is 0x97 (not 0xD4),
SetTxParams is 0x8E (not 0x8D), SetBufferBaseAddress is 0x8F (not 0x8E),
SetSyncWord does not exist as a command (sync word lives in register
0x0740 via WriteRegister 0x0D), and CalibrateImage pairs are
(0xE1, 0xE9) for 902-928 MHz. These tests pin all of it to the values
in LoRaRF-Python (the vendored, proven driver for this exact board).
"""
import pytest

from cleanmodem import sx126x


def test_tcxo_opcode_matches_lorarf():
    # LoRaRF SX126x.py: SET_DIO3_AS_TCXO_CTRL = 0x97.
    assert sx126x.OP_SET_DIO3_AS_TCXO_CTRL == 0x97


def test_tx_params_opcode_matches_lorarf():
    # LoRaRF: SET_TX_PARAMS = 0x8E. Our old 0x8D was a nonexistent
    # command, so TX power was never configured.
    assert sx126x.OP_SET_TX_PARAMS == 0x8E


def test_buffer_base_opcode_matches_lorarf():
    # LoRaRF: SET_BUFFER_BASE_ADDRESS = 0x8F.
    assert sx126x.OP_SET_BUFFER_BASE_ADDRESS == 0x8F


def test_set_sync_word_is_register_write():
    # There is no SetSyncWord command on the SX126x: the sync word
    # lives in register 0x0740 and is written via WriteRegister 0x0D.
    assert not hasattr(sx126x, "OP_SET_SYNC_WORD")
    assert sx126x.OP_WRITE_REGISTER == 0x0D
    assert sx126x.SYNC_WORD_REGISTER == 0x0740


def test_tcxo_params_voltage_and_timeout():
    # LoRaRF sends voltage code + 3-byte timeout; the proven delay for
    # a 1.8 V TCXO is 0x0560 (~5 ms of 32 MHz counts... actually units
    # of 15.625 us -> 21.875 ms is 0x058C; 0x0560 is the LoRaRF default
    # 5.5 ms class value used by the reference drivers).
    params = sx126x._tcxo_ctrl_params(1.8)
    assert params == [0x02, 0x00, 0x05, 0x60]   # 1.8 V + 0x000560


def test_calibrate_image_pair_902_928():
    # LoRaRF calibrateImagePairs: 902-928 -> (0xE1, 0xE9). Our old
    # (0x7B, 0x81) was an invalid pair -> EXEC_FAIL.
    assert sx126x._calibrate_image_pair(915_000_000) == [0xE1, 0xE9]


def test_calibrate_image_pair_863_870():
    assert sx126x._calibrate_image_pair(869_525_000) == [0xD7, 0xDB]


def test_sync_word_meshcore_mapping():
    # 0x12 (MeshCore config value) maps to register bytes 0x14 0x24.
    assert sx126x._sync_word_bytes(0x12) == b"\x14\x24"

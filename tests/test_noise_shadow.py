"""v0.0.186: the noise-attribute shadow that killed every NOISE_REQ.

RadioHal.__init__ assigned self.noise = -105.0, shadowing the async
noise() METHOD the server calls for every NOISE_REQ - self.hal.noise()
raised 'float' object is not callable and every read died into the
NO-VALUE sentinel (the real story of the frozen -105 line: the old
silent -105.0 fallback made the shadow look like a quiet channel).

These tests pin the class shape so it can never come back:
* the base class has a noise METHOD and no noise ATTRIBUTE;
* the full server round-trip answers a NOISE_REQ with a real payload
  through a HAL whose noise() returns a live value.
"""
from __future__ import annotations

import asyncio
import inspect
import struct

from cleanmodem import frames
from cleanmodem.hal import RadioHal

from tests.test_cleanmodem_server import (
    CTRL,
    _read_frame,
    connect,
    make_server,
    start_server,
)


def test_radiohal_has_noise_method_and_no_noise_attribute():
    """The async noise() method must never be shadowed by an attribute."""
    assert inspect.iscoroutinefunction(RadioHal.noise)
    hal = RadioHal()
    assert inspect.iscoroutinefunction(hal.noise), \
        "RadioHal.noise must stay the async method - an attribute with " \
        "this name shadows it and kills every NOISE_REQ"


def test_server_noise_roundtrip_returns_real_value():
    """End to end: NOISE_REQ against a working noise() answers a real
    NOISE_RESP payload (not the sentinel)."""

    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        try:
            hal.noise_value = -96.0
            reader, writer, answer = await connect(port, CTRL)
            assert answer == b"\x01"
            writer.write(frames.build_frame(frames.CMD_NOISE_REQ, b""))
            await writer.drain()
            cmd, payload, _ = await _read_frame(reader)
            assert cmd == frames.CMD_NOISE_RESP
            assert payload != frames.NOISE_NO_VALUE, \
                "a working noise() must answer a real value, not the sentinel"
            assert struct.unpack("<h", payload)[0] / 10.0 == -96.0
        finally:
            await server.stop()

    asyncio.run(_run())


def test_failed_noise_read_answers_sentinel():
    """A noise() that raises still answers the NO-VALUE sentinel (honest
    gap), not a fabricated number."""

    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        try:
            async def broken():
                raise RuntimeError("chip read failed")

            hal.noise = broken  # type: ignore[method-assign]
            reader, writer, answer = await connect(port, CTRL)
            assert answer == b"\x01"
            writer.write(frames.build_frame(frames.CMD_NOISE_REQ, b""))
            await writer.drain()
            cmd, payload, _ = await _read_frame(reader)
            assert cmd == frames.CMD_NOISE_RESP
            assert payload == frames.NOISE_NO_VALUE
        finally:
            await server.stop()

    asyncio.run(_run())

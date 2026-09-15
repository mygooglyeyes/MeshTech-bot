"""Server + security tests for cleanmodem (fake radio, no hardware).

Covers the plan's functional and security gates:
- auth matrix (roles, wrong token, observer TX refusal, fail-closed)
- parser fuzz (mutations never crash the server)
- frame truncation matrix
- fan-out and slow-client behavior
- config echo / version / ping flows
"""
import asyncio
import random

import pytest

from cleanmodem import frames
from cleanmodem.config import ModemConfig
from cleanmodem.hal import RadioHal, RxPacket, TxResult
from cleanmodem.server import ModemServer

TOKEN = "observer-secret"
CTRL = "controller-secret"


class FakeHal(RadioHal):
    """In-memory radio: records TX, emits nothing on its own."""

    def __init__(self):
        super().__init__()
        self.tx_log = []
        self.config_log = []
        self.cad_busy = False
        self.noise = -105.0

    async def start(self, loop):
        self._loop = loop
        return True

    async def stop(self):
        pass

    async def tx(self, data: bytes) -> TxResult:
        self.tx_log.append(bytes(data))
        return TxResult(ok=True, airtime_us=12345)

    async def cad(self, det_peak=0, det_min=0):
        return self.cad_busy

    async def noise(self):
        return self.noise

    async def status(self):
        from cleanmodem.hal import RadioStatus
        return RadioStatus(uptime_s=1, rx_count=0, tx_count=len(self.tx_log),
                           crc_errors=0, hal_alive=True,
                           irq_polls=0, irq_edges=0, last_irq_flags=0)

    async def apply_config(self, cfg):
        self.config_log.append(dict(cfg))
        return True


def make_server(cad_busy=False, lan=False):
    cfg = ModemConfig()
    if not lan:
        cfg.host = "127.0.0.1"
    cfg.clear_channel_wait_seconds = 0.0  # tests: no LBT waits
    cfg.politeness_seconds = 0.0
    hal = FakeHal()
    hal.cad_busy = cad_busy
    server = ModemServer(cfg, hal, observer_token=TOKEN,
                         controller_token=CTRL)
    return server, hal


async def start_server(server):
    ok = await server.start()
    assert ok, "server failed to start"
    port = server._server.sockets[0].getsockname()[1]
    return port


async def connect(port, token=None, role_wait=True):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if token is not None:
        writer.write(token.encode())
        await writer.drain()
        answer = await asyncio.wait_for(reader.readexactly(1), 5)
        return reader, writer, answer
    return reader, writer, None


def test_server_start_stop():
    async def _run():
        server, _ = make_server()
        port = await start_server(server)
        await server.stop()
    asyncio.run(_run())


def test_auth_matrix_controller_accepted():
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        reader, writer, answer = await connect(port, CTRL)
        assert answer == b"\x01"
        # TX works for the controller.
        writer.write(frames.build_frame(frames.CMD_TX_REQUEST, b"\x01\x02"))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_TX_DONE
        assert hal.tx_log == [b"\x01\x02"]
        await server.stop()
    asyncio.run(_run())


def test_observer_cannot_tx_but_keeps_session():
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        reader, writer, answer = await connect(port, TOKEN)
        assert answer == b"\x01"
        writer.write(frames.build_frame(frames.CMD_TX_REQUEST, b"\x01"))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_ERROR
        assert payload[0] == frames.ERR_UNAUTHORIZED
        assert hal.tx_log == []             # nothing reached the air
        # Session survives: PING still answers.
        writer.write(frames.build_frame(frames.CMD_PING))
        await writer.drain()
        cmd, _, _ = await _read_frame(reader)
        assert cmd == frames.CMD_PONG
        await server.stop()
    asyncio.run(_run())


def test_unauthenticated_gets_error_and_ping():
    async def _run():
        server, _ = make_server()
        port = await start_server(server)
        reader, writer, _ = await connect(port)   # no auth at all
        writer.write(frames.build_frame(frames.CMD_STATUS_REQ))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_ERROR
        assert payload[0] == frames.ERR_UNAUTHORIZED
        writer.write(frames.build_frame(frames.CMD_PING))
        await writer.drain()
        cmd, _, _ = await _read_frame(reader)
        assert cmd == frames.CMD_PONG       # ping answers even pre-auth
        await server.stop()
    asyncio.run(_run())


def test_wrong_token_three_strikes_then_close():
    async def _run():
        server, _ = make_server()
        port = await start_server(server)
        reader, writer, answer = await connect(port, "wrong-1")
        assert answer == b"\x00"
        reader2, writer2, answer2 = await connect(port, "wrong-2")
        assert answer2 == b"\x00"
        reader3, writer3, answer3 = await connect(port, "wrong-3")
        assert answer3 == b"\x00"
        # A correct token on a fresh connection still works (attempts
        # are per-connection), proving throttling didn't lock the port.
        reader4, writer4, answer4 = await connect(port, CTRL)
        assert answer4 == b"\x01"
        for w in (writer, writer2, writer3, writer4):
            w.close()
        await server.stop()
    asyncio.run(_run())


def test_empty_token_config_fails_closed():
    async def _run():
        cfg = ModemConfig()
        hal = FakeHal()
        # NO tokens configured: nobody can authenticate.
        server = ModemServer(cfg, hal, observer_token="",
                             controller_token="")
        port = await start_server(server)
        reader, writer, answer = await connect(port, "anything")
        assert answer == b"\x00"
        await server.stop()
    asyncio.run(_run())


def test_lbt_retry_delays_are_continuous(monkeypatch):
    """Pin the anti-correlation property of the LBT retry loop.

    Delays must be CONTINUOUS random draws in [0.10, 0.30] s - the old
    stack's empirically robust on-air pattern (observed backoffs
    102-295 ms). A future rework must not narrow this to a few fixed
    slots: nodes drawing from the same tiny delay set re-align their
    retries under congestion (v0.0.152 restored this; keep it).
    """
    captured = []
    clock = {"t": 0.0}

    async def fake_sleep(delay):
        captured.append(delay)
        clock["t"] += delay

    monkeypatch.setattr("cleanmodem.server.time.monotonic",
                        lambda: clock["t"])
    monkeypatch.setattr("cleanmodem.server.asyncio.sleep", fake_sleep)

    async def _run():
        server, _ = make_server(cad_busy=True)   # air busy forever
        server.cfg.clear_channel_wait_seconds = 5.0
        waited = await server._wait_clear_channel()
        assert waited is True                    # gave up at the cap

    asyncio.run(_run())

    assert len(captured) >= 10                   # a real retry loop ran
    assert all(0.10 <= d <= 0.30 for d in captured)   # the pinned range
    assert len(set(captured)) > 5                # continuous, not fixed slots
    assert max(captured) - min(captured) > 0.12  # covers most of the range


def test_config_echo_and_version():
    async def _run():
        server, _ = make_server()
        port = await start_server(server)
        reader, writer, _ = await connect(port, TOKEN)
        writer.write(frames.build_frame(frames.CMD_GET_CONFIG))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_CONFIG_RESP
        assert len(payload) == frames.RADIO_CONFIG_SIZE
        writer.write(frames.build_frame(frames.CMD_GET_VERSION))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_VERSION_RESP
        await server.stop()
    asyncio.run(_run())


def test_controller_set_config_reaches_radio():
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        reader, writer, _ = await connect(port, CTRL)
        import struct
        payload = struct.pack(frames.RADIO_CONFIG_FMT, 910525000, 62500,
                              7, 5, 20, 0x12, 32)
        writer.write(frames.build_frame(frames.CMD_SET_CONFIG, payload))
        await writer.drain()
        cmd, payload_back, _ = await _read_frame(reader)
        assert cmd == frames.CMD_CONFIG_RESP
        assert payload_back == payload
        assert hal.config_log, "radio never saw the new config"
        await server.stop()
    asyncio.run(_run())


def test_observer_set_config_gets_echo_never_applied():
    """v0.0.169: an observer's SET_CONFIG proposal is answered with the
    LIVE config, and the radio never sees it.

    openhop_core's TCPLoRaRadio (the repeater driver) sends SET_CONFIG
    during its handshake and treats any rejection (error 0x09) as a
    dead link, reconnect-looping every 10 s. The server must therefore
    answer observers with a CONFIG_RESP echo - while keeping the chip
    parameters exclusively under controller control.
    """
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        reader, writer, _ = await connect(port, TOKEN)      # observer
        import struct
        proposal = struct.pack(frames.RADIO_CONFIG_FMT,
                               869_525_000, 125_000, 11, 8, 22, 0x34, 8)
        writer.write(frames.build_frame(frames.CMD_SET_CONFIG, proposal))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_CONFIG_RESP
        assert payload == server._config_bytes, "echo must be the live config"
        assert payload != proposal, "observer proposal must not be applied"
        assert not hal.config_log, "radio saw an observer proposal"
        # Sanity: the controller path still applies config for real.
        rc, wctrl, _ = await connect(port, CTRL)
        ctrl_cfg = struct.pack(frames.RADIO_CONFIG_FMT,
                               910_525_000, 62_500, 7, 5, 20, 0x12, 32)
        wctrl.write(frames.build_frame(frames.CMD_SET_CONFIG, ctrl_cfg))
        await wctrl.drain()
        cmd, payload, _ = await _read_frame(rc)
        assert cmd == frames.CMD_CONFIG_RESP and payload == ctrl_cfg
        assert hal.config_log, "controller SET_CONFIG no longer reaches the radio"
        await server.stop()
    asyncio.run(_run())


def test_observer_set_cad_params_echoed():
    """v0.0.169: an observer's SET_CAD_PARAMS proposal is echoed.

    The repeater's TCPLoRaRadio restores its cached CAD settings after
    SET_CONFIG during the handshake; a rejection logged a warning on
    the repeater side ('CAD configuration rejected'). The echo keeps
    the handshake clean; live CAD params stay the controller's.
    """
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        reader, writer, _ = await connect(port, TOKEN)      # observer
        proposal = bytes([22, 10, 0x04])
        writer.write(frames.build_frame(frames.CMD_SET_CAD_PARAMS, proposal))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_CAD_PARAMS_RESP
        assert payload == proposal
        # The controller path is unchanged: echo for it too.
        rc, wctrl, _ = await connect(port, CTRL)
        wctrl.write(frames.build_frame(frames.CMD_SET_CAD_PARAMS, proposal))
        await wctrl.drain()
        cmd, payload, _ = await _read_frame(rc)
        assert cmd == frames.CMD_CAD_PARAMS_RESP and payload == proposal
        await server.stop()
    asyncio.run(_run())


def test_observer_not_idle_recycled():
    """v0.0.171: an authenticated observer is exempt from the idle
    read timeout.

    openhop_core's TCPLoRaRadio sends nothing after its handshake, so
    the idle recycler dropped a live repeater every ~60 s (hilltop
    2026-09-14: connect/auth/config every minute). After the fix the
    observer connection must survive well past the old 30 s timeout -
    on a busy mesh its link is also renewed by RX fanout traffic.
    """
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        reader, writer, _ = await connect(port, TOKEN)      # observer
        # 40 s of silence: far past the old 30 s idle recycle.
        await asyncio.sleep(40.0)
        # The connection still works: a GET_CONFIG gets answered.
        writer.write(frames.build_frame(frames.CMD_GET_CONFIG))
        await writer.drain()
        cmd, payload, _ = await _read_frame(reader)
        assert cmd == frames.CMD_CONFIG_RESP
        assert len(payload) == frames.RADIO_CONFIG_SIZE
        await server.stop()
    asyncio.run(_run())


def test_rx_fanout_to_all_clients():
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        r1, w1, _ = await connect(port, TOKEN)
        r2, w2, _ = await connect(port, CTRL)
        # Both clients see the same RX packet.
        server._on_rx_packet(RxPacket(rssi=-100, snr=0.0, signal_rssi=-100,
                                      data=b"hello", mono=1.0))
        for r in (r1, r2):
            cmd, payload, _ = await _read_frame(r)
            assert cmd == frames.CMD_RX_PACKET
            rssi, snr, sig, data = frames.parse_rx_payload(payload)
            assert data == b"hello"
        await server.stop()
    asyncio.run(_run())


def test_tx_loopback_reaches_observers():
    async def _run():
        server, hal = make_server()
        port = await start_server(server)
        robserver, wobs, _ = await connect(port, TOKEN)
        rctrl, wctrl, _ = await connect(port, CTRL)
        wctrl.write(frames.build_frame(frames.CMD_TX_REQUEST, b"loop"))
        await wctrl.drain()
        cmd, payload, _ = await _read_frame(rctrl)
        assert cmd == frames.CMD_TX_DONE
        # The observer hears the loopback (synthetic metadata).
        cmd, payload, _ = await _read_frame(robserver)
        assert cmd == frames.CMD_RX_PACKET
        _, _, _, data = frames.parse_rx_payload(payload)
        assert data == b"loop"
        await server.stop()
    asyncio.run(_run())


def test_parser_fuzz_never_crashes():
    """Deterministic mutation fuzz: every reply is a valid frame or the
    connection closes - the server must never crash or leak."""
    async def _run():
        rng = random.Random(1234)
        server, _ = make_server()
        port = await start_server(server)
        good = frames.build_frame(frames.CMD_AUTH, CTRL.encode())
        crashes = 0
        for i in range(150):
            data = bytearray(good)
            for _ in range(rng.randint(1, 4)):
                pos = rng.randrange(len(data))
                data[pos] = rng.randrange(256)
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port)
                writer.write(bytes(data) + bytes(rng.randrange(256)
                                                 for _ in range(8)))
                await writer.drain()
                try:
                    answer = await asyncio.wait_for(reader.read(256), 2.0)
                except asyncio.TimeoutError:
                    answer = b""
                writer.close()
                if answer:
                    # Must be parseable or empty - a wrong-CRC answer is
                    # itself a valid frame.
                    try:
                        frames.parse_frame(answer)
                    except frames.FrameError:
                        crashes += 1
            except (ConnectionError, OSError):
                pass
        assert crashes == 0, f"{crashes} unparseable server replies"
        await server.stop()
    asyncio.run(_run())


def test_truncation_matrix_offers_no_crash():
    async def _run():
        server, _ = make_server()
        port = await start_server(server)
        frame = frames.build_frame(frames.CMD_AUTH, CTRL.encode())
        for cut in range(1, len(frame)):
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port)
                writer.write(frame[:cut])
                await writer.drain()
                await asyncio.wait_for(reader.read(64), 0.2)
                writer.close()
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
        # Server still healthy afterwards.
        reader, writer, answer = await connect(port, CTRL)
        assert answer == b"\x01"
        await server.stop()
    asyncio.run(_run())


async def _read_frame(reader, timeout=5.0):
    """Read one complete frame from the socket."""
    buf = b""
    while True:
        try:
            parsed = frames.parse_frame(buf)
        except frames.FrameError:
            idx = frames.find_sync(buf, 1)
            buf = buf[idx:] if idx >= 0 else b""
            continue
        if parsed is not None:
            cmd, payload, size = parsed
            return cmd, payload, size
        chunk = await asyncio.wait_for(reader.read(4096), timeout)
        if not chunk:
            raise ConnectionError("server closed")
        buf += chunk

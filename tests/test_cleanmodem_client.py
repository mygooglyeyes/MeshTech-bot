"""Tests for cleanmodem.client - the bot's controller link (fake modem)."""
import asyncio

import pytest

from cleanmodem import frames
from cleanmodem.client import ModemClient

TOKEN = "ctrl-token"


class FakeModem:
    """A minimal modem: token auth, honest TX replies, RX push."""

    def __init__(self):
        self.tx_log = []
        self._writer = None
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle,
                                                 "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        # Close the established client connection too, or its handler
        # keeps looping on read timeouts and wait_closed() hangs.
        if self._writer is not None:
            self._writer.close()
        self.server.close()
        await self.server.wait_closed()

    async def push_rx(self, data: bytes, rssi=-100, snr=0.0, sig=-100):
        """Push one RX packet to the authenticated client."""
        assert self._writer is not None, "no authenticated client yet"
        self._writer.write(frames.build_rx_packet(rssi, snr, sig, data))
        await self._writer.drain()

    async def _handle(self, reader, writer):
        first = await reader.read(256)
        if first.decode("utf-8", "replace") != TOKEN:
            writer.write(b"\x00")
            await writer.drain()
            return
        writer.write(b"\x01")
        await writer.drain()
        self._writer = writer
        buf = b""
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), 5.0)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    return
                buf += chunk
                while True:
                    try:
                        parsed = frames.parse_frame(buf)
                    except frames.FrameError:
                        buf = buf[1:]
                        continue
                    if parsed is None:
                        break
                    cmd, payload, size = parsed
                    buf = buf[size:]
                    if cmd == frames.CMD_TX_REQUEST:
                        self.tx_log.append(payload)
                        writer.write(frames.build_frame(
                            frames.CMD_TX_DONE, b"\x00\x00\x00\x00"))
                        await writer.drain()
        finally:
            if self._writer is writer:
                self._writer = None


async def _wait_connected(client, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not client.connected:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("client never connected")
        await asyncio.sleep(0.05)


async def _shutdown_client(client, task):
    """Stop the client AND release the socket (a cancelled task does
    not close its transport by itself - the fake modem would wait for
    EOF forever)."""
    client.stop()
    task.cancel()
    writer, client._writer = client._writer, None
    if writer is not None:
        writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    await asyncio.gather(task, return_exceptions=True)


def test_client_auth_rx_and_tx_roundtrip():
    async def _run():
        modem = FakeModem()
        port = await modem.start()
        rx_seen = []

        async def on_rx(rssi, snr, sig, data):
            rx_seen.append((rssi, snr, sig, data))

        client = ModemClient("127.0.0.1", port, TOKEN, on_rx)
        task = asyncio.create_task(client.run())
        await _wait_connected(client)

        # TX resolves True and reaches the modem.
        assert await client.send(b"\x01\x02") is True
        assert modem.tx_log == [b"\x01\x02"]

        # A pushed RX packet lands in the callback.
        for _ in range(50):
            if modem._writer is not None:
                break
            await asyncio.sleep(0.05)
        await modem.push_rx(b"rx-bytes", rssi=-73, snr=9.5)
        for _ in range(50):
            if rx_seen:
                break
            await asyncio.sleep(0.05)
        assert rx_seen == [(-73, 9.5, -100, b"rx-bytes")]

        await _shutdown_client(client, task)
        await asyncio.wait_for(modem.stop(), 5)
    asyncio.run(_run())


def test_client_wrong_token_never_connects():
    async def _run():
        modem = FakeModem()
        port = await modem.start()
        client = ModemClient("127.0.0.1", port, "WRONG", None)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.4)
        assert client.connected is False
        # TX on a dead link fails closed, never raises.
        assert await client.send(b"\x01") is False
        await _shutdown_client(client, task)
        await asyncio.wait_for(modem.stop(), 5)
    asyncio.run(_run())


def test_client_tx_after_link_loss_fails_closed():
    async def _run():
        modem = FakeModem()
        port = await modem.start()
        client = ModemClient("127.0.0.1", port, TOKEN, None)
        task = asyncio.create_task(client.run())
        await _wait_connected(client)
        # Kill the modem server (and the live link): the client must
        # fail its next TX closed, never raise.
        await asyncio.wait_for(modem.stop(), 5)
        await asyncio.sleep(0.3)
        assert await client.send(b"\x01") is False
        await _shutdown_client(client, task)
    asyncio.run(_run())


def test_client_keepalive_prevents_idle_recycle():
    """v0.0.168: the client PINGs so the server never recycles it.

    The server recycles sessions quiet for ~30 s; before the
    keepalive, a controller idle on a quiet mesh was dropped every
    ~32 s (hilltop 2026-09-14 flapping). The fake modem drops
    silent clients after 2.5 s (a compressed stand-in for the
    recycler) and the PING interval is patched to 0.5 s, so the
    client must survive many recycle windows.
    """
    import cleanmodem.client as client_mod

    async def _run():
        modem = FakeModem()

        async def dropping_handle(reader, writer):
            first = await reader.read(256)
            if first.decode('utf-8', 'replace') != TOKEN:
                return
            writer.write(b'')
            await writer.drain()
            buf = b''
            try:
                while True:
                    chunk = await asyncio.wait_for(reader.read(256), 2.5)
                    if not chunk:
                        return
                    buf += chunk
                    while True:
                        try:
                            parsed = frames.parse_frame(buf)
                        except frames.FrameError:
                            buf = buf[1:]
                            continue
                        if parsed is None:
                            break
                        cmd, payload, size = parsed
                        buf = buf[size:]
                    # Authenticated traffic received: session alive.
            except asyncio.TimeoutError:
                return                     # silent client: recycled

        modem._handle = dropping_handle
        port = await modem.start()
        old_interval = client_mod.PING_INTERVAL_S
        client_mod.PING_INTERVAL_S = 0.5
        try:
            client = ModemClient('127.0.0.1', port, TOKEN, None)
            task = asyncio.create_task(client.run())
            await _wait_connected(client)
            # 6 s = twelve PING cycles; without keepalive the fake
            # recycles us at ~2.5 s of silence.
            await asyncio.sleep(6.0)
            assert client.connected is True, (
                'client was recycled - keepalive PINGs not sent')
            await _shutdown_client(client, task)
        finally:
            client_mod.PING_INTERVAL_S = old_interval
        await asyncio.wait_for(modem.stop(), 5)
    asyncio.run(_run())

"""End-to-end unit test for TunnelForwarder.

We don't have a real userspace tunnel available in unit tests, so we
simulate the RSD's ``create_service_connection`` by wiring a fake service
that connects to an in-test echo server. That's enough to prove the
forwarder actually splices bytes both ways.
"""
from __future__ import annotations

import asyncio

import pytest

from src.tunnel_forwarder import TunnelForwarder


class _FakeServiceConnection:
    """Minimal stand-in for pymobiledevice3.ServiceConnection.

    Only exposes what TunnelForwarder actually uses: ``.reader``,
    ``.writer``, ``start()``, and ``aclose()``.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class _FakeRsd:
    """Fake RSD that dials an in-process echo server on demand.

    The forwarder's contract is that ``create_service_connection(port)``
    returns something whose ``.reader`` / ``.writer`` route to the device
    on ``port``. Here we route to a local TCP echo server instead.
    """

    def __init__(self, target_port: int):
        self.target_port = target_port

    async def create_service_connection(self, port: int):
        assert port == self.target_port
        r, w = await asyncio.open_connection("127.0.0.1", port)
        return _FakeServiceConnection(r, w)


async def _run_echo_server() -> tuple[asyncio.base_events.Server, int]:
    async def handle(reader, writer):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    return server, port


@pytest.mark.asyncio
async def test_forwarder_splices_bytes_both_ways():
    echo_server, echo_port = await _run_echo_server()
    try:
        rsd = _FakeRsd(target_port=echo_port)
        fwd = TunnelForwarder(rsd, remote_port=echo_port, label="test")
        local_port = await fwd.start()
        assert local_port > 0
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
            writer.write(b"hello via tunnel\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.readexactly(len(b"hello via tunnel\n")), timeout=2.0)
            assert data == b"hello via tunnel\n"

            # Send more, receive more — proves both directions stay alive
            writer.write(b"second\n")
            await writer.drain()
            data2 = await asyncio.wait_for(reader.readexactly(7), timeout=2.0)
            assert data2 == b"second\n"

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        finally:
            await fwd.close()
    finally:
        echo_server.close()
        await echo_server.wait_closed()


@pytest.mark.asyncio
async def test_forwarder_close_stops_new_connections():
    echo_server, echo_port = await _run_echo_server()
    try:
        rsd = _FakeRsd(target_port=echo_port)
        fwd = TunnelForwarder(rsd, remote_port=echo_port, label="test")
        local_port = await fwd.start()
        await fwd.close()

        with pytest.raises((ConnectionRefusedError, OSError)):
            _r, w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", local_port),
                timeout=1.0,
            )
            w.close()
    finally:
        echo_server.close()
        await echo_server.wait_closed()


@pytest.mark.asyncio
async def test_forwarder_start_twice_is_idempotent():
    echo_server, echo_port = await _run_echo_server()
    try:
        rsd = _FakeRsd(target_port=echo_port)
        fwd = TunnelForwarder(rsd, remote_port=echo_port, label="test")
        port1 = await fwd.start()
        port2 = await fwd.start()  # second start returns same port
        assert port1 == port2
        await fwd.close()
    finally:
        echo_server.close()
        await echo_server.wait_closed()


@pytest.mark.asyncio
async def test_forwarder_rsd_failure_closes_client_cleanly():
    class _FailingRsd:
        async def create_service_connection(self, port: int):
            raise ConnectionRefusedError("iPhone side not ready")

    fwd = TunnelForwarder(_FailingRsd(), remote_port=8100, label="test")
    local_port = await fwd.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        # The forwarder logs a warning and closes; the client sees EOF
        # instead of a hang.
        data = await asyncio.wait_for(reader.read(16), timeout=2.0)
        assert data == b""
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    finally:
        await fwd.close()

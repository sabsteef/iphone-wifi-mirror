"""Tests for TunnelManager health monitoring + auto-reconnect.

We can't spin up a real userspace tunnel without an iPhone, so
:class:`UserspaceRsdTunnel` is monkey-patched with a fake that produces
predictable RSD stand-ins. This lets us verify the state machine — connect,
health probe, drop, reconnect, backoff, failure — without a device.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Optional
from unittest.mock import MagicMock

import pytest

from src import tunnel_manager
from src.tunnel_manager import (
    HEALTH_INTERVAL_S,
    MAX_RECONNECT_ATTEMPTS,
    STATUS_CONNECTED,
    STATUS_FAILED,
    STATUS_LOST,
    STATUS_RECONNECTED,
    STATUS_RECONNECTING,
    TunnelManager,
)


class FakeRsd:
    """Matches the v9 RSD shape: get_date() is a coroutine, not sync.

    This is deliberately async — the health probe in TunnelManager must
    await it. If we made this sync, the ``run_in_executor(rsd.get_date)``
    bug from the initial v9 migration would have slipped through the test
    (executor scheduled the coroutine but never awaited it, and the caller
    read an unfulfilled coroutine object as ``truthy`` → probe reported
    healthy even when the device was gone).
    """
    def __init__(self, udid: str = "TEST-UDID", healthy: bool = True):
        self.udid = udid
        self.product_type = "iPhone18,2"
        self.product_version = "27.0"
        self._healthy = healthy

    def set_healthy(self, ok: bool) -> None:
        self._healthy = ok

    async def get_date(self) -> dt.datetime:
        if not self._healthy:
            raise RuntimeError("tunnel dead")
        return dt.datetime(2026, 7, 20)


class FakeTunnel:
    """Mimics UserspaceRsdTunnel enough for TunnelManager to drive it."""
    _open_calls = 0
    _fail_next: Optional[int] = None  # class-level counter to fail N next opens

    def __init__(self, serial: Optional[str] = None, autopair: bool = True):
        self.serial = serial
        self.rsd = FakeRsd(udid=serial or "TEST-UDID")
        self._closed = False

    async def aopen(self) -> FakeRsd:
        FakeTunnel._open_calls += 1
        if FakeTunnel._fail_next is not None and FakeTunnel._fail_next > 0:
            FakeTunnel._fail_next -= 1
            raise RuntimeError("simulated open failure")
        return self.rsd

    async def aclose(self) -> None:
        self._closed = True


@pytest.fixture(autouse=True)
def _patch_tunnel(monkeypatch):
    """Replace UserspaceRsdTunnel with FakeTunnel for every test."""
    FakeTunnel._open_calls = 0
    FakeTunnel._fail_next = None
    monkeypatch.setattr(tunnel_manager, "UserspaceRsdTunnel", FakeTunnel)


@pytest.mark.asyncio
async def test_connect_returns_rsd():
    mgr = TunnelManager()
    rsd = await mgr.connect("TEST-UDID")
    assert isinstance(rsd, FakeRsd)
    assert rsd.udid == "TEST-UDID"
    assert mgr.is_connected
    assert mgr.udid == "TEST-UDID"
    await mgr.disconnect()
    assert not mgr.is_connected


@pytest.mark.asyncio
async def test_connect_idempotent_for_same_udid():
    mgr = TunnelManager()
    rsd1 = await mgr.connect("A")
    rsd2 = await mgr.connect("A")
    assert rsd1 is rsd2
    assert FakeTunnel._open_calls == 1
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_connect_switches_devices():
    mgr = TunnelManager()
    await mgr.connect("A")
    await mgr.connect("B")
    assert mgr.udid == "B"
    assert FakeTunnel._open_calls == 2
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_status_callback_fires_on_connect():
    events: list[tuple[str, str]] = []
    mgr = TunnelManager()
    mgr.on_status_change(lambda s, r: events.append((s, r)))
    await mgr.connect("A")
    await asyncio.sleep(0)  # let callback run
    assert any(s == STATUS_CONNECTED for s, _ in events)
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_health_probe_success(monkeypatch):
    # Speed the health loop way up so the test doesn't take 6s.
    monkeypatch.setattr(tunnel_manager, "HEALTH_INTERVAL_S", 0.05)
    mgr = TunnelManager()
    await mgr.connect("A")
    # Let the health loop fire a few times
    await asyncio.sleep(0.25)
    assert mgr.is_connected
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_health_failure_triggers_reconnect(monkeypatch):
    monkeypatch.setattr(tunnel_manager, "HEALTH_INTERVAL_S", 0.05)
    monkeypatch.setattr(tunnel_manager, "HEALTH_TIMEOUT_S", 0.1)

    events: list[tuple[str, str]] = []
    mgr = TunnelManager()
    mgr.on_status_change(lambda s, r: events.append((s, r)))

    rsd = await mgr.connect("A")
    # Break the tunnel — next health probe will fail
    rsd.set_healthy(False)
    # Give health loop time to detect + trigger reconnect + reopen
    await asyncio.sleep(1.5)

    statuses = [s for s, _ in events]
    assert STATUS_LOST in statuses
    # After lost we should see either reconnecting or reconnected
    assert STATUS_RECONNECTING in statuses or STATUS_RECONNECTED in statuses
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_reconnect_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(tunnel_manager, "HEALTH_INTERVAL_S", 0.05)
    monkeypatch.setattr(tunnel_manager, "HEALTH_TIMEOUT_S", 0.1)
    monkeypatch.setattr(tunnel_manager, "MAX_RECONNECT_ATTEMPTS", 2)

    # Patch backoff calculation: use 0.1 * attempt instead of 2^(attempt-1)
    # so 2 attempts take ~0.4s instead of 3s.
    orig_sleep = asyncio.sleep
    async def fast_sleep(delay):
        # Cap all sleeps at 0.1s during test
        await orig_sleep(min(delay, 0.1))
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    events: list[tuple[str, str]] = []
    mgr = TunnelManager()
    mgr.on_status_change(lambda s, r: events.append((s, r)))

    rsd = await mgr.connect("A")
    rsd.set_healthy(False)
    FakeTunnel._fail_next = 10  # every reconnect open will fail

    # Wait long enough for: health probe (0.05s) + lost + 2 reconnect attempts
    # (each 0.1s backoff + open attempt) + failed emission.
    await orig_sleep(2.0)

    statuses = [s for s, _ in events]
    assert STATUS_LOST in statuses, f"got: {statuses}"
    assert STATUS_FAILED in statuses, f"got: {statuses}"
    assert not mgr.is_connected
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_disconnect_cancels_in_flight_reconnect(monkeypatch):
    monkeypatch.setattr(tunnel_manager, "HEALTH_INTERVAL_S", 0.05)
    monkeypatch.setattr(tunnel_manager, "HEALTH_TIMEOUT_S", 0.1)

    mgr = TunnelManager()
    rsd = await mgr.connect("A")
    rsd.set_healthy(False)
    # Force reconnect attempts to hang so we can cancel mid-way
    FakeTunnel._fail_next = 99
    await asyncio.sleep(0.3)  # loss detected, reconnect started

    await mgr.disconnect()
    assert not mgr.is_connected

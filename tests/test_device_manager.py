"""Tests for DeviceManager async lifecycle.

Uses the same fake tunnel plumbing as test_tunnel_manager plus a fake
usbmux ``list_devices`` so we can drive discovery without an iPhone.
Qt signals are captured with a manual listener list — no QApplication
needed for these unit tests.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Optional

import pytest

from src import device_manager, tunnel_manager
from src.device_manager import ConnectionState


class FakeRsd:
    def __init__(self, udid: str = "TEST-UDID"):
        self.udid = udid
        self.product_type = "iPhone18,2"
        self.product_version = "27.0"

    def get_date(self):
        return dt.datetime(2026, 7, 20)

    def get_value(self, key=None):
        return {"DeviceName": "TestPhone"}.get(key, "")


class FakeTunnel:
    _open_calls = 0
    _fail_next: Optional[int] = None

    def __init__(self, serial: Optional[str] = None, autopair: bool = True):
        self.serial = serial
        self.rsd = FakeRsd(udid=serial or "TEST-UDID")

    async def aopen(self) -> FakeRsd:
        FakeTunnel._open_calls += 1
        if FakeTunnel._fail_next and FakeTunnel._fail_next > 0:
            FakeTunnel._fail_next -= 1
            raise RuntimeError("simulated open failure")
        return self.rsd

    async def aclose(self) -> None:
        pass


class FakeMuxDevice:
    def __init__(self, serial: str, connection_type: str = "Network",
                 name: str = "iPhone", model: str = "iPhone18,2",
                 ios: str = "27.0"):
        self.serial = serial
        self.connection_type = connection_type
        self.device_name = name
        self.properties = {"ProductType": model, "ProductVersion": ios}


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    FakeTunnel._open_calls = 0
    FakeTunnel._fail_next = None
    monkeypatch.setattr(tunnel_manager, "UserspaceRsdTunnel", FakeTunnel)


class SignalCatcher:
    """Manual QObject.pyqtSignal capture — records emissions in order."""
    def __init__(self):
        self.emissions: list[tuple[str, tuple]] = []

    def bind(self, dm):
        for name in [
            "device_connected", "device_disconnected",
            "connection_error", "connection_state_changed",
            "tunnel_status_changed",
        ]:
            sig = getattr(dm, name)
            sig.connect(lambda *args, _n=name: self.emissions.append((_n, args)))

    def by(self, name: str) -> list[tuple]:
        return [args for n, args in self.emissions if n == name]


@pytest.fixture
def qapp():
    """Create a QApplication once for signal machinery."""
    from PyQt6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.mark.asyncio
async def test_list_available_devices_empty(qapp, monkeypatch):
    async def fake_list(*a, **kw):
        return []
    monkeypatch.setattr(device_manager, "usbmux_list_devices", fake_list)

    dm = device_manager.DeviceManager()
    devices = await dm.list_available_devices()
    assert devices == []


@pytest.mark.asyncio
async def test_list_available_devices_finds_device(qapp, monkeypatch):
    async def fake_list(*a, **kw):
        return [FakeMuxDevice("TEST-UDID")]
    monkeypatch.setattr(device_manager, "usbmux_list_devices", fake_list)

    dm = device_manager.DeviceManager()
    devices = await dm.list_available_devices()
    assert len(devices) == 1
    assert devices[0]["udid"] == "TEST-UDID"
    assert devices[0]["model"] == "iPhone18,2"


@pytest.mark.asyncio
async def test_pick_best_prefers_configured_udid(qapp, monkeypatch):
    async def fake_list(*a, **kw):
        return [FakeMuxDevice("A"), FakeMuxDevice("B")]
    monkeypatch.setattr(device_manager, "usbmux_list_devices", fake_list)

    dm = device_manager.DeviceManager()
    dm.set_preferred_udid("B")
    devices = await dm.list_available_devices()
    best = dm._pick_best(devices)
    assert best["udid"] == "B"


@pytest.mark.asyncio
async def test_pick_best_skips_when_preferred_missing(qapp):
    dm = device_manager.DeviceManager()
    dm.set_preferred_udid("B")
    best = dm._pick_best([{"udid": "A"}])
    assert best is None


@pytest.mark.asyncio
async def test_pick_best_defaults_to_first(qapp):
    dm = device_manager.DeviceManager()
    best = dm._pick_best([{"udid": "X"}, {"udid": "Y"}])
    assert best["udid"] == "X"


@pytest.mark.asyncio
async def test_connect_populates_device_info(qapp, monkeypatch):
    async def fake_list(*a, **kw):
        return [FakeMuxDevice("A")]
    monkeypatch.setattr(device_manager, "usbmux_list_devices", fake_list)

    catcher = SignalCatcher()
    dm = device_manager.DeviceManager()
    catcher.bind(dm)

    await dm._connect("A")
    assert dm.is_connected
    assert dm.device_info["udid"] == "A"
    assert dm.device_info["model"] == "iPhone18,2"
    assert dm.device_info["ios_version"] == "27.0"
    assert len(catcher.by("device_connected")) == 1

    await dm.cleanup()


@pytest.mark.asyncio
async def test_wda_url_uses_rsd_address(qapp, monkeypatch):
    async def fake_list(*a, **kw):
        return [FakeMuxDevice("A")]
    monkeypatch.setattr(device_manager, "usbmux_list_devices", fake_list)

    # Extend FakeRsd with a service attribute for URL building
    class FakeService:
        address = ("fd00::1", 12345)

    old_open = FakeTunnel.aopen
    async def open_with_service(self):
        rsd = await old_open(self)
        rsd.service = FakeService()
        return rsd
    monkeypatch.setattr(FakeTunnel, "aopen", open_with_service)

    dm = device_manager.DeviceManager()
    await dm._connect("A")
    url = dm.get_wda_url()
    assert url == "http://[fd00::1]:8100"
    await dm.cleanup()

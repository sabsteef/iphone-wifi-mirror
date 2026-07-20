"""Async device discovery + connect + WDA lifecycle for v9.

Replaces the v7 sync/threaded manager. All pymobiledevice3 interaction is
awaited on the qasync loop so the Qt UI stays responsive.

Responsibilities:
    * Poll usbmux for connected devices (WiFi and USB).
    * Filter by user's preferred UDID (from :class:`InputHandler` UI / QSettings).
    * Ask :class:`TunnelManager` to open a userspace tunnel to the selected device.
    * Populate ``device_info`` (name / model / iOS / connection type).
    * Manage the WDA xcuitest runner subprocess (start / stop, own process
      group so SIGTERM to the app tears it down cleanly).

Screen capture and touch input read ``rsd`` / ``get_wda_url()`` from us and go
straight to the device — we don't proxy those calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from enum import Enum
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from pymobiledevice3.bonjour import browse_mobdev2
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.usbmux import list_devices as usbmux_list_devices

from src.tunnel_manager import (
    STATUS_CONNECTED,
    STATUS_FAILED,
    STATUS_LOST,
    STATUS_RECONNECTED,
    STATUS_RECONNECTING,
    TunnelManager,
)

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    ERROR = 3


def _log_task_exception(label: str):
    """Return a Task done-callback that logs exceptions with a label.

    ``asyncio.ensure_future(coro)`` without a stored reference silently
    swallows exceptions until GC time. Attach this callback so any
    coroutine failure shows up in the log immediately with context.
    """
    def _cb(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("%s failed: %s", label, exc, exc_info=exc)
    return _cb


class DeviceManager(QObject):
    device_connected = pyqtSignal(dict)
    device_disconnected = pyqtSignal()
    connection_error = pyqtSignal(str)
    connection_state_changed = pyqtSignal(ConnectionState)
    tunnel_status_changed = pyqtSignal(str, str)  # (status, reason)

    WDA_BUNDLE_ID = "com.sabsteef.WebDriverAgentRunner.xctrunner"
    DISCOVERY_INTERVAL_S = 2.0

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._state = ConnectionState.DISCONNECTED
        self._tunnel_mgr = TunnelManager()
        self._tunnel_mgr.on_status_change(self._on_tunnel_status)
        self._current_udid: Optional[str] = None
        self._device_info: dict = {}
        self._wda_proc: Optional[subprocess.Popen] = None
        self._preferred_udid: Optional[str] = None
        self._discovery_task: Optional[asyncio.Task] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._stop_discovery = asyncio.Event()

    # ────────────────────────────── public state ─────────────────────────────

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def device_info(self) -> dict:
        return self._device_info

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def rsd(self) -> Optional[RemoteServiceDiscoveryService]:
        return self._tunnel_mgr.rsd

    def set_preferred_udid(self, udid: Optional[str]) -> None:
        prev = self._preferred_udid
        self._preferred_udid = udid or None
        if prev == self._preferred_udid:
            return
        # If we're currently connected to a different device, drop it so the
        # discovery loop picks the new preference next tick.
        if self._current_udid and self._current_udid != self._preferred_udid:
            logger.info(
                "Preferred device changed (%s -> %s), disconnecting to switch",
                prev, self._preferred_udid,
            )
            # Cancel any in-flight switch so rapid combo changes don't stack.
            prior = getattr(self, "_switch_task", None)
            if prior is not None and not prior.done():
                prior.cancel()
            self._switch_task = asyncio.ensure_future(self.disconnect())
            self._switch_task.add_done_callback(_log_task_exception("device-switch disconnect"))

    def get_wda_url(self) -> str:
        rsd = self._tunnel_mgr.rsd
        if rsd is None or not rsd.service.address:
            return "http://localhost:8100"
        return f"http://[{rsd.service.address[0]}]:8100"

    # ────────────────────────────── discovery ────────────────────────────────

    def start_discovery(self) -> None:
        if self._discovery_task and not self._discovery_task.done():
            return
        self._stop_discovery.clear()
        self._discovery_task = asyncio.ensure_future(self._discovery_loop())

    def stop_discovery(self) -> None:
        self._stop_discovery.set()
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()

    async def _discovery_loop(self) -> None:
        try:
            while not self._stop_discovery.is_set():
                if self._state != ConnectionState.CONNECTED and (
                    self._connect_task is None or self._connect_task.done()
                ):
                    try:
                        devices = await self.list_available_devices()
                        best = self._pick_best(devices)
                        if best is not None:
                            self._connect_task = asyncio.ensure_future(
                                self._connect(best["udid"])
                            )
                    except Exception as e:
                        logger.debug("Discovery iteration error: %s", e)
                try:
                    await asyncio.wait_for(
                        self._stop_discovery.wait(),
                        timeout=self.DISCOVERY_INTERVAL_S,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        finally:
            logger.debug("Discovery loop exited")

    async def list_available_devices(self) -> list[dict]:
        """Return every device usbmux/bonjour sees, USB + WiFi both."""
        try:
            devices = await usbmux_list_devices()
        except Exception as e:
            logger.debug("usbmux list failed: %s", e)
            return []
        result: list[dict] = []
        for d in devices:
            info = {"udid": getattr(d, "serial", "") or ""}
            if not info["udid"]:
                continue
            info["connection_type"] = getattr(d, "connection_type", "unknown")
            info["name"] = getattr(d, "device_name", None) or "iPhone"
            info["model"] = ""
            info["ios"] = ""
            try:
                props = getattr(d, "properties", {}) or {}
                info["model"] = props.get("ProductType", "") or ""
                info["ios"] = props.get("ProductVersion", "") or ""
            except Exception:
                pass
            result.append(info)
        return result

    async def bonjour_visible_hosts(self, timeout: float = 2.5) -> list[str]:
        """Return iPhone-like hostnames Bonjour advertises on the local network.

        Used as a supplementary signal when usbmux sees zero devices:
        Bonjour can pick up iPhones that usbmux hasn't paired with yet,
        letting the UI hint at "iPhone in range but trust missing" instead
        of an empty search screen.
        """
        try:
            answers = await browse_mobdev2(timeout=timeout)
        except Exception as e:
            logger.debug("Bonjour browse failed: %s", e)
            return []
        seen = []
        for a in answers:
            host = getattr(a, "host", "") or ""
            if host and host not in seen:
                seen.append(host)
        return seen

    def _pick_best(self, devices: list[dict]) -> Optional[dict]:
        if not devices:
            return None
        if self._preferred_udid:
            for d in devices:
                if d["udid"] == self._preferred_udid:
                    return d
            return None
        return devices[0]

    # ────────────────────────────── connect ──────────────────────────────────

    def _set_state(self, state: ConnectionState) -> None:
        self._state = state
        self.connection_state_changed.emit(state)

    async def _connect(self, udid: str) -> None:
        if self._state == ConnectionState.CONNECTED and self._current_udid == udid:
            return
        self._set_state(ConnectionState.CONNECTING)
        try:
            rsd = await self._tunnel_mgr.connect(udid)
            self._current_udid = udid
            self._device_info = {
                "name": await self._safe_get_value(rsd, "DeviceName", default="iPhone"),
                "model": rsd.product_type,
                "ios_version": rsd.product_version,
                "udid": rsd.udid,
                "connection_type": "tunnel",
            }
            self._set_state(ConnectionState.CONNECTED)
            self.device_connected.emit(self._device_info)
            logger.info(
                "Connected to %s (%s, iOS %s)",
                self._device_info["name"],
                self._device_info["model"],
                self._device_info["ios_version"],
            )
        except Exception as e:
            logger.error("Tunnel connect failed for %s: %s", udid, e)
            self._set_state(ConnectionState.ERROR)
            self.connection_error.emit(str(e))

    async def _safe_get_value(self, rsd, key: str, default: str = "") -> str:
        try:
            v = await rsd.get_value(key=key)
            return v or default
        except Exception:
            return default

    # ────────────────────────────── WDA lifecycle ────────────────────────────

    def start_wda(self, auth_token: Optional[str] = None) -> bool:
        if self._wda_proc and self._wda_proc.poll() is None:
            logger.info("WDA already running")
            return True
        if not self._current_udid:
            logger.warning("No UDID selected — cannot start WDA")
            return False

        logger.info("Starting WDA via xcuitest (device %s)", self._current_udid)
        cmd = [
            sys.executable, "-m", "pymobiledevice3",
            "developer", "dvt", "xcuitest",
            self.WDA_BUNDLE_ID,
            "--userspace",
            "--udid", self._current_udid,
            "--env", "MJPEG_SERVER_SCREENSHOT_QUALITY=55",
            "--env", "MJPEG_SCALING_FACTOR=50",
            "--env", "MJPEG_SERVER_FRAMERATE=12",
        ]
        if auth_token:
            cmd.extend(["--env", f"WDA_AUTH_TOKEN={auth_token}"])
            logger.info("WDA will require auth token")

        # Log to a temp file so early-exit diagnostics survive, but we do NOT
        # keep a PIPE open — after ~64 KiB of xcuitest log, pymobiledevice3
        # would block on write() and WDA would freeze after a few minutes
        # with no obvious cause in our own log.
        import tempfile
        wda_log = tempfile.NamedTemporaryFile(
            mode="w+b", prefix="wda-", suffix=".log", delete=False,
        )
        self._wda_log_path = wda_log.name
        try:
            self._wda_proc = subprocess.Popen(
                cmd,
                stdout=wda_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(5)
            if self._wda_proc.poll() is not None:
                wda_log.seek(0)
                output = (wda_log.read() or b"").decode(errors="replace")
                wda_log.close()
                logger.error("WDA exited early: %s", output[-500:])
                self._wda_proc = None
                return False
            wda_log.close()  # subprocess still holds its own dup'd fd
            logger.info(
                "WDA xcuitest started (PID %d, log: %s)",
                self._wda_proc.pid, self._wda_log_path,
            )
            return True
        except Exception as e:
            try:
                wda_log.close()
            except Exception:
                pass
            logger.error("Failed to start WDA: %s", e)
            return False

    def stop_wda(self) -> None:
        if not self._wda_proc:
            return
        proc = self._wda_proc
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception as e:
            logger.debug("killpg SIGTERM failed: %s", e)
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._wda_proc = None
        logger.info("WDA xcuitest stopped")

    def is_wda_running(self) -> bool:
        return self._wda_proc is not None and self._wda_proc.poll() is None

    # ────────────────────────────── tunnel status ────────────────────────────

    def _on_tunnel_status(self, status: str, reason: str) -> None:
        """Called by TunnelManager on connect/lost/reconnecting/failed events.

        Not a coroutine; runs on the qasync loop from TunnelManager._emit().
        Forwarded to Qt signal so UI can react.
        """
        self.tunnel_status_changed.emit(status, reason)

        if status == STATUS_LOST:
            logger.warning("Tunnel lost: %s — killing WDA until reconnect", reason)
            self.stop_wda()
            if self._state == ConnectionState.CONNECTED:
                self._set_state(ConnectionState.CONNECTING)
        elif status == STATUS_RECONNECTED:
            logger.info("Tunnel reconnected: %s", reason)
            self._set_state(ConnectionState.CONNECTED)
            self.device_connected.emit(self._device_info)
        elif status == STATUS_FAILED:
            logger.error("Tunnel gave up reconnecting: %s", reason)
            self._set_state(ConnectionState.ERROR)
            self.connection_error.emit(f"Tunnel dropped and could not recover: {reason}")

    # ────────────────────────────── shutdown ─────────────────────────────────

    async def disconnect(self) -> None:
        self.stop_wda()
        await self._tunnel_mgr.disconnect()
        self._current_udid = None
        self._device_info = {}
        self._set_state(ConnectionState.DISCONNECTED)
        self.device_disconnected.emit()

    async def cleanup(self) -> None:
        self.stop_discovery()
        await self.disconnect()

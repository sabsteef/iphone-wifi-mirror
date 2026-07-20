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
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
from pymobiledevice3.usbmux import list_devices as usbmux_list_devices

from src.tunnel_forwarder import TunnelForwarder
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

    # Bundle ID of the signed WebDriverAgentRunner app on the iPhone.
    # Read from ~/.config/iphone-mirror/config.json OR the WDA_BUNDLE_ID
    # env var. The env var wins for Terminal launches; the config file
    # is what Finder-launched .app bundles use (they don't inherit the
    # shell environment). See README.md → "Build & install WebDriverAgent".
    from src.user_config import get_wda_bundle_id as _get_wda_bundle_id
    WDA_BUNDLE_ID = _get_wda_bundle_id()
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
        # Optional callback invoked BEFORE stop_wda kills xcuitest — gives
        # the InputHandler a chance to POST /wda/shutdown so the
        # WebDriverAgentRunner app on the iPhone terminates itself
        # cleanly. Runs synchronously; must be fast (few hundred ms).
        self._on_pre_stop_wda: Optional[callable] = None
        # HTTP-to-userspace-tunnel bridge for WDA (port 8100). None until
        # a tunnel is up. The stdlib ``requests`` client used by input_handler
        # cannot reach the RSD's IPv6 directly (OS sockets do not see the
        # in-process TCP stack), so we bind localhost and splice.
        self._wda_forwarder: Optional[TunnelForwarder] = None

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
        """URL that stdlib HTTP clients (``requests``) can actually reach.

        Behind the scenes this is a localhost socket that the forwarder
        splices into the userspace tunnel's port 8100. The forwarder is
        started as part of :meth:`_connect`; if it hasn't come up yet we
        fall back to a placeholder that will fail cleanly rather than
        hand out a URL nothing can dial.
        """
        if self._wda_forwarder is not None and self._wda_forwarder.local_port:
            return f"http://127.0.0.1:{self._wda_forwarder.local_port}"
        return "http://127.0.0.1:0"

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
        # Consecutive failure counter so a persistently broken discovery
        # path (e.g. usbmux socket gone) becomes visible in the log
        # instead of ticking away silently on DEBUG.
        fail_streak = 0
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
                        fail_streak = 0
                    except Exception as e:
                        fail_streak += 1
                        # First failure logs DEBUG (transient hiccup expected
                        # during boot). After 3 in a row, escalate so a
                        # broken discovery path stops hiding.
                        if fail_streak == 3:
                            logger.warning(
                                "Discovery has failed %d times in a row: %s",
                                fail_streak, e, exc_info=e,
                            )
                        elif fail_streak > 3 and fail_streak % 15 == 0:
                            logger.warning(
                                "Discovery still failing (%d attempts): %s",
                                fail_streak, e,
                            )
                        else:
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
            await self._start_wda_forwarder(rsd)
            self._set_state(ConnectionState.CONNECTED)
            self.device_connected.emit(self._device_info)
            logger.info(
                "Connected to %s (%s, iOS %s)",
                self._device_info["name"],
                self._device_info["model"],
                self._device_info["ios_version"],
            )
        except Exception as e:
            # Some pymobiledevice3 errors render as empty strings via
            # __str__ — include the type name and stack so a persistent
            # connect failure (iOS 27 WiFi tunnel drop, dev image not
            # mounted, developer mode off) is actually diagnosable.
            logger.error(
                "Tunnel connect failed for %s: %s: %s",
                udid, type(e).__name__, e or "<no message>",
                exc_info=e,
            )
            self._set_state(ConnectionState.ERROR)
            self.connection_error.emit(
                f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            )

    async def _start_wda_forwarder(self, rsd) -> None:
        """(Re)start the localhost → tunnel forwarder that carries WDA HTTP.

        Idempotent: closes any prior forwarder first so we never leak a
        listener on a stale RSD after a reconnect.
        """
        await self._stop_wda_forwarder()
        fwd = TunnelForwarder(rsd, remote_port=8100, label="wda-8100")
        try:
            port = await fwd.start()
        except Exception as e:
            logger.error("Failed to start WDA forwarder: %s", e)
            raise
        self._wda_forwarder = fwd
        logger.info("WDA forwarder up on 127.0.0.1:%d -> tunnel:8100", port)

    async def _stop_wda_forwarder(self) -> None:
        if self._wda_forwarder is None:
            return
        try:
            await self._wda_forwarder.close()
        except Exception as e:
            logger.debug("WDA forwarder close raised: %s", e)
        self._wda_forwarder = None

    async def _safe_get_value(self, rsd, key: str, default: str = "") -> str:
        try:
            v = await rsd.get_value(key=key)
            return v or default
        except Exception as e:
            # A single miss on `DeviceName` etc. isn't fatal, but silently
            # returning "iPhone" for every device (which is what happens
            # if lockdown lost its handshake) hides the real problem. Log
            # so the wrong-device-name UI has a trail.
            logger.debug("get_value(%s) failed: %s", key, e)
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
            # MJPEG tuning: 30 fps looks great in practice. It briefly
            # stalls when WiFi bandwidth peaks, but our reconnect
            # supervisor in screen_capture.py picks it up within ~2s —
            # net UX is smooth. Keep it high.
            "--env", "MJPEG_SERVER_SCREENSHOT_QUALITY=55",
            "--env", "MJPEG_SCALING_FACTOR=50",
            "--env", "MJPEG_SERVER_FRAMERATE=30",
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
                # xcuitest exited before we could rely on stop_wda for
                # cleanup; unlink the log ourselves.
                try:
                    os.unlink(self._wda_log_path)
                except Exception:
                    pass
                self._wda_log_path = None
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
            try:
                os.unlink(self._wda_log_path)
            except Exception:
                pass
            self._wda_log_path = None
            logger.error("Failed to start WDA: %s", e)
            return False

    def stop_wda(self) -> None:
        if not self._wda_proc:
            return
        # Ask the runner to shut itself down FIRST (while the tunnel is
        # still open). If this succeeds iOS terminates the WDA app on
        # the phone within a second; if it fails (tunnel already gone)
        # we fall through to the xcuitest kill and let iOS reap the
        # runner via its heartbeat loss.
        if self._on_pre_stop_wda is not None:
            try:
                self._on_pre_stop_wda()
            except Exception as e:
                logger.debug("pre_stop_wda callback raised: %s", e)
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
            # 2s is plenty for xcuitest to close cleanly on SIGTERM;
            # beyond that we go straight to SIGKILL so shutdown stays
            # snappy.
            proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._wda_proc = None
        # Unlink the tempfile we created in start_wda so /tmp doesn't
        # collect a wda-*.log for every restart. Only best-effort; if the
        # file's gone (test env, someone rotated it) we don't care.
        log_path = getattr(self, "_wda_log_path", None)
        if log_path:
            try:
                os.unlink(log_path)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug("WDA log unlink failed: %s", e)
            self._wda_log_path = None
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
            # Old forwarder holds a stale RSD; tear it down so new HTTP
            # calls fast-fail against the empty local port until the
            # reconnect handler builds a fresh one.
            t = asyncio.ensure_future(self._stop_wda_forwarder())
            t.add_done_callback(_log_task_exception("stop wda forwarder on tunnel loss"))
            if self._state == ConnectionState.CONNECTED:
                self._set_state(ConnectionState.CONNECTING)
        elif status == STATUS_RECONNECTED:
            logger.info("Tunnel reconnected: %s", reason)
            rsd = self._tunnel_mgr.rsd
            if rsd is not None:
                t = asyncio.ensure_future(self._start_wda_forwarder(rsd))
                t.add_done_callback(_log_task_exception("restart wda forwarder on reconnect"))
            self._set_state(ConnectionState.CONNECTED)
            self.device_connected.emit(self._device_info)
        elif status == STATUS_FAILED:
            logger.error("Tunnel gave up reconnecting: %s", reason)
            self._set_state(ConnectionState.ERROR)
            self.connection_error.emit(f"Tunnel dropped and could not recover: {reason}")

    async def _terminate_wda_on_device(self) -> bool:
        """Kill the WebDriverAgentRunner app on the iPhone via DVT."""
        rsd = self._tunnel_mgr.rsd
        if rsd is None:
            logger.info("Terminate-WDA-on-device: no RSD, skipping")
            return False
        logger.info("Terminate-WDA-on-device: connecting DVT…")
        try:
            return await asyncio.wait_for(self._do_terminate_wda(rsd), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Terminate-WDA-on-device: overall timeout after 5s")
            return False
        except Exception as e:
            logger.warning("Terminate-WDA-on-device raised %s: %s", type(e).__name__, e)
            return False

    async def _do_terminate_wda(self, rsd) -> bool:
        async with DvtProvider(rsd) as dvt:
            logger.info("Terminate-WDA-on-device: DVT open, connecting process control…")
            pc = ProcessControl(dvt)
            await pc.connect()
            logger.info("Terminate-WDA-on-device: looking up pid for %s", self.WDA_BUNDLE_ID)
            pid = await pc.process_identifier_for_bundle_identifier(self.WDA_BUNDLE_ID)
            if not pid:
                logger.info("Terminate-WDA-on-device: no pid found")
                return False
            logger.info("Terminate-WDA-on-device: killing pid %d", pid)
            await pc.kill(pid)
            logger.info("Terminate-WDA-on-device: kill sent")
            return True

    # ────────────────────────────── shutdown ─────────────────────────────────

    async def disconnect(self) -> None:
        # 1. Ask iOS to terminate the WebDriverAgentRunner app FIRST
        #    (while the RSD tunnel is still up). If we skipped this and
        #    just killed the local xcuitest, the on-device runner would
        #    linger until iOS's XCTest heartbeat timeout — which is
        #    exactly the "WDA blijft draaien" symptom.
        await self._terminate_wda_on_device()
        # 2. Kill the local xcuitest subprocess (also does its own
        #    pre-stop callback that hits POST /wda/shutdown as a
        #    backup; a no-op on WDA builds that don't route it).
        self.stop_wda()
        await self._stop_wda_forwarder()
        await self._tunnel_mgr.disconnect()
        self._current_udid = None
        self._device_info = {}
        self._set_state(ConnectionState.DISCONNECTED)
        self.device_disconnected.emit()

    async def cleanup(self) -> None:
        self.stop_discovery()
        await self.disconnect()

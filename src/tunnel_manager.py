"""Userspace tunnel manager with health monitoring and auto-reconnect.

Wraps :class:`pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel` so
the app can start a per-device developer tunnel from Python without sudo,
without a LaunchDaemon, and without a system tunneld service.

Adds two features on top of the raw v9 primitive:

1. **Health monitoring** — a background task pings the RSD every few seconds
   with a cheap lockdown query. If it fails or times out, the tunnel is
   considered lost.

2. **Auto-reconnect** — on a detected loss, the manager tears the tunnel down
   and attempts to re-establish it with exponential backoff. All while
   respecting PyTCP's "one userspace tunnel per process" constraint (the
   previous tunnel is fully closed before a new one is opened).

Public interface:
    * :class:`TunnelManager` — long-lived owner of at most one active
      ``UserspaceRsdTunnel`` per session. Async: callers await from qasync.
    * ``on_status_change`` callback fires with ``(status, reason)`` for
      status transitions (``connected``, ``lost``, ``reconnecting``,
      ``reconnected``, ``failed``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

logger = logging.getLogger(__name__)

# Callback: (status, reason) -> None or awaitable
StatusCallback = Callable[[str, str], Optional[Awaitable[None]]]

STATUS_CONNECTED = "connected"
STATUS_LOST = "lost"
STATUS_RECONNECTING = "reconnecting"
STATUS_RECONNECTED = "reconnected"
STATUS_FAILED = "failed"

HEALTH_INTERVAL_S = 6.0
HEALTH_TIMEOUT_S = 4.0
MAX_RECONNECT_ATTEMPTS = 6


class TunnelManager:
    def __init__(self) -> None:
        self._tunnel: Optional[UserspaceRsdTunnel] = None
        self._rsd: Optional[RemoteServiceDiscoveryService] = None
        self._udid: Optional[str] = None
        self._lock = asyncio.Lock()
        self._health_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._on_status_change: Optional[StatusCallback] = None
        self._closing = False

    # ─────────────────────────── properties ──────────────────────────────────

    @property
    def rsd(self) -> Optional[RemoteServiceDiscoveryService]:
        return self._rsd

    @property
    def udid(self) -> Optional[str]:
        return self._udid

    @property
    def is_connected(self) -> bool:
        return self._rsd is not None

    def on_status_change(self, cb: Optional[StatusCallback]) -> None:
        self._on_status_change = cb

    # ─────────────────────────── connect ─────────────────────────────────────

    async def connect(self, udid: str) -> RemoteServiceDiscoveryService:
        """Open a userspace tunnel to *udid* and start health monitoring.

        Idempotent for the same UDID. Switches devices if a different UDID
        was previously connected.
        """
        async with self._lock:
            if self._rsd is not None and self._udid == udid:
                logger.debug("Tunnel already active for %s", udid)
                return self._rsd
            if self._rsd is not None:
                logger.info("Switching tunnel: %s -> %s", self._udid, udid)
                await self._close_locked()

            await self._open_locked(udid)
            self._start_health_monitor()
            await self._emit(STATUS_CONNECTED, f"opened tunnel to {udid}")
            return self._rsd

    async def disconnect(self) -> None:
        self._closing = True
        # Cancel any in-flight reconnect
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        async with self._lock:
            await self._close_locked()
        self._closing = False

    # ─────────────────────────── internal ────────────────────────────────────

    async def _open_locked(self, udid: str) -> None:
        logger.info("Opening userspace tunnel to %s", udid)
        tunnel = UserspaceRsdTunnel(serial=udid)
        rsd = await tunnel.aopen()
        self._tunnel = tunnel
        self._rsd = rsd
        self._udid = udid
        logger.info(
            "Tunnel up: %s (%s, iOS %s)",
            rsd.udid, rsd.product_type, rsd.product_version,
        )

    async def _close_locked(self) -> None:
        # Stop health task first so it doesn't fire during teardown
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except (asyncio.CancelledError, Exception):
                pass
            self._health_task = None

        if self._tunnel is None:
            self._rsd = None
            self._udid = None
            return
        logger.info("Closing tunnel to %s", self._udid)
        try:
            await self._tunnel.aclose()
        except Exception as e:
            logger.warning("Tunnel close raised: %s", e)
        finally:
            self._tunnel = None
            self._rsd = None
            self._udid = None

    # ─────────────────────────── health monitoring ───────────────────────────

    def _start_health_monitor(self) -> None:
        if self._health_task and not self._health_task.done():
            return
        self._health_task = asyncio.ensure_future(self._health_loop())

    async def _health_loop(self) -> None:
        logger.debug("Health monitor started")
        try:
            while self._rsd is not None:
                await asyncio.sleep(HEALTH_INTERVAL_S)
                if self._rsd is None or self._closing:
                    return
                healthy = await self._probe_health()
                if not healthy:
                    logger.warning("Tunnel health check failed for %s", self._udid)
                    lost_udid = self._udid
                    # Trigger reconnect but do not block the health loop task
                    self._reconnect_task = asyncio.ensure_future(
                        self._reconnect(lost_udid, reason="health check failed")
                    )
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Health monitor crashed: %s", e)
        finally:
            logger.debug("Health monitor exited")

    async def _probe_health(self) -> bool:
        """Cheap tunnel-liveness probe.

        In v9 the RSD accessors (``get_date``, ``get_value``, …) are
        coroutines, so await directly — wrapping in ``run_in_executor``
        just schedules the coroutine object without ever awaiting it,
        making every probe pass without actually talking to the device.
        """
        rsd = self._rsd
        if rsd is None:
            return False
        try:
            await asyncio.wait_for(rsd.get_date(), timeout=HEALTH_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            logger.debug("Health probe timed out")
            return False
        except Exception as e:
            logger.debug("Health probe raised: %s", e)
            return False

    # ─────────────────────────── auto-reconnect ──────────────────────────────

    async def _reconnect(self, udid: str, reason: str) -> None:
        if self._closing:
            return
        await self._emit(STATUS_LOST, reason)
        async with self._lock:
            await self._close_locked()

        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            if self._closing:
                return
            delay = min(30, 2 ** (attempt - 1))
            await self._emit(
                STATUS_RECONNECTING,
                f"attempt {attempt}/{MAX_RECONNECT_ATTEMPTS}, wait {delay}s",
            )
            await asyncio.sleep(delay)
            if self._closing:
                return

            try:
                async with self._lock:
                    await self._open_locked(udid)
                    self._start_health_monitor()
                await self._emit(
                    STATUS_RECONNECTED,
                    f"attempt {attempt} succeeded",
                )
                return
            except Exception as e:
                logger.warning(
                    "Reconnect attempt %d/%d failed: %s",
                    attempt, MAX_RECONNECT_ATTEMPTS, e,
                )

        await self._emit(
            STATUS_FAILED,
            f"gave up after {MAX_RECONNECT_ATTEMPTS} attempts",
        )

    # ─────────────────────────── event emission ──────────────────────────────

    async def _emit(self, status: str, reason: str) -> None:
        logger.info("TunnelManager: %s (%s)", status, reason)
        cb = self._on_status_change
        if cb is None:
            return
        try:
            result = cb(status, reason)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning("Status callback raised: %s", e)

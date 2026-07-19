"""Userspace tunnel manager.

Wraps :class:`pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel` so the
app can start a per-device developer tunnel from Python without sudo, without
a LaunchDaemon, and without a system tunneld service.

Public interface:
    * :class:`TunnelManager` — long-lived owner of at most one active
      ``UserspaceRsdTunnel`` per session. Starts, stops, and exposes the RSD
      handle other modules need (DVT, WDA URL discovery).
    * The manager is async; callers await ``connect``/``disconnect`` from the
      qasync event loop.

Everything the old ``tunnel_manager`` did (plist install/remove, sudoers
handling, ``launchctl load/unload``) is gone in v9 — the in-process userspace
network stack replaces it entirely.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

logger = logging.getLogger(__name__)


class TunnelManager:
    def __init__(self) -> None:
        self._tunnel: Optional[UserspaceRsdTunnel] = None
        self._rsd: Optional[RemoteServiceDiscoveryService] = None
        self._udid: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def rsd(self) -> Optional[RemoteServiceDiscoveryService]:
        return self._rsd

    @property
    def udid(self) -> Optional[str]:
        return self._udid

    @property
    def is_connected(self) -> bool:
        return self._rsd is not None

    async def connect(self, udid: str) -> RemoteServiceDiscoveryService:
        """Open a userspace tunnel to *udid* and return the RSD handle.

        Idempotent: if the same UDID is already connected, the existing RSD
        is returned. If a different UDID is active it is closed first.
        """
        async with self._lock:
            if self._rsd is not None and self._udid == udid:
                logger.debug("Tunnel already active for %s", udid)
                return self._rsd

            if self._rsd is not None:
                logger.info("Switching tunnel: %s -> %s", self._udid, udid)
                await self._close_locked()

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
            return rsd

    async def disconnect(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._tunnel is None:
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

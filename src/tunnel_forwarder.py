"""Localhost → userspace-tunnel TCP forwarder.

v9's :class:`pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel` runs
a pure-Python TCP/IP stack inside the current process. Its IPv6 endpoint is
reachable only via the RSD's own dialer — a plain ``asyncio.open_connection``
or the ``requests`` library goes through the OS socket layer, which has no
route to the userspace stack and returns ``[Errno 65] No route to host``.

This forwarder bridges the two worlds: it listens on ``127.0.0.1:<dynamic>``
using stdlib sockets and, for every incoming connection, opens a matching
:class:`ServiceConnection` on the RSD and splices bytes in both directions.
Now any stdlib HTTP client (``requests``, ``urllib``, ``curl``) can hit the
localhost URL and reach the device on WDA's port 8100 through the tunnel.

Public surface:
    * :class:`TunnelForwarder` — one instance per (rsd, remote_port). Async.
      ``start()`` returns the local port; ``close()`` shuts down cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 32 KiB chunks keep the memory footprint low and match typical MTU boundaries.
_SPLICE_CHUNK = 32 * 1024


class TunnelForwarder:
    """Forward ``127.0.0.1:<local>`` TCP to ``<userspace_tunnel>:<remote>``."""

    def __init__(self, rsd, remote_port: int, *, label: str = "forward") -> None:
        self._rsd = rsd
        self._remote_port = remote_port
        self._label = label
        self._server: Optional[asyncio.base_events.Server] = None
        self._local_port: Optional[int] = None
        # Track in-flight per-client tasks so shutdown can cancel them.
        self._active: set[asyncio.Task] = set()

    @property
    def local_port(self) -> Optional[int]:
        return self._local_port

    async def start(self) -> int:
        """Bind on a free localhost port and start serving. Returns the port."""
        if self._server is not None:
            return self._local_port  # already started
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
        )
        # asyncio can bind on multiple sockets (v4/v6); pick the first.
        sock = self._server.sockets[0]
        self._local_port = sock.getsockname()[1]
        logger.info(
            "TunnelForwarder[%s]: 127.0.0.1:%d -> RSD:%d",
            self._label, self._local_port, self._remote_port,
        )
        return self._local_port

    async def close(self) -> None:
        """Stop accepting new connections and drain in-flight splices."""
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception as e:
            logger.debug("TunnelForwarder[%s]: wait_closed raised: %s", self._label, e)
        self._server = None

        for task in list(self._active):
            if not task.done():
                task.cancel()
        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)
        self._active.clear()
        logger.info("TunnelForwarder[%s] stopped", self._label)

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        peer = client_writer.get_extra_info("peername")
        svc = None
        try:
            svc = await self._rsd.create_service_connection(self._remote_port)
            await svc.start()
        except Exception as e:
            logger.warning(
                "TunnelForwarder[%s]: RSD connect for %s failed: %s",
                self._label, peer, e,
            )
            try:
                client_writer.close()
            except Exception:
                pass
            if task is not None:
                self._active.discard(task)
            return

        remote_reader = svc.reader
        remote_writer = svc.writer

        async def splice(src: asyncio.StreamReader, dst: asyncio.StreamWriter, direction: str) -> None:
            try:
                while True:
                    data = await src.read(_SPLICE_CHUNK)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.IncompleteReadError, OSError) as e:
                logger.debug(
                    "TunnelForwarder[%s] %s splice ended: %s",
                    self._label, direction, e,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(
                    "TunnelForwarder[%s] %s splice raised %s: %s",
                    self._label, direction, type(e).__name__, e,
                )
            finally:
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except Exception:
                    pass

        try:
            await asyncio.gather(
                splice(client_reader, remote_writer, "c->r"),
                splice(remote_reader, client_writer, "r->c"),
                return_exceptions=False,
            )
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await svc.aclose()
            except Exception:
                pass
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            if task is not None:
                self._active.discard(task)

"""In-process async MJPEG capture over the v9 userspace tunnel.

Design notes:

v9's :class:`pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel`
runs a pure-Python TCP/IP stack **inside the current process**. Its RSD
IPv6 address is reachable **only from that same process** — a subprocess
gets ``[Errno 65] No route to host`` when it tries to connect. That
invalidates the v7 pattern of "spawn a subprocess to keep DVT calls off
the Qt GIL": the subprocess can't reach the tunnel at all.

So we do the reads on the main process, on the qasync event loop, using
async sockets. That works because:

* MJPEG is I/O-bound. `asyncio.open_connection` / `StreamReader.read` do
  not hold the GIL while waiting on the socket, so the Qt UI paints
  freely between frames.
* JPEG decoding happens in Qt's C++ side via ``QImage.loadFromData``,
  which releases the GIL for the decode step.

This module exposes a Qt-friendly object that emits :attr:`frame_ready`
and :attr:`fps_updated` signals from an asyncio task on the qasync loop.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)

BOUNDARY = b"--BoundaryString"
READ_BUFFER = 65536
MAX_FRAME_BYTES = 50_000_000
CONNECT_RETRY_DELAY_S = 1.0
CONNECT_MAX_ATTEMPTS = 30


class ScreenCaptureThread(QObject):
    """Compatibility name — used to be a QThread, now an asyncio task owner.

    The public surface (`frame_ready`, `fps_updated`, `capture_error`,
    `start`, `stop`, `pause`, `resume`) is preserved so main_window.py
    doesn't need changes.
    """

    frame_ready = pyqtSignal(QImage)
    fps_updated = pyqtSignal(float)
    capture_error = pyqtSignal(str)

    MJPEG_PORT = 9100

    def __init__(self, device_manager, target_fps: int = 30, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._device_manager = device_manager
        self._target_fps = target_fps
        self._paused = False
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ─────────────────────────── public controls ─────────────────────────────

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def start(self) -> None:
        """Kick off the async capture task on the running qasync loop."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.ensure_future(self._run())
        self._task.add_done_callback(self._on_task_done)

    # QThread parity — old code called .isRunning() / .stop() / .wait()
    def isRunning(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def stop(self) -> None:
        self._running = False
        task = self._task
        if task is None or task.done():
            return
        task.cancel()

    def wait(self, ms: int = 5000) -> None:
        """QThread parity no-op: callers already ran ``stop()``.

        The event-loop-based teardown drains via cancellation; there's
        nothing meaningful to block on from a Qt slot without stalling
        the loop that would run the cleanup.
        """
        return None

    # ─────────────────────────── internal ────────────────────────────────────

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            logger.info("Screen capture cancelled")
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Screen capture task crashed: %s", exc, exc_info=exc)
            self.capture_error.emit(str(exc))

    def _extract_host(self) -> Optional[str]:
        url = self._device_manager.get_wda_url()
        m = re.match(r"http://\[([^\]]+)\]:", url)
        if m:
            return m.group(1)
        m = re.match(r"http://([^:]+):", url)
        if m:
            return m.group(1)
        return None

    async def _run(self) -> None:
        logger.info("Screen capture started (in-process, async)")
        host = self._extract_host()
        if not host:
            logger.error("No WDA host available — cannot start MJPEG stream")
            self.capture_error.emit("Geen tunnel adres — kan MJPEG niet starten")
            return

        # Keep retrying the connect until WDA is up — its MJPEG server on
        # port 9100 only starts listening once the xcuitest runner has
        # bootstrapped inside iOS, which takes 5-15s after we spawned it.
        reader = writer = None
        for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
            if not self._running:
                return
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, self.MJPEG_PORT),
                    timeout=5.0,
                )
                logger.info("MJPEG connected on attempt %d (%s:%d)", attempt, host, self.MJPEG_PORT)
                break
            except (OSError, asyncio.TimeoutError) as e:
                logger.debug("MJPEG connect attempt %d/%d failed: %s", attempt, CONNECT_MAX_ATTEMPTS, e)
                await asyncio.sleep(CONNECT_RETRY_DELAY_S)
        else:
            logger.error("MJPEG connect gave up after %d attempts", CONNECT_MAX_ATTEMPTS)
            self.capture_error.emit("MJPEG server niet bereikbaar — WDA niet gestart?")
            return

        try:
            await self._stream(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("MJPEG stream error: %s", e, exc_info=e)
            self.capture_error.emit(f"MJPEG fout: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Kick the WDA server so it starts sending frames.
        writer.write(b"GET / HTTP/1.0\r\n\r\n")
        await writer.drain()

        # Consume the HTTP response headers and land at the first --Boundary.
        await reader.readuntil(b"\r\n\r\n")

        frame_count = 0
        fps_timer = time.time()
        smoothed_fps = 0.0
        alpha = 0.35

        while self._running:
            if self._paused:
                await asyncio.sleep(0.1)
                continue

            # Advance to next multipart boundary.
            try:
                await reader.readuntil(BOUNDARY)
            except asyncio.IncompleteReadError:
                logger.warning("MJPEG stream ended (server closed)")
                return

            # Optional trailing \r\n after the boundary
            peek = await reader.readexactly(2)
            if peek != b"\r\n":
                # not the expected separator; feed back? readuntil already
                # consumed BOUNDARY so we can't put bytes back, but part
                # headers always start with a name — fall through and let
                # the header parse below re-sync on \r\n\r\n.
                # Prepend what we grabbed by reading into a buffer.
                header_start = peek
            else:
                header_start = b""

            headers_bytes = header_start + await reader.readuntil(b"\r\n\r\n")

            # Parse Content-Length
            content_length = None
            for line in headers_bytes.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        content_length = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        pass
                    break
            if content_length is None or content_length <= 0 or content_length > MAX_FRAME_BYTES:
                logger.warning("Missing/invalid Content-Length in MJPEG part; resyncing")
                continue

            jpeg = await reader.readexactly(content_length)

            qimage = QImage()
            if not qimage.loadFromData(jpeg):
                continue
            self.frame_ready.emit(qimage)

            frame_count += 1
            elapsed = time.time() - fps_timer
            if elapsed >= 1.0:
                instant = frame_count / elapsed
                smoothed_fps = instant if smoothed_fps == 0 else (
                    alpha * instant + (1 - alpha) * smoothed_fps
                )
                self.fps_updated.emit(smoothed_fps)
                frame_count = 0
                fps_timer = time.time()

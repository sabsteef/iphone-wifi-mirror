"""In-process async MJPEG capture over the v9 userspace tunnel.

Design notes:

v9's :class:`pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel`
runs a pure-Python TCP/IP stack **inside the current process**. Its RSD
IPv6 address is reachable **only from that same process** — and only via
the tunnel's own dialer. A plain ``asyncio.open_connection(host, port)``
goes through the OS socket layer, which has no route to the userspace
stack and returns ``[Errno 65] No route to host``.

To reach the MJPEG server on port 9100 we ask the RSD to open the socket
for us: :meth:`RemoteServiceDiscoveryService.create_service_connection`
routes through the userspace dialer and hands back a
:class:`ServiceConnection` whose :attr:`reader` / :attr:`writer` are
plain ``asyncio`` stream objects — usable with ``readuntil`` /
``readexactly`` for MJPEG boundary parsing.

MJPEG is I/O-bound. Reader/writer do not hold the GIL while waiting on
the socket, so the Qt UI paints freely between frames; JPEG decoding
happens in Qt's C++ side via ``QImage.loadFromData``, which also
releases the GIL for the decode step.

This module exposes a Qt-friendly object that emits :attr:`frame_ready`
and :attr:`fps_updated` signals from an asyncio task on the qasync loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)

BOUNDARY = b"--BoundaryString"
READ_BUFFER = 65536
MAX_FRAME_BYTES = 50_000_000
CONNECT_RETRY_DELAY_S = 1.0
CONNECT_MAX_ATTEMPTS = 90
FIRST_DATA_TIMEOUT_S = 8.0
# Longest we'll wait for the next chunk of an already-flowing MJPEG stream
# before treating the socket as stalled. Well above the 12 FPS server
# tick but small enough to recover quickly when iOS pauses (e.g. lock
# screen, WiFi hiccup).
STREAM_READ_TIMEOUT_S = 6.0


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

    async def _run(self) -> None:
        """Outer supervisor: (re)connect, stream, and re-open on drop.

        Streams die naturally when the WDA testrunner restarts (which
        happens whenever we (re)spawn the xcuitest subprocess). Rather
        than surface that as a fatal capture_error, retry a fresh
        connect right after — the WDA HTTP + MJPEG servers come back a
        few seconds after the runner's next testCaseDidStart.
        """
        logger.info("Screen capture started (in-process, async)")
        while self._running:
            rsd = self._device_manager.rsd
            if rsd is None:
                logger.info("Waiting for RSD tunnel before MJPEG connect")
                await asyncio.sleep(1.0)
                continue

            conn = await self._connect_mjpeg(rsd)
            if conn is None:
                # Gave up after CONNECT_MAX_ATTEMPTS — surface a real error
                last = getattr(self, "_last_connect_error", None)
                detail = f" (last: {last})" if last else ""
                self.capture_error.emit(
                    f"MJPEG server niet bereikbaar — WDA niet gestart?{detail}"
                )
                return

            try:
                await self._stream(conn.reader, conn.writer)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("MJPEG stream error: %s — reconnecting", e)
            finally:
                try:
                    await conn.aclose()
                except Exception:
                    pass

            if not self._running:
                return
            # Small backoff so the WDA testrunner respawn (if any) has
            # a moment to bring HTTP + MJPEG back up.
            logger.info("MJPEG stream dropped; reconnecting in %.1fs", CONNECT_RETRY_DELAY_S)
            await asyncio.sleep(CONNECT_RETRY_DELAY_S)

    async def _connect_mjpeg(self, rsd):
        """Loop until we get an MJPEG socket that actually delivers data.

        The userspace tunnel will happily hand back a "connected" socket
        even when nothing on the device is bound to the port yet — the
        actual failure only shows up as the read never producing bytes.
        So each attempt sends the HTTP GET and waits for a real header
        response; a timeout means "not ready yet, try again."
        """
        self._last_connect_error: Optional[str] = None
        for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
            if not self._running:
                return None
            candidate = None
            try:
                candidate = await asyncio.wait_for(
                    rsd.create_service_connection(self.MJPEG_PORT),
                    timeout=5.0,
                )
                await candidate.start()  # populate .reader / .writer
                candidate.writer.write(b"GET / HTTP/1.0\r\n\r\n")
                await candidate.writer.drain()
                headers = await asyncio.wait_for(
                    candidate.reader.readuntil(b"\r\n\r\n"),
                    timeout=FIRST_DATA_TIMEOUT_S,
                )
                logger.info(
                    "MJPEG connected on attempt %d (HTTP headers %d bytes)",
                    attempt, len(headers),
                )
                return candidate
            except asyncio.TimeoutError:
                self._last_connect_error = f"timeout after {FIRST_DATA_TIMEOUT_S:.0f}s (WDA MJPEG not accepting or not streaming)"
                logger.debug(
                    "MJPEG attempt %d/%d: %s",
                    attempt, CONNECT_MAX_ATTEMPTS, self._last_connect_error,
                )
            except (OSError, ConnectionError, asyncio.IncompleteReadError) as e:
                self._last_connect_error = f"{type(e).__name__}: {e}"
                logger.debug(
                    "MJPEG attempt %d/%d failed: %s",
                    attempt, CONNECT_MAX_ATTEMPTS, self._last_connect_error,
                )
            except Exception as e:
                self._last_connect_error = f"{type(e).__name__}: {e}"
                logger.debug(
                    "MJPEG attempt %d/%d raised %s",
                    attempt, CONNECT_MAX_ATTEMPTS, self._last_connect_error,
                )
            if candidate is not None:
                try:
                    await candidate.aclose()
                except Exception:
                    pass
            await asyncio.sleep(CONNECT_RETRY_DELAY_S)
        logger.error(
            "MJPEG connect gave up after %d attempts; last error: %s",
            CONNECT_MAX_ATTEMPTS, self._last_connect_error or "<none captured>",
        )
        return None

    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # HTTP GET + response headers were consumed by the connect loop.
        frame_count = 0
        fps_timer = time.time()
        smoothed_fps = 0.0
        alpha = 0.35
        total_frames_since_start = 0

        while self._running:
            if self._paused:
                await asyncio.sleep(0.1)
                continue

            # Advance to next multipart boundary. Bound every read so a
            # server that goes silent (WDA hang, iOS locked, WiFi drop)
            # doesn't leave the capture task blocked forever with no
            # visible signal — TimeoutError propagates out of _stream and
            # the supervisor reconnects.
            try:
                await asyncio.wait_for(reader.readuntil(BOUNDARY), timeout=STREAM_READ_TIMEOUT_S)
            except asyncio.IncompleteReadError as e:
                logger.warning(
                    "MJPEG stream ended (server closed) after %d frames; partial=%d bytes",
                    total_frames_since_start, len(e.partial) if e.partial else 0,
                )
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "MJPEG stream stalled (no data in %.1fs) after %d frames",
                    STREAM_READ_TIMEOUT_S, total_frames_since_start,
                )
                return

            # Optional trailing \r\n after the boundary
            peek = await asyncio.wait_for(reader.readexactly(2), timeout=STREAM_READ_TIMEOUT_S)
            if peek != b"\r\n":
                # not the expected separator; feed back? readuntil already
                # consumed BOUNDARY so we can't put bytes back, but part
                # headers always start with a name — fall through and let
                # the header parse below re-sync on \r\n\r\n.
                # Prepend what we grabbed by reading into a buffer.
                header_start = peek
            else:
                header_start = b""

            headers_bytes = header_start + await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=STREAM_READ_TIMEOUT_S,
            )

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

            jpeg = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=STREAM_READ_TIMEOUT_S,
            )

            qimage = QImage()
            if not qimage.loadFromData(jpeg):
                continue
            self.frame_ready.emit(qimage)
            if total_frames_since_start == 0:
                logger.info(
                    "MJPEG first frame decoded (%dx%d, %d bytes)",
                    qimage.width(), qimage.height(), content_length,
                )
            total_frames_since_start += 1

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

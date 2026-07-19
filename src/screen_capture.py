"""Screen capture pipeline (v9-compatible).

Design: capture happens in a *subprocess*, not in a Qt thread.

Rationale (unchanged from v7): pymobiledevice3 DVT calls (and MJPEG socket
reads under load) hold the GIL for hundreds of ms at a time. In-process that
starves the Qt UI, leading to stuttering, dropped frames on click, and janky
scroll. A separate Python process solves this by design — the GIL contention
is gone and IPC over a length-prefixed stdout pipe is cheap.

Three modes, tried in order:
    1. **MJPEG subprocess** (preferred) — talks to WDA's MJPEG server on port
       9100 via the developer tunnel. ~10-15 FPS with tunable quality/scale.
       We just need the tunnel IPv6 host — the worker knows nothing about
       pymobiledevice3.
    2. **DVT subprocess** — DVT ``Screenshot`` service. ~2 FPS but works
       without WDA (fallback when MJPEG can't connect).
    3. **DVT inline** — final fallback, DVT called in-process. Only used when
       subprocess spawn itself fails.

The DVT worker is not yet ported to v9. The MJPEG worker doesn't need
pymobiledevice3 at all, so it works unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import struct
import subprocess
import sys
import time
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)


class ScreenCaptureThread(QThread):
    frame_ready = pyqtSignal(QImage)
    fps_updated = pyqtSignal(float)
    capture_error = pyqtSignal(str)

    MJPEG_PORT = 9100

    def __init__(self, device_manager, target_fps: int = 30):
        super().__init__()
        self._device_manager = device_manager
        self._running = False
        self._target_fps = target_fps
        self._paused = False
        self._worker: Optional[subprocess.Popen] = None
        self._mode = "mjpeg"

    # ─────────────────────────── public controls ─────────────────────────────

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._running = False
        self._stop_worker()
        self.wait(5000)

    # ─────────────────────────── QThread entry ───────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Screen capture started")

        udid = self._device_manager._current_udid
        host = self._extract_host()
        if host and self._run_mjpeg(host, self.MJPEG_PORT):
            pass
        elif udid and self._run_dvt_subprocess(udid):
            pass
        else:
            self._run_dvt_inline()

        logger.info("Screen capture stopped")

    # ─────────────────────────── helpers ─────────────────────────────────────

    def _extract_host(self) -> Optional[str]:
        url = self._device_manager.get_wda_url()
        m = re.match(r"http://\[([^\]]+)\]:", url)
        if m:
            return m.group(1)
        m = re.match(r"http://([^:]+):", url)
        if m:
            return m.group(1)
        return None

    def _start_worker(self, module: str, *args: str) -> Optional[subprocess.Popen]:
        cmd = [sys.executable, "-m", module, *args]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
            ready_line = proc.stdout.readline()
            if ready_line.strip() == b"READY":
                logger.info("%s started (PID %d)", module, proc.pid)
                return proc

            stderr_out = b""
            try:
                proc.wait(timeout=3)
                stderr_out = proc.stderr.read()
            except Exception:
                pass
            logger.warning(
                "%s failed (handshake=%r):\n%s",
                module,
                ready_line,
                stderr_out.decode(errors="replace")[-2000:],
            )
            proc.kill()
            return None
        except Exception as e:
            logger.warning("Failed to start %s: %s", module, e)
            return None

    def _read_frame(self, proc: subprocess.Popen) -> Optional[bytes]:
        header = b""
        while len(header) < 4:
            chunk = proc.stdout.read(4 - len(header))
            if not chunk:
                return None
            header += chunk

        length = struct.unpack(">I", header)[0]
        if length > 50_000_000:
            return None

        data = b""
        while len(data) < length:
            chunk = proc.stdout.read(length - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _stream_from_worker(self) -> bool:
        frame_count = 0
        fps_timer = time.time()
        smoothed_fps = 0.0
        alpha = 0.35

        while self._running:
            if self._paused:
                time.sleep(0.1)
                continue
            if self._worker.poll() is not None:
                return False
            data = self._read_frame(self._worker)
            if data is None:
                return False

            qimage = QImage()
            if not qimage.loadFromData(data):
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

        return True

    # ─────────────────────────── modes ───────────────────────────────────────

    def _run_mjpeg(self, host: str, port: int) -> bool:
        for attempt in range(3):
            self._worker = self._start_worker(
                "src.mjpeg_capture_worker", host, str(port),
            )
            if self._worker:
                self._mode = "mjpeg"
                logger.info("Using MJPEG capture (host=%s port=%d)", host, port)
                clean = self._stream_from_worker()
                self._stop_worker()
                if not self._running or clean:
                    return True
                logger.warning("MJPEG worker died, restarting...")
                continue
            time.sleep(2)
        logger.warning("MJPEG capture unavailable")
        return False

    def _run_dvt_subprocess(self, udid: str) -> bool:
        self._worker = self._start_worker("src.capture_worker", udid)
        if not self._worker:
            return False
        self._mode = "dvt-subprocess"
        logger.info("Using DVT subprocess capture (fallback)")
        while self._running:
            clean = self._stream_from_worker()
            self._stop_worker()
            if not self._running or clean:
                return True
            logger.warning("DVT worker died, restarting")
            self._worker = self._start_worker("src.capture_worker", udid)
            if not self._worker:
                return False
        return True

    def _run_dvt_inline(self) -> None:
        self._mode = "dvt-inline"
        logger.info("Using DVT inline capture (final fallback — expect stutter)")
        logger.warning(
            "Inline DVT is not implemented in v9 yet — capture disabled."
        )
        self.capture_error.emit(
            "Screen capture unavailable: MJPEG failed and DVT inline not supported in v9."
        )
        self._running = False

    def _stop_worker(self) -> None:
        if not self._worker:
            return
        proc = self._worker
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._worker = None

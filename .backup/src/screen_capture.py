import logging
import re
import struct
import subprocess
import sys
import time

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)


class ScreenCaptureThread(QThread):
    frame_ready = pyqtSignal(QImage)
    fps_updated = pyqtSignal(float)
    capture_error = pyqtSignal(str)

    MJPEG_PORT = 9100
    WORKER_READY_TIMEOUT = 30

    def __init__(self, device_manager, target_fps: int = 30):
        super().__init__()
        self._device_manager = device_manager
        self._running = False
        self._target_fps = target_fps
        self._paused = False
        self._worker: subprocess.Popen | None = None
        self._mode = "mjpeg"

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def run(self):
        self._running = True
        logger.info("Screen capture started")

        udid = self._device_manager._current_udid
        host = self._extract_host()
        if host and self._run_mjpeg(host, self.MJPEG_PORT):
            pass
        elif udid and udid != "tunnel" and self._run_dvt_subprocess(udid):
            pass
        else:
            self._run_dvt_inline()

        logger.info("Screen capture stopped")

    def _extract_host(self) -> str | None:
        url = self._device_manager.get_wda_url()
        m = re.match(r"http://\[([^\]]+)\]:", url)
        if m:
            return m.group(1)
        m = re.match(r"http://([^:]+):", url)
        if m:
            return m.group(1)
        return None

    def _start_worker(self, module: str, *args) -> subprocess.Popen | None:
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

    def _read_frame(self, proc: subprocess.Popen) -> bytes | None:
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
                instant_fps = frame_count / elapsed
                if smoothed_fps == 0:
                    smoothed_fps = instant_fps
                else:
                    smoothed_fps = alpha * instant_fps + (1 - alpha) * smoothed_fps
                self.fps_updated.emit(smoothed_fps)
                frame_count = 0
                fps_timer = time.time()

        return True

    def _run_mjpeg(self, host: str, port: int) -> bool:
        for attempt in range(3):
            self._worker = self._start_worker(
                "src.mjpeg_capture_worker", host, str(port),
            )
            if self._worker:
                self._mode = "mjpeg"
                logger.info("Using MJPEG capture (host=%s port=%d)", host, port)
                clean_exit = self._stream_from_worker()
                self._stop_worker()
                if not self._running or clean_exit:
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

    def _run_dvt_inline(self):
        self._mode = "dvt-inline"
        logger.info("Using DVT inline capture (final fallback)")
        frame_count = 0
        fps_timer = time.time()
        consecutive_errors = 0

        while self._running:
            if self._paused:
                time.sleep(0.1)
                continue

            try:
                screenshot_bytes = self._device_manager.take_screenshot()
                qimage = QImage()
                if not qimage.loadFromData(screenshot_bytes):
                    continue
                self.frame_ready.emit(qimage)

                frame_count += 1
                elapsed = time.time() - fps_timer
                if elapsed >= 2.0:
                    self.fps_updated.emit(frame_count / elapsed)
                    frame_count = 0
                    fps_timer = time.time()

                consecutive_errors = 0

            except ConnectionError as e:
                logger.error("Connection error in capture: %s", e)
                self.capture_error.emit(str(e))
                self._running = False
                break

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    self.capture_error.emit(f"Too many errors: {e}")
                    self._running = False
                    break
                time.sleep(0.5)

    def _stop_worker(self):
        if not self._worker:
            return
        import os
        import signal
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

    def stop(self):
        self._running = False
        self._stop_worker()
        self.wait(5000)

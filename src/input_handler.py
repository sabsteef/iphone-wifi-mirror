import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

import requests
from PyQt6.QtCore import QObject, QPointF, QTimer, pyqtSignal

logger = logging.getLogger(__name__)

# Tap coordinate scaling. Historically a linear shrink was needed
# because stock WDA reported a stale screen size (390x844 on
# iPhone 17 Pro Max). Our patched WDA reports the real UIScreen size,
# so the default is now 1.0 (no compensation). Still overridable via
# config/env for unpatched WDA installs or fine-tuning per device.
from src.user_config import get_tap_x_scale, get_tap_y_scale
TAP_Y_SCALE = get_tap_y_scale()
TAP_X_SCALE = get_tap_x_scale()


class GestureState(Enum):
    IDLE = 0
    PRESSED = 1
    DRAGGING = 2
    LONG_PRESSING = 3


# Bounded pool for WDA HTTP calls. Every keystroke, tap, swipe, and pinch
# hits WDA over HTTP with a shared session lock, so unbounded threads pile
# up behind the lock on any slow round-trip. Two workers is enough because
# _post is serialized by self._lock anyway; the pool is really there to
# cap thread creation and let exceptions surface via Future.
_WDA_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wda")


def _fire_and_forget(fn):
    """Dispatch a blocking WDA HTTP call on the bounded pool.

    Logs exceptions instead of swallowing them so a broken tap doesn't
    disappear silently.
    """
    fut = _WDA_POOL.submit(fn)

    def _log_exc(f):
        exc = f.exception()
        if exc is not None:
            logger.warning("WDA background call failed: %s", exc)

    fut.add_done_callback(_log_exc)


class WDAClient:
    # After this many consecutive non-2xx responses that aren't session
    # errors, we treat WDA as unreachable and drop the session so the UI
    # banner reappears instead of hiding a broken client behind a green
    # status. Reset to zero on any 200.
    ERROR_STREAK_THRESHOLD = 5

    def __init__(self, base_url: str = "http://localhost:8100", auth_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.session_id = None
        self._session = requests.Session()
        if auth_token:
            self._session.headers["Authorization"] = f"Bearer {auth_token}"
        self._screen_scale = 3
        self._screen_size = None
        self._lock = threading.Lock()
        self._error_streak = 0

    def set_auth_token(self, token: str | None):
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        else:
            self._session.headers.pop("Authorization", None)

    @property
    def is_connected(self) -> bool:
        return self.session_id is not None

    def connect(self) -> bool:
        try:
            resp = self._session.get(f"{self.base_url}/status", timeout=2)
            if resp.status_code != 200:
                return False

            resp = self._session.post(
                f"{self.base_url}/session",
                json={"capabilities": {}},
                timeout=5,
            )
            data = resp.json()
            self.session_id = data.get("sessionId")

            if not self.session_id and "value" in data:
                self.session_id = data["value"].get("sessionId")

            if not self.session_id:
                logger.warning(
                    "WDA /session returned 200 but no sessionId — treating as failure. body=%s",
                    str(data)[:200],
                )
                return False

            self._fetch_screen_info()
            logger.info("WDA session created: %s", self.session_id)
            return True

        except requests.ConnectionError:
            logger.debug("WDA not reachable")
            return False
        except Exception as e:
            logger.error("WDA connection failed: %s", e)
            return False

    def _fetch_screen_info(self):
        try:
            resp = self._session.get(
                f"{self.base_url}/session/{self.session_id}/wda/screen",
                timeout=5,
            )
            data = resp.json()
            if "value" in data:
                self._screen_scale = data["value"].get("scale", 3)
                self._screen_size = {
                    "width": data["value"].get("width", 390),
                    "height": data["value"].get("height", 844),
                }
            logger.info("Screen scale: %s, size: %s", self._screen_scale, self._screen_size)
        except Exception as e:
            logger.debug("Screen info fetch failed: %s", e)

    @property
    def screen_scale(self) -> int:
        return self._screen_scale

    def disconnect(self):
        # Ask WDA on the iPhone to shut itself down. Without this, the
        # WebDriverAgentRunner test runner stays alive on-device as long
        # as iOS's XCTest heartbeat keeps ticking — which can be up to
        # a minute after our xcuitest subprocess exits. Sending
        # /wda/shutdown here makes iOS terminate the runner app
        # immediately.
        if self.session_id:
            try:
                self._session.post(
                    f"{self.base_url}/wda/shutdown",
                    timeout=2,
                )
            except Exception as e:
                logger.debug("WDA shutdown call failed (expected if tunnel down): %s", e)
            try:
                self._session.delete(
                    f"{self.base_url}/session/{self.session_id}",
                    timeout=2,
                )
            except Exception:
                pass
            self.session_id = None

    def _post(self, path: str, json_data: dict, timeout: float = 5):
        with self._lock:
            for attempt in range(2):
                if not self.session_id:
                    if not self._reconnect_locked():
                        return
                try:
                    resp = self._session.post(
                        f"{self.base_url}/session/{self.session_id}/{path}",
                        json=json_data,
                        timeout=timeout,
                    )
                    if resp.status_code == 200:
                        self._error_streak = 0
                        return
                    if resp.status_code == 404 or self._is_session_error(resp):
                        logger.warning("WDA session dead on %s, reconnecting", path)
                        self.session_id = None
                        continue
                    # Surface the response body so tail /tmp/mirror.log actually
                    # explains why the tap silently failed instead of just
                    # "-> HTTP 500".
                    body = resp.text[:200] if resp.text else "<empty>"
                    logger.error("WDA %s -> HTTP %d: %s", path, resp.status_code, body)
                    self._error_streak += 1
                    if self._error_streak >= self.ERROR_STREAK_THRESHOLD:
                        logger.error(
                            "WDA %d consecutive errors — dropping session so UI reflects failure",
                            self._error_streak,
                        )
                        self.session_id = None
                    return
                except requests.Timeout:
                    logger.warning("WDA timeout: %s", path)
                    return
                except requests.ConnectionError:
                    logger.error("WDA connection lost: %s", path)
                    self.session_id = None
                    return
                except Exception as e:
                    logger.error("WDA error (%s): %s", path, e)
                    return

    def _is_session_error(self, resp) -> bool:
        try:
            err = resp.json().get("value", {}).get("error", "")
            return "session" in err.lower()
        except Exception:
            return False

    def _reconnect_locked(self) -> bool:
        try:
            resp = self._session.post(
                f"{self.base_url}/session",
                json={"capabilities": {}},
                timeout=5,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            self.session_id = data.get("sessionId") or data.get("value", {}).get("sessionId")
            if self.session_id:
                logger.info("WDA session re-created: %s", self.session_id)
                return True
        except Exception as e:
            logger.warning("WDA reconnect failed: %s", e)
        return False

    def tap(self, x: float, y: float):
        _fire_and_forget(lambda: self._post("wda/tap", {"x": x, "y": y}))

    def long_press(self, x: float, y: float, duration: float = 1.0):
        _fire_and_forget(lambda: self._post(
            "wda/touchAndHold", {"x": x, "y": y, "duration": duration}, timeout=15,
        ))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.3):
        _fire_and_forget(lambda: self._post(
            "wda/dragfromtoforduration",
            {"fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "duration": duration},
            timeout=10,
        ))

    def stroke(self, points: list[tuple[float, float]], duration_ms: int = 300):
        """Draw / drag through an arbitrary sequence of points.

        Uses WDA's W3C actions endpoint with one ``pointerDown`` at the
        first point, a ``pointerMove`` per subsequent point, and a
        ``pointerUp`` at the end. Unlike a single swipe from start to
        end, this preserves the intermediate positions — so drawing
        apps (Notes, Freeform) see the actual pen path instead of a
        straight A→B line.

        The total motion duration is split evenly across segments; short
        strokes end up with per-segment durations of a few tens of ms,
        which iOS treats as continuous motion.
        """
        if len(points) < 2:
            return
        n_moves = len(points) - 1
        per_seg = max(5, duration_ms // n_moves)
        actions = [
            {"type": "pointerMove", "duration": 0, "x": points[0][0], "y": points[0][1]},
            {"type": "pointerDown", "button": 0},
        ]
        for px, py in points[1:]:
            actions.append({"type": "pointerMove", "duration": per_seg, "x": px, "y": py})
        actions.append({"type": "pointerUp", "button": 0})
        payload = {
            "actions": [
                {
                    "type": "pointer",
                    "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": actions,
                }
            ]
        }
        _fire_and_forget(lambda: self._post("actions", payload, timeout=15))

    def pinch(self, cx: float, cy: float, scale: float, duration_ms: int = 250):
        gap_start = 25.0
        gap_end = 25.0 * scale
        f1_start = (cx - gap_start, cy - gap_start)
        f1_end = (cx - gap_end, cy - gap_end)
        f2_start = (cx + gap_start, cy + gap_start)
        f2_end = (cx + gap_end, cy + gap_end)
        payload = {
            "actions": [
                {
                    "type": "pointer",
                    "id": "f1",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": f1_start[0], "y": f1_start[1]},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerMove", "duration": duration_ms, "x": f1_end[0], "y": f1_end[1]},
                        {"type": "pointerUp", "button": 0},
                    ],
                },
                {
                    "type": "pointer",
                    "id": "f2",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": f2_start[0], "y": f2_start[1]},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerMove", "duration": duration_ms, "x": f2_end[0], "y": f2_end[1]},
                        {"type": "pointerUp", "button": 0},
                    ],
                },
            ]
        }
        _fire_and_forget(lambda: self._post("actions", payload, timeout=10))

    def press_button(self, name: str):
        _fire_and_forget(lambda: self._post("wda/pressButton", {"name": name}))

    def home_screen(self):
        _fire_and_forget(lambda: self._post("wda/pressButton", {"name": "home"}))

    def lock(self):
        _fire_and_forget(lambda: self._post("wda/lock", {}))

    def unlock(self):
        _fire_and_forget(lambda: self._post("wda/unlock", {}))

    def unlock_with_passcode(self, passcode: str):
        def _do():
            # None (query failed) is treated as "assume locked": waking is
            # cheap and always safe, but typing a passcode into a dark
            # screen leaks it into whatever notification / lock UI is up.
            locked = self.is_locked()
            if locked is None or locked:
                logger.info("Screen off (or unknown) — waking")
                self._post("wda/pressButton", {"name": "home"})
                time.sleep(0.6)
            screen = self._screen_size or {"width": 390, "height": 844}
            w, h = screen["width"], screen["height"]
            logger.info("Unlock: swipe up + type passcode (%d digits)", len(passcode) if passcode else 0)
            self._post(
                "wda/dragfromtoforduration",
                {
                    "fromX": w / 2, "fromY": h * 0.92,
                    "toX": w / 2, "toY": h * 0.25,
                    "duration": 0.25,
                },
            )
            time.sleep(0.9)
            if passcode:
                self._post("wda/keys", {"value": list(passcode)})
        _fire_and_forget(_do)

    def send_keys(self, text: str):
        if not text:
            return
        _fire_and_forget(lambda: self._post("wda/keys", {"value": list(text)}))

    def send_key_codes(self, codes: list[str]):
        if not codes:
            return
        _fire_and_forget(lambda: self._post("wda/keys", {"value": codes}))

    def is_locked(self) -> bool | None:
        """True/False when WDA can answer; None when the query itself failed.

        Callers that ONLY care about "should I wake first" (e.g.
        unlock_with_passcode) must treat None as "assume locked" — a
        transient WDA timeout should not cause the passcode to be typed
        into a dark screen.
        """
        if not self.session_id:
            return None
        try:
            resp = self._session.get(
                f"{self.base_url}/session/{self.session_id}/wda/locked",
                timeout=3,
            )
            data = resp.json()
            return bool(data.get("value", False))
        except Exception as e:
            logger.warning("is_locked query failed: %s", e)
            return None

    def keep_alive(self):
        if not self.session_id:
            return
        if self.is_locked():
            self.unlock()
        else:
            def _ping():
                try:
                    self._session.get(
                        f"{self.base_url}/session/{self.session_id}/wda/screen",
                        timeout=3,
                    )
                except Exception:
                    pass
            _fire_and_forget(_ping)

    def get_battery_info(self) -> dict:
        if not self.session_id:
            return {}
        try:
            resp = self._session.get(
                f"{self.base_url}/session/{self.session_id}/wda/batteryInfo",
                timeout=5,
            )
            data = resp.json()
            value = data.get("value", {})
            return {
                "level": int(value.get("level", -1) * 100),
                "state": value.get("state", 0),
            }
        except Exception:
            return {}


class InputHandler(QObject):
    wda_status_changed = pyqtSignal(bool)

    TAP_MAX_DURATION = 0.5
    LONG_PRESS_DELAY = 900
    DRAG_THRESHOLD = 12
    SCROLL_MIN_INTERVAL = 0.25

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wda = WDAClient()

        self._state = GestureState.IDLE
        self._press_pos = QPointF(0, 0)
        self._press_time = 0.0
        self._iphone_press_pos = (0.0, 0.0)
        self._last_scroll_time = 0.0
        # Draw geometry pushed from ScreenView.paintEvent — the exact
        # x/y/w/h of the MJPEG pixmap on-screen. When >0, these override
        # the aspect-ratio recomputation in translate_coordinates so the
        # painter and the coord translator can never disagree.
        self._display_x = 0
        self._display_y = 0
        self._display_w = 0
        self._display_h = 0

        self._iphone_width = 1170
        self._iphone_height = 2532
        self._known_screen_size: tuple[int, int] | None = None

        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.timeout.connect(self._on_long_press_timeout)

        self._scroll_accum_dx = 0.0
        self._scroll_accum_dy = 0.0
        self._scroll_anchor = None
        self._scroll_modifiers = 0
        self._scroll_flush_timer = QTimer(self)
        self._scroll_flush_timer.setSingleShot(True)
        self._scroll_flush_timer.setInterval(120)
        self._scroll_flush_timer.timeout.connect(self._flush_scroll)

    def try_connect_wda(self) -> bool:
        connected = self.wda.connect()
        self.wda_status_changed.emit(connected)
        return connected

    def update_screen_size(self, width: int, height: int):
        self._iphone_width = width
        self._iphone_height = height

    def set_actual_screen_size(self, width: int, height: int):
        self._known_screen_size = (width, height)

    def set_display_geometry(self, x: int, y: int, w: int, h: int):
        """The exact pixel rect the MJPEG frame is drawn into.

        Called from ScreenView.paintEvent every repaint. Using the
        painter's own numbers here (instead of recomputing from aspect
        ratios) removes the ~1-pixel rounding drift that showed up as
        "paar mm naast" on drawn lines.
        """
        self._display_x = x
        self._display_y = y
        self._display_w = w
        self._display_h = h

    def translate_coordinates(
        self,
        mouse_x: float,
        mouse_y: float,
        label_width: float,
        label_height: float,
    ) -> tuple[float, float] | None:
        # Prefer the painter's own draw geometry — it's the ONLY source
        # of truth about where the MJPEG frame actually lives on screen.
        # If it hasn't been pushed yet (no frame painted), fall back to
        # the aspect-ratio recomputation.
        if self._display_w > 0 and self._display_h > 0:
            offset_x = self._display_x
            offset_y = self._display_y
            display_width = self._display_w
            display_height = self._display_h
        else:
            iphone_w = self._iphone_width
            iphone_h = self._iphone_height
            iphone_aspect = iphone_w / iphone_h
            label_aspect = label_width / label_height
            if label_aspect > iphone_aspect:
                display_height = label_height
                display_width = label_height * iphone_aspect
                offset_x = (label_width - display_width) / 2
                offset_y = 0
            else:
                display_width = label_width
                display_height = label_width / iphone_aspect
                offset_x = 0
                offset_y = (label_height - display_height) / 2

        rel_x = mouse_x - offset_x
        rel_y = mouse_y - offset_y

        if rel_x < 0 or rel_x > display_width or rel_y < 0 or rel_y > display_height:
            return None

        frac_x = rel_x / display_width
        frac_y = rel_y / display_height

        # Coordinate space: WDA's /wda/screen reports 390x844 on our
        # iPhone 17 Pro Max but its tap API internally translates from
        # THAT reported space to the physical XCTest space. Verified
        # empirically: with target=(440,956) tapping App Store opens
        # Settings — the icon one row below — meaning y overshoots by
        # ~13% (956/844 ≈ 1.133). Use WDA's own report as the tap
        # target; only fall back to the device-model override if WDA
        # hasn't fetched its screen info yet.
        screen = self.wda._screen_size
        if screen:
            target_w, target_h = screen["width"], screen["height"]
            source = "wda"
        elif self._known_screen_size:
            target_w, target_h = self._known_screen_size
            source = "device-model"
        else:
            scale = self.wda.screen_scale
            return (frac_x * iphone_w / scale, frac_y * iphone_h / scale)

        wda_x = frac_x * target_w * TAP_X_SCALE
        wda_y = frac_y * target_h * TAP_Y_SCALE
        logger.debug(
            "coord: mouse(%.0f,%.0f) -> wda(%.1f,%.1f) [x_scale=%.3f y_scale=%.3f]",
            mouse_x, mouse_y, wda_x, wda_y, TAP_X_SCALE, TAP_Y_SCALE,
        )
        return (wda_x, wda_y)

    def on_mouse_press(
        self,
        mouse_x: float,
        mouse_y: float,
        label_width: float,
        label_height: float,
    ):
        if not self.wda.is_connected:
            return
        coords = self.translate_coordinates(mouse_x, mouse_y, label_width, label_height)
        if coords is None:
            return

        self._state = GestureState.PRESSED
        self._press_pos = QPointF(mouse_x, mouse_y)
        self._press_time = time.time()
        self._iphone_press_pos = coords
        self._long_press_timer.start(self.LONG_PRESS_DELAY)

    def on_mouse_move(
        self,
        mouse_x: float,
        mouse_y: float,
        label_width: float,
        label_height: float,
    ):
        if self._state == GestureState.IDLE:
            return
        dx = mouse_x - self._press_pos.x()
        dy = mouse_y - self._press_pos.y()
        distance = (dx * dx + dy * dy) ** 0.5
        if self._state == GestureState.PRESSED and distance > self.DRAG_THRESHOLD:
            self._state = GestureState.DRAGGING
            self._long_press_timer.stop()

    def on_mouse_release(
        self,
        mouse_x: float,
        mouse_y: float,
        label_width: float,
        label_height: float,
    ):
        if not self.wda.is_connected:
            self._state = GestureState.IDLE
            return

        self._long_press_timer.stop()
        coords = self.translate_coordinates(mouse_x, mouse_y, label_width, label_height)

        if self._state == GestureState.PRESSED:
            elapsed = time.time() - self._press_time
            if elapsed < self.TAP_MAX_DURATION:
                x, y = self._iphone_press_pos
                logger.debug("TAP -> (%.1f, %.1f)", x, y)
                self.wda.tap(x, y)

        elif self._state == GestureState.DRAGGING and coords:
            # 2-point stroke via W3C actions instead of the classic
            # wda/dragfromtoforduration endpoint. Empirically the
            # dragfromtoforduration path landed strokes progressively
            # LOWER than intent on iPhone 17 Pro Max + iOS 27 while
            # the W3C actions path uses the same coord semantics as
            # wda/tap — which we verified is correct across the full
            # y-range. Same single-request latency as before, no
            # per-move round-trip.
            x1, y1 = self._iphone_press_pos
            x2, y2 = coords
            dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            duration_ms = max(120, min(600, int(dist * 1.2)))
            logger.debug("STROKE -> (%.1f,%.1f) → (%.1f,%.1f) dur=%dms",
                        x1, y1, x2, y2, duration_ms)
            self.wda.stroke([(x1, y1), (x2, y2)], duration_ms=duration_ms)

        self._state = GestureState.IDLE

    def _on_long_press_timeout(self):
        if self._state == GestureState.PRESSED:
            self._state = GestureState.LONG_PRESSING
            x, y = self._iphone_press_pos
            self.wda.long_press(x, y, duration=1.0)

    def on_scroll(
        self,
        mouse_x: float,
        mouse_y: float,
        delta_x: float,
        delta_y: float,
        label_width: float,
        label_height: float,
        modifiers: int = 0,
    ):
        if not self.wda.is_connected:
            return

        coords = self.translate_coordinates(mouse_x, mouse_y, label_width, label_height)
        if coords is None:
            return

        cmd_mod = 0x04000000
        if modifiers & cmd_mod:
            magnitude = (delta_x * delta_x + delta_y * delta_y) ** 0.5
            if magnitude < 5:
                return
            zoom_in = delta_y > 0
            scale = 1.6 if zoom_in else 1 / 1.6
            self.wda.pinch(coords[0], coords[1], scale)
            return

        if self._scroll_anchor is None:
            self._scroll_anchor = coords
        self._scroll_accum_dx += delta_x
        self._scroll_accum_dy += delta_y
        self._scroll_modifiers = modifiers
        self._scroll_flush_timer.start()

    def _flush_scroll(self):
        if self._scroll_anchor is None:
            return
        x, y = self._scroll_anchor
        dx = self._scroll_accum_dx
        dy = self._scroll_accum_dy
        self._scroll_anchor = None
        self._scroll_accum_dx = 0.0
        self._scroll_accum_dy = 0.0

        magnitude = (dx * dx + dy * dy) ** 0.5
        if magnitude < 5:
            return

        distance = min(400.0, max(60.0, magnitude * 0.6))
        ndx = dx / magnitude
        ndy = dy / magnitude
        # User preference (calibrated live):
        # - Vertical: REVERSED — trackpad down = iPhone content moves up
        #   (you see what's below), Windows/classic convention.
        # - Horizontal: NATURAL — trackpad right = iPhone content moves
        #   right (page pans right). Not inverted.
        finger_dx = ndx * distance
        finger_dy = ndy * distance

        from_x = x - finger_dx / 2
        from_y = y - finger_dy / 2
        to_x = x + finger_dx / 2
        to_y = y + finger_dy / 2

        duration = max(0.20, min(0.45, distance / 700))
        self.wda.swipe(from_x, from_y, to_x, to_y, duration=duration)
        logger.debug(
            "scroll flush: accum(%.0f,%.0f) mag=%.0f dist=%.0f dur=%.2fs",
            dx, dy, magnitude, distance, duration,
        )

    def press_button(self, name: str):
        self.wda.press_button(name)

    def on_key_press(self, qt_key: int, modifiers: int, text: str):
        logger.debug(
            "key_press: qt_key=0x%08x mods=0x%08x text=%r",
            qt_key, modifiers, text,
        )
        if not self.wda.is_connected:
            return

        # WDA /wda/keys calls FBTypeText directly and does NOT run the
        # W3C special-key mapping (that lives in FBW3CActionsHelpers.m
        # and is only used by /actions). So PUA codes like U+E003 land
        # in the text field as visible "kruisje" glyphs. XCTest typeText
        # does understand the plain ASCII control chars though — send
        # those for keys that map to a control char. Arrow / Home / End
        # have no control-char equivalent, so route those through the
        # W3C actions path (send_key_codes) instead, which handles the
        # PUA mapping correctly.
        typetext_map = {
            0x01000003: "\x08",  # Backspace
            0x01000000: "\x1b",  # Escape
            0x01000004: "\r",    # Return
            0x01000005: "\r",    # Enter (numpad)
            0x01000001: "\t",    # Tab
            0x01000006: "\x7f",  # Delete (forward)
        }
        if qt_key in typetext_map:
            self.wda.send_keys(typetext_map[qt_key])
            return

        pua_map = {
            0x01000012: "",  # ArrowLeft
            0x01000013: "",  # ArrowUp
            0x01000014: "",  # ArrowRight
            0x01000015: "",  # ArrowDown
            0x01000010: "",  # Home
            0x01000011: "",  # End
        }
        if qt_key in pua_map:
            self.wda.send_key_codes([pua_map[qt_key]])
            return

        if text and text.isprintable():
            self.wda.send_keys(text)
            return

        if text == "\r" or text == "\n":
            self.wda.send_keys("\r")
        elif text == "\t":
            self.wda.send_keys("\t")
        elif text == "\b":
            self.wda.send_keys("\x08")

    def go_home(self):
        self.wda.home_screen()

    def lock_device(self):
        self.wda.lock()

    def unlock_device(self, passcode: str | None = None):
        if passcode:
            self.wda.unlock_with_passcode(passcode)
        else:
            self.wda.unlock()

    def cleanup(self):
        self.wda.disconnect()

import logging
import threading

from datetime import datetime

from PyQt6.QtCore import QPoint, QRectF, QSettings, Qt, QTimer
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src import device_models, passcode_store, tunnel_manager, wda_auth
from src.device_manager import ConnectionState, DeviceManager
from src.input_handler import InputHandler
from src.screen_capture import ScreenCaptureThread

logger = logging.getLogger(__name__)


PHONE_BEZEL = 10
PHONE_RADIUS_OUTER = 48
PHONE_RADIUS_INNER = 38
IPHONE_SCREEN_ASPECT = 390 / 844
DOCK_HEIGHT = 50


class ScreenView(QWidget):
    def __init__(self, input_handler: InputHandler, parent=None):
        super().__init__(parent)
        self._input_handler = input_handler
        self._pixmap: QPixmap | None = None
        self._has_frame = False
        self._placeholder_text = (
            "Searching for iPhone…\n\n"
            "• Same WiFi network\n"
            "• Developer Mode on\n"
            "• pymobiledevice3 remote tunneld"
        )

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, PHONE_RADIUS_INNER, PHONE_RADIUS_INNER)
        painter.setClipPath(clip)

        painter.fillRect(rect, QColor("#000"))

        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) / 2
            y = (self.height() - scaled.height()) / 2
            painter.drawPixmap(int(x), int(y), scaled)
        else:
            painter.setPen(QColor("#888"))
            font = QFont()
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._placeholder_text,
            )


    def update_frame(self, qimage: QImage):
        self._has_frame = True
        self._pixmap = QPixmap.fromImage(qimage)
        fitted = self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._input_handler.update_screen_size(fitted.width(), fitted.height())
        self.update()

    def show_disconnected(self):
        self._has_frame = False
        self._pixmap = None
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._has_frame:
            self.setFocus()
            pos = event.position()
            self._input_handler.on_mouse_press(
                pos.x(), pos.y(), self.width(), self.height(),
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._has_frame:
            pos = event.position()
            self._input_handler.on_mouse_move(
                pos.x(), pos.y(), self.width(), self.height(),
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._has_frame:
            pos = event.position()
            self._input_handler.on_mouse_release(
                pos.x(), pos.y(), self.width(), self.height(),
            )
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._has_frame:
            pos = event.position()
            delta = event.angleDelta()
            self._input_handler.on_scroll(
                pos.x(), pos.y(),
                delta.x(), delta.y(),
                self.width(), self.height(),
                int(event.modifiers().value),
            )
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        if self._has_frame:
            self._input_handler.on_key_press(
                int(event.key()),
                int(event.modifiers().value),
                event.text(),
            )
            event.accept()
            return
        super().keyPressEvent(event)


class PhoneFrame(QWidget):
    def __init__(self, input_handler: InputHandler, width: int, height: int, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(width, height)

        self.screen_view = ScreenView(input_handler, self)
        self.screen_view.setGeometry(
            PHONE_BEZEL,
            PHONE_BEZEL,
            width - PHONE_BEZEL * 2,
            height - PHONE_BEZEL * 2,
        )

        self._drag_offset: QPoint | None = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        body_rect = QRectF(0, 0, self.width(), self.height())
        body_path = QPainterPath()
        body_path.addRoundedRect(body_rect, PHONE_RADIUS_OUTER, PHONE_RADIUS_OUTER)
        painter.fillPath(body_path, QBrush(QColor("#0a0a0a")))

        painter.setPen(QColor(60, 60, 60))
        painter.drawPath(body_path)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._point_on_screen(event.position()):
                self._drag_offset = event.globalPosition().toPoint() - self.window().pos()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _point_on_screen(self, pos) -> bool:
        return self.screen_view.geometry().contains(pos.toPoint())


class DockButton(QToolButton):
    def __init__(self, label: str, tooltip: str, parent=None):
        super().__init__(parent)
        self.setText(label)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(36, 36)
        self.setStyleSheet(
            "QToolButton {"
            "  background: #1a1a1a; color: #e5e5e5;"
            "  border: 1px solid #2a2a2a; border-radius: 18px;"
            "  font-size: 14px;"
            "}"
            "QToolButton:hover { background: #262626; border-color: #3a3a3a; }"
            "QToolButton:pressed { background: #0f0f0f; }"
        )


class WindowButton(QPushButton):
    def __init__(self, color: str, hover: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton {{ background: {color}; border-radius: 6px; border: none; }}"
            f"QPushButton:hover {{ background: {hover}; }}"
        )


class SettingsDialog(QDialog):
    def __init__(self, udid: str, device_manager, parent=None):
        super().__init__(parent)
        self._udid = udid
        self._device_manager = device_manager
        self.setWindowTitle("Instellingen")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        layout.addWidget(self._device_group())
        layout.addWidget(self._passcode_group())
        layout.addWidget(self._service_group())

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

    def _device_group(self) -> QWidget:
        group = QFrame()
        group.setStyleSheet(
            "QFrame { background: #1a1a1a; border-radius: 8px; padding: 8px; }"
            "QLabel { color: #ccc; }"
        )
        layout = QVBoxLayout(group)

        title = QLabel("iPhone kiezen")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        info = QLabel(
            "Kies welke iPhone gemirrord wordt. Wijziging vereist reconnect."
        )
        info.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(info)

        self._device_combo = QComboBox()
        self._device_combo.addItem("Automatisch (eerste beschikbare)", "")

        settings = QSettings("iPhoneMirroring", "iPhoneMirror")
        preferred = settings.value("device/preferred_udid", "", type=str)

        available = self._device_manager.list_available_devices() if self._device_manager else []
        seen_udids = set()
        for d in available:
            udid = d.get("udid", "")
            if not udid or udid in seen_udids:
                continue
            seen_udids.add(udid)
            model = d.get("model", "")
            from src import device_models
            friendly = device_models.friendly_name(model) if model else d.get("name", "iPhone")
            ios = d.get("ios", "")
            label = f"{friendly} · iOS {ios}" if ios else friendly
            self._device_combo.addItem(label, udid)

        if preferred and preferred not in seen_udids:
            self._device_combo.addItem(f"(offline) {preferred[:8]}…", preferred)

        for i in range(self._device_combo.count()):
            if self._device_combo.itemData(i) == preferred:
                self._device_combo.setCurrentIndex(i)
                break

        self._device_combo.currentIndexChanged.connect(self._save_device)
        layout.addWidget(self._device_combo)
        return group

    def _save_device(self):
        udid = self._device_combo.currentData()
        settings = QSettings("iPhoneMirroring", "iPhoneMirror")
        settings.setValue("device/preferred_udid", udid or "")
        if self._device_manager:
            self._device_manager.set_preferred_udid(udid or None)

    def _passcode_group(self) -> QWidget:
        group = QFrame()
        group.setStyleSheet(
            "QFrame { background: #1a1a1a; border-radius: 8px; padding: 8px; }"
            "QLabel { color: #ccc; }"
        )
        layout = QVBoxLayout(group)

        title = QLabel("iPhone passcode")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        info = QLabel(
            "Opgeslagen in macOS Keychain per device.\n"
            "Wordt gebruikt door Unlock (🔓)."
        )
        info.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(info)

        row = QHBoxLayout()
        self._pc_input = QLineEdit()
        self._pc_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pc_input.setPlaceholderText("6 cijfers")
        if self._udid:
            self._pc_input.setText(passcode_store.get_passcode(self._udid) or "")
        row.addWidget(self._pc_input, 1)

        save_btn = QPushButton("Opslaan")
        save_btn.clicked.connect(self._save_passcode)
        row.addWidget(save_btn)
        layout.addLayout(row)

        if not self._udid:
            info.setText("Geen device verbonden — passcode niet opslaanbaar")
            self._pc_input.setEnabled(False)
            save_btn.setEnabled(False)

        return group

    def _service_group(self) -> QWidget:
        group = QFrame()
        group.setStyleSheet(
            "QFrame { background: #1a1a1a; border-radius: 8px; padding: 8px; }"
            "QLabel { color: #ccc; }"
        )
        layout = QVBoxLayout(group)

        title = QLabel("Tunnel service")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        self._svc_status = QLabel()
        self._svc_status.setStyleSheet("font-size: 11px; color: #888;")
        layout.addWidget(self._svc_status)

        row = QHBoxLayout()
        self._btn_install = QPushButton("Installeren")
        self._btn_install.clicked.connect(self._install)
        self._btn_uninstall = QPushButton("Verwijderen")
        self._btn_uninstall.clicked.connect(self._uninstall)
        self._btn_start = QPushButton("Start")
        self._btn_start.clicked.connect(self._start)
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.clicked.connect(self._stop)
        for b in (self._btn_install, self._btn_uninstall, self._btn_start, self._btn_stop):
            row.addWidget(b)
        layout.addLayout(row)

        self._refresh_service_state()
        return group

    def _refresh_service_state(self):
        installed = tunnel_manager.is_fully_installed()
        running = tunnel_manager.is_tunneld_running()
        parts = []
        parts.append("Service: geïnstalleerd" if installed else "Service: niet geïnstalleerd")
        parts.append("draait" if running else "gestopt")
        self._svc_status.setText(" · ".join(parts))

        self._btn_install.setEnabled(not installed)
        self._btn_uninstall.setEnabled(installed)
        self._btn_start.setEnabled(installed and not running)
        self._btn_stop.setEnabled(installed and running)

    def _save_passcode(self):
        code = self._pc_input.text().strip()
        if code and not code.isdigit():
            QMessageBox.warning(self, "Ongeldig", "Passcode moet alleen cijfers bevatten.")
            return
        if passcode_store.set_passcode(self._udid, code):
            QMessageBox.information(self, "Opgeslagen", "Passcode opgeslagen in Keychain.")
        else:
            QMessageBox.warning(self, "Fout", "Kon passcode niet opslaan.")

    def _install(self):
        ok, err = tunnel_manager.install_service()
        if not ok:
            QMessageBox.warning(self, "Installatie mislukt", err)
        self._refresh_service_state()

    def _uninstall(self):
        reply = QMessageBox.question(
            self, "Verwijderen",
            "Service en sudoers regel verwijderen?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, err = tunnel_manager.uninstall_service()
        if not ok:
            QMessageBox.warning(self, "Verwijderen mislukt", err)
        self._refresh_service_state()

    def _start(self):
        ok, err = tunnel_manager.start_service()
        if not ok:
            QMessageBox.warning(self, "Start mislukt", err)
        self._refresh_service_state()

    def _stop(self):
        ok, err = tunnel_manager.stop_service()
        if not ok:
            QMessageBox.warning(self, "Stop mislukt", err)
        self._refresh_service_state()


class MainWindow(QMainWindow):
    WINDOW_MARGIN = 20

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Window,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("iPhone Mirror")

        self.device_manager = DeviceManager(self)
        _settings = QSettings("iPhoneMirroring", "iPhoneMirror")
        preferred = _settings.value("device/preferred_udid", "", type=str)
        if preferred:
            self.device_manager.set_preferred_udid(preferred)
        self.input_handler = InputHandler(self)
        self.capture_thread = None
        self._connected = False

        self._setup_ui()
        self._connect_signals()

        self._battery_timer = QTimer(self)
        self._battery_timer.timeout.connect(self._update_battery)

        self._wda_retry_timer = QTimer(self)
        self._wda_retry_timer.timeout.connect(self._try_wda)
        self._wda_retry_timer.setInterval(5000)

        self._keepalive_timer = QTimer(self)
        self._keepalive_timer.timeout.connect(self._keep_alive)
        self._keepalive_timer.setInterval(25000)

        QTimer.singleShot(200, self._ensure_tunnel_and_discover)

    def _ensure_tunnel_and_discover(self):
        self._started_tunnel = False
        if tunnel_manager.is_tunneld_running():
            self._status_label.setText("Tunnel actief")
            self.device_manager.start_discovery()
            return

        if tunnel_manager.is_fully_installed():
            self._status_label.setText("Tunnel starten…")
            ok, err = tunnel_manager.start_service()
            if ok:
                self._started_tunnel = True
                self._status_label.setText("Tunnel actief")
                self.device_manager.start_discovery()
            else:
                self._status_label.setText("Tunnel start faalde")
                QMessageBox.warning(
                    self, "Tunnel start mislukt",
                    f"{err}\n\nCheck /var/log/iphonemirror-tunneld.log",
                )
            return

        reply = QMessageBox.question(
            self, "Tunnel service",
            "Installeer de tunnel service?\n\n"
            "De service start/stopt automatisch met de app.\n"
            "macOS vraagt éénmalig je admin wachtwoord.\n"
            "Daarna nooit meer prompts.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._status_label.setText("Geen tunnel")
            return

        self._status_label.setText("Tunnel installeren…")
        ok, err = tunnel_manager.install_service()
        if not ok:
            QMessageBox.warning(self, "Installatie mislukt", err)
            self._status_label.setText("Geen tunnel")
            return

        ok, err = tunnel_manager.start_service()
        if ok:
            self._started_tunnel = True
            self._status_label.setText("Tunnel actief")
            self.device_manager.start_discovery()
        else:
            QMessageBox.warning(self, "Tunnel start mislukt", err)
            self._status_label.setText("Tunnel geïnstalleerd, niet gestart")

    def _setup_ui(self):
        central = QWidget()
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(
            self.WINDOW_MARGIN, self.WINDOW_MARGIN,
            self.WINDOW_MARGIN, self.WINDOW_MARGIN,
        )
        outer.setSpacing(8)
        outer.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        top_bar = self._build_top_bar()
        outer.addWidget(top_bar)

        phone_w, phone_h = self._compute_phone_size()
        self.phone_frame = PhoneFrame(self.input_handler, phone_w, phone_h, self)
        self.screen_view = self.phone_frame.screen_view
        outer.addWidget(self.phone_frame, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._wda_banner = QLabel("Touch control: WebDriverAgent connecting…")
        self._wda_banner.setStyleSheet(
            "background: #f59e0b; color: #000;"
            "border-radius: 8px; padding: 4px 10px; font-size: 11px;"
        )
        self._wda_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._wda_banner.setVisible(False)
        outer.addWidget(self._wda_banner, alignment=Qt.AlignmentFlag.AlignHCenter)

        dock = self._build_dock()
        outer.addWidget(dock, alignment=Qt.AlignmentFlag.AlignHCenter)

        status = self._build_status()
        outer.addWidget(status, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.setCentralWidget(central)
        self.setFixedSize(
            phone_w + self.WINDOW_MARGIN * 2,
            phone_h + self.WINDOW_MARGIN * 2 + self._chrome_height(),
        )
        QTimer.singleShot(0, self._restore_position)

        self._save_pos_timer = QTimer(self)
        self._save_pos_timer.setSingleShot(True)
        self._save_pos_timer.setInterval(400)
        self._save_pos_timer.timeout.connect(self._save_position)

    def _chrome_height(self) -> int:
        return 20 + 8 + DOCK_HEIGHT + 8 + 24 + 8

    def _compute_phone_size(self) -> tuple[int, int]:
        screen = self.screen()
        avail_h = screen.availableGeometry().height() if screen else 900
        max_phone_h = avail_h - self.WINDOW_MARGIN * 2 - self._chrome_height() - 20
        phone_h = min(720, max(500, max_phone_h))
        inner_h = phone_h - PHONE_BEZEL * 2
        inner_w = int(inner_h * IPHONE_SCREEN_ASPECT)
        phone_w = inner_w + PHONE_BEZEL * 2
        return phone_w, phone_h

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_save_pos_timer"):
            self._save_pos_timer.start()

    def _save_position(self):
        settings = QSettings("iPhoneMirroring", "iPhoneMirror")
        settings.setValue("window/pos", self.pos())

    def _restore_position(self):
        settings = QSettings("iPhoneMirroring", "iPhoneMirror")
        pos = settings.value("window/pos")
        if isinstance(pos, QPoint) and self._is_on_screen(pos):
            self.move(pos)
            return
        self._center_on_screen()

    def _is_on_screen(self, pos: QPoint) -> bool:
        for screen in QApplication.screens():
            g = screen.availableGeometry()
            if g.contains(pos):
                return True
        return False

    def _center_on_screen(self):
        screen = self.screen()
        if not screen:
            return
        geo = screen.availableGeometry()
        centered_x = (geo.width() - self.width()) // 2
        offset_x = int(geo.width() * 0.12)
        self.move(
            geo.x() + max(0, centered_x - offset_x),
            geo.y() + max(0, (geo.height() - self.height()) // 2),
        )

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(20)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(8)

        close_btn = WindowButton("#ff5f57", "#ff8a80")
        close_btn.setToolTip("Sluiten")
        close_btn.clicked.connect(self.close)

        min_btn = WindowButton("#febc2e", "#ffd580")
        min_btn.setToolTip("Minimaliseren")
        min_btn.clicked.connect(self.showMinimized)

        layout.addWidget(close_btn)
        layout.addWidget(min_btn)
        layout.addStretch(1)

        title = QLabel("iPhone Mirror")
        title.setStyleSheet("color: #999; font-size: 10px;")
        layout.addWidget(title)
        layout.addStretch(1)

        return bar

    def _build_dock(self) -> QWidget:
        dock = QFrame()
        dock.setFixedHeight(DOCK_HEIGHT)
        dock.setStyleSheet(
            "QFrame { background: rgba(20,20,20,220); border-radius: 18px; }"
        )
        layout = QHBoxLayout(dock)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        specs = [
            ("⌂", "Home (Ctrl+H)", "Ctrl+H", self._on_home),
            ("🔒", "Lock (Ctrl+L)", "Ctrl+L", self._on_lock),
            ("🔓", "Unlock (Ctrl+U)", "Ctrl+U", self._on_unlock),
            ("＋", "Volume up", None, lambda: self.input_handler.press_button("volumeUp")),
            ("−", "Volume down", None, lambda: self.input_handler.press_button("volumeDown")),
            ("↻", "Reconnect", None, self._on_reconnect),
            ("⚙", "Passcode configureren", None, self._on_configure_passcode),
        ]
        for label, tip, shortcut, handler in specs:
            btn = DockButton(label, tip)
            btn.clicked.connect(handler)
            if shortcut:
                action = QAction(self)
                action.setShortcut(shortcut)
                action.triggered.connect(handler)
                self.addAction(action)
            layout.addWidget(btn)

        return dock

    def _build_status(self) -> QWidget:
        bar = QFrame()
        bar.setStyleSheet(
            "QFrame { background: rgba(20,20,20,180); border-radius: 10px; }"
            "QLabel { color: #ccc; font-size: 10px; padding: 0 2px; }"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(12)

        self._status_label = QLabel("Disconnected")
        self._device_label = QLabel("")
        self._fps_label = QLabel("")
        self._battery_label = QLabel("")

        layout.addWidget(self._status_label)
        layout.addStretch(1)
        layout.addWidget(self._device_label)
        layout.addWidget(self._fps_label)
        layout.addWidget(self._battery_label)

        return bar

    def _connect_signals(self):
        self.device_manager.device_connected.connect(self._on_device_connected)
        self.device_manager.device_disconnected.connect(self._on_device_disconnected)
        self.device_manager.connection_error.connect(self._on_connection_error)
        self.device_manager.connection_state_changed.connect(self._on_state_changed)
        self.input_handler.wda_status_changed.connect(self._on_wda_status)

    def _on_device_connected(self, info: dict):
        if self._connected:
            return
        self._connected = True
        self.device_manager.stop_discovery()
        model = info.get("model", "")
        friendly = device_models.friendly_name(model)
        version = info.get("ios_version", "?")
        conn_type = info.get("connection_type", "?")
        short_conn = "WiFi" if "tunnel" in conn_type or "wifi" in conn_type.lower() else "USB"
        self._device_label.setText(f"{friendly} · iOS {version} · {short_conn}")
        self._status_label.setText("Connected")

        size = device_models.screen_size(model)
        if size:
            self.input_handler.set_actual_screen_size(*size)
            logger.info("Device %s screen: %dx%d points", friendly, *size)

        self._start_capture()
        self._start_wda_auto()

        self._battery_timer.start(30000)
        self._update_battery()

    def _on_device_disconnected(self):
        self._connected = False
        self._stop_capture()
        self._battery_timer.stop()
        self._wda_retry_timer.stop()
        self._keepalive_timer.stop()

        self._status_label.setText("Disconnected")
        self._fps_label.setText("")
        self._battery_label.setText("")
        self._device_label.setText("")
        self._wda_banner.setVisible(False)

        self.screen_view.show_disconnected()

    def _on_connection_error(self, msg: str):
        self._status_label.setText("Error")
        QMessageBox.warning(self, "Connection Error", msg)

    def _on_state_changed(self, state: ConnectionState):
        labels = {
            ConnectionState.DISCONNECTED: "Disconnected",
            ConnectionState.CONNECTING: "Connecting…",
            ConnectionState.CONNECTED: "Connected",
            ConnectionState.ERROR: "Error",
        }
        self._status_label.setText(labels.get(state, "?"))

    def _start_capture(self):
        if self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.stop()

        self.capture_thread = ScreenCaptureThread(self.device_manager, target_fps=15)
        self.capture_thread.frame_ready.connect(self._on_frame)
        self.capture_thread.fps_updated.connect(self._on_fps)
        self.capture_thread.capture_error.connect(self._on_capture_error)
        self.capture_thread.start()

    def _stop_capture(self):
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread = None

    def _on_frame(self, qimage: QImage):
        self.screen_view.update_frame(qimage)

    def _on_fps(self, fps: float):
        self._fps_label.setText(f"{fps:.0f} FPS")

    def _on_capture_error(self, msg: str):
        logger.warning("Capture error: %s", msg)
        self._status_label.setText(f"Error: {msg[:50]}")

    def _start_wda_auto(self):
        def _setup():
            token = wda_auth.get_or_create_token()
            self.input_handler.wda.set_auth_token(token)
            wda_url = self.device_manager.get_wda_url()
            self.input_handler.wda.base_url = wda_url.rstrip("/")
            logger.info("WDA URL: %s", wda_url)
            self.device_manager.start_wda(auth_token=token)

        threading.Thread(target=_setup, daemon=True).start()
        self._wda_retry_timer.start()

    def _try_wda(self):
        if self.input_handler.wda.is_connected:
            return
        token = wda_auth.get_or_create_token()
        self.input_handler.wda.set_auth_token(token)
        wda_url = self.device_manager.get_wda_url()
        self.input_handler.wda.base_url = wda_url.rstrip("/")
        self.input_handler.try_connect_wda()

    def _on_wda_status(self, connected: bool):
        self._wda_banner.setVisible(not connected)
        if connected:
            logger.info("WDA connected — touch control enabled")

    def _keep_alive(self):
        pass

    def _update_battery(self):
        try:
            if self.input_handler.wda.is_connected:
                info = self.input_handler.wda.get_battery_info()
                if info and info.get("level", -1) >= 0:
                    level = info["level"]
                    state = info.get("state", 0)
                    charging = " +" if state == 2 else ""
                    self._battery_label.setText(f"🔋 {level}%{charging}")
                    return

            if self.device_manager.is_connected:
                info = self.device_manager.get_battery_info()
                level = info.get("level", -1)
                if level >= 0:
                    charging = " +" if info.get("charging", False) else ""
                    self._battery_label.setText(f"🔋 {level}%{charging}")
        except Exception as e:
            logger.debug("Battery update failed: %s", e)

    def _on_home(self):
        self.input_handler.go_home()

    def _on_lock(self):
        self.input_handler.lock_device()

    def _on_unlock(self):
        udid = self.device_manager.device_info.get("udid", "")
        passcode = passcode_store.get_passcode(udid)
        self.input_handler.unlock_device(passcode)

    def _on_configure_passcode(self):
        udid = self.device_manager.device_info.get("udid", "")
        dialog = SettingsDialog(udid, self.device_manager, self)
        dialog.exec()

    def _on_reconnect(self):
        self._on_device_disconnected()
        self.device_manager.disconnect()
        self.device_manager.start_discovery()

    def closeEvent(self, event):
        settings = QSettings("iPhoneMirroring", "iPhoneMirror")
        settings.setValue("window/pos", self.pos())
        self._stop_capture()
        self.input_handler.cleanup()
        self.device_manager.cleanup()
        if getattr(self, "_started_tunnel", False):
            logger.info("Stopping tunnel service (started by app)")
            tunnel_manager.stop_service()
        super().closeEvent(event)

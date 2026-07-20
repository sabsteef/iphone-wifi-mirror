"""Application entry point.

v9 migration: bootstraps qasync so pymobiledevice3's async API runs in the
same event loop as the Qt widgets. No more sudo prompt, no LaunchDaemon —
:class:`TunnelManager` opens the tunnel in-process on the qasync loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import qasync
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from src.main_window import MainWindow


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def check_dependencies() -> None:
    missing = []
    for name, module in [
        ("pymobiledevice3", "pymobiledevice3"),
        ("PyQt6", "PyQt6"),
        ("qasync", "qasync"),
        ("Pillow", "PIL"),
        ("requests", "requests"),
    ]:
        try:
            __import__(module)
        except ImportError:
            missing.append(name)
    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print("Install with: pip install -r requirements.txt")
        sys.exit(1)


def apply_dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(35, 35, 35))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 50, 50))
    p.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    p.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(p)


async def _shutdown(window: MainWindow) -> None:
    logging.info("Graceful shutdown starting")
    try:
        await asyncio.wait_for(window.async_close(), timeout=15.0)
    except asyncio.TimeoutError:
        logging.error("async_close did not finish in 15s — forcing quit")
    except Exception as e:
        logging.error("async_close raised: %s", e, exc_info=e)
    QApplication.instance().quit()


def main() -> None:
    setup_logging()
    check_dependencies()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("iPhone Mirror")
    app.setOrganizationName("iPhoneMirroring")
    apply_dark_palette(app)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    def _handle_signal():
        logging.info("Signal received, shutting down")
        asyncio.ensure_future(_shutdown(window))

    # loop.add_signal_handler dispatches the coroutine on the actual
    # running loop instead of latching onto whatever thread happens to
    # receive the signal. With qasync + Qt, signal.signal() delivers on
    # the main thread but the callback runs BEFORE the loop picks up its
    # next iteration, so ensure_future can miss and shutdown never
    # fires. add_signal_handler is the documented supported path.
    #
    # It's Unix-only; on Windows fall back to signal.signal (SIGTERM
    # doesn't exist there anyway, only SIGINT).
    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_signal)
        loop.add_signal_handler(signal.SIGINT, _handle_signal)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: _handle_signal())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: _handle_signal())

    # Kick off the async device discovery now that the loop is bound to the
    # Qt application.
    asyncio.ensure_future(window.start_async())

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()

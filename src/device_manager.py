import logging
import subprocess
import sys
import threading
import time
from enum import Enum
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    ERROR = 3


class DeviceManager(QObject):
    device_connected = pyqtSignal(dict)
    device_disconnected = pyqtSignal()
    connection_error = pyqtSignal(str)
    connection_state_changed = pyqtSignal(ConnectionState)

    WDA_BUNDLE_ID = "com.sabsteef.WebDriverAgentRunner.xctrunner"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = ConnectionState.DISCONNECTED
        self._lockdown = None
        self._dvt = None
        self._rsd = None
        self._screenshot_service = None
        self._device_info: dict = {}
        self._lock = threading.Lock()
        self._current_udid: Optional[str] = None
        self._wda_proc: Optional[subprocess.Popen] = None
        self._tunnel_address: Optional[str] = None
        self._dev_image_mounted = False

        self._discovery_timer = QTimer(self)
        self._discovery_timer.timeout.connect(self._poll_devices)

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def device_info(self) -> dict:
        return self._device_info

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    def start_discovery(self, interval_ms: int = 2000):
        self._discovery_timer.start(interval_ms)
        self._poll_devices()

    def stop_discovery(self):
        self._discovery_timer.stop()

    def _set_state(self, state: ConnectionState):
        self._state = state
        self.connection_state_changed.emit(state)

    def _poll_devices(self):
        if self.is_connected:
            return
        try:
            devices = self._discover_devices()
            if not devices:
                return
            best = self._pick_best_device(devices)
            if best is None:
                return
            device_type, device = best
            self._connect_in_background(device_type, device)
        except Exception as e:
            logger.debug("Discovery poll error: %s", e)

    def set_preferred_udid(self, udid: str | None):
        self._preferred_udid = udid or None

    def _pick_best_device(self, devices: list) -> tuple | None:
        preferred = getattr(self, "_preferred_udid", None)
        tunnel_devs = [(t, d) for t, d in devices if t == "tunnel"]
        if not tunnel_devs:
            return None
        if preferred:
            for t, d in tunnel_devs:
                if getattr(d, "udid", None) == preferred:
                    return (t, d)
            return None
        return tunnel_devs[0]

    def list_available_devices(self) -> list[dict]:
        try:
            devices = self._discover_devices()
        except Exception:
            return []
        result = []
        for device_type, device in devices:
            if device_type != "tunnel":
                continue
            info = {"udid": getattr(device, "udid", "")}
            try:
                info["name"] = device.get_value(key="DeviceName") or "iPhone"
            except Exception:
                info["name"] = "iPhone"
            try:
                props = device.peer_info.get("Properties", {})
                info["model"] = props.get("ProductType", "")
                info["ios"] = props.get("OSVersion", "")
            except Exception:
                info["model"] = ""
                info["ios"] = ""
            result.append(info)
        return result

    def _discover_devices(self) -> list:
        found = []

        try:
            from pymobiledevice3.tunneld.api import get_tunneld_devices
            tunneld_devices = get_tunneld_devices()
            if tunneld_devices:
                for td in tunneld_devices:
                    found.append(("tunnel", td))
                return found
        except ImportError:
            logger.debug("tunneld API not available")
        except Exception as e:
            logger.debug("Tunnel discovery failed: %s", e)

        try:
            from pymobiledevice3.usbmux import list_devices
            for device in list_devices():
                conn = device.connection_type
                found.append((conn.lower() if conn else "usb", device))
        except Exception as e:
            logger.debug("usbmux discovery failed: %s", e)

        return found

    def _connect_in_background(self, device_type: str, device):
        if self._state == ConnectionState.CONNECTING:
            return
        self._set_state(ConnectionState.CONNECTING)
        thread = threading.Thread(
            target=self._connect, args=(device_type, device), daemon=True
        )
        thread.start()

    def _connect(self, device_type: str, device):
        try:
            if self._state == ConnectionState.CONNECTED:
                return

            if device_type == "tunnel":
                self._connect_via_tunnel(device)
            else:
                udid = device.serial
                self._connect_via_usbmux(udid)

            self._mount_developer_image()

            self._set_state(ConnectionState.CONNECTED)
            self.device_connected.emit(self._device_info)
            logger.info(
                "Connected to %s (%s) via %s",
                self._device_info.get("name", "iPhone"),
                self._device_info.get("ios_version", "?"),
                device_type,
            )

        except Exception as e:
            logger.error("Connection failed: %s", e)
            self._set_state(ConnectionState.ERROR)
            self.connection_error.emit(str(e))

    def _connect_via_tunnel(self, tunnel_device):
        self._rsd = tunnel_device
        udid = getattr(tunnel_device, "udid", "tunnel")
        self._current_udid = udid

        try:
            addr = tunnel_device.service.address
            self._tunnel_address = addr[0] if addr else None
        except Exception:
            self._tunnel_address = None

        name = "iPhone"
        model = "unknown"
        ios_version = "unknown"

        try:
            name = tunnel_device.get_value(key="DeviceName") or name
        except Exception:
            pass

        try:
            props = tunnel_device.peer_info.get("Properties", {})
            model = props.get("ProductType", model)
            ios_version = props.get("OSVersion", ios_version)
            if name == "iPhone":
                name = props.get("DeviceClass", name)
        except Exception:
            pass

        try:
            if model == "unknown":
                model = getattr(tunnel_device, "product_type", model)
            if ios_version == "unknown":
                ios_version = getattr(tunnel_device, "product_version", ios_version)
        except Exception:
            pass

        self._device_info = {
            "name": name,
            "model": model,
            "ios_version": ios_version,
            "udid": udid,
            "connection_type": "tunnel (WiFi)",
        }
        logger.info("Connected via developer tunnel (tunnel addr: %s)", self._tunnel_address)

    def _connect_via_usbmux(self, udid: str):
        from pymobiledevice3.lockdown import create_using_usbmux

        self._lockdown = create_using_usbmux(serial=udid)
        self._current_udid = udid
        self._device_info = {
            "name": self._lockdown.get_value(key="DeviceName"),
            "model": self._lockdown.get_value(key="ProductType"),
            "ios_version": self._lockdown.product_version,
            "udid": self._lockdown.identifier,
            "connection_type": "usbmux",
        }

    def _connect_dvt_with_rsd(self, rsd):
        from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import (
            DvtSecureSocketProxyService,
        )
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

        self._dvt = DvtSecureSocketProxyService(lockdown=rsd)
        self._dvt.__enter__()
        self._screenshot_service = Screenshot(self._dvt)
        logger.info("DVT screenshot service connected via RSD")

    def _connect_dvt_tunnel(self):
        from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import (
            DvtSecureSocketProxyService,
        )
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
        from pymobiledevice3.tunneld.api import get_tunneld_devices

        tunneld_devices = get_tunneld_devices()
        if not tunneld_devices:
            raise ConnectionError("No tunnel found. Start tunneld first.")

        rsd = None
        for device in tunneld_devices:
            if hasattr(device, "udid") and device.udid == self._current_udid:
                rsd = device
                break
        if rsd is None:
            rsd = tunneld_devices[0]

        self._rsd = rsd
        self._dvt = DvtSecureSocketProxyService(lockdown=rsd)
        self._dvt.__enter__()
        self._screenshot_service = Screenshot(self._dvt)
        logger.info("DVT screenshot service connected via tunnel")

    def _connect_dvt_direct(self):
        from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import (
            DvtSecureSocketProxyService,
        )
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

        self._dvt = DvtSecureSocketProxyService(lockdown=self._lockdown)
        self._dvt.__enter__()
        self._screenshot_service = Screenshot(self._dvt)
        logger.info("DVT screenshot service connected (direct)")

    def take_screenshot(self) -> bytes:
        if not self._screenshot_service:
            self._ensure_dvt()
        return self._screenshot_service.get_screenshot()

    def _ensure_dvt(self):
        if self._screenshot_service:
            return
        if self._rsd is not None:
            self._connect_dvt_with_rsd(self._rsd)
        else:
            self._connect_dvt_tunnel()
        logger.info("DVT screenshot service connected (on-demand)")

    def get_battery_info(self) -> dict:
        try:
            if self._lockdown:
                from pymobiledevice3.services.diagnostics import DiagnosticsService
                diag = DiagnosticsService(self._lockdown)
                battery = diag.get_battery()
                return {
                    "level": battery.get("CurrentCapacity", -1),
                    "charging": battery.get("IsCharging", False),
                }
        except Exception as e:
            logger.debug("Battery info failed: %s", e)
        return {"level": -1, "charging": False}

    def get_wda_url(self) -> str:
        if self._tunnel_address:
            return f"http://[{self._tunnel_address}]:8100"
        return "http://localhost:8100"

    def _mount_developer_image(self) -> bool:
        if self._dev_image_mounted:
            return True
        if not self._current_udid or self._current_udid == "tunnel":
            return False
        try:
            cmd = [
                sys.executable, "-m", "pymobiledevice3",
                "mounter", "auto-mount",
                "--tunnel", self._current_udid,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.info("Developer image mounted")
                self._dev_image_mounted = True
                return True
            if "already mounted" in (result.stdout + result.stderr).lower():
                logger.info("Developer image already mounted")
                self._dev_image_mounted = True
                return True
            logger.warning("Developer image mount output: %s", result.stderr or result.stdout)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.error("Developer image mount timed out")
            return False
        except Exception as e:
            logger.error("Developer image mount failed: %s", e)
            return False

    def start_wda(self, auth_token: str | None = None) -> bool:
        if self._wda_proc and self._wda_proc.poll() is None:
            logger.info("WDA already running")
            return True

        if not self._current_udid or self._current_udid == "tunnel":
            logger.warning("No device UDID for WDA")
            return False

        logger.info("Starting WDA via xcuitest (device: %s)...", self._current_udid)

        cmd = [
            sys.executable, "-m", "pymobiledevice3",
            "developer", "dvt", "xcuitest",
            self.WDA_BUNDLE_ID,
            "--tunnel", self._current_udid,
            "--env", "MJPEG_SERVER_SCREENSHOT_QUALITY=55",
            "--env", "MJPEG_SCALING_FACTOR=50",
            "--env", "MJPEG_SERVER_FRAMERATE=12",
        ]
        if auth_token:
            cmd.extend(["--env", f"WDA_AUTH_TOKEN={auth_token}"])
            logger.info("WDA will require auth token")

        try:
            self._wda_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(5)

            if self._wda_proc.poll() is not None:
                output = self._wda_proc.stdout.read().decode()
                logger.error("WDA xcuitest exited early: %s", output[-500:])
                self._wda_proc = None
                return False

            logger.info("WDA xcuitest started (PID %d)", self._wda_proc.pid)
            return True
        except Exception as e:
            logger.error("Failed to start WDA: %s", e)
            return False

    def stop_wda(self):
        if not self._wda_proc:
            return
        import os
        import signal
        proc = self._wda_proc
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception as e:
            logger.debug("killpg SIGTERM failed: %s", e)
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._wda_proc = None
        logger.info("WDA xcuitest stopped")

    def is_wda_running(self) -> bool:
        return self._wda_proc is not None and self._wda_proc.poll() is None

    def _handle_disconnect(self):
        self.disconnect()
        self.device_disconnected.emit()

    def disconnect(self):
        self.stop_wda()

        with self._lock:
            if self._dvt:
                try:
                    self._dvt.__exit__(None, None, None)
                except Exception:
                    pass
                self._dvt = None
                self._screenshot_service = None

            if self._rsd:
                try:
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(self._rsd.close())
                        else:
                            loop.run_until_complete(self._rsd.close())
                    except RuntimeError:
                        asyncio.run(self._rsd.close())
                except Exception:
                    pass
                self._rsd = None

            self._lockdown = None
            self._current_udid = None
            self._tunnel_address = None
            self._device_info = {}

        self._set_state(ConnectionState.DISCONNECTED)
        logger.info("Disconnected from device")

    def cleanup(self):
        self.stop_discovery()
        self.disconnect()

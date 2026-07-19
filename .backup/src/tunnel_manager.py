import getpass
import logging
import os
import shlex
import subprocess
import time

import requests

logger = logging.getLogger(__name__)

TUNNELD_URL = "http://127.0.0.1:49151"
TUNNELD_TIMEOUT = 1.5
DAEMON_LABEL = "com.sabsteef.iphonemirror.tunneld"
DAEMON_PLIST_PATH = f"/Library/LaunchDaemons/{DAEMON_LABEL}.plist"
DAEMON_LOG_PATH = "/var/log/iphonemirror-tunneld.log"
SUDOERS_PATH = "/etc/sudoers.d/iphonemirror"
LAUNCHCTL = "/bin/launchctl"

SYSTEM_PYTHONS = [
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
]
PYMD3_VERSION = "7.8.3"


def is_tunneld_running() -> bool:
    try:
        resp = requests.get(TUNNELD_URL, timeout=TUNNELD_TIMEOUT)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def is_service_installed() -> bool:
    return os.path.exists(DAEMON_PLIST_PATH)


def is_sudoers_installed() -> bool:
    return os.path.exists(SUDOERS_PATH)


def is_fully_installed() -> bool:
    return is_service_installed() and is_sudoers_installed()


def wait_until_ready(timeout: float = 25.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_tunneld_running():
            return True
        time.sleep(interval)
    return False


def wait_until_stopped(timeout: float = 5.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_tunneld_running():
            return True
        time.sleep(interval)
    return False


def _find_system_python() -> str | None:
    for path in SYSTEM_PYTHONS:
        if os.path.exists(path) and not path.startswith("/Volumes/"):
            return path
    return None


def _pymd3_available(python_path: str) -> bool:
    try:
        result = subprocess.run(
            [python_path, "-c",
             f"import pymobiledevice3; import sys; "
             f"sys.exit(0 if pymobiledevice3.__version__.startswith('{PYMD3_VERSION[:3]}') else 1)"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _make_plist(python_path: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{DAEMON_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>-i</string>
        <string>PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <string>HOME=/var/root</string>
        <string>{python_path}</string>
        <string>-m</string>
        <string>pymobiledevice3</string>
        <string>remote</string>
        <string>tunneld</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/tmp</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{DAEMON_LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>{DAEMON_LOG_PATH}</string>
</dict>
</plist>
"""


def _make_sudoers(username: str) -> str:
    return (
        f"# iPhone Mirror tunnel service — allow load/unload without password\n"
        f"{username} ALL=(root) NOPASSWD: {LAUNCHCTL} load {DAEMON_PLIST_PATH}\n"
        f"{username} ALL=(root) NOPASSWD: {LAUNCHCTL} unload {DAEMON_PLIST_PATH}\n"
    )


def install_service() -> tuple[bool, str]:
    python_path = _find_system_python()
    if not python_path:
        return False, (
            "Geen system Python gevonden.\n"
            "Installeer via: brew install python@3.14"
        )

    username = getpass.getuser()
    tmp_plist = "/tmp/iphonemirror-tunneld.plist"
    tmp_sudoers = "/tmp/iphonemirror-sudoers"

    try:
        with open(tmp_plist, "w") as f:
            f.write(_make_plist(python_path))
        with open(tmp_sudoers, "w") as f:
            f.write(_make_sudoers(username))
    except Exception as e:
        return False, f"Kon config niet schrijven: {e}"

    site_packages = os.path.dirname(os.path.dirname(python_path)) + "/lib/python3.14/site-packages"
    needs_pymd3 = not _pymd3_available(python_path)
    steps = ["cd /tmp"]
    if needs_pymd3:
        steps.append(
            f"{shlex.quote(python_path)} -m pip install --break-system-packages "
            f"--target={shlex.quote(site_packages)} --upgrade "
            f"pymobiledevice3=={PYMD3_VERSION}"
        )
    steps.extend([
        f"cp {shlex.quote(tmp_plist)} {shlex.quote(DAEMON_PLIST_PATH)}",
        f"chown root:wheel {shlex.quote(DAEMON_PLIST_PATH)}",
        f"chmod 644 {shlex.quote(DAEMON_PLIST_PATH)}",
        f"cp {shlex.quote(tmp_sudoers)} {shlex.quote(SUDOERS_PATH)}",
        f"chown root:wheel {shlex.quote(SUDOERS_PATH)}",
        f"chmod 440 {shlex.quote(SUDOERS_PATH)}",
        f"visudo -cf {shlex.quote(SUDOERS_PATH)}",
    ])
    cmd = " && ".join(steps)
    script = f'do shell script "{cmd}" with administrator privileges'

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0:
            logger.info("Service installed (python=%s)", python_path)
            return True, ""
        err = result.stderr.strip() or "unknown error"
        if "-128" in err:
            return False, "Geannuleerd door gebruiker"
        return False, err
    except subprocess.TimeoutExpired:
        return False, "Time-out bij installeren"
    except Exception as e:
        return False, str(e)


def uninstall_service() -> tuple[bool, str]:
    if not is_service_installed() and not is_sudoers_installed():
        return True, "Was al gedeïnstalleerd"
    stop_service()
    cmd = (
        f"rm -f {shlex.quote(DAEMON_PLIST_PATH)} {shlex.quote(SUDOERS_PATH)}"
    )
    script = f'do shell script "{cmd}" with administrator privileges'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, ""
        err = result.stderr.strip() or "unknown error"
        if "-128" in err:
            return False, "Geannuleerd door gebruiker"
        return False, err
    except Exception as e:
        return False, str(e)


def start_service() -> tuple[bool, str]:
    if is_tunneld_running():
        return True, "Draaide al"
    if not is_fully_installed():
        return False, "Service niet geïnstalleerd"
    try:
        result = subprocess.run(
            ["sudo", "-n", LAUNCHCTL, "load", DAEMON_PLIST_PATH],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            if wait_until_ready(timeout=15.0):
                return True, ""
            return False, "Daemon geladen maar tunnel niet ready"
        return False, result.stderr.strip() or "load failed"
    except Exception as e:
        return False, str(e)


def stop_service() -> tuple[bool, str]:
    if not is_service_installed():
        return True, "Service niet geïnstalleerd"
    try:
        result = subprocess.run(
            ["sudo", "-n", LAUNCHCTL, "unload", DAEMON_PLIST_PATH],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            wait_until_stopped(timeout=3.0)
            return True, ""
        return False, result.stderr.strip() or "unload failed"
    except Exception as e:
        return False, str(e)

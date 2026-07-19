"""Subprocess capture worker — owns its own DVT connection and GIL.

Streams length-prefixed screenshot frames to stdout. Run as:
    python -m src.capture_worker <UDID>

Protocol (stdout, binary):
    b"READY\\n"                      — connection established
    [4-byte big-endian uint32 len]   — frame byte count
    [len bytes of image data]        — TIFF/PNG from device
    (repeat)
"""
import struct
import sys
import time

MAX_DVT_RETRIES = 5
DVT_RETRY_DELAY = 2.0


def _log(msg: str) -> None:
    sys.stderr.write(f"capture_worker: {msg}\n")
    sys.stderr.flush()


def _find_rsd(udid: str | None):
    from pymobiledevice3.tunneld.api import get_tunneld_devices

    devices = get_tunneld_devices()
    _log(f"found {len(devices)} tunnel device(s)")

    if udid:
        for d in devices:
            if getattr(d, "udid", None) == udid:
                return d

    return devices[0] if devices else None


def _connect_dvt(rsd):
    from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import (
        DvtSecureSocketProxyService,
    )
    from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

    dvt = DvtSecureSocketProxyService(lockdown=rsd)
    dvt.__enter__()
    return dvt, Screenshot(dvt)


def main() -> None:
    udid = sys.argv[1] if len(sys.argv) > 1 else None

    dvt = None
    screenshot_svc = None

    for attempt in range(1, MAX_DVT_RETRIES + 1):
        rsd = _find_rsd(udid)
        if rsd is None:
            _log("no tunnel device found")
            sys.exit(1)

        _log(f"attempt {attempt}/{MAX_DVT_RETRIES}: DVT to {getattr(rsd, 'udid', '?')}")
        try:
            dvt, screenshot_svc = _connect_dvt(rsd)
            _log("DVT connected")
            break
        except Exception as e:
            _log(f"DVT failed: {e}")
            if attempt < MAX_DVT_RETRIES:
                time.sleep(DVT_RETRY_DELAY)

    if screenshot_svc is None:
        _log("all DVT attempts exhausted")
        sys.exit(1)

    sys.stdout.buffer.write(b"READY\n")
    sys.stdout.buffer.flush()

    try:
        while True:
            data = screenshot_svc.get_screenshot()
            header = struct.pack(">I", len(data))
            sys.stdout.buffer.write(header)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if dvt:
            try:
                dvt.__exit__(None, None, None)
            except Exception:
                pass


if __name__ == "__main__":
    main()

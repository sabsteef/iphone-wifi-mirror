"""Subprocess DVT screenshot fallback (v9-async).

Runs in its own process to keep pymobiledevice3's DVT calls off the Qt event
loop. Uses the v9 :class:`UserspaceRsdTunnel` — no sudo, no tunneld daemon —
and streams raw screenshot frames to stdout, length-prefixed.

Usage:
    python -m src.capture_worker <UDID>

Protocol (stdout, binary):
    b"READY\\n"                      — connection established
    [4-byte big-endian uint32 len]   — frame byte count
    [len bytes of image data]        — TIFF/PNG from device
    (repeat)

Only used as fallback when MJPEG via WDA is unavailable — expected FPS is
low (2-3 over WiFi) because DVT screenshot is inherently slow.
"""
from __future__ import annotations

import asyncio
import struct
import sys

MAX_DVT_RETRIES = 5
DVT_RETRY_DELAY = 2.0


def _log(msg: str) -> None:
    sys.stderr.write(f"capture_worker: {msg}\n")
    sys.stderr.flush()


async def _run(udid: str) -> None:
    """Open tunnel → DVT → Screenshot and stream frames to stdout."""
    from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

    async with UserspaceRsdTunnel(serial=udid) as rsd:
        _log(f"tunnel up: {rsd.udid} ({rsd.product_type}, iOS {rsd.product_version})")
        async with DvtProvider(rsd) as dvt:
            async with Screenshot(dvt) as svc:
                _log("DVT screenshot channel open")
                sys.stdout.buffer.write(b"READY\n")
                sys.stdout.buffer.flush()
                try:
                    while True:
                        data = await svc.get_screenshot()
                        header = struct.pack(">I", len(data))
                        sys.stdout.buffer.write(header)
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                except (BrokenPipeError, KeyboardInterrupt):
                    pass


async def main_async(udid: str) -> None:
    for attempt in range(1, MAX_DVT_RETRIES + 1):
        _log(f"attempt {attempt}/{MAX_DVT_RETRIES}: opening tunnel + DVT for {udid}")
        try:
            await _run(udid)
            return
        except Exception as e:
            _log(f"failed: {type(e).__name__}: {e}")
            if attempt < MAX_DVT_RETRIES:
                await asyncio.sleep(DVT_RETRY_DELAY)
    _log("all DVT attempts exhausted")
    sys.exit(1)


def main() -> None:
    udid = sys.argv[1] if len(sys.argv) > 1 else None
    if not udid:
        _log("usage: capture_worker <UDID>")
        sys.exit(2)
    asyncio.run(main_async(udid))


if __name__ == "__main__":
    main()

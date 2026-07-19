"""Subprocess worker for WDA MJPEG stream.

Reads multipart/x-mixed-replace JPEG stream from WDA and forwards each
frame to stdout as length-prefixed bytes.

Protocol (stdout, binary):
    b"READY\\n"                      — connection established
    [4-byte big-endian uint32 len]   — frame byte count
    [len bytes of JPEG data]         — one JPEG frame
    (repeat)
"""
import socket
import struct
import sys
import time

BOUNDARY = b"--BoundaryString"
CONNECT_RETRIES = 30
CONNECT_RETRY_DELAY = 1.0
READ_BUFFER = 65536


def _log(msg: str) -> None:
    sys.stderr.write(f"mjpeg_worker: {msg}\n")
    sys.stderr.flush()


def _connect(host: str, port: int) -> socket.socket | None:
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            addrs = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except Exception as e:
            _log(f"getaddrinfo failed: {e}")
            time.sleep(CONNECT_RETRY_DELAY)
            continue

        for family, kind, proto, _cname, sockaddr in addrs:
            try:
                s = socket.socket(family, kind, proto)
                s.settimeout(5)
                s.connect(sockaddr)
                s.settimeout(30)
                _log(f"connected to {sockaddr}")
                return s
            except Exception as e:
                _log(f"attempt {attempt}: connect {sockaddr} failed: {e}")

        time.sleep(CONNECT_RETRY_DELAY)
    return None


def _read_headers(sock: socket.socket, buf: bytearray) -> bytearray:
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(READ_BUFFER)
        if not chunk:
            raise ConnectionError("connection closed while reading headers")
        buf.extend(chunk)
    return buf


def _fill(sock: socket.socket, buf: bytearray, at_least: int) -> None:
    while len(buf) < at_least:
        chunk = sock.recv(READ_BUFFER)
        if not chunk:
            raise ConnectionError("connection closed")
        buf.extend(chunk)


def main() -> None:
    if len(sys.argv) < 3:
        _log("usage: mjpeg_capture_worker <host> <port>")
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2])

    sock = _connect(host, port)
    if not sock:
        _log("all connect attempts exhausted")
        sys.exit(1)

    # WDA MJPEG server requires the client to send *something* before it starts
    # streaming. A tiny GET request is sufficient.
    try:
        sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
    except Exception as e:
        _log(f"initial send failed: {e}")
        sys.exit(1)

    buf = bytearray()
    try:
        _read_headers(sock, buf)
    except Exception as e:
        _log(f"reading initial HTTP headers failed: {e}")
        sys.exit(1)

    # Drop the initial HTTP response headers up to and including the double CRLF.
    header_end = buf.index(b"\r\n\r\n") + 4
    del buf[:header_end]

    sys.stdout.buffer.write(b"READY\n")
    sys.stdout.buffer.flush()

    try:
        while True:
            # Advance to the next boundary
            while True:
                idx = buf.find(BOUNDARY)
                if idx != -1:
                    del buf[: idx + len(BOUNDARY)]
                    break
                chunk = sock.recv(READ_BUFFER)
                if not chunk:
                    raise ConnectionError("stream ended")
                buf.extend(chunk)

            # Skip optional trailing \r\n after boundary
            while len(buf) < 2:
                _fill(sock, buf, 2)
            if buf[:2] == b"\r\n":
                del buf[:2]

            # Read part headers up to \r\n\r\n
            while b"\r\n\r\n" not in buf:
                _fill(sock, buf, len(buf) + 1)

            headers_end = buf.index(b"\r\n\r\n") + 4
            header_block = bytes(buf[:headers_end])
            del buf[:headers_end]

            # Extract Content-Length
            content_length = None
            for line in header_block.split(b"\r\n"):
                lower = line.lower()
                if lower.startswith(b"content-length:"):
                    try:
                        content_length = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        pass
                    break

            if content_length is None:
                _log(f"missing content-length in headers: {header_block[:200]!r}")
                continue

            _fill(sock, buf, content_length)
            jpeg = bytes(buf[:content_length])
            del buf[:content_length]

            sys.stdout.buffer.write(struct.pack(">I", len(jpeg)))
            sys.stdout.buffer.write(jpeg)
            sys.stdout.buffer.flush()

    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as e:
        _log(f"stream error: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

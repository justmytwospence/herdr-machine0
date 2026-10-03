"""Minimal herdr socket API client (stdlib only): one JSON line each way."""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any, Dict, Optional

MAX_REPLY_BYTES = 8 * 1024 * 1024


class Unavailable(Exception):
    """The herdr socket is missing, refused, timed out, or answered garbage."""


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def socket_path() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


def exchange(path: str, line: bytes, timeout: float = 5.0) -> bytes:
    """Send one request line, return the reply line (without the newline)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(line if line.endswith(b"\n") else line + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_REPLY_BYTES:
                raise Unavailable("reply too large")
    except OSError as e:
        raise Unavailable(str(e))
    finally:
        sock.close()
    reply = buf.split(b"\n", 1)[0]
    if not reply:
        raise Unavailable("empty reply")
    return reply


def call(method: str, params: Dict[str, Any], path: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]:
    request = {"id": "machine0:%s:%d" % (method, time.time_ns()), "method": method, "params": params}
    raw = exchange(path or socket_path(), json.dumps(request).encode(), timeout)
    try:
        reply = json.loads(raw.decode())
    except ValueError as e:
        raise Unavailable("unreadable reply: %s" % e)
    error = reply.get("error")
    if error:
        raise HerdrError(str(error.get("code", "error")), str(error.get("message", "")))
    return reply.get("result") or {}


def quiet(method: str, params: Dict[str, Any], path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        return call(method, params, path)
    except (Unavailable, HerdrError):
        return None


def set_tokens(pane_id: str, tokens: Dict[str, Optional[str]], path: Optional[str] = None) -> None:
    quiet("pane.report_metadata", {
        "pane_id": pane_id,
        "source": "spoke:wrapper",
        "seq": time.time_ns(),
        "tokens": tokens,
    }, path)


def notify(title: str, body: str = "", path: Optional[str] = None, sound: str = "none") -> None:
    quiet("notification.show", {"title": title, "body": body, "sound": sound}, path)

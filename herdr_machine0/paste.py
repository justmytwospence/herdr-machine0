"""Paste bridge: hub file paths inside a bracketed paste become spoke paths.

herdr pastes a clipboard image by saving it to a temp file on the server host
(the hub) and pasting the path; Heeler stages attachments over SFTP the same
way. The agent runs on the spoke, where that path does not exist. Inside each
bracketed paste, every absolute path to an existing regular file under one of
the allowed hub directories is copied to the spoke and rewritten.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Callable, Iterable, List, Optional, Tuple

START = b"\x1b[200~"
END = b"\x1b[201~"
MAX_BUFFER = 1 << 20
# Absolute paths, optionally quoted; stops at whitespace and quotes.
PATH_RE = re.compile(rb"(?<![\w./~-])(/[^\s'\"`]+)")

Uploader = Callable[[str, str], None]  # (hub_path, spoke_path)


def allowed(path: str, dirs: Iterable[str]) -> bool:
    real = os.path.realpath(path)
    for d in dirs:
        root = os.path.realpath(os.path.expanduser(d))
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True
    return False


def rewrite(text: bytes, dirs: Iterable[str], dest_dir: str, upload: Uploader, max_bytes: int) -> bytes:
    """Rewrite paths in one paste body. Failed uploads leave the path untouched."""
    dirs = list(dirs)

    def sub(m: "re.Match[bytes]") -> bytes:
        raw = m.group(1)
        try:
            path = raw.decode()
        except UnicodeDecodeError:
            return raw
        # A trailing punctuation mark is usually not part of the path.
        trail = ""
        while path and path[-1] in ".,;:)" and not os.path.isfile(path):
            trail = path[-1] + trail
            path = path[:-1]
        if not os.path.isfile(path) or not allowed(path, dirs):
            return raw
        try:
            if os.path.getsize(path) > max_bytes:
                return raw
            dest = "%s/%s-%s" % (dest_dir.rstrip("/"), uuid.uuid4().hex[:8], os.path.basename(path))
            upload(path, dest)
        except Exception:
            return raw
        return (dest + trail).encode()

    return PATH_RE.sub(sub, text)


class PasteFilter:
    """Feed raw input chunks; get back the bytes to forward."""

    def __init__(self, transform: Callable[[bytes], bytes]):
        self.transform = transform
        self.buf: Optional[bytearray] = None  # inside a paste when not None
        self.pending = b""  # a possibly split START marker

    def feed(self, data: bytes) -> bytes:
        out: List[bytes] = []
        data = self.pending + data
        self.pending = b""
        while data:
            if self.buf is None:
                i = data.find(START)
                if i < 0:
                    keep = _partial_suffix(data, START)
                    out.append(data[:len(data) - keep])
                    self.pending = data[len(data) - keep:]
                    break
                out.append(data[:i + len(START)])
                data = data[i + len(START):]
                self.buf = bytearray()
            else:
                self.buf += data
                j = self.buf.find(END)
                if j < 0:
                    if len(self.buf) > MAX_BUFFER:
                        # Too large to be a path paste: pass it through untouched,
                        # keeping a tail that may hold a split END marker.
                        keep = len(END) - 1
                        out.append(bytes(self.buf[:-keep]))
                        self.buf = bytearray(self.buf[-keep:])
                    break
                body, rest = bytes(self.buf[:j]), bytes(self.buf[j + len(END):])
                out.append(self.transform(body) + END)
                self.buf = None
                data = rest
        return b"".join(out)


def _partial_suffix(data: bytes, marker: bytes) -> int:
    for n in range(min(len(marker) - 1, len(data)), 0, -1):
        if data.endswith(marker[:n]):
            return n
    return 0


def split_paths(text: bytes) -> List[Tuple[int, int]]:
    return [m.span(1) for m in PATH_RE.finditer(text)]

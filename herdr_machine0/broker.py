"""Hub-side access to subscription credentials (see broker/broker.mjs)."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, Optional

from . import config

SENTINEL = "herdr-machine0-broker"
PI_PACKAGE = "@earendil-works/pi-coding-agent"


class BrokerError(Exception):
    pass


# A private SDK copy for hosts whose pi is a compiled binary with no JavaScript
# inside (exe.dev's exeuntu build): the dotfiles' chezmoi setup installs it here on the hub.
SDK_DIR = os.path.expanduser("~/.local/share/herdr-machine0/pi-sdk/node_modules/" + PI_PACKAGE)


def _is_pi_package(path: str) -> bool:
    """An importable pi SDK: the package, with its JavaScript entry point."""
    try:
        with open(os.path.join(path, "package.json")) as f:
            if json.load(f).get("name") != PI_PACKAGE:
                return False
    except (OSError, ValueError):
        return False
    return os.path.isfile(os.path.join(path, "dist", "index.js"))


def find_pi_package() -> str:
    """The installed pi package directory (for the SDK the broker imports)."""
    override = config.settings().get("pi_package_dir")
    if override and _is_pi_package(os.path.expanduser(override)):
        return os.path.expanduser(override)
    if _is_pi_package(SDK_DIR):
        return SDK_DIR
    binary = shutil.which("pi")
    if not binary:
        raise BrokerError("pi is not on PATH")
    candidates = [os.path.realpath(binary)]
    # Homebrew-style shell shims exec the real entry point.
    try:
        with open(candidates[0], "rb") as f:
            head = f.read(4096).decode(errors="ignore")
        for m in re.finditer(r'exec\s+"?([^"\s]+)', head):
            candidates.append(os.path.realpath(m.group(1)))
    except OSError:
        pass
    for start in candidates:
        d = os.path.dirname(start)
        for _ in range(8):
            for probe in (d, os.path.join(d, "lib", "node_modules", PI_PACKAGE),
                          os.path.join(d, "node_modules", PI_PACKAGE)):
                if _is_pi_package(probe):
                    return probe
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    raise BrokerError("cannot locate the pi package; set pi_package_dir in config.json")


def _script() -> str:
    return os.path.join(config.PLUGIN_ROOT, "broker", "broker.mjs")


def min_validity_hours(provider: str) -> float:
    value = config.settings()["credential_min_validity_h"]
    if isinstance(value, dict):
        return float(value.get(provider, value.get("default", 24)))
    return float(value)


def get(provider: str, min_validity_ms: Optional[int] = None) -> Dict[str, Any]:
    """A credential valid for at least min_validity_ms, refresh token replaced."""
    if config.role() != "hub":
        raise BrokerError("the broker only runs on the hub")
    if min_validity_ms is None:
        min_validity_ms = int(min_validity_hours(provider) * 3600 * 1000)
    os.makedirs(config.BROKER_DIR, mode=0o700, exist_ok=True)
    lock = open(os.path.join(config.BROKER_DIR, ".lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        proc = subprocess.run(
            ["node", _script(), find_pi_package(), config.BROKER_DIR, "get", provider, str(min_validity_ms)],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    if proc.returncode != 0:
        raise BrokerError(proc.stderr.strip() or "broker failed")
    cred = json.loads(proc.stdout)
    if cred.get("refresh") != SENTINEL:
        raise BrokerError("broker returned a refresh token; refusing")
    return cred


def status() -> Dict[str, Any]:
    path = os.path.join(config.BROKER_DIR, "auth.json")
    try:
        with open(path) as f:
            store = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time() * 1000
    return {
        k: {"type": v.get("type"), "hours_left": round((v.get("expires", now) - now) / 3.6e6, 1)
            if v.get("type") == "oauth" else None}
        for k, v in store.items() if isinstance(v, dict)
    }


def access_token(provider: str) -> str:
    return str(get(provider, 10 * 60 * 1000)["access"])

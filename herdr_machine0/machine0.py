"""Thin wrapper over the machine0 CLI (`--json` everywhere it exists)."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Dict, List, Optional

RUNNING = "RUNNING"
SUSPENDED = "SUSPENDED"
STOPPED = "STOPPED"
# States a wrapper may wake from with `machine0 start`.
WAKEABLE = frozenset({SUSPENDED, STOPPED, "UNAVAILABLE", "ERRORED"})


class Machine0Error(Exception):
    pass


def run(args: List[str], timeout: float = 600, input: Optional[str] = None) -> str:
    try:
        proc = subprocess.run(
            ["machine0"] + args, capture_output=True, text=True, timeout=timeout, input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise Machine0Error("machine0 CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise Machine0Error("machine0 %s timed out" % " ".join(args))
    if proc.returncode != 0:
        raise Machine0Error((proc.stderr or proc.stdout).strip() or "machine0 %s failed" % args[0])
    return proc.stdout


def run_json(args: List[str], timeout: float = 120) -> Any:
    out = run(args + ["--json"], timeout=timeout)
    try:
        return json.loads(out)
    except ValueError:
        raise Machine0Error("machine0 %s: unreadable JSON" % " ".join(args))


def machines() -> List[Dict[str, Any]]:
    data = run_json(["ls"])
    if isinstance(data, dict):
        data = data.get("machines") or data.get("vms") or []
    return [m for m in data if isinstance(m, dict)]


def get(name: str) -> Optional[Dict[str, Any]]:
    from . import config
    static = config.static_spoke(name)
    if static:
        return {"name": name, "status": RUNNING, "ip": static.get("host"), "size": "static"}
    for m in machines():
        if m.get("name") == name:
            return m
    return None


def status(machine: Optional[Dict[str, Any]]) -> str:
    return str((machine or {}).get("status") or "MISSING").upper()


def ip(machine: Optional[Dict[str, Any]]) -> Optional[str]:
    value = (machine or {}).get("ip")
    return str(value) if value else None


def new(name: str, size: str, region: str, image: Optional[str], key: str, profile: Optional[str]) -> None:
    """image=None lets machine0 pick (GPU sizes get their GPU base image)."""
    args = ["new", name, "-s", size, "-r", region, "-k", key]
    if image:
        args += ["-i", image]
    if profile:
        args += ["--profile", profile]
    run(args, timeout=1800)


def start(name: str) -> None:
    run(["start", name], timeout=1800)


def suspend(name: str) -> None:
    run(["suspend", name, "-y"], timeout=1800)


def destroy(name: str) -> None:
    run(["rm", name, "-y"], timeout=600)


def wait_running(name: str, timeout: float = 1800, poll: float = 10) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        m = get(name)
        if status(m) == RUNNING and ip(m):
            return m  # type: ignore[return-value]
        if status(m) in ("ERRORED", "UNAVAILABLE", "MISSING"):
            raise Machine0Error("%s is %s" % (name, status(m)))
        if time.time() > deadline:
            raise Machine0Error("timed out waiting for %s" % name)
        time.sleep(poll)

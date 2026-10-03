"""The hub's record of spokes and slots, plus per-slot single-owner locks.

registry.json:
  spokes: {name: {size, region, created, keep_awake, idle_since, harness}}
  slots:  {"<spoke>/<slot>": {spoke, slot, harness, cwd, pane_id, session}}
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import time
from typing import Any, Dict, Iterator, Optional

from . import config


def _path() -> str:
    return config.state_path("registry.json")


@contextlib.contextmanager
def locked() -> Iterator[Dict[str, Any]]:
    """Read-modify-write the registry under an exclusive lock."""
    lock = open(config.state_path("registry.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load()
        yield data
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, _path())
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def load() -> Dict[str, Any]:
    try:
        with open(_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("spokes", {})
    data.setdefault("slots", {})
    return data


def slot_key(spoke: str, slot: str) -> str:
    return "%s/%s" % (spoke, slot)


def get_slot(spoke: str, slot: str) -> Optional[Dict[str, Any]]:
    return load()["slots"].get(slot_key(spoke, slot))


def put_slot(spoke: str, slot: str, **fields: Any) -> Dict[str, Any]:
    with locked() as data:
        entry = data["slots"].setdefault(slot_key(spoke, slot), {"spoke": spoke, "slot": slot})
        entry.update({k: v for k, v in fields.items() if v is not None})
        return dict(entry)


def put_spoke(name: str, **fields: Any) -> Dict[str, Any]:
    with locked() as data:
        entry = data["spokes"].setdefault(name, {"created": int(time.time())})
        entry.update(fields)
        return dict(entry)


def drop_spoke(name: str) -> None:
    with locked() as data:
        data["spokes"].pop(name, None)
        for key in [k for k, v in data["slots"].items() if v.get("spoke") == name]:
            data["slots"].pop(key)


def next_slot(spoke: str, base: str = "main") -> str:
    slots = {v.get("slot") for v in load()["slots"].values() if v.get("spoke") == spoke}
    if base not in slots:
        return base
    n = 2
    while "%s-%d" % (base, n) in slots:
        n += 1
    return "%s-%d" % (base, n)


class SlotBusy(Exception):
    def __init__(self, pid: int):
        super().__init__("slot is attached by pid %d" % pid)
        self.pid = pid


class SlotLock:
    """One wrapper per slot. The lock file holds the owner's pid."""

    def __init__(self, spoke: str, slot: str):
        self.path = config.state_path("locks", "%s__%s.lock" % (spoke, slot))
        self.f = None  # type: Optional[Any]

    def owner(self) -> Optional[int]:
        try:
            with open(self.path) as f:
                return int(f.read().strip() or 0) or None
        except (OSError, ValueError):
            return None

    def acquire(self, takeover: bool = False, wait: float = 5.0) -> None:
        self.f = open(self.path, "a+")
        deadline = time.time() + wait
        signalled = False
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pid = self.owner() or 0
                if not takeover:
                    self.f.close()
                    self.f = None
                    raise SlotBusy(pid)
                if pid and not signalled:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    signalled = True
                if time.time() > deadline:
                    self.f.close()
                    self.f = None
                    raise SlotBusy(pid)
                time.sleep(0.2)
        self.f.seek(0)
        self.f.truncate()
        self.f.write(str(os.getpid()))
        self.f.flush()

    def release(self) -> None:
        if self.f:
            try:
                self.f.seek(0)
                self.f.truncate()
                fcntl.flock(self.f, fcntl.LOCK_UN)
            finally:
                self.f.close()
                self.f = None

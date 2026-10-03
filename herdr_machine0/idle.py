"""The auto-suspend rule, kept pure for tests.

A spoke is idle when every slot is idle or done, no slot carries an
attention-queue `bg` (background work will wake it) or `activity` token, its
15-minute load is under the threshold (no build or dev server burning CPU),
and keep-awake is off. It is suspended once it has been idle that long.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

QUIET_STATES = frozenset({"idle", "done"})


def slot_quiet(slot: Dict[str, Any]) -> bool:
    status = slot.get("agent_status")
    tokens = slot.get("tokens") or {}
    if status not in QUIET_STATES:
        return False
    if tokens.get("bg") or tokens.get("activity"):
        return False
    return True


def idle_now(slots: Iterable[Dict[str, Any]], load15: Optional[float], keep_awake: bool,
             load_threshold: float) -> Tuple[bool, str]:
    if keep_awake:
        return False, "keep-awake"
    slots = list(slots)
    busy = [s.get("slot", "?") for s in slots if not slot_quiet(s)]
    if busy:
        return False, "busy: " + ", ".join(sorted(busy))
    if load15 is None:
        return False, "load unknown"
    if load15 >= load_threshold:
        return False, "load %.2f" % load15
    return True, "idle"


def decide(idle: bool, idle_since: Optional[float], now: float, idle_minutes: float) -> Tuple[bool, Optional[float]]:
    """(suspend?, new idle_since)."""
    if not idle:
        return False, None
    since = idle_since if idle_since is not None else now
    return now - since >= idle_minutes * 60, since


def parse_loadavg(text: str) -> Optional[float]:
    try:
        return float(text.split()[2])
    except (IndexError, ValueError):
        return None

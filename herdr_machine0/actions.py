"""herdr plugin actions. herdr passes the invoking context in HERDR_PLUGIN_CONTEXT_JSON."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional, Tuple

from . import config, herdr, hub, machine0, registry


def focused_slot() -> Optional[Tuple[str, str]]:
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    pane_id = ctx.get("pane_id") or os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        return None
    pane = herdr.call("pane.get", {"pane_id": pane_id}).get("pane") or {}
    cwd = os.path.realpath(pane.get("cwd") or "")
    for s in registry.load()["slots"].values():
        if os.path.realpath(config.slot_dir(s["spoke"], s["slot"])) == cwd:
            return s["spoke"], s["slot"]
    return None


def detached(*args: str) -> None:
    log = config.state_path("logs", "actions.log")
    with open(log, "a") as out:
        subprocess.Popen([sys.executable, "-B", os.path.join(config.PLUGIN_ROOT, "spoke.py")] + list(args),
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                         env=dict(os.environ))


def run(action: str) -> int:
    if action == "reconcile":
        done = hub.reconcile(include_resumable=True)
        herdr.notify("Reattached %d slot(s)" % len(done), ", ".join(done))
        return 0
    if action == "status":
        reg = registry.load()
        states = {m.get("name"): machine0.status(m).lower() for m in machine0.machines()}
        body = ", ".join("%s %s" % (n, states.get(n, "?")) for n in sorted(reg["spokes"])) or "no spokes"
        herdr.notify("Spokes", body)
        return 0
    target = focused_slot()
    if not target:
        herdr.notify("Not a spoke pane", "focus a spoke slot first")
        return 0
    spoke, _slot = target
    if action == "suspend-focused":
        herdr.notify("Suspending %s" % spoke)
        detached("suspend", spoke)
    elif action == "wake-focused":
        herdr.notify("Waking %s" % spoke, "its panes reconnect when it is up")
        detached("wake", spoke)
    elif action == "keep-awake":
        on = not bool(registry.load()["spokes"].get(spoke, {}).get("keep_awake"))
        registry.put_spoke(spoke, keep_awake=on, idle_since=None)
        herdr.notify("%s keep-awake %s" % (spoke, "on" if on else "off"))
    else:
        print("unknown action %s" % action, file=sys.stderr)
        return 2
    return 0

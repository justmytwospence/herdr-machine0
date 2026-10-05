"""Interactive hub UI: the new-agent popup and the board pane."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import termios
import time
import tty
from typing import List, Optional

from . import config, hub, machine0, registry


def ask(prompt: str, default: str = "") -> str:
    try:
        value = input("%s%s: " % (prompt, " [%s]" % default if default else "")).strip()
    except EOFError:
        raise SystemExit(1)
    return value or default


def choose(title: str, options: List[str]) -> int:
    print(title)
    for i, opt in enumerate(options, 1):
        print("  %d) %s" % (i, opt))
    while True:
        raw = ask("choice", "1")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        if raw in ("q", "Q"):
            raise SystemExit(0)


def _new_space(label: str) -> tuple:
    result = hub.herdr.call("workspace.create", {"cwd": os.path.expanduser("~"), "label": label, "focus": True})
    return result["workspace"]["workspace_id"], result["root_pane"]["pane_id"]


def main() -> int:
    """prefix+N: a repo's space (or a new worktree tab in it), a new repo spoke, or a scratch spoke."""
    from . import autospoke, lifecycle, picker, repos, space
    choice = picker.pick("open")
    if not choice or choice["kind"] == "hub":
        return 0
    if choice["kind"] == "scratch" or not choice.get("spoke"):
        repo = choice.get("repo")
        name = repos.spoke_name(repo) if repo else autospoke.new_name(registry.load()["spokes"])
        # Registered before the space exists, so the new-space hook leaves it alone.
        registry.put_spoke(name, pending=True, **({"repo": repo} if repo else {}))
        workspace, pane = _new_space(name)
        hub.run_in_pane(pane, space.start_new(workspace, name, repo))
        return 0
    spoke = choice["spoke"]
    branch = ask("new worktree branch on %s (empty: open its main checkout)" % spoke)
    if branch:
        rc = lifecycle.add_worktree(spoke, branch)
        if rc:
            ask("press Enter to close")
        return rc
    workspace = hub.spoke_workspace(spoke)
    if workspace:
        hub.herdr.quiet("workspace.focus", {"workspace_id": workspace})
        return 0
    workspace, pane = _new_space(spoke)
    hub.run_in_pane(pane, space.claim(workspace, spoke))
    return 0


def board() -> int:
    """A refreshing overview; q closes it."""
    old = termios.tcgetattr(0) if os.isatty(0) else None
    if old:
        tty.setcbreak(0)
    try:
        while True:
            lines = ["\x1b[2J\x1b[H  herdr-machine0 spokes   (q closes)\n"]
            reg = registry.load()
            try:
                states = {m.get("name"): m for m in machine0.machines()}
            except machine0.Machine0Error as e:
                states = {}
                lines.append("  machine0: %s\n" % e)
            now = time.time()
            for name, info in sorted(reg["spokes"].items()):
                m = states.get(name)
                idle = info.get("idle_since")
                lines.append("  %-16s %-10s %-10s %s%s\n" % (
                    name, machine0.status(m).lower(), (m or {}).get("size") or info.get("size") or "-",
                    "idle %dm" % ((now - idle) / 60) if idle else "",
                    "  keep-awake" if info.get("keep_awake") else ""))
                for s in sorted(v["slot"] for v in reg["slots"].values() if v.get("spoke") == name):
                    slot = reg["slots"][registry.slot_key(name, s)]
                    lines.append("      %-14s %-9s %s\n" % (s, slot.get("harness", "-"), slot.get("cwd", "-")))
            sys.stdout.write("".join(lines))
            sys.stdout.flush()
            r, _, _ = select.select([0], [], [], 10)
            if r and os.read(0, 16).strip().lower().startswith(b"q"):
                return 0
    finally:
        if old:
            termios.tcsetattr(0, termios.TCSADRAIN, old)

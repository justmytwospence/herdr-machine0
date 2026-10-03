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


def main() -> int:
    cfg = config.settings()
    reg = registry.load()
    try:
        states = {m.get("name"): machine0.status(m) for m in machine0.machines()}
    except machine0.Machine0Error as e:
        print("machine0: %s" % e)
        states = {}
    names = sorted(reg["spokes"])
    options = ["%s (%s)" % (n, states.get(n, "?").lower()) for n in names] + ["+ new spoke"]
    pick = choose("New agent on which spoke? (q quits)", options)
    if pick == len(names):
        name = ask("spoke name")
        size = ask("size", cfg["default_size"])
        repos = ask("repos to clone (owner/repo, space separated)", "").split()
        harness = ask("harness (%s)" % "/".join(config.HARNESSES), cfg["default_harness"])
        argv = [os.path.join(config.PLUGIN_ROOT, "spoke.py"), "new", name, "--size", size, "--harness", harness]
        for r in repos:
            argv += ["--repo", r]
        log = config.state_path("logs", "new-%s.log" % name)
        with open(log, "a") as out:
            subprocess.Popen([sys.executable, "-B"] + argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                             start_new_session=True, env=dict(os.environ))
        print("creating %s in the background (log: %s); its pane opens when it is ready" % (name, log))
        time.sleep(2)
        return 0
    spoke = names[pick]
    default_harness = reg["spokes"][spoke].get("harness") or cfg["default_harness"]
    harness = ask("harness (%s)" % "/".join(config.HARNESSES), default_harness)
    if harness not in config.HARNESSES:
        print("unknown harness")
        return 1
    main_cwd = (reg["slots"].get(registry.slot_key(spoke, "main")) or {}).get("cwd") or "~"
    cwd = ask("directory on %s" % spoke, main_cwd)
    label = ask("label", harness)
    slot = registry.next_slot(spoke, label)
    hub.open_slot(spoke, slot, harness, cwd, focus=True)
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

"""Which repo a new hub space is for (also the popup's first question).

Draws on the terminal (fzf when installed, a numbered menu otherwise) and
returns a choice; stdout stays free for `pick-space`'s shell code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from . import machine0, repos

SCRATCH = "+ scratch spoke (no repo)"
HUB = "· plain hub shell"


def _states() -> Dict[str, str]:
    try:
        return {m.get("name"): machine0.status(m).lower() for m in machine0.machines()}
    except machine0.Machine0Error:
        return {}


def entries() -> List[Dict[str, Any]]:
    states = _states()
    out = []
    for c in repos.choices():
        if c["spoke"]:
            label = "● %-28s %s · %s" % (repos.short(c["repo"]), c["repo"], states.get(c["spoke"], "?"))
        else:
            label = "○ %-28s %s  %s" % (repos.short(c["repo"]), c["repo"], c.get("description") or "")
        out.append(dict(c, label=label.rstrip()))
    return out


def _tty() -> Any:
    return open("/dev/tty", "r+") if os.path.exists("/dev/tty") else None


def pick(prompt: str = "repo") -> Optional[Dict[str, Any]]:
    """{"kind": "repo", "repo", "spoke"} | {"kind": "scratch"} | {"kind": "hub"} | None (cancelled)."""
    items = entries()
    labels = [i["label"] for i in items] + [SCRATCH, HUB]
    if shutil.which("fzf"):
        proc = subprocess.run(
            ["fzf", "--prompt", "%s> " % prompt, "--print-query", "--layout=reverse", "--height=60%",
             "--header", "Enter opens; type owner/repo for one not listed; Esc keeps a plain hub shell",
             "--no-sort"],
            input="\n".join(labels), stdout=subprocess.PIPE, text=True)
        if proc.returncode not in (0, 1):  # 130: Esc / Ctrl-C
            return None
        lines = proc.stdout.split("\n")
        query, selected = (lines + ["", ""])[:2]
    else:
        tty = _tty() or sys.stderr
        tty.write("\nOpen which repo?\n")
        for n, label in enumerate(labels, 1):
            tty.write("  %2d) %s\n" % (n, label))
        tty.write("number, or owner/repo (empty keeps a plain hub shell): ")
        tty.flush()
        answer = (tty.readline() if hasattr(tty, "readline") and tty is not sys.stderr else input()).strip()
        query, selected = answer, ""
        if answer.isdigit() and 1 <= int(answer) <= len(labels):
            selected = labels[int(answer) - 1]
        elif not answer:
            return {"kind": "hub"}
    if selected == SCRATCH:
        return {"kind": "scratch"}
    if selected == HUB:
        return {"kind": "hub"}
    for item in items:
        if item["label"] == selected:
            return {"kind": "repo", "repo": item["repo"], "spoke": item["spoke"]}
    typed = repos.normalize(query)
    if typed:
        return {"kind": "repo", "repo": typed, "spoke": repos.spoke_for(typed)}
    return None

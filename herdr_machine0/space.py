"""`spoke pick-space`: what a freshly opened hub space becomes.

The new-space hook types `eval "$(spoke pick-space --workspace <id>)"` into the
space's pane. This asks which repo the space is for, does the herdr side (names
the space, focuses an already open one), and prints the shell code that puts
the pane in the right directory and starts the right command: herdr restores a
pane in its shell's directory, which is how slots are found again after a
restart, so the shell itself has to `cd` there.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import List, Optional

from . import autospoke, config, herdr, hub, picker, registry, repos

KEEP_HUB = "cd ~"


def _q(*parts: str) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def _rename(workspace_id: str, label: str) -> None:
    herdr.quiet("workspace.rename", {"workspace_id": workspace_id, "label": label})
    tabs = herdr.quiet("tab.list", {"workspace_id": workspace_id}) or {}
    first = (tabs.get("tabs") or [{}])[0].get("tab_id")
    if first:
        herdr.quiet("tab.rename", {"tab_id": first, "label": "main"})


def claim(workspace_id: str, spoke: str) -> str:
    """Make this space the spoke's: its main slot here, its other slots as tabs."""
    _rename(workspace_id, spoke)
    registry.put_spoke(spoke, workspace_id=workspace_id)
    slots = [s for s in registry.load()["slots"].values() if s.get("spoke") == spoke]
    main = next((s for s in slots if s.get("slot") == "main"), {"slot": "main"})
    for s in slots:
        if s is not main:
            hub.open_slot(spoke, s["slot"], s.get("harness") or config.settings()["default_harness"],
                          s.get("cwd"))
    harness = main.get("harness") or registry.load()["spokes"].get(spoke, {}).get("harness") \
        or config.settings()["default_harness"]
    attach = ["spoke", "attach", spoke, "main", "--harness", harness]
    if main.get("cwd"):
        attach += ["--cwd", main["cwd"]]
    return "cd %s && %s" % (shlex.quote(config.slot_dir(spoke, "main")), _q(*attach))


def start_new(workspace_id: str, spoke: str, repo: Optional[str]) -> str:
    registry.put_spoke(spoke, pending=True, workspace_id=workspace_id, **({"repo": repo} if repo else {}))
    _rename(workspace_id, spoke)
    argv = ["spoke", "new", spoke, "--in-pane"]
    if repo:
        argv += ["--repo", repo]
    return "cd %s && %s || cd ~" % (shlex.quote(config.slot_dir(spoke, "main")), _q(*argv))


def pick_space(workspace_id: str) -> str:
    choice = picker.pick("new space")
    if not choice or choice["kind"] == "hub":
        herdr.quiet("workspace.rename", {"workspace_id": workspace_id, "label": "hub"})
        return KEEP_HUB
    if choice["kind"] == "scratch":
        return start_new(workspace_id, autospoke.new_name(registry.load()["spokes"]), None)
    repo, spoke = choice["repo"], choice.get("spoke")
    if not spoke:
        return start_new(workspace_id, repos.spoke_name(repo), repo)
    open_elsewhere = hub.spoke_workspace(spoke)
    if open_elsewhere and open_elsewhere != workspace_id:
        # The repo already has its space: go there, and drop this one.
        herdr.quiet("workspace.focus", {"workspace_id": open_elsewhere})
        herdr.quiet("workspace.close", {"workspace_id": workspace_id})
        return KEEP_HUB
    return claim(workspace_id, spoke)


def main(workspace_id: Optional[str]) -> int:
    workspace_id = workspace_id or os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace_id:
        print(KEEP_HUB)
        return 0
    try:
        print(pick_space(workspace_id))
    except Exception as e:  # never leave the user's new shell in a broken state
        print("spoke pick-space: %s" % e, file=sys.stderr)
        print(KEEP_HUB)
    return 0

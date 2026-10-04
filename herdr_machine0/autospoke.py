"""New herdr space on the hub -> new spoke.

The plugin's `workspace.created` hook runs `spoke on-workspace-created`. A
space the user opens (prefix+c, the sidebar, the API) gets a fresh spoke: the
hook names one, renames the space after it, and types `spoke new <name>
--in-pane` into the space's pane, which shows a short grace period (any key
keeps a plain hub shell), creates the VM, and turns the pane into the spoke's
main slot.

Not every new space is the user's: herdr-machine0 opens spaces for spokes it
creates itself (labelled with a registered spoke name), and herdr worktrees open
spaces too. Restoring a session after a restart emits no `workspace.created`.
"""

from __future__ import annotations

import json
import os
import random
import shlex
from typing import Any, Dict, Optional

from . import config, herdr, registry

ADJECTIVES = (
    "amber", "brisk", "calm", "clever", "crisp", "dapper", "eager", "fleet", "gentle", "glad",
    "hardy", "jolly", "keen", "lively", "lucid", "mellow", "nimble", "plucky", "quiet", "rapid",
    "sly", "spry", "steady", "sunny", "swift", "tidy", "vivid", "witty", "zesty", "bold",
)
NOUNS = (
    "badger", "beaver", "bison", "crane", "falcon", "ferret", "finch", "heron", "ibex", "lynx",
    "marten", "moose", "newt", "ocelot", "otter", "owl", "panda", "puffin", "quail", "raven",
    "robin", "seal", "shrike", "stoat", "swift", "tapir", "teal", "vole", "walrus", "wren",
)


def new_name(taken: Any, rng: Optional[random.Random] = None) -> str:
    rng = rng or random.Random()
    taken = set(taken)
    for _ in range(200):
        name = "%s-%s" % (rng.choice(ADJECTIVES), rng.choice(NOUNS))
        if name not in taken:
            return name
    n = 2
    while "spoke-%d" % n in taken:
        n += 1
    return "spoke-%d" % n


def should_handle(workspace: Dict[str, Any], spokes: Any, enabled: bool) -> bool:
    """A space the user opened, not one herdr-machine0 or a worktree opened."""
    if not enabled:
        return False
    if workspace.get("worktree"):
        return False
    if workspace.get("label") in set(spokes):
        return False
    if int(workspace.get("pane_count") or 1) != 1 or int(workspace.get("tab_count") or 1) != 1:
        return False
    return True


def root_pane(workspace_id: str, path: Optional[str] = None) -> Optional[str]:
    for pane in herdr.call("pane.list", {}, path).get("panes") or []:
        if pane.get("workspace_id") == workspace_id:
            return pane.get("pane_id")
    return None


def handle(event_json: str, path: Optional[str] = None) -> Optional[str]:
    """The spoke name started for this event, or None when the space is left alone."""
    if config.role() != "hub":
        return None
    try:
        event = json.loads(event_json or "{}")
    except ValueError:
        return None
    workspace = ((event.get("data") or {}).get("workspace")) or {}
    workspace_id = workspace.get("workspace_id")
    if not workspace_id:
        return None
    cfg = config.settings()
    spokes = registry.load()["spokes"]
    if not should_handle(workspace, spokes, bool(cfg.get("auto_spoke_on_new_space", True))):
        return None
    pane_id = root_pane(workspace_id, path)
    if not pane_id:
        return None
    name = new_name(spokes)
    # Registering first makes the rename (and any other hook on this space) see
    # a spoke's space, not a user's.
    registry.put_spoke(name, pending=True, workspace_id=workspace_id)
    herdr.quiet("workspace.rename", {"workspace_id": workspace_id, "label": name}, path)
    # Kept as a hub shell (or failed): back to ~ rather than the unused slot dir.
    command = "cd %s && spoke new %s --in-pane || cd ~" % (
        shlex.quote(config.slot_dir(name, "main")), shlex.quote(name))
    herdr.call("pane.send_text", {"pane_id": pane_id, "text": command}, path)
    herdr.call("pane.send_keys", {"pane_id": pane_id, "keys": ["enter"]}, path)
    return name


def main() -> int:
    try:
        handle(os.environ.get("HERDR_PLUGIN_EVENT_JSON", ""))
    except (herdr.Unavailable, herdr.HerdrError):
        pass
    return 0

"""Hub-side herdr layout: one workspace per spoke, one pane per slot."""

from __future__ import annotations

import os
import shlex
from typing import Any, Dict, List, Optional

from . import config, herdr, registry


def attach_command(spoke: str, slot: str, harness: str, cwd: Optional[str]) -> str:
    argv = ["spoke", "attach", spoke, slot, "--harness", harness]
    if cwd:
        argv += ["--cwd", cwd]
    return " ".join(shlex.quote(a) for a in argv)


def run_in_pane(pane_id: str, command: str, path: Optional[str] = None) -> None:
    herdr.call("pane.send_text", {"pane_id": pane_id, "text": command}, path)
    herdr.call("pane.send_keys", {"pane_id": pane_id, "keys": ["enter"]}, path)


def workspaces(path: Optional[str] = None) -> List[Dict[str, Any]]:
    return herdr.call("workspace.list", {}, path).get("workspaces") or []


def spoke_workspace(spoke: str, path: Optional[str] = None) -> Optional[str]:
    """The spoke's space: the one recorded for it if still open, else by label."""
    spaces = workspaces(path)
    recorded = (registry.load()["spokes"].get(spoke) or {}).get("workspace_id")
    if recorded and any(ws.get("workspace_id") == recorded for ws in spaces):
        return recorded
    for ws in spaces:
        if ws.get("label") == spoke:
            return ws.get("workspace_id")
    return None


def open_slot(spoke: str, slot: str, harness: str, cwd: Optional[str],
              path: Optional[str] = None, focus: bool = False) -> str:
    """A new pane for slot (a workspace for the spoke's first slot, else a tab)."""
    pane_cwd = config.slot_dir(spoke, slot)
    ws = spoke_workspace(spoke, path)
    if ws is None:
        result = herdr.call("workspace.create", {"cwd": pane_cwd, "label": spoke, "focus": focus}, path)
        pane_id = result["root_pane"]["pane_id"]
        registry.put_spoke(spoke, workspace_id=result["workspace"]["workspace_id"])
    else:
        result = herdr.call("tab.create", {"workspace_id": ws, "cwd": pane_cwd, "label": slot, "focus": focus}, path)
        pane_id = (result.get("root_pane") or {}).get("pane_id") or _first_pane(result, path)
    registry.put_slot(spoke, slot, harness=harness, cwd=cwd, pane_id=pane_id)
    run_in_pane(pane_id, attach_command(spoke, slot, harness, cwd), path)
    return pane_id


def _first_pane(result: Dict[str, Any], path: Optional[str]) -> str:
    tab_id = (result.get("tab") or {}).get("tab_id")
    for pane in herdr.call("pane.list", {}, path).get("panes") or []:
        if pane.get("tab_id") == tab_id:
            return pane["pane_id"]
    raise herdr.HerdrError("not_found", "new tab has no pane")


def panes(path: Optional[str] = None) -> List[Dict[str, Any]]:
    return herdr.call("pane.list", {}, path).get("panes") or []


def foreground_is_shell(pane_id: str, path: Optional[str] = None) -> bool:
    info = herdr.call("pane.process_info", {"pane_id": pane_id}, path).get("process_info") or {}
    procs = info.get("foreground_processes") or []
    shell_pid = info.get("shell_pid")
    return not procs or all(p.get("pid") == shell_pid for p in procs)


def reconcile(path: Optional[str] = None, include_resumable: bool = False) -> List[str]:
    """Re-run `spoke attach` in restored panes that came back as plain shells.

    herdr restores a pane in its saved cwd; every slot pane's cwd is that slot's
    own directory, so the cwd identifies the slot without relying on pane ids.
    Never wakes a spoke: an attach to a suspended spoke shows its wake screen.
    """
    slots = registry.load()["slots"]
    by_dir = {os.path.realpath(config.slot_dir(s["spoke"], s["slot"])): s for s in slots.values()}
    restarted = []
    for pane in panes(path):
        cwd = os.path.realpath(pane.get("cwd") or "")
        slot = by_dir.get(cwd)
        if not slot:
            continue
        pane_id = pane["pane_id"]
        if pane.get("agent_status") not in (None, "unknown") or not foreground_is_shell(pane_id, path):
            continue
        harness = slot.get("harness") or config.settings()["default_harness"]
        # herdr resumes pi and opencode panes itself (the relay gave it a
        # resume_argv) once a client attaches; reattaching too would race it.
        if harness in ("pi", "opencode") and slot.get("session") and not include_resumable:
            continue
        registry.put_slot(slot["spoke"], slot["slot"], pane_id=pane_id)
        run_in_pane(pane_id, attach_command(slot["spoke"], slot["slot"], harness, slot.get("cwd")), path)
        restarted.append("%s/%s" % (slot["spoke"], slot["slot"]))
    return restarted


def spoke_panes(spoke: str, path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Agent records whose wrapper token names this spoke."""
    agents = herdr.call("agent.list", {}, path).get("agents") or []
    return [a for a in agents if str((a.get("tokens") or {}).get("spoke", "")).startswith(spoke + "/")]

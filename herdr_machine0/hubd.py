"""`spoke hubd`: the hub daemon. Started (once) by the plugin's startup hook.

Every check interval it applies the auto-suspend rule to each running spoke.
At start it reattaches slot panes that a herdr restart left as plain shells.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import config, herdr, hub, idle, machine0, registry, sshconf


def _pidfile() -> str:
    return config.state_path("hubd.pid")


def log(msg: str) -> None:
    with open(config.state_path("logs", "hubd.log"), "a") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))


def running_pid() -> Optional[int]:
    try:
        with open(_pidfile()) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def ensure() -> int:
    """Start the daemon detached unless it already runs (the startup hook calls this)."""
    if running_pid():
        return 0
    with open(config.state_path("logs", "hubd.out"), "a") as out:
        subprocess.Popen(
            [sys.executable, "-B", os.path.join(config.PLUGIN_ROOT, "spoke.py"), "hubd"],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True,
            env=dict(os.environ),
        )
    return 0


def slot_view(path: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    """spoke -> [{slot, agent_status, tokens}] for every hub pane showing one of its slots."""
    slots = registry.load()["slots"]
    by_dir = {os.path.realpath(config.slot_dir(s["spoke"], s["slot"])): s for s in slots.values()}
    tokens = {a.get("pane_id"): a.get("tokens") or {}
              for a in herdr.call("agent.list", {}, path).get("agents") or []}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for pane in hub.panes(path):
        slot = by_dir.get(os.path.realpath(pane.get("cwd") or ""))
        if not slot:
            continue
        out.setdefault(slot["spoke"], []).append({
            "slot": slot["slot"],
            "agent_status": pane.get("agent_status"),
            "tokens": tokens.get(pane.get("pane_id"), {}),
        })
    return out


def load15(spoke: str) -> Optional[float]:
    try:
        out = subprocess.run(
            config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", sshconf.alias(spoke), "cat /proc/loadavg"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    return idle.parse_loadavg(out.stdout) if out.returncode == 0 else None


def check(path: Optional[str]) -> None:
    cfg = config.settings()
    reg = registry.load()
    if not reg["spokes"]:
        return
    states = {m.get("name"): m for m in machine0.machines()}
    view = slot_view(path)
    now = time.time()
    for name, info in reg["spokes"].items():
        m = states.get(name)
        if machine0.status(m) != machine0.RUNNING:
            if info.get("idle_since") is not None:
                registry.put_spoke(name, idle_since=None)
            continue
        sshconf.update(name, machine0.ip(m) or "")
        is_idle, reason = idle.idle_now(view.get(name, []), load15(name),
                                        bool(info.get("keep_awake")), float(cfg["load_threshold"]))
        suspend, since = idle.decide(is_idle, info.get("idle_since"), now, float(cfg["idle_minutes"]))
        if since != info.get("idle_since"):
            registry.put_spoke(name, idle_since=since)
        if suspend:
            log("suspending %s (idle %d min)" % (name, (now - (since or now)) / 60))
            herdr.notify("Suspending %s" % name, "idle for %d minutes" % cfg["idle_minutes"], path)
            try:
                machine0.suspend(name)
                registry.put_spoke(name, idle_since=None)
            except machine0.Machine0Error as e:
                log("suspend %s failed: %s" % (name, e))
        elif reason != "idle":
            log("%s awake: %s" % (name, reason))


def main() -> int:
    if config.role() != "hub":
        print("hubd runs on the hub only", file=sys.stderr)
        return 2
    other = running_pid()
    if other and other != os.getpid():
        return 0
    with open(_pidfile(), "w") as f:
        f.write(str(os.getpid()))
    path = os.environ.get("HERDR_SOCKET_PATH") or herdr.socket_path()
    log("hubd started (pid %d, herdr %s)" % (os.getpid(), path))
    # Give herdr time to restore its layout before reattaching panes.
    time.sleep(10)
    try:
        restarted = hub.reconcile(path)
        if restarted:
            log("reattached %s" % ", ".join(restarted))
    except (herdr.Unavailable, herdr.HerdrError) as e:
        log("reconcile failed: %s" % e)
    interval = float(config.settings()["check_interval_s"])
    while True:
        try:
            check(path)
        except Exception as e:  # keep the daemon alive; the log says why
            log("check failed: %s" % e)
        time.sleep(interval)

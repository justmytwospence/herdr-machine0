"""`spoke`: command-line entry point for hub and spokes."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import config

MANAGED_MARKER = "// managed by herdr-machine0"


def need(role: str) -> None:
    if config.role() != role:
        print("this command runs on the %s (role here: %s)" % (role, config.role()), file=sys.stderr)
        sys.exit(2)


# ---- hub commands -------------------------------------------------------------


def cmd_attach(a: argparse.Namespace) -> int:
    from . import wrapper
    return wrapper.attach(a.spoke, a.slot, a.harness, a.cwd, a.takeover)


def cmd_new(a: argparse.Namespace) -> int:
    need("hub")
    from . import herdr, lifecycle
    from . import repos
    repo = repos.normalize(a.repo[0]) if a.repo else None
    if a.repo and not repo:
        print("not a GitHub repo: %s" % a.repo[0], file=sys.stderr)
        return 2
    name = a.name or (repo and (repos.spoke_for(repo) or repos.spoke_name(repo)))
    if not name:
        print("spoke new needs a name or --repo owner/repo", file=sys.stderr)
        return 2
    a.name = name
    if a.in_pane:
        return lifecycle.new_in_pane(name, a.size, a.harness, repo=repo)
    try:
        rc = lifecycle.new(name, a.size, a.repo[1:] if a.repo else [], a.harness, focus=not a.no_focus,
                           repo=repo)
    except Exception as e:
        herdr.notify("spoke new %s failed" % a.name, str(e)[:200])
        raise
    if rc == 0:
        herdr.notify("%s is ready" % a.name, "its pane is open")
    return rc


def cmd_pick_space(a: argparse.Namespace) -> int:
    need("hub")
    from . import space
    return space.main(a.workspace)


def cmd_worktree(a: argparse.Namespace) -> int:
    need("hub")
    from . import lifecycle
    return lifecycle.add_worktree(a.spoke, a.branch, a.harness, focus=not a.no_focus)


def cmd_repos(a: argparse.Namespace) -> int:
    need("hub")
    from . import machine0, registry, repos
    if a.action == "import":
        repos.store(json.load(sys.stdin))
    elif a.action == "refresh":
        running = [n for n in registry.load()["spokes"]
                   if machine0.status(machine0.get(n)) == machine0.RUNNING]
        if not any(repos.refresh_from(n) for n in running):
            print("no running spoke could list repos (gh); `spoke repos import` takes gh's JSON", file=sys.stderr)
            return 1
    for c in repos.choices():
        print("%-40s %s" % (c["repo"], c["spoke"] or ""))
    return 0


def cmd_on_workspace_created(a: argparse.Namespace) -> int:
    from . import autospoke
    return autospoke.main()


def cmd_rm(a: argparse.Namespace) -> int:
    need("hub")
    from . import lifecycle
    return lifecycle.rm(a.name, a.force)


def cmd_ls(a: argparse.Namespace) -> int:
    need("hub")
    from . import machine0, registry
    reg = registry.load()
    states = {m.get("name"): m for m in machine0.machines()}
    rows = []
    for name, info in sorted(reg["spokes"].items()):
        m = states.get(name)
        slots = sorted(s["slot"] for s in reg["slots"].values() if s.get("spoke") == name)
        idle = info.get("idle_since")
        rows.append({
            "name": name, "status": machine0.status(m), "ip": machine0.ip(m) or "-",
            "size": (m or {}).get("size") or info.get("size") or "-",
            "slots": ",".join(slots) or "-",
            "keep_awake": bool(info.get("keep_awake")),
            "idle_min": int((time.time() - idle) / 60) if idle else None,
        })
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    fmt = "%-16s %-11s %-16s %-12s %-6s %-8s %s"
    print(fmt % ("NAME", "STATUS", "IP", "SIZE", "AWAKE", "IDLE", "SLOTS"))
    for r in rows:
        print(fmt % (r["name"], r["status"], r["ip"], r["size"], "pin" if r["keep_awake"] else "-",
                     "%dm" % r["idle_min"] if r["idle_min"] is not None else "-", r["slots"]))
    return 0


def cmd_wake(a: argparse.Namespace) -> int:
    need("hub")
    from . import lifecycle
    return lifecycle.wake(a.name)


def cmd_suspend(a: argparse.Namespace) -> int:
    need("hub")
    from . import lifecycle
    return lifecycle.suspend(a.name)


def cmd_ssh(a: argparse.Namespace) -> int:
    need("hub")
    from . import machine0, sshconf
    m = machine0.get(a.name)
    if machine0.status(m) != machine0.RUNNING:
        print("%s is %s" % (a.name, machine0.status(m).lower()), file=sys.stderr)
        return 1
    sshconf.update(a.name, machine0.ip(m) or "")
    argv = config.ssh_base() + [sshconf.alias(a.name)] + a.command
    os.execvp(argv[0], argv)
    return 0


def cmd_keep_awake(a: argparse.Namespace) -> int:
    need("hub")
    from . import registry
    on = a.state != "off"
    registry.put_spoke(a.name, keep_awake=on, idle_since=None)
    print("%s keep-awake %s" % (a.name, "on" if on else "off"))
    return 0


def cmd_sync(a: argparse.Namespace) -> int:
    need("hub")
    from . import lifecycle, machine0, registry, sshconf
    names = [a.name] if a.name else []
    if a.running:
        running = {m.get("name") for m in machine0.machines() if machine0.status(m) == machine0.RUNNING}
        names = [n for n in registry.load()["spokes"] if n in running]
    rc = 0
    for name in names:
        m = machine0.get(name)
        if machine0.status(m) != machine0.RUNNING:
            print("%s is not running; skipped" % name, file=sys.stderr)
            continue
        sshconf.update(name, machine0.ip(m) or "")
        try:
            lifecycle.sync(name)
        except Exception as e:
            print("%s: %s" % (name, e), file=sys.stderr)
            rc = 1
    return rc


def cmd_image(a: argparse.Namespace) -> int:
    need("hub")
    from . import lifecycle
    return lifecycle.image_build(a.fresh)


def cmd_hubd(a: argparse.Namespace) -> int:
    from . import hubd
    return hubd.ensure() if a.ensure else hubd.main()


def cmd_reconcile(a: argparse.Namespace) -> int:
    need("hub")
    from . import hub
    for s in hub.reconcile(include_resumable=a.all):
        print("reattached %s" % s)
    return 0


def cmd_hub_status(a: argparse.Namespace) -> int:
    need("hub")
    from . import broker, hubd
    out: Dict[str, Any] = {"hubd_pid": hubd.running_pid()}
    try:
        with open("/proc/loadavg") as f:
            out["load"] = f.read().split()[:3]
    except OSError:
        pass
    ps = subprocess.run(["ps", "-eo", "rss=,comm="], capture_output=True, text=True).stdout
    rss: Dict[str, int] = {}
    for line in ps.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            rss[parts[1].strip()] = rss.get(parts[1].strip(), 0) + int(parts[0])
    out["rss_mb"] = {k: round(v / 1024) for k, v in sorted(rss.items(), key=lambda kv: -kv[1])[:8]}
    out["credentials"] = broker.status()
    print(json.dumps(out, indent=2))
    return 0


def cmd_secrets(a: argparse.Namespace) -> int:
    need("hub")
    from . import broker
    if a.action == "show":
        values = config.read_secrets()
        print(json.dumps({
            "secrets.env": {k: ("set (%d chars)" % len(v)) for k, v in sorted(values.items())},
            "pushed_to_spokes": sorted(config.spoke_secrets(values)),
            "broker": broker.status(),
        }, indent=2))
        return 0
    if a.action == "set":
        if not a.key:
            print("usage: spoke secrets set KEY  (the value is read from stdin or a prompt)", file=sys.stderr)
            return 2
        value = sys.stdin.read().strip() if not sys.stdin.isatty() else getpass.getpass("%s: " % a.key).strip()
        values = config.read_secrets()
        if value:
            values[a.key] = value
            # One setup-token serves both names.
            if a.key in ("ANTHROPIC_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
                values["ANTHROPIC_OAUTH_TOKEN"] = values["CLAUDE_CODE_OAUTH_TOKEN"] = value
        else:
            values.pop(a.key, None)
        config.write_secrets(values)
        print("%s %s" % (a.key, "set" if value else "removed"))
        return 0
    if a.action == "login":
        os.makedirs(config.BROKER_DIR, mode=0o700, exist_ok=True)
        print("A pi session opens on the broker's credential store.\n"
              "Run /login for: openai-codex (OpenAI ChatGPT), radius, anthropic (Claude Pro/Max).\n"
              "Use the device-code or paste-the-URL options; then /quit.\n"
              "Never use these logins anywhere else: the broker must be the only refresher.\n")
        env = dict(os.environ, PI_CODING_AGENT_DIR=config.BROKER_DIR)
        env.pop("HERDR_ENV", None)
        return subprocess.call(["pi", "--no-session"], env=env)
    return 2


def cmd_token(a: argparse.Namespace) -> int:
    need("hub")
    from . import broker
    print(broker.access_token(a.provider))
    return 0


def cmd_usage(a: argparse.Namespace) -> int:
    if config.role() == "hub":
        from . import usage
        print(json.dumps(usage.get(a.name)))
        return 0
    from . import spokerun
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        return 1
    print(json.dumps(spokerun.relay_call(sock, "herdr_machine0.usage", {"name": a.name}, timeout=20)))
    return 0


def cmd_popup(a: argparse.Namespace) -> int:
    need("hub")
    from . import popup
    return popup.main()


def cmd_board(a: argparse.Namespace) -> int:
    need("hub")
    from . import popup
    return popup.board()


def cmd_action(a: argparse.Namespace) -> int:
    need("hub")
    from . import actions
    return actions.run(a.id)


def cmd_setup(a: argparse.Namespace) -> int:
    if a.target == "hub":
        from . import setup
        return setup.setup_hub()
    return subprocess.call(["bash", os.path.join(config.PLUGIN_ROOT, "setup", "spoke.sh")])


def cmd_doctor(a: argparse.Namespace) -> int:
    from . import setup
    return setup.doctor()


def cmd_role(a: argparse.Namespace) -> int:
    if a.role:
        os.makedirs(config.CONFIG_DIR, exist_ok=True)
        with open(config.ROLE_FILE, "w") as f:
            f.write(a.role + "\n")
    print(config.role())
    return 0


# ---- spoke commands -----------------------------------------------------------


def cmd_run(a: argparse.Namespace) -> int:
    from . import spokerun
    providers = [p for p in (a.providers or "").split(",") if p]
    return spokerun.run(a.slot, a.harness, a.cwd, a.pane, a.session, a.tcp_port, providers)


def cmd_open_slot(a: argparse.Namespace) -> int:
    from . import spokerun
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if os.environ.get("HERDR_MACHINE0_ROLE") != "spoke" or not sock:
        print("spoke open-slot runs inside a spoke slot", file=sys.stderr)
        return 2
    params: Dict[str, Any] = {"cwd": os.path.abspath(os.path.expanduser(a.cwd)), "label": a.label or "",
                              "focus": a.focus}
    if a.harness:
        params["harness"] = a.harness
    result = spokerun.relay_call(sock, "herdr_machine0.open_slot", params, timeout=60)
    print("opened %s" % result.get("slot"))
    return 0


def ensure_attention_bridge() -> None:
    """herdr-attention-queue installs its pi bridge from its herdr startup hook,
    which never runs on a spoke (no herdr server); install it here instead, when
    a checkout of it is around ($HERDR_MACHINE0_ATTENTION_QUEUE, or a sibling)."""
    candidates = [os.environ.get("HERDR_MACHINE0_ATTENTION_QUEUE", ""),
                  os.path.join(os.path.dirname(config.PLUGIN_ROOT), "herdr-attention-queue")]
    root = next((c for c in candidates
                 if c and os.path.isfile(os.path.join(c, "attention_queue", "pi_bridge.py"))), None)
    if not root:
        return
    sys.path.insert(0, root)
    try:
        from attention_queue import pi_bridge  # type: ignore
        print("attention-queue pi bridge: %s" % pi_bridge.ensure(root))
    except Exception as e:
        print("attention-queue pi bridge not installed: %s" % e, file=sys.stderr)
    finally:
        sys.path.remove(root)


def cmd_install_pi_extension(a: argparse.Namespace) -> int:
    ensure_attention_bridge()
    src = os.path.join(config.PLUGIN_ROOT, "pi", "herdr-machine0.ts")
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or os.path.expanduser("~/.pi/agent")
    dest = os.path.join(agent_dir, "extensions", "herdr-machine0.ts")
    with open(src) as f:
        body = MANAGED_MARKER + "; rewritten by `spoke install-pi-extension`.\n" + f.read()
    try:
        with open(dest) as f:
            current = f.read()
        if not current.startswith(MANAGED_MARKER):
            print("%s exists and is not managed by herdr-machine0; left alone" % dest, file=sys.stderr)
            return 1
        if current == body:
            return 0
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    print("installed %s" % dest)
    return 0


# ---- parser -------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spoke", description="herdr hub with machine0 spokes")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("attach", help="show a spoke slot in this herdr pane (hub)")
    s.add_argument("spoke")
    s.add_argument("slot", nargs="?", default="main")
    s.add_argument("--harness", choices=config.HARNESSES)
    s.add_argument("--cwd", help="working directory on the spoke")
    s.add_argument("--takeover", action="store_true")
    s.set_defaults(fn=cmd_attach)

    s = sub.add_parser("new", help="create a spoke from the golden image")
    s.add_argument("name", nargs="?", help="defaults to the repo's name with --repo")
    s.add_argument("--size")
    s.add_argument("--repo", action="append",
                   help="owner/repo the spoke is for (cloned to ~/Projects/<repo>); more are cloned beside it")
    s.add_argument("--harness", choices=config.HARNESSES)
    s.add_argument("--no-focus", action="store_true")
    s.add_argument("--in-pane", action="store_true", help="create here, then become its main slot")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("on-workspace-created", help="(hook) turn a new hub space into a spoke")
    s.set_defaults(fn=cmd_on_workspace_created)

    s = sub.add_parser("pick-space", help="(hook) ask which repo a new space is for; prints shell code")
    s.add_argument("--workspace")
    s.set_defaults(fn=cmd_pick_space)

    s = sub.add_parser("worktree", help="a worktree of a repo spoke, as a new tab of its space")
    s.add_argument("spoke")
    s.add_argument("branch")
    s.add_argument("--harness", choices=config.HARNESSES)
    s.add_argument("--no-focus", action="store_true")
    s.set_defaults(fn=cmd_worktree)

    s = sub.add_parser("repos", help="the repos new spaces can open")
    s.add_argument("action", nargs="?", choices=("list", "refresh", "import"), default="list")
    s.set_defaults(fn=cmd_repos)

    s = sub.add_parser("rm", help="destroy a spoke (refuses with unpushed work)")
    s.add_argument("name")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("ls", help="list spokes")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ls)

    for name, fn in (("wake", cmd_wake), ("suspend", cmd_suspend)):
        s = sub.add_parser(name)
        s.add_argument("name")
        s.set_defaults(fn=fn)

    s = sub.add_parser("ssh", help="ssh to a running spoke")
    s.add_argument("name")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_ssh)

    s = sub.add_parser("keep-awake", help="exempt a spoke from auto-suspend")
    s.add_argument("name")
    s.add_argument("state", nargs="?", choices=("on", "off"), default="on")
    s.set_defaults(fn=cmd_keep_awake)

    s = sub.add_parser("sync", help="pull dotfiles and restow on spokes")
    s.add_argument("name", nargs="?")
    s.add_argument("--running", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("image", help="golden image")
    isub = s.add_subparsers(dest="image_cmd", required=True)
    b = isub.add_parser("build")
    b.add_argument("--fresh", action="store_true", help="start from the machine0 base image")
    b.set_defaults(fn=cmd_image)

    s = sub.add_parser("hubd", help="hub daemon (auto-suspend, reattach)")
    s.add_argument("--ensure", action="store_true", help="start it detached unless running")
    s.set_defaults(fn=cmd_hubd)

    s = sub.add_parser("reconcile", help="reattach restored slot panes")
    s.add_argument("--all", action="store_true", help="include pi/opencode panes herdr would resume")
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("hub-status")
    s.set_defaults(fn=cmd_hub_status)

    s = sub.add_parser("secrets", help="hub secrets and the credential broker")
    s.add_argument("action", choices=("show", "set", "login"))
    s.add_argument("key", nargs="?")
    s.set_defaults(fn=cmd_secrets)

    s = sub.add_parser("token", help="print a fresh access token from the broker (hub)")
    s.add_argument("provider")
    s.set_defaults(fn=cmd_token)

    s = sub.add_parser("usage", help="cached plan usage JSON (claude|codex)")
    s.add_argument("name", choices=("claude", "codex"))
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("popup", help="new agent / new spoke picker (hub keybinding)")
    s.set_defaults(fn=cmd_popup)

    s = sub.add_parser("board", help="spoke overview (plugin pane)")
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("action", help="herdr plugin action")
    s.add_argument("id")
    s.set_defaults(fn=cmd_action)

    s = sub.add_parser("setup", help="install what herdr-machine0 needs on this hub or spoke")
    s.add_argument("target", choices=("hub", "spoke"))
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("doctor", help="check this hub (or spoke) and say how to fix gaps")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("role", help="show or set this machine's role")
    s.add_argument("role", nargs="?", choices=("hub", "spoke"))
    s.set_defaults(fn=cmd_role)

    s = sub.add_parser("run", help="(spoke) run a slot's agent in dtach")
    s.add_argument("slot")
    s.add_argument("--harness", required=True, choices=config.HARNESSES)
    s.add_argument("--cwd", required=True)
    s.add_argument("--pane", required=True)
    s.add_argument("--session")
    s.add_argument("--tcp-port", type=int)
    s.add_argument("--providers", default="")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("open-slot", help="(spoke) open another hub pane on this spoke")
    s.add_argument("--cwd", required=True)
    s.add_argument("--label")
    s.add_argument("--harness", choices=config.HARNESSES)
    s.add_argument("--focus", action="store_true")
    s.set_defaults(fn=cmd_open_slot)

    s = sub.add_parser("install-pi-extension", help="(spoke) install the managed pi extension")
    s.set_defaults(fn=cmd_install_pi_extension)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    return int(args.fn(args) or 0)

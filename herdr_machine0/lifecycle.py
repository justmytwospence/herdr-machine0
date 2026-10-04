"""Spoke lifecycle on the hub: new, rm, sync, wake, suspend, image build."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import config, herdr, hub, machine0, registry, sshconf

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
BUILDER = "m0-spoke-build"


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def remote(name: str, script: str, timeout: float = 3600, check: bool = True,
           capture: bool = False, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", sshconf.alias(name), "bash -lc " + shlex.quote(script)],
        timeout=timeout, capture_output=capture, input=input,
    )
    if check and proc.returncode != 0:
        raise RuntimeError("%s: remote command failed (%d)" % (name, proc.returncode))
    return proc


def wait_ssh(name: str, timeout: float = 600) -> None:
    deadline = time.time() + timeout
    while True:
        rc = subprocess.run(config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", sshconf.alias(name), "true"],
                            capture_output=True).returncode
        if rc == 0:
            return
        if time.time() > deadline:
            raise RuntimeError("ssh to %s did not come up" % name)
        time.sleep(5)


def bring_up(name: str) -> Dict[str, Any]:
    m = machine0.wait_running(name)
    sshconf.update(name, machine0.ip(m) or "")
    wait_ssh(name)
    return m


SYNC_SCRIPT = r"""
set -e
cd ~/dotfiles
git pull --rebase --autostash -q
git submodule sync --recursive -q
git submodule update --init --recursive -q
(cd plugins/pi-plan-mode && npm ci --omit=dev --no-audit --no-fund --loglevel=error)
~/dotfiles/shell/.local/bin/dotfiles-restow shell m0 || [ $? -eq 1 ]
~/dotfiles/shell/.local/bin/skills-install >/dev/null 2>&1 || true
~/.local/bin/spoke install-pi-extension
"""


def sync(name: str) -> None:
    say("syncing dotfiles on %s" % name)
    remote(name, SYNC_SCRIPT, timeout=1800)


def clone(name: str, repos: List[str]) -> List[str]:
    paths = []
    for repo in repos:
        short = repo.rstrip("/").split("/")[-1].replace(".git", "")
        dest = "~/Projects/%s" % short
        url = repo if "://" in repo or repo.startswith("git@") else repo
        remote(name, "mkdir -p ~/Projects && (test -d {d} || gh repo clone {u} {d} -- -q || git clone -q {u} {d})"
               .format(d=dest, u=shlex.quote(url)), timeout=1800)
        paths.append(dest)
    return paths


def create(name: str, size: Optional[str], repos: List[str], harness: Optional[str]) -> str:
    """Create, bring up and provision a spoke; returns the main slot's cwd."""
    cfg = config.settings()
    if not NAME_RE.match(name) or name == BUILDER:
        raise ValueError("spoke names are lowercase letters, digits and dashes")
    if machine0.get(name):
        raise ValueError("%s already exists" % name)
    size = size or cfg["default_size"]
    harness = harness or cfg["default_harness"]
    gpu = size.startswith("gpu-")
    region = cfg["gpu_region"] if gpu else cfg["region"]
    say("creating %s (%s, %s)" % (name, size, region))
    registry.put_spoke(name, size=size, region=region, harness=harness, keep_awake=False, idle_since=None)
    machine0.new(name, size, region, None if gpu else cfg["image"], cfg["ssh_key"], cfg["profile"])
    bring_up(name)
    if gpu:
        bootstrap(name)
    else:
        sync(name)
    paths = clone(name, repos)
    registry.put_spoke(name, pending=False)
    return paths[0] if paths else "~"


def new(name: str, size: Optional[str], repos: List[str], harness: Optional[str], focus: bool = True) -> int:
    try:
        cwd = create(name, size, repos, harness)
    except ValueError as e:
        say(str(e))
        return 1
    harness = harness or config.settings()["default_harness"]
    hub.open_slot(name, "main", harness, cwd, focus=focus)
    say("%s is up" % name)
    return 0


def _grace(seconds: int) -> bool:
    """Count down; True to go ahead, False when a key was pressed."""
    import select
    import termios
    import tty
    if not os.isatty(0):
        return True
    old = termios.tcgetattr(0)
    tty.setcbreak(0)
    try:
        for left in range(seconds, 0, -1):
            sys.stderr.write("\r  starting in %ds -- press any key to keep a plain hub shell " % left)
            sys.stderr.flush()
            r, _, _ = select.select([0], [], [], 1)
            if r:
                os.read(0, 64)
                return False
        return True
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)
        sys.stderr.write("\n")


def new_in_pane(name: str, size: Optional[str], harness: Optional[str], grace: int = 5) -> int:
    """`spoke new --in-pane`: the new-space hook's command. Creates the spoke in
    front of you, then turns this pane into the spoke's main slot."""
    cfg = config.settings()
    size = size or cfg["default_size"]
    harness = harness or cfg["default_harness"]
    say("\n  New space, new spoke: %s (%s, %s, %s)." % (name, size, cfg["region"], harness))
    if not _grace(grace):
        registry.drop_spoke(name)
        say("  Kept as a plain hub shell. `spoke new <name>` makes a spoke later.")
        return 0
    try:
        cwd = create(name, size, [], harness)
    except KeyboardInterrupt:
        say("\n  cancelled; removing %s" % name)
        try:
            if machine0.get(name):
                machine0.destroy(name)
        except machine0.Machine0Error as e:
            say("  could not destroy %s: %s (spoke rm %s --force)" % (name, e, name))
        sshconf.remove(name)
        registry.drop_spoke(name)
        return 130
    except Exception as e:
        say("\n  creating %s failed: %s\n  `spoke rm %s --force` cleans up." % (name, e, name))
        return 1
    registry.put_slot(name, "main", harness=harness, cwd=cwd, pane_id=os.environ.get("HERDR_PANE_ID"))
    argv = [sys.executable, "-B", os.path.join(config.PLUGIN_ROOT, "spoke.py"),
            "attach", name, "main", "--harness", harness, "--cwd", cwd]
    os.execv(argv[0], argv)
    return 0  # not reached


BOOTSTRAP_SCRIPT = r"""
set -e
test -d ~/dotfiles || git clone -q {url} ~/dotfiles
~/dotfiles/m0/bin/bootstrap-m0 --role spoke --host machine0
"""


def bootstrap(name: str) -> None:
    say("bootstrapping %s (this takes a while)" % name)
    remote(name, BOOTSTRAP_SCRIPT.format(url=shlex.quote(config.settings()["dotfiles_url"])), timeout=7200)


WORK_SCRIPT = r"""
shopt -s nullglob
for d in ~/Projects/*/ ~/Projects/*/.worktrees/*/; do
  [ -d "$d/.git" ] || [ -f "$d/.git" ] || continue
  cd "$d" || continue
  dirty=$(git status --porcelain 2>/dev/null | head -1)
  ahead=$(git log --oneline @{u}.. 2>/dev/null | head -1)
  noup=$(git rev-parse --abbrev-ref @{u} >/dev/null 2>&1 || echo noupstream)
  if [ -n "$dirty" ] || [ -n "$ahead" ] || [ -n "$noup" ]; then echo "$d ${dirty:+dirty }${ahead:+unpushed }$noup"; fi
done
"""


def unsaved_work(name: str) -> List[str]:
    proc = remote(name, WORK_SCRIPT, timeout=300, check=False, capture=True)
    if proc.returncode != 0:
        raise RuntimeError("could not inspect %s" % name)
    return [l for l in proc.stdout.decode().splitlines() if l.strip()]


def archive(name: str) -> str:
    dest = config.state_path("archive", "%s-%s.tgz" % (name, time.strftime("%Y%m%d-%H%M%S")))
    with open(dest, "wb") as out:
        subprocess.run(
            config.ssh_base() + ["-o", "BatchMode=yes", sshconf.alias(name),
             "cd ~ && tar czf - --ignore-failed-read .pi/agent/sessions .claude/projects .codex/sessions "
             ".local/share/opencode/storage 2>/dev/null"],
            stdout=out, timeout=1800,
        )
    return dest


def rm(name: str, force: bool) -> int:
    m = machine0.get(name)
    if m is None:
        say("%s does not exist on machine0; forgetting it" % name)
    elif machine0.status(m) == machine0.RUNNING:
        sshconf.update(name, machine0.ip(m) or "")
        try:
            work = unsaved_work(name)
        except RuntimeError as e:
            if not force:
                say("%s; use --force to destroy anyway" % e)
                return 1
            work = []
        if work and not force:
            say("%s has work that is not pushed:\n  %s\nPush it, or use --force." % (name, "\n  ".join(work)))
            return 1
        say("archived sessions to %s" % archive(name))
    elif not force:
        say("%s is %s; wake it so its work can be checked, or use --force" % (name, machine0.status(m).lower()))
        return 1
    if m is not None:
        machine0.destroy(name)
    ws = hub.spoke_workspace(name)
    if ws:
        herdr.quiet("workspace.close", {"workspace_id": ws, "close_group": True})
    sshconf.remove(name)
    registry.drop_spoke(name)
    say("%s removed" % name)
    return 0


def wake(name: str) -> int:
    machine0.start(name)
    bring_up(name)
    return 0


def suspend(name: str) -> int:
    machine0.suspend(name)
    registry.put_spoke(name, idle_since=None)
    return 0


SCRUB_SCRIPT = r"""
set -e
rm -f ~/.pi/agent/auth.json ~/.pi/agent/mcp-auth.json ~/.codex/auth.json ~/.claude/.credentials.json \
      ~/.config/herdr-machine0/secrets.env ~/.local/share/opencode/auth.json \
      ~/.zsh_history ~/.bash_history ~/.local/share/atuin/history.db
rm -rf ~/.pi/agent/sessions ~/.claude/projects ~/.codex/sessions \
       ~/.local/state/herdr-machine0/sock ~/.local/state/herdr-machine0/dtach ~/.local/state/herdr-machine0/paste
rm -rf ~/.config/gh/hosts.yml
"""


def image_build(fresh: bool) -> int:
    cfg = config.settings()
    images = machine0.run_json(["images", "ls"])
    have = any(i.get("name") == cfg["image"] for i in images if isinstance(i, dict))
    base = cfg["base_image"] if fresh or not have else cfg["image"]
    if machine0.get(BUILDER):
        say("removing a leftover %s" % BUILDER)
        machine0.destroy(BUILDER)
    say("building %s from %s" % (cfg["image"], base))
    machine0.new(BUILDER, "large", cfg["region"], base, cfg["ssh_key"], None)
    try:
        bring_up(BUILDER)
        if base == cfg["base_image"]:
            bootstrap(BUILDER)
        else:
            sync(BUILDER)
            remote(BUILDER, "~/dotfiles/m0/bin/bootstrap-m0 --role spoke --host machine0", timeout=7200)
        remote(BUILDER, SCRUB_SCRIPT, timeout=300)
        # machine0 only snapshots a stopped instance, and `images save` returns
        # before the snapshot exists, so stop first and wait for it after.
        say("stopping %s" % BUILDER)
        machine0.run(["stop", BUILDER], timeout=600)
        wait_status(BUILDER, machine0.STOPPED)
        out = machine0.run(["images", "save", BUILDER, cfg["image"]], timeout=3600)
        m = re.search(r"v(\d+) \(draft\)", out)
        say("snapshotting (this takes a while)")
        wait_image(cfg["image"], int(m.group(1)) if m else None)
        if m:
            machine0.run(["images", "versions", "promote", cfg["image"], m.group(1)])
            prune(cfg["image"], keep=2)
        say("image %s ready" % cfg["image"])
    finally:
        machine0.destroy(BUILDER)
        sshconf.remove(BUILDER)
    return 0


def wait_status(name: str, want: str, timeout: float = 900) -> None:
    deadline = time.time() + timeout
    while machine0.status(machine0.get(name)) != want:
        if time.time() > deadline:
            raise RuntimeError("%s did not reach %s" % (name, want))
        time.sleep(10)


BUSY = ("PENDING", "CREATING", "SNAPSHOT", "PROGRESS", "SAVING", "BUILD", "VERIFY", "CLEANUP", "TRANSFER")


def wait_image(image: str, version: Optional[int], timeout: float = 3600) -> None:
    """Until the image (or that version's snapshot) is ready; raises on failure."""
    deadline = time.time() + timeout
    while True:
        if version is None:
            entry = next((i for i in machine0.run_json(["images", "ls"])
                          if isinstance(i, dict) and i.get("name") == image), {})
            state = str(entry.get("status") or "")
        else:
            versions = machine0.run_json(["images", "versions", "ls", image])
            if isinstance(versions, dict):
                versions = versions.get("versions") or []
            entry = next((v for v in versions if isinstance(v, dict) and int(v.get("version") or 0) == version), {})
            state = str(entry.get("snapshotStatus") or "")
        up = state.upper()
        if up and not any(b in up for b in BUSY):
            if "ERROR" in up or "FAIL" in up:
                raise RuntimeError("image %s: %s" % (image, state))
            return
        if time.time() > deadline:
            raise RuntimeError("image %s still %s" % (image, state or "missing"))
        time.sleep(20)


def prune(image: str, keep: int) -> None:
    try:
        versions = machine0.run_json(["images", "versions", "ls", image])
    except machine0.Machine0Error as e:
        say("could not list versions of %s: %s" % (image, e))
        return
    if isinstance(versions, dict):
        versions = versions.get("versions") or []
    numbers = sorted((int(v.get("version")) for v in versions if isinstance(v, dict) and v.get("version")),
                     reverse=True)
    for n in numbers[keep:]:
        try:
            machine0.run(["images", "versions", "rm", image, str(n), "-y"])
        except machine0.Machine0Error as e:
            say("could not remove %s v%d: %s" % (image, n, e))

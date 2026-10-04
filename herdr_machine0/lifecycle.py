"""Spoke lifecycle on the hub: new, rm, sync, wake, suspend, image build."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
import contextlib
from typing import Any, Dict, Iterator, List, Optional

from . import config, herdr, hub, machine0, progress, registry, sshconf

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
BUILDER = "m0-spoke-build"


# While a progress display owns the terminal, command output and messages go to
# its log instead (see progress.py).
_out: Optional[Any] = None


def say(msg: str) -> None:
    if _out is not None:
        _out.write(msg + "\n")
        _out.flush()
    else:
        print(msg, file=sys.stderr, flush=True)


@contextlib.contextmanager
def logging_to(prog: Any) -> Iterator[None]:
    global _out
    previous, _out = _out, getattr(prog, "log", None)
    try:
        yield
    finally:
        _out = previous


def remote(name: str, script: str, timeout: float = 3600, check: bool = True,
           capture: bool = False, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    route = {} if capture or _out is None else {"stdout": _out, "stderr": subprocess.STDOUT}
    proc = subprocess.run(
        config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", sshconf.alias(name), "bash -lc " + shlex.quote(script)],
        timeout=timeout, capture_output=capture, input=input, **route,
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


# A fresh clone of the golden image is usually already current: everything
# after the pull is skipped when dotfiles and submodules are where the image
# (or the last sync) left them.
SYNC_SCRIPT = r"""
set -e
cd ~/dotfiles
stamp=~/.local/state/herdr-machine0/synced
git pull --rebase --autostash -q
git submodule sync --recursive -q
git submodule update --init --recursive -q
now=$( (git rev-parse HEAD; git submodule status --recursive) | sha1sum | cut -d' ' -f1)
# No `exit` here: in a login shell it runs ~/.bash_logout, whose last test
# (Ubuntu's clear_console check) would become the exit status.
if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$now" ]; then
  echo "dotfiles already current"
else
  (cd plugins/pi-plan-mode && npm ci --omit=dev --no-audit --no-fund --loglevel=error)
  ~/dotfiles/shell/.local/bin/dotfiles-restow shell m0 || [ $? -eq 1 ]
  ~/dotfiles/shell/.local/bin/skills-install >/dev/null 2>&1 || true
  ~/.local/bin/spoke install-pi-extension
  mkdir -p "$(dirname "$stamp")" && echo "$now" > "$stamp"
fi
true
"""


def sync(name: str) -> bool:
    """Pull and restow on the spoke; False when it was already current."""
    say("syncing dotfiles on %s" % name)
    proc = remote(name, SYNC_SCRIPT, timeout=1800, capture=True)
    text = (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace")
    if _out is not None:
        _out.write(text)
    elif text.strip():
        sys.stderr.write(text)
    return "dotfiles already current" not in text


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


def spoke_phases(gpu: bool, repos: List[str]) -> List[Any]:
    phases = [("create", "Create VM", 100), ("boot", "Boot and SSH", 20)]
    phases.append(("bootstrap", "Bootstrap (GPU image)", 1500) if gpu else ("sync", "Sync dotfiles", 45))
    phases += [("clone", "Clone repos", 15 * max(len(repos), 1)), ("agent", "Start agent", 3)]
    return phases


def create(name: str, size: Optional[str], repos: List[str], harness: Optional[str],
           prog: Any = None) -> str:
    """Create, bring up and provision a spoke; returns the main slot's cwd."""
    prog = prog or progress.NullProgress()
    cfg = config.settings()
    if not NAME_RE.match(name) or name == BUILDER:
        raise ValueError("spoke names are lowercase letters, digits and dashes")
    if machine0.get(name):
        raise ValueError("%s already exists" % name)
    size = size or cfg["default_size"]
    harness = harness or cfg["default_harness"]
    gpu = size.startswith("gpu-")
    region = cfg["gpu_region"] if gpu else cfg["region"]
    with logging_to(prog):
        say("creating %s (%s, %s)" % (name, size, region))
        registry.put_spoke(name, size=size, region=region, harness=harness, keep_awake=False, idle_since=None)
        with prog.step("create"):
            machine0.new(name, size, region, None if gpu else cfg["image"], cfg["ssh_key"], cfg["profile"])
        with prog.step("boot"):
            bring_up(name)
        if gpu:
            with prog.step("bootstrap"):
                bootstrap(name)
        else:
            with prog.step("sync"):
                if not sync(name):
                    prog.note("sync", "already current")
        if repos:
            with prog.step("clone"):
                paths = clone(name, repos)
        else:
            paths = []
            prog.skip("clone", "none")
        registry.put_spoke(name, pending=False)
    return paths[0] if paths else "~"


def spoke_progress(name: str, size: Optional[str], harness: Optional[str], repos: List[str]) -> Any:
    cfg = config.settings()
    size = size or cfg["default_size"]
    gpu = size.startswith("gpu-")
    sub = "%s · %s · %s" % (size, cfg["gpu_region"] if gpu else cfg["region"], harness or cfg["default_harness"])
    return progress.Progress("New spoke %s" % name, sub, spoke_phases(gpu, repos),
                             config.state_path("logs", "new-%s.log" % name))


def report_failure(prog: Any, name: str, error: BaseException) -> None:
    lines = prog.tail(12)
    print("\n  creating %s failed: %s" % (name, error), file=sys.stderr)
    if lines:
        print("  last lines of %s:" % getattr(prog, "log_path", "the log"), file=sys.stderr)
        for line in lines:
            print("    " + line, file=sys.stderr)
    print("  `spoke rm %s --force` cleans up." % name, file=sys.stderr)


def new(name: str, size: Optional[str], repos: List[str], harness: Optional[str], focus: bool = True) -> int:
    harness = harness or config.settings()["default_harness"]
    prog = spoke_progress(name, size, harness, repos)
    try:
        with prog:
            cwd = create(name, size, repos, harness, prog)
            with prog.step("agent"):
                hub.open_slot(name, "main", harness, cwd, focus=focus)
    except ValueError as e:
        say(str(e))
        return 1
    except Exception as e:
        report_failure(prog, name, e)
        return 1
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


KEPT = 3  # exit status of `new --in-pane` when the user keeps a hub shell


def new_in_pane(name: str, size: Optional[str], harness: Optional[str], grace: int = 5) -> int:
    """`spoke new --in-pane`: the new-space hook's command. Creates the spoke in
    front of you, then turns this pane into the spoke's main slot."""
    cfg = config.settings()
    size = size or cfg["default_size"]
    harness = harness or cfg["default_harness"]
    say("\n  New space, new spoke: %s (%s · %s · %s)" % (name, size, cfg["region"], harness))
    if not _grace(grace):
        registry.drop_spoke(name)
        import shutil
        shutil.rmtree(os.path.join(config.STATE_DIR, "panes", name), ignore_errors=True)
        workspace = os.environ.get("HERDR_WORKSPACE_ID")
        if workspace:
            herdr.quiet("workspace.rename", {"workspace_id": workspace, "label": "hub"})
        say("  Kept as a plain hub shell. `spoke new <name>` makes a spoke later.")
        return KEPT
    prog = spoke_progress(name, size, harness, [])
    try:
        with prog:
            cwd = create(name, size, [], harness, prog)
            with prog.step("agent"):
                registry.put_slot(name, "main", harness=harness, cwd=cwd,
                                  pane_id=os.environ.get("HERDR_PANE_ID"))
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
        report_failure(prog, name, e)
        return 1
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
    prog = progress.Progress(
        "Golden image %s" % cfg["image"], "from %s · large · %s" % (base, cfg["region"]),
        [("create", "Create builder VM", 100), ("boot", "Boot and SSH", 20),
         ("provision", "Provision spoke", 1200 if base == cfg["base_image"] else 240),
         ("scrub", "Scrub credentials", 5), ("stop", "Stop builder", 30),
         ("snapshot", "Snapshot", 300), ("promote", "Promote and prune", 10),
         ("cleanup", "Delete builder", 20)],
        config.state_path("logs", "image-build.log"))
    try:
        with prog, logging_to(prog):
            with prog.step("create"):
                machine0.new(BUILDER, "large", cfg["region"], base, cfg["ssh_key"], None)
            try:
                with prog.step("boot"):
                    bring_up(BUILDER)
                with prog.step("provision"):
                    if base == cfg["base_image"]:
                        bootstrap(BUILDER)
                    else:
                        sync(BUILDER)
                        remote(BUILDER, "~/dotfiles/m0/bin/bootstrap-m0 --role spoke --host machine0",
                               timeout=7200)
                    remote(BUILDER, "rm -f ~/.local/state/herdr-machine0/synced", timeout=60)
                    sync(BUILDER)  # leaves the stamp, so clones skip a redundant sync
                with prog.step("scrub"):
                    remote(BUILDER, SCRUB_SCRIPT, timeout=300)
                # machine0 only snapshots a stopped instance, and `images save`
                # returns before the snapshot exists, so stop first, wait after.
                with prog.step("stop"):
                    machine0.run(["stop", BUILDER], timeout=600)
                    wait_status(BUILDER, machine0.STOPPED)
                with prog.step("snapshot"):
                    out = machine0.run(["images", "save", BUILDER, cfg["image"]], timeout=3600)
                    say(out)
                    m = re.search(r"v(\d+) \(draft\)", out)
                    wait_image(cfg["image"], int(m.group(1)) if m else None)
                with prog.step("promote"):
                    if m:
                        machine0.run(["images", "versions", "promote", cfg["image"], m.group(1)])
                        prune(cfg["image"], keep=2)
                        prog.note("promote", "v" + m.group(1))
            finally:
                with prog.step("cleanup"):
                    machine0.destroy(BUILDER)
                    sshconf.remove(BUILDER)
    except Exception as e:
        print("\n  image build failed: %s" % e, file=sys.stderr)
        for line in prog.tail(12):
            print("    " + line, file=sys.stderr)
        return 1
    print("  image %s ready" % cfg["image"], file=sys.stderr)
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

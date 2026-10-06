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

from . import config, herdr, hub, machine0, progress, registry, repos as repos_mod, sshconf

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
    # -n and a closed stdin: no remote program can stop at a prompt or read the
    # keys typed while a progress display owns the terminal.
    proc = subprocess.run(
        config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"] + ([] if input else ["-n"])
        + [sshconf.alias(name), "bash -lc " + shlex.quote(script)],
        timeout=timeout, capture_output=capture, input=input,
        stdin=None if input else subprocess.DEVNULL, **route,
    )
    if check and proc.returncode != 0:
        raise RuntimeError("%s: remote command failed (%d)" % (name, proc.returncode))
    return proc


def wait_ssh(name: str, timeout: float = 600) -> None:
    deadline = time.time() + timeout
    while True:
        rc = subprocess.run(config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-n", sshconf.alias(name), "true"],
                            capture_output=True, stdin=subprocess.DEVNULL).returncode
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


CLOUD_INIT_WAIT = r"""
if command -v cloud-init >/dev/null; then
  timeout 420 sudo cloud-init status --wait >/dev/null 2>&1
  case $? in
    0|2) echo done ;;      # 2: finished with recoverable errors
    124) echo timeout ;;
    *) echo error ;;
  esac
else
  echo none
fi
"""


def settle_cloud_init(name: str, prog: Any) -> None:
    """machine0 injects the profile (gh's GitHub login, env) through cloud-init;
    clone nothing before it is done."""
    proc = remote(name, CLOUD_INIT_WAIT, timeout=480, check=False, capture=True)
    state = (proc.stdout or b"").decode().strip().splitlines()[-1:] or ["?"]
    if state[0] == "timeout":
        prog.note("boot", "cloud-init still running; continuing")
        say("cloud-init on %s did not finish within 7 minutes" % name)


def push_plugin(name: str) -> None:
    """Copy the hub's plugin checkout to the spoke, so both run the same code."""
    dest = config.settings()["spoke_plugin_dir"]
    tar = subprocess.Popen(
        ["tar", "-C", config.PLUGIN_ROOT, "--exclude", ".git", "--exclude", "__pycache__",
         "--exclude", "tests", "-czf", "-", "."],
        stdout=subprocess.PIPE)
    unpack = ("set -e; d={d}; rm -rf \"$d.new\"; mkdir -p \"$d.new\"; tar -xzf - -C \"$d.new\"; "
              "rm -rf \"$d\"; mv \"$d.new\" \"$d\"").format(d=dest.replace("~", "$HOME", 1))
    try:
        remote(name, unpack, timeout=300, input=tar.stdout.read() if tar.stdout else b"")
    finally:
        tar.wait()


def provision(name: str) -> None:
    """Everything a spoke needs: the plugin's setup, then the user's own."""
    cfg = config.settings()
    say("provisioning %s" % name)
    push_plugin(name)
    remote(name, "bash %s/setup/spoke.sh" % cfg["spoke_plugin_dir"], timeout=3600)
    if cfg.get("provision_command"):
        remote(name, cfg["provision_command"], timeout=7200)


def sync(name: str) -> bool:
    """Bring a running spoke up to date: the plugin, then `sync_command`.
    False when the user's sync reported it was already current."""
    cfg = config.settings()
    say("syncing %s" % name)
    push_plugin(name)
    remote(name, "ln -sfn {d}/spoke.py ~/.local/bin/spoke && ~/.local/bin/spoke install-pi-extension"
           .format(d=cfg["spoke_plugin_dir"]), timeout=300)
    if not cfg.get("sync_command"):
        return True
    proc = remote(name, cfg["sync_command"], timeout=1800, capture=True)
    text = (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace")
    if _out is not None:
        _out.write(text)
    elif text.strip():
        sys.stderr.write(text)
    return "already current" not in text


def clone(name: str, repos: List[str]) -> List[str]:
    paths = []
    for repo in repos:
        short = repo.rstrip("/").split("/")[-1].replace(".git", "")
        dest = "~/Projects/%s" % short
        url = repo if "://" in repo or repo.startswith("git@") else "https://github.com/%s.git" % repo
        remote(name, "mkdir -p ~/Projects && (test -d {d} || gh repo clone {u} {d} -- -q || git clone -q {u} {d})"
               .format(d=dest, u=shlex.quote(url)), timeout=1800)
        paths.append(dest)
    return paths


def spoke_phases(gpu: bool, repos: List[str]) -> List[Any]:
    phases = [("create", "Create VM", 95), ("boot", "Boot and SSH", 10)]
    if gpu:
        phases.append(("provision", "Provision (GPU image)", 1500))
    phases.append(("sync", "Sync", 45))
    phases += [("clone", "Clone %s" % repos[0] if len(repos) == 1 else "Clone repos", 15 * max(len(repos), 1)),
               ("setup", "Repo setup", 30), ("agent", "Start agent", 3)]
    return phases


def run_setup(name: str, path: str, root: str, branch: str) -> None:
    """The repo's own setup convention, if it has one (repos.SETUP_SCRIPT)."""
    remote(name, "bash -s -- %s %s %s <<'HERDR_MACHINE0_SETUP'\n%s\nHERDR_MACHINE0_SETUP" % (
        path, root, shlex.quote(branch), repos_mod.SETUP_SCRIPT), timeout=3600, check=False)


def create(name: str, size: Optional[str], repos: List[str], harness: Optional[str],
           prog: Any = None, repo: Optional[str] = None) -> str:
    """Create, bring up and provision a spoke; returns the main slot's cwd.
    With `repo`, the spoke is that repo's: cloned first, its setup run."""
    prog = prog or progress.NullProgress()
    cfg = config.settings()
    if repo:
        repos = [repo] + [r for r in repos if r != repo]
        size = size or (cfg.get("repo_sizes") or {}).get(repo)
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
        registry.put_spoke(name, size=size, region=region, harness=harness, keep_awake=False, idle_since=None,
                           **({"repo": repo} if repo else {}))
        with prog.step("create"):
            machine0.new(name, size, region, None if gpu else cfg["image"], cfg["ssh_key"], cfg["profile"])
            m = machine0.wait_running(name)
        with prog.step("boot"):
            sshconf.update(name, machine0.ip(m) or "")
            wait_ssh(name)
            settle_cloud_init(name, prog)
        if gpu:
            with prog.step("provision"):
                provision(name)
        with prog.step("sync"):
            if not sync(name):
                prog.note("sync", "already current")
        if repos:
            with prog.step("clone"):
                paths = clone(name, repos)
        else:
            paths = []
            prog.skip("clone", "none")
        if repo:
            with prog.step("setup"):
                run_setup(name, paths[0], paths[0], "main")
        else:
            prog.skip("setup", "no repo")
        registry.put_spoke(name, pending=False)
    return paths[0] if paths else "~"


def spoke_progress(name: str, size: Optional[str], harness: Optional[str], repos: List[str]) -> Any:
    """`repos` names what gets cloned (the phase label); empty for scratch spokes."""
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


def new(name: str, size: Optional[str], repos: List[str], harness: Optional[str], focus: bool = True,
        repo: Optional[str] = None) -> int:
    harness = harness or config.settings()["default_harness"]
    prog = spoke_progress(name, size, harness, ([repo] if repo else []) + [r for r in repos if r != repo])
    try:
        with prog:
            cwd = create(name, size, repos, harness, prog, repo=repo)
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


def new_in_pane(name: str, size: Optional[str], harness: Optional[str], grace: int = 0,
                repo: Optional[str] = None) -> int:
    """`spoke new --in-pane`: what a new hub space runs once its repo is picked.
    Creates the spoke in front of you, then turns this pane into its main slot."""
    cfg = config.settings()
    size = size or (cfg.get("repo_sizes") or {}).get(repo or "") or cfg["default_size"]
    harness = harness or cfg["default_harness"]
    if grace and not _grace(grace):
        registry.drop_spoke(name)
        import shutil
        shutil.rmtree(os.path.join(config.STATE_DIR, "panes", name), ignore_errors=True)
        workspace = os.environ.get("HERDR_WORKSPACE_ID")
        if workspace:
            herdr.quiet("workspace.rename", {"workspace_id": workspace, "label": "hub"})
        say("  Kept as a plain hub shell. `spoke new <name>` makes a spoke later.")
        return KEPT
    prog = spoke_progress(name, size, harness, [repo] if repo else [])
    try:
        with prog:
            cwd = create(name, size, [], harness, prog, repo=repo)
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
            config.ssh_base() + ["-o", "BatchMode=yes", "-n", sshconf.alias(name),
             "cd ~ && tar czf - --ignore-failed-read .pi/agent/sessions .claude/projects .codex/sessions "
             ".local/share/opencode/storage 2>/dev/null"],
            stdout=out, stdin=subprocess.DEVNULL, timeout=1800,
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


def wake_spoke(name: str, subtitle: str = "") -> None:
    """Start a suspended (or stopped) spoke and wait until ssh answers, with a
    progress display. Raises on failure (the display shows where)."""
    prog = progress.Progress(
        "Waking %s" % name, subtitle or "resumes from its snapshot on a new IP",
        [("start", "Start VM", 90), ("ssh", "Reach SSH", 10)],
        config.state_path("logs", "wake-%s.log" % name))
    with prog, logging_to(prog):
        with prog.step("start"):
            machine0.start(name)
            m = machine0.wait_running(name)
            prog.note("start", machine0.ip(m) or "")
        with prog.step("ssh"):
            sshconf.update(name, machine0.ip(m) or "")
            wait_ssh(name, timeout=300)


def wake(name: str) -> int:
    try:
        wake_spoke(name)
    except Exception as e:
        say("waking %s failed: %s" % (name, e))
        return 1
    return 0


WORKTREE_SCRIPT = r"""
set -e
root={root}; branch={branch}; dest={dest}
cd "$root"
git fetch -q origin 2>/dev/null || true
if [ -d "$dest" ]; then echo "worktree exists: $dest"
elif git show-ref --verify -q "refs/heads/$branch"; then git worktree add -q "$dest" "$branch"
elif git show-ref --verify -q "refs/remotes/origin/$branch"; then git worktree add -q -b "$branch" "$dest" "origin/$branch"
else git worktree add -q -b "$branch" "$dest" "$(git symbolic-ref -q --short refs/remotes/origin/HEAD || echo HEAD)"
fi
"""


def add_worktree(spoke: str, branch: str, harness: Optional[str] = None, focus: bool = True) -> int:
    """A worktree of the spoke's repo on the spoke, as a new tab of its space."""
    info = registry.load()["spokes"].get(spoke) or {}
    repo = info.get("repo")
    if not repo:
        say("%s is a scratch spoke; worktrees need a repo spoke" % spoke)
        return 1
    m = machine0.get(spoke)
    if machine0.status(m) != machine0.RUNNING:
        say("%s is %s; wake it first (spoke wake %s)" % (spoke, machine0.status(m).lower(), spoke))
        return 1
    sshconf.update(spoke, machine0.ip(m) or "")
    root = repos_mod.checkout(repo)
    dest = "%s/.worktrees/%s" % (root, repos_mod.slug(branch, 60))
    remote(spoke, WORKTREE_SCRIPT.format(root=root, branch=shlex.quote(branch), dest=dest), timeout=600)
    run_setup(spoke, dest, root, branch)
    slot = registry.next_slot(spoke, repos_mod.branch_slot(branch))
    hub.open_slot(spoke, slot, harness or info.get("harness") or config.settings()["default_harness"],
                  dest, focus=focus)
    say("%s: worktree %s open as tab %s" % (spoke, branch, slot))
    return 0


def remove_worktree(spoke: str, branch: str, force: bool = False) -> int:
    """Close the worktree's tab, stop its agent and remove the checkout (the branch stays)."""
    info = registry.load()["spokes"].get(spoke) or {}
    if not info.get("repo"):
        say("%s is a scratch spoke" % spoke)
        return 1
    root = repos_mod.checkout(info["repo"])
    dest = "%s/.worktrees/%s" % (root, repos_mod.slug(branch, 60))
    slots = [s for s in registry.load()["slots"].values()
             if s.get("spoke") == spoke and (s.get("cwd") or "").rstrip("/") == dest]
    m = machine0.get(spoke)
    if machine0.status(m) != machine0.RUNNING:
        say("%s is %s; wake it first" % (spoke, machine0.status(m).lower()))
        return 1
    sshconf.update(spoke, machine0.ip(m) or "")
    if not force:
        dirty = remote(spoke, "cd %s && git status --porcelain | head -3" % dest, check=False, capture=True)
        if (dirty.stdout or b"").strip():
            say("%s has uncommitted changes; commit them or use --force" % dest)
            return 1
    for s in slots:
        if s.get("pane_id"):
            herdr.quiet("pane.close", {"pane_id": s["pane_id"]})
        remote(spoke, "pkill -f 'dtach/%s.sock' || true" % s["slot"], check=False)
    remote(spoke, "cd %s && git worktree remove %s %s" % (root, "--force" if force else "", dest), timeout=300)
    with registry.locked() as data:
        for s in slots:
            data["slots"].pop(registry.slot_key(spoke, s["slot"]), None)
    say("%s: removed worktree %s (branch kept)" % (spoke, branch))
    return 0


def suspend(name: str) -> int:
    machine0.suspend(name)
    registry.put_spoke(name, idle_since=None)
    return 0


SCRUB_SCRIPT = r"""
set -e
# A snapshot taken mid-install leaves dpkg "interrupted" in every clone, and
# DigitalOcean's first-boot agent install then retries forever, so cloud-init
# never finishes (and machine0's profile, gh's login, never lands). Stop the
# periodic apt jobs, let any running one finish, and repair.
sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
sudo systemctl stop apt-daily.service apt-daily-upgrade.service unattended-upgrades.service 2>/dev/null || true
while sudo fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do sleep 3; done
sudo dpkg --configure -a
sudo apt-get -o DPkg::Lock::Timeout=600 -qq -f install -y >/dev/null
sync
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
         ("snapshot", "Snapshot", 300), ("promote", "Promote", 10),
         ("cleanup", "Delete builder", 20)],
        config.state_path("logs", "image-build.log"))
    try:
        with prog, logging_to(prog):
            with prog.step("create"):
                machine0.new(BUILDER, "large", cfg["region"], base, cfg["ssh_key"], None)
                m = machine0.wait_running(BUILDER)
            try:
                with prog.step("boot"):
                    sshconf.update(BUILDER, machine0.ip(m) or "")
                    wait_ssh(BUILDER)
                with prog.step("provision"):
                    provision(BUILDER)
                    # A sync on the builder lets `sync_command` record where the
                    # image stands, so fresh clones can skip a redundant one.
                    sync(BUILDER)
                with prog.step("scrub"):
                    remote(BUILDER, SCRUB_SCRIPT, timeout=300)
                # machine0 only snapshots a stopped instance, and `images save`
                # returns before the snapshot exists, so stop first, wait after.
                with prog.step("stop"):
                    machine0.run(["stop", BUILDER], timeout=600)
                    wait_status(BUILDER, machine0.STOPPED)
                with prog.step("snapshot"):
                    before = set(image_versions(cfg["image"])) if have else set()
                    out = machine0.run(["images", "save", BUILDER, cfg["image"]], timeout=3600)
                    say(out)
                    version = new_version(out, before, image_versions(cfg["image"]))
                    wait_image(cfg["image"], version)
                with prog.step("promote"):
                    if version is None:
                        # Never report success with the old version still live.
                        raise RuntimeError("could not tell which version the snapshot created; "
                                           "promote it with `machine0 images versions promote %s <n>`" % cfg["image"])
                    # Promoting retires the previous version. machine0 only
                    # deletes drafts, so retired versions are its to manage.
                    machine0.run(["images", "versions", "promote", cfg["image"], str(version)])
                    prog.note("promote", "v%d" % version)
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


def image_versions(image: str) -> List[int]:
    try:
        versions = machine0.run_json(["images", "versions", "ls", image])
    except machine0.Machine0Error:
        return []
    if isinstance(versions, dict):
        versions = versions.get("versions") or []
    return [int(v["version"]) for v in versions if isinstance(v, dict) and v.get("version")]


def new_version(save_output: str, before: Any, after: List[int]) -> Optional[int]:
    """The version `images save` created: the one the version list gained, else
    the `vN` its output names. None when neither says."""
    added = sorted(set(after) - set(before))
    if added:
        return added[-1]
    m = re.search(r"\bv(\d+)\b", save_output)
    return int(m.group(1)) if m else None


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

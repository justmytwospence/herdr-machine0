"""Repos: one spoke (VM) per GitHub repo, one herdr tab per worktree.

A repo spoke is named after its repo, keeps the main checkout at
~/Projects/<repo> and worktrees at ~/Projects/<repo>/.worktrees/<branch>.
Spokes without a repo ("scratch") still exist for repo-less work.

The list of repos you can open comes from `gh repo list` on any running spoke
(gh there is authenticated by the machine0 profile's GitHub App), cached on
the hub; any owner/repo can also be typed in.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from . import config, registry, sshconf

CACHE_TTL_S = 3600

# Run in a checkout on the spoke, after a clone or a new worktree. The first of
# a repo's own setup conventions that exists wins; none is fine.
SETUP_SCRIPT = r"""
cd "$1" || exit 0
export HERDR_MACHINE0_REPO_ROOT="$2" HERDR_MACHINE0_WORKTREE="$1" HERDR_MACHINE0_BRANCH="$3"
# Conductor's names, so a repo's conductor.json setup script runs unchanged.
export CONDUCTOR_ROOT_PATH="$2" CONDUCTOR_WORKSPACE_PATH="$1" CONDUCTOR_WORKSPACE_NAME="$3"
if [ -x .herdr-machine0/setup.sh ]; then
  echo "running .herdr-machine0/setup.sh"; ./.herdr-machine0/setup.sh || echo "setup.sh failed ($?)"
elif [ -f conductor.json ]; then
  cmd=$(python3 -c 'import json;print((json.load(open("conductor.json")).get("scripts") or {}).get("setup") or "")' 2>/dev/null)
  if [ -n "$cmd" ]; then echo "running conductor.json setup"; bash -c "$cmd" || echo "conductor setup failed ($?)"; fi
fi
true
"""

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize(repo: str) -> Optional[str]:
    """owner/repo from owner/repo, a GitHub URL or git@github.com:owner/repo(.git)."""
    repo = repo.strip()
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)?([^/\s]+/[^/\s]+?)(?:\.git)?/?$",
                 repo)
    if not m or not REPO_RE.match(m.group(1)):
        return None
    return m.group(1)


def short(repo: str) -> str:
    return repo.split("/", 1)[1]


def checkout(repo: str) -> str:
    return "~/Projects/%s" % short(repo)


def slug(text: str, limit: int = 40) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not out or not out[0].isalpha():
        out = "r-" + out
    return out[:limit].rstrip("-")


def spoke_for(repo: str) -> Optional[str]:
    for name, info in registry.load()["spokes"].items():
        if info.get("repo") == repo:
            return name
    return None


def spoke_name(repo: str) -> str:
    """The spoke an unopened repo gets: its name, or owner-name on a clash."""
    taken = set(registry.load()["spokes"])
    name = slug(short(repo))
    if name in taken:
        name = slug(repo.replace("/", "-"))
    n = 2
    base = name
    while name in taken:
        name = "%s-%d" % (base, n)
        n += 1
    return name


def branch_slot(branch: str) -> str:
    return slug(branch.split("/")[-1], 24)


# ---- the repo list -------------------------------------------------------------


def _cache_path() -> str:
    return config.state_path("repos.json")


def cached() -> Dict[str, Any]:
    try:
        with open(_cache_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"repos": [], "updated": 0}


def store(repos: List[Dict[str, Any]]) -> None:
    clean = []
    for r in repos:
        name = normalize(str(r.get("nameWithOwner") or r.get("name") or ""))
        if name:
            clean.append({"name": name, "pushed": r.get("pushedAt") or r.get("pushed") or "",
                          "description": (r.get("description") or "")[:80]})
    clean.sort(key=lambda r: r["pushed"], reverse=True)
    with open(_cache_path(), "w") as f:
        json.dump({"repos": clean, "updated": int(time.time())}, f)


GH_LIST = "gh repo list --limit 300 --no-archived --json nameWithOwner,pushedAt,description"


def refresh_from(spoke: str) -> bool:
    try:
        out = subprocess.run(
            config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-n", sshconf.alias(spoke),
                                 "bash -lc " + json.dumps(GH_LIST)],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False
    if out.returncode != 0:
        return False
    try:
        store(json.loads(out.stdout))
    except ValueError:
        return False
    return True


def stale() -> bool:
    return time.time() - float(cached().get("updated") or 0) > CACHE_TTL_S


def choices() -> List[Dict[str, Any]]:
    """Repos with a spoke first (with its state), then the rest of the cache."""
    reg = registry.load()["spokes"]
    have = {info.get("repo"): name for name, info in reg.items() if info.get("repo")}
    out = [{"repo": repo, "spoke": name} for repo, name in sorted(have.items())]
    for r in cached()["repos"]:
        if r["name"] not in have:
            out.append({"repo": r["name"], "spoke": None, "description": r.get("description", "")})
    return out

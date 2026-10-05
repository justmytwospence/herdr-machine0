"""`spoke setup hub` and `spoke doctor`.

Setup installs what herdr-machine0 itself needs on the hub; doctor checks a hub
(or a spoke) end to end and says how to fix each gap. Personal configuration
(dotfiles, shells, editors) is not this plugin's business: see the
`provision_command` and `sync_command` settings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, List, Optional, Tuple

from . import broker, config, herdr, sshconf

GREEN, RED, YELLOW, DIM, RESET = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[2m", "\x1b[0m"


def _run(argv: List[str], timeout: float = 600) -> Tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _log(msg: str) -> None:
    print("==> " + msg, flush=True)


def link_cli() -> None:
    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    target = os.path.join(bin_dir, "spoke")
    source = os.path.join(config.PLUGIN_ROOT, "spoke.py")
    if os.path.realpath(target) != os.path.realpath(source):
        if os.path.lexists(target):
            os.unlink(target)
        os.symlink(source, target)


def npm_install(package: str) -> int:
    rc, _ = _run(["npm", "install", "-g", "--no-audit", "--no-fund", "--loglevel=error", package])
    if rc != 0:  # no write access to the global prefix: install per user
        rc, _ = _run(["npm", "install", "-g", "--prefix", os.path.expanduser("~/.local"),
                      "--no-audit", "--no-fund", "--loglevel=error", package])
    return rc


def setup_hub() -> int:
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with open(config.ROLE_FILE, "w") as f:
        f.write("hub\n")
    _log("role: hub")
    link_cli()
    _log("spoke CLI: ~/.local/bin/spoke")
    if not shutil.which("npm"):
        print("npm is required (Node.js 20+) for the machine0 CLI and the broker's pi SDK", file=sys.stderr)
        return 1
    if not shutil.which("machine0"):
        _log("installing the machine0 CLI")
        npm_install("@machine0/cli")
    try:
        broker.find_pi_package()
    except broker.BrokerError:
        _log("installing a pi SDK for the credential broker")
        sdk = os.path.dirname(os.path.dirname(os.path.dirname(broker.SDK_DIR)))
        os.makedirs(sdk, exist_ok=True)
        _run(["npm", "install", "--prefix", sdk, "--no-audit", "--no-fund", "--loglevel=error", broker.PI_PACKAGE])
    key = os.path.expanduser("~/.ssh/id_ed25519")
    if not os.path.exists(key):
        _log("generating %s" % key)
        os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
        _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "herdr-machine0 hub", "-f", key])
    try:
        sshconf.ensure_include()
    except RuntimeError as e:
        print("  %s" % e, file=sys.stderr)
    if shutil.which("herdr"):
        rc, out = _run(["herdr", "plugin", "list"])
        # Registered already, linked or installed from GitHub (dotfiles pins it).
        if not re.search(r"^- machine0 ", out, re.M):
            _log("linking the herdr plugin")
            _run(["herdr", "plugin", "link", config.PLUGIN_ROOT])
    _log("done; `spoke doctor` lists what is left")
    return 0


# ---- doctor -----------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def line(self, state: str, label: str, detail: str = "", fix: str = "") -> None:
        mark = {"ok": GREEN + "✓", "warn": YELLOW + "!", "fail": RED + "✗"}[state] + RESET
        print("  %s %s%s" % (mark, label, (DIM + "  " + detail + RESET) if detail else ""))
        if fix and state != "ok":
            print("      " + fix)
        if state == "fail":
            self.failed += 1

    def check(self, label: str, fn: Callable[[], Tuple[str, str, str]]) -> None:
        try:
            state, detail, fix = fn()
        except Exception as e:  # a broken check is a failed check, not a crash
            state, detail, fix = "fail", str(e)[:120], ""
        self.line(state, label, detail, fix)


def _m0_json(args: List[str]) -> Any:
    rc, out = _run(["machine0"] + args + ["--json"], timeout=60)
    if rc != 0:
        raise RuntimeError(out.splitlines()[-1] if out else "machine0 failed")
    return json.loads(out)


def _names(data: Any) -> List[str]:
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [str(d.get("name")) for d in data if isinstance(d, dict)]


def doctor_hub(r: Report) -> None:
    cfg = config.settings()

    def herdr_server():
        herdr.call("ping", {})
        plugins = herdr.call("plugin.list", {}).get("plugins") or []
        if not any(p.get("plugin_id") == "machine0" for p in plugins):
            return "fail", "plugin not linked", "spoke setup hub (or: herdr plugin link %s)" % config.PLUGIN_ROOT
        return "ok", "plugin linked", ""
    r.check("herdr server and plugin", herdr_server)

    def hubd():
        from . import hubd as h
        pid = h.running_pid()
        return ("ok", "pid %d" % pid, "") if pid else ("warn", "not running", "starts with the herdr server; or: spoke hubd --ensure")
    r.check("hubd (auto-suspend)", hubd)

    def account():
        if not shutil.which("machine0"):
            return "fail", "CLI missing", "spoke setup hub"
        user = (_m0_json(["account"]) or {}).get("user") or {}
        balance = user.get("walletBalance")
        text = "balance $%.2f" % (balance / 1e6) if isinstance(balance, (int, float)) else "logged in"
        if not user.get("autoTopUpEnabled"):
            # A wallet at its threshold gets every VM snapshotted and destroyed.
            return "warn", text + ", auto-topup off", "turn on auto-topup in the machine0 dashboard"
        if user.get("lastAutoTopUpFailedAt"):
            return "warn", text + ", last auto-topup failed", str(user.get("lastAutoTopUpFailureReason") or "")
        return "ok", text + ", auto-topup on", ""
    r.check("machine0 account", account)

    def ssh_key():
        names = _names(_m0_json(["keys", "ls"]))
        if cfg["ssh_key"] in names:
            return "ok", cfg["ssh_key"], ""
        return ("fail", "no key named %s" % cfg["ssh_key"],
                "machine0 keys new %s --type PUBLIC --publicKeyPath ~/.ssh/id_ed25519.pub --default" % cfg["ssh_key"])
    r.check("machine0 ssh key", ssh_key)

    def profile():
        if cfg["profile"] not in _names(_m0_json(["profiles", "ls"])):
            return "fail", "no profile %s" % cfg["profile"], "machine0 profiles new %s" % cfg["profile"]
        rows = _m0_json(["integrations", "ls", "-p", cfg["profile"]])
        github = next((x for x in rows if isinstance(x, dict) and x.get("name") == "github"), {})
        if not (github.get("connected") is True or str(github.get("status", "")).lower() == "connected"):
            return "warn", "GitHub not connected (spokes cannot clone private repos)", \
                "machine0 integrations connect github -p %s" % cfg["profile"]
        return "ok", "%s, GitHub connected" % cfg["profile"], ""
    r.check("machine0 profile", profile)

    def image():
        rows = _m0_json(["images", "ls"])
        found = next((x for x in rows if isinstance(x, dict) and x.get("name") == cfg["image"]), None)
        if not found:
            return "fail", "no image %s" % cfg["image"], "spoke image build --fresh"
        return "ok", "%s %s" % (cfg["image"], str(found.get("status", "")).lower()), ""
    r.check("golden image", image)

    def sdk():
        return "ok", broker.find_pi_package(), ""
    r.check("pi SDK for the broker", sdk)

    def secrets():
        values = config.read_secrets()
        if not values.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "warn", "no Claude setup-token (spokes have no Claude login)", \
                "claude setup-token, then: spoke secrets set CLAUDE_CODE_OAUTH_TOKEN"
        return "ok", ", ".join(sorted(config.spoke_secrets(values))), ""
    r.check("spoke secrets", secrets)

    def brokered():
        status = broker.status()
        missing = [p for p in cfg["brokered_providers"] if p not in status]
        if missing:
            return "warn", "not logged in: %s" % ", ".join(missing), "spoke secrets login"
        expiring = [p for p in cfg["brokered_providers"] if (status[p].get("hours_left") or 0) <= 0]
        if expiring:
            return "warn", "expired: %s (refreshed on next use)" % ", ".join(expiring), ""
        return "ok", ", ".join("%s %sh" % (p, status[p]["hours_left"]) for p in cfg["brokered_providers"]), ""
    r.check("broker logins", brokered)

    def usage_login():
        return ("ok", "anthropic", "") if "anthropic" in broker.status() else \
            ("warn", "no anthropic login: no Claude quota on spokes", "spoke secrets login (Anthropic)")
    r.check("usage login", usage_login)

    def include():
        path = os.path.expanduser("~/.ssh/config")
        try:
            with open(path) as f:
                ok = sshconf.INCLUDE in f.read()
        except OSError:
            ok = False
        return ("ok", "", "") if ok else ("warn", "`ssh m0-<spoke>` will not resolve", "spoke setup hub")
    r.check("ssh config include", include)

    def hooks():
        set_ = [k for k in ("provision_command", "sync_command") if cfg.get(k)]
        return "ok", ", ".join(set_) if set_ else "none (plain spokes)", ""
    r.check("personal hooks", hooks)


def doctor_spoke(r: Report) -> None:
    for tool in ("dtach", "herdr", "pi", "claude", "codex", "opencode"):
        path = shutil.which(tool)
        state = "ok" if path else ("fail" if tool in ("dtach", "herdr") else "warn")
        r.line(state, tool, path or "missing", "re-run setup/spoke.sh (spoke image build)")
    ext = os.path.expanduser("~/.pi/agent/extensions/herdr-machine0.ts")
    r.line("ok" if os.path.exists(ext) else "fail", "pi extension", ext, "spoke install-pi-extension")
    r.line("ok" if os.path.exists(config.SECRETS_FILE) else "warn", "secrets from the hub",
           config.SECRETS_FILE, "pushed on the next attach")
    conf = "/etc/ssh/sshd_config.d/herdr-machine0.conf"
    r.line("ok" if os.path.exists(conf) else "warn", "sshd StreamLocalBindUnlink", conf,
           "re-run setup/spoke.sh")


def doctor() -> int:
    role = config.role()
    print("\n  herdr-machine0 doctor (%s)\n" % role)
    r = Report()
    if role == "hub":
        doctor_hub(r)
    elif role == "spoke":
        doctor_spoke(r)
    else:
        r.line("fail", "role", role, "spoke setup hub")
    print()
    return 1 if r.failed else 0

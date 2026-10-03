"""Paths, role and settings shared by every command.

Hub and spokes run the same checkout. The role decides what a command may do:
the hub owns the herdr server, the machine0 CLI and every credential; a spoke
only runs agents.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

CONFIG_DIR = os.path.expanduser(os.environ.get("HERDR_MACHINE0_CONFIG_DIR", "~/.config/herdr-machine0"))
STATE_DIR = os.path.expanduser(os.environ.get("HERDR_MACHINE0_STATE_DIR", "~/.local/state/herdr-machine0"))
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROLE_FILE = os.path.join(CONFIG_DIR, "role")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SECRETS_FILE = os.path.join(CONFIG_DIR, "secrets.env")
BROKER_DIR = os.path.join(CONFIG_DIR, "broker")
KNOWN_HOSTS = os.path.join(CONFIG_DIR, "known_hosts")
SSH_CONFIG = os.path.expanduser(os.environ.get("HERDR_MACHINE0_SSH_CONFIG", "~/.ssh/config.d/herdr-machine0"))

# Spoke-side paths, also used by the hub to address them (the spoke user is fixed).
SPOKE_STATE = "~/.local/state/herdr-machine0"

DEFAULTS: Dict[str, Any] = {
    "region": "us-west",
    "gpu_region": "us-east",
    "default_size": "large",
    "image": "m0-spoke",
    "base_image": "ubuntu-24-04-loaded",
    "profile": "m0",
    "ssh_key": "m0-hub",
    "spoke_user": "ubuntu",
    "idle_minutes": 120,
    "load_threshold": 0.3,
    "check_interval_s": 300,
    "brokered_providers": ["openai-codex", "radius"],
    "credential_min_validity_h": 24,
    "usage_ttl_s": 60,
    # How the relay socket reaches a spoke: "unix" (OpenSSH streamlocal, machine0)
    # or "tcp" (a loopback port plus a socket shim, for sshds without streamlocal).
    "forward": "unix",
    "dotfiles_url": "https://github.com/justmytwospence/dotfiles.git",
    "default_harness": "pi",
    # The spoke-side entry point (spokes link ~/.local/bin/spoke to their checkout).
    "spoke_command": "~/.local/bin/spoke",
    # Spokes that are not machine0 VMs (always "running"): {name: {host, user, home}}.
    "static_spokes": {},
    # Hub files that a paste may reference and that are copied to the spoke.
    "paste_dirs": ["/tmp", "/var/tmp", "~/.local/state/herdr-machine0/inbox"],
    "paste_max_bytes": 64 * 1024 * 1024,
}

# Only these secrets ever leave the hub.
SPOKE_SECRETS = ("ANTHROPIC_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL_API_KEY", "TYPESAFE_API_KEY")

HARNESSES = ("pi", "claude", "codex", "opencode")


def settings() -> Dict[str, Any]:
    merged = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        if isinstance(user, dict):
            merged.update(user)
    except (OSError, ValueError):
        pass
    return merged


def role() -> str:
    env = os.environ.get("HERDR_MACHINE0_ROLE")
    if env:
        return env
    try:
        with open(ROLE_FILE) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def state_path(*parts: str) -> str:
    path = os.path.join(STATE_DIR, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def static_spoke(name: str) -> Dict[str, Any]:
    entry = (settings().get("static_spokes") or {}).get(name)
    return entry if isinstance(entry, dict) else {}


def spoke_user(name: str) -> str:
    return static_spoke(name).get("user") or settings()["spoke_user"]


def spoke_home(name: str) -> str:
    entry = static_spoke(name)
    if entry.get("home"):
        return entry["home"]
    user = spoke_user(name)
    return settings().get("spoke_home") or ("/root" if user == "root" else "/home/" + user)


def ssh_base() -> list:
    """Every ssh to a spoke goes through the managed config only."""
    return ["ssh", "-F", SSH_CONFIG]


def slot_dir(spoke: str, slot: str) -> str:
    """The hub pane's cwd for one slot. herdr restores panes in their saved cwd,
    which is how a restored pane is matched back to its slot."""
    path = os.path.join(STATE_DIR, "panes", spoke, slot)
    os.makedirs(path, exist_ok=True)
    return path


def parse_env_file(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def render_env_file(values: Dict[str, str]) -> str:
    lines = ["# written by herdr-machine0; do not commit"]
    for key in sorted(values):
        value = values[key].replace("'", "'\"'\"'")
        lines.append("export %s='%s'" % (key, value))
    return "\n".join(lines) + "\n"


def read_secrets() -> Dict[str, str]:
    try:
        with open(SECRETS_FILE) as f:
            return parse_env_file(f.read())
    except OSError:
        return {}


def write_secrets(values: Dict[str, str]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SECRETS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render_env_file(values))
    os.replace(tmp, SECRETS_FILE)


def spoke_secrets(values: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in values.items() if k in SPOKE_SECRETS and v}

"""ssh aliases for spokes: `m0-<name>` in ~/.ssh/config.d/herdr-machine0.

A spoke's IP changes on every resume, and cloud-init may give a resumed or
cloned VM new host keys, so host keys are pinned per alias (HostKeyAlias) in a
private known_hosts that is reset whenever the IP changes: trust on first use
after each create or resume. Nothing reaches a spoke except through these
aliases.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, Optional

from . import config

BEGIN = "# >>> herdr-machine0 %s"
END = "# <<< herdr-machine0 %s"
INCLUDE = "Include ~/.ssh/config.d/*"


def alias(name: str) -> str:
    return "m0-" + name


def block(name: str, ip: str, user: str) -> str:
    a = alias(name)
    return "\n".join([
        BEGIN % name,
        "Host %s" % a,
        "  HostName %s" % ip,
        "  User %s" % user,
        "  HostKeyAlias %s" % a,
        "  UserKnownHostsFile %s" % config.KNOWN_HOSTS,
        "  StrictHostKeyChecking accept-new",
        "  IdentitiesOnly yes",
        "  IdentityFile ~/.ssh/id_ed25519",
        "  ServerAliveInterval 15",
        "  ServerAliveCountMax 4",
        "  ControlMaster no",
        "  ControlPath none",
        "  ForwardAgent no",
        END % name,
        "",
    ])


def parse(text: str) -> Dict[str, str]:
    """name -> ip of every managed block."""
    out: Dict[str, str] = {}
    for m in re.finditer(r"# >>> herdr-machine0 (\S+)\n.*?HostName (\S+)", text, re.S):
        out[m.group(1)] = m.group(2)
    return out


def without(text: str, name: str) -> str:
    pattern = re.escape(BEGIN % name) + r".*?" + re.escape(END % name) + r"\n?"
    return re.sub(pattern, "", text, flags=re.S)


def read() -> str:
    try:
        with open(config.SSH_CONFIG) as f:
            return f.read()
    except OSError:
        return ""


def write(text: str) -> None:
    os.makedirs(os.path.dirname(config.SSH_CONFIG), mode=0o700, exist_ok=True)
    tmp = config.SSH_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, config.SSH_CONFIG)


def forget_host_key(name: str) -> None:
    if os.path.exists(config.KNOWN_HOSTS):
        subprocess.run(["ssh-keygen", "-R", alias(name), "-f", config.KNOWN_HOSTS],
                       capture_output=True)


def ensure_include() -> None:
    """~/.ssh/config must include config.d; the include goes first (ssh uses the first match)."""
    path = os.path.expanduser("~/.ssh/config")
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    if INCLUDE in text:
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.islink(path):
        raise RuntimeError("~/.ssh/config is a symlink; add `%s` to it by hand" % INCLUDE)
    with open(path, "w") as f:
        f.write(INCLUDE + "\n\n" + text)
    os.chmod(path, 0o600)


def update(name: str, ip: str, user: Optional[str] = None) -> bool:
    """Point the alias at ip. True when it changed (and the host key was forgotten)."""
    text = read()
    current = parse(text).get(name)
    if current == ip:
        return False
    write(without(text, name) + block(name, ip, user or config.spoke_user(name)))
    forget_host_key(name)
    return True


def remove(name: str) -> None:
    write(without(read(), name))
    forget_host_key(name)

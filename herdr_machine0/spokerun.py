"""`spoke run`: the spoke side of one slot.

Runs over the slot's ssh connection. Sets up the environment the agent sees
(herdr pane variables pointing at the forwarded relay socket, the spoke's
secrets, brokered credentials), then attaches to the slot's dtach session,
creating it with the harness when it does not exist. The agent outlives the
ssh connection: a hub restart or network blip only detaches.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import socket
import sys
import threading
from typing import Any, Dict, List, Optional

from . import config

SENTINEL = "herdr-machine0-broker"


def spoke_state(*parts: str) -> str:
    path = os.path.join(os.path.expanduser(config.SPOKE_STATE), *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def relay_socket(slot: str) -> str:
    return spoke_state("sock", "%s.sock" % slot)


def harness_command(harness: str, session: Optional[str]) -> List[str]:
    """Resume the slot's own session, else start fresh (never `pi -c`: another
    slot in the same directory may own the most recent session)."""
    if harness == "pi":
        return ["pi", "--session", session] if session else ["pi"]
    if harness == "claude":
        return ["claude", "--resume", session] if session else ["claude"]
    if harness == "codex":
        return ["codex", "resume", session] if session else ["codex"]
    if harness == "opencode":
        return ["opencode", "--session", session] if session else ["opencode"]
    raise ValueError("unknown harness %s" % harness)


def relay_call(path: str, method: str, params: Dict[str, Any], timeout: float = 60) -> Dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall((json.dumps({"id": "spoke:%s" % method, "method": method, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    reply = json.loads(buf.split(b"\n", 1)[0].decode())
    if reply.get("error"):
        raise RuntimeError(reply["error"].get("message") or reply["error"].get("code"))
    return reply.get("result") or {}


def _write_json_private(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".machine0.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def seed_pi(provider: str, cred: Dict[str, Any], agent_dir: Optional[str] = None) -> None:
    agent_dir = agent_dir or os.environ.get("PI_CODING_AGENT_DIR") or os.path.expanduser("~/.pi/agent")
    path = os.path.join(agent_dir, "auth.json")
    try:
        with open(path) as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    store[provider] = dict(cred, refresh=SENTINEL)
    _write_json_private(path, store)


def seed_codex_cli(cred: Dict[str, Any], codex_home: Optional[str] = None) -> None:
    """Best effort: Codex CLI reads ~/.codex/auth.json. last_refresh=now keeps it
    from refreshing (it would fail on the sentinel) until the hub writes again."""
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    if not os.path.isdir(home):
        return
    access = str(cred.get("access") or "")
    _write_json_private(os.path.join(home, "auth.json"), {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": access,
            "access_token": access,
            "refresh_token": SENTINEL,
            "account_id": cred.get("accountId"),
        },
        "last_refresh": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    })


def seed_credentials(sock: str, providers: List[str], log=lambda m: None) -> None:
    for provider in providers:
        try:
            cred = relay_call(sock, "herdr_machine0.credential", {"provider": provider})
        except Exception as e:
            log("credential %s unavailable: %s" % (provider, e))
            continue
        seed_pi(provider, cred)
        if provider == "openai-codex":
            seed_codex_cli(cred)


def tcp_shim(path: str, port: int) -> None:
    """Expose a reverse-forwarded loopback port as the unix socket herdr clients expect."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o077)
    try:
        server.bind(path)
    finally:
        os.umask(old)
    server.listen(32)

    def pipe(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
                upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
            except OSError:
                continue
            threading.Thread(target=pipe, args=(conn, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, conn), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()


def load_secrets_into(env: Dict[str, str]) -> None:
    try:
        with open(config.SECRETS_FILE) as f:
            env.update(config.parse_env_file(f.read()))
    except OSError:
        pass


def run(slot: str, harness: str, cwd: str, pane: str, session: Optional[str],
        tcp_port: Optional[int], providers: List[str]) -> int:
    if harness not in config.HARNESSES:
        print("spoke run: unknown harness %s" % harness, file=sys.stderr)
        return 2
    sock = relay_socket(slot)
    env = dict(os.environ)
    load_secrets_into(env)
    herdr_bin = shutil.which("herdr") or "herdr"
    env.update({
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": pane,
        "HERDR_SOCKET_PATH": sock,
        "HERDR_BIN_PATH": herdr_bin,
        "HERDR_MACHINE0_ROLE": "spoke",
        "HERDR_MACHINE0_SLOT": slot,
        "HERDR_MACHINE0_BROKERED": ",".join(providers),
    })
    env.pop("HERDR_AGENT", None)
    os.environ.update(env)

    dtach_sock = spoke_state("dtach", "%s.sock" % slot)
    alive = os.path.exists(dtach_sock) and _socket_alive(dtach_sock)
    if tcp_port:
        # A running session keeps using this path; rebinding points it at the new port.
        tcp_shim(sock, tcp_port)
    # Every (re)connect refreshes the seeded credentials, so a long-lived Codex
    # CLI session keeps a fresh access token as long as the hub reaches it.
    seed_credentials(sock, providers, log=lambda m: print("spoke run: %s" % m, file=sys.stderr))
    if not alive:
        try:
            os.unlink(dtach_sock)
        except FileNotFoundError:
            pass

    expanded = os.path.expanduser(cwd)
    if not os.path.isdir(expanded):
        print("spoke run: %s does not exist on this spoke" % cwd, file=sys.stderr)
        return 2
    os.chdir(expanded)
    command = harness_command(harness, session)
    argv = ["dtach", "-A", dtach_sock, "-E", "-r", "winch", "-z",
            "zsh", "-c", 'exec "$@"', "zsh"] + command
    if tcp_port:
        # The shim threads must outlive this process's role as dtach client.
        import subprocess
        return subprocess.call(argv, env=env)
    os.execvpe(argv[0], argv, env)
    return 0  # not reached


def _socket_alive(path: str) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()

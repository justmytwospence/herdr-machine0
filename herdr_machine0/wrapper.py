"""`spoke attach`: the hub pane that shows one spoke slot.

The wrapper is the pane's foreground process. It carries HERDR_AGENT=<harness>
in its own environment so herdr applies that agent's screen detection to the
pane (herdr reads the hint from the foreground process; it cannot see past ssh,
and on macOS it cannot read the environment of platform binaries such as ssh
at all). It runs ssh on its own pty, forwards bytes and window size, and runs
the slot's relay.
"""

from __future__ import annotations

import fcntl
import os
import random
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from typing import Any, Dict, List, Optional

from . import broker, config, herdr, machine0, paste, registry, relay, sshconf, usage

BACKOFF = (1, 2, 5, 15, 30)
CLEAR = "\x1b[2J\x1b[H"


class Quit(Exception):
    def __init__(self, close_pane: bool = False):
        super().__init__("quit")
        self.close_pane = close_pane


def _quit(*_: Any) -> None:
    raise Quit()


def ensure_hint(harness: str) -> None:
    """herdr reads the hint from the process environment as exec'd, so re-exec once."""
    if os.environ.get("HERDR_AGENT") == harness:
        return
    env = dict(os.environ, HERDR_AGENT=harness)
    os.execve(sys.executable, [sys.executable, "-B"] + sys.argv, env)


def ssh(alias: str, command: str, input: Optional[bytes] = None, timeout: float = 60) -> int:
    try:
        return subprocess.run(
            config.ssh_base() + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", alias, command],
            input=input, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
        ).returncode
    except subprocess.TimeoutExpired:
        return 255


class Wrapper:
    def __init__(self, spoke: str, slot: str, harness: str, cwd: str):
        self.spoke = spoke
        self.slot = slot
        self.harness = harness
        self.cwd = cwd
        self.cfg = config.settings()
        self.alias = sshconf.alias(spoke)
        self.pane = os.environ.get("HERDR_PANE_ID", "")
        self.herdr_socket = os.environ.get("HERDR_SOCKET_PATH") or herdr.socket_path()
        self.home = config.spoke_home(spoke)
        self.relay_path = config.state_path("relay", "%s__%s.sock" % (spoke, slot))
        self.server = None
        self.tty_attrs = None
        self.child = 0
        self.master = -1
        self.close_pane = False

    # ---- relay -------------------------------------------------------------------

    def handlers(self) -> Dict[str, relay.Handler]:
        brokered = set(self.cfg["brokered_providers"]) | {"anthropic"}

        def credential(params: Dict[str, Any]) -> Dict[str, Any]:
            provider = str(params.get("provider") or "")
            if provider not in brokered or provider == "anthropic":
                raise ValueError("provider %s is not brokered" % provider)
            return broker.get(provider)

        def usage_(params: Dict[str, Any]) -> Dict[str, Any]:
            return usage.get(str(params.get("name") or "claude"))

        def open_slot(params: Dict[str, Any]) -> Dict[str, Any]:
            from . import hub
            label = str(params.get("label") or "")
            base = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:24] or "slot"
            slot = registry.next_slot(self.spoke, base)
            harness = str(params.get("harness") or self.harness)
            if harness not in config.HARNESSES:
                raise ValueError("unknown harness %s" % harness)
            pane = hub.open_slot(self.spoke, slot, harness, str(params.get("cwd") or self.cwd),
                                 self.herdr_socket, focus=bool(params.get("focus")))
            return {"slot": slot, "pane_id": pane}

        return {
            "herdr_machine0.credential": credential,
            "herdr_machine0.usage": usage_,
            "herdr_machine0.open_slot": open_slot,
        }

    def on_session(self, harness: str, kind: str, value: str) -> None:
        if harness == self.harness:
            registry.put_slot(self.spoke, self.slot, session=value)

    def start_relay(self) -> None:
        log_path = config.state_path("logs", "relay.log")

        def log(msg: str) -> None:
            with open(log_path, "a") as f:
                f.write("%s %s/%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), self.spoke, self.slot, msg))

        r = relay.Relay(
            self.herdr_socket, self.pane,
            ["spoke", "attach", self.spoke, self.slot, "--harness", self.harness],
            handlers=self.handlers(), on_session=self.on_session, log=log,
        )
        self.server = r.serve(self.relay_path)

    # ---- tokens / screen ---------------------------------------------------------

    def state(self, value: Optional[str]) -> None:
        herdr.set_tokens(self.pane, {"spoke": "%s/%s" % (self.spoke, self.slot), "spoke_state": value},
                         self.herdr_socket)

    def say(self, text: str) -> None:
        sys.stdout.write(text.replace("\n", "\r\n"))
        sys.stdout.flush()

    def raw(self) -> None:
        if self.tty_attrs is None and os.isatty(0):
            self.tty_attrs = termios.tcgetattr(0)
            tty.setraw(0)

    def cooked(self) -> None:
        if self.tty_attrs is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, self.tty_attrs)
            self.tty_attrs = None

    def read_key(self, timeout: float) -> Optional[bytes]:
        r, _, _ = select.select([0], [], [], timeout)
        if not r:
            return None
        data = os.read(0, 1024)
        if not data:
            raise Quit()
        return data

    def suspended_screen(self, status: str) -> None:
        """Wait for Enter (wake), q (close) or someone else waking the spoke."""
        self.state("suspended")
        self.say(CLEAR + "\n  %s/%s: the spoke is %s.\n\n"
                 "  Enter  wake it (a few minutes)\n  q      close this pane\n\n" % (self.spoke, self.slot, status.lower()))
        self.raw()
        last_poll = time.time()
        while True:
            key = self.read_key(5)
            if key is None:
                if time.time() - last_poll > 30:
                    last_poll = time.time()
                    try:
                        now = machine0.status(machine0.get(self.spoke))
                    except machine0.Machine0Error:
                        continue
                    if now == machine0.RUNNING:
                        return
                    if now != status:
                        status = now
                        self.say("  (now %s)\n" % now.lower())
                continue
            if key in (b"q", b"Q"):
                raise Quit(close_pane=True)
            if b"\r" in key or b"\n" in key:
                current = machine0.status(machine0.get(self.spoke))
                if current not in machine0.WAKEABLE:
                    self.say("  %s is %s; Enter works once it has settled.\n" % (self.spoke, current.lower()))
                    continue
                try:
                    self.wake()
                except machine0.Machine0Error as e:
                    self.say("\n  wake failed: %s\n  Enter retries, q closes.\n" % e)
                    continue
                return
            if key.startswith(b"\x1b[200~"):
                self.say("  (paste discarded: the spoke is not running)\n")
            else:
                self.say("  (input discarded: the spoke is not running; Enter wakes it)\n")

    def wake(self) -> None:
        self.state("waking")
        self.say("\n  Waking %s...\n" % self.spoke)
        machine0.start(self.spoke)
        m = machine0.wait_running(self.spoke)
        sshconf.update(self.spoke, machine0.ip(m) or "")
        deadline = time.time() + 300
        while ssh(self.alias, "true", timeout=30) != 0:
            if time.time() > deadline:
                raise machine0.Machine0Error("%s is running but ssh does not answer" % self.spoke)
            time.sleep(5)
        herdr.notify("%s is awake" % self.spoke, self.slot, self.herdr_socket)

    # ---- connection --------------------------------------------------------------

    def prepare_remote(self) -> int:
        secrets = config.render_env_file(config.spoke_secrets(config.read_secrets())).encode()
        sock = "%s/.local/state/herdr-machine0/sock/%s.sock" % (self.home, self.slot)
        cmd = ("umask 077; mkdir -p ~/.config/herdr-machine0 ~/.local/state/herdr-machine0/sock "
               "~/.local/state/herdr-machine0/paste && cat > ~/.config/herdr-machine0/secrets.env "
               "&& rm -f %s" % sock)
        return ssh(self.alias, cmd, input=secrets)

    def remote_command(self, tcp_port: Optional[int]) -> str:
        session = (registry.get_slot(self.spoke, self.slot) or {}).get("session")
        argv = [self.cfg["spoke_command"], "run", self.slot, "--harness", self.harness,
                "--cwd", self.cwd, "--pane", self.pane,
                "--providers", ",".join(self.cfg["brokered_providers"])]
        if session:
            argv += ["--session", session]
        if tcp_port:
            argv += ["--tcp-port", str(tcp_port)]
        import shlex
        return " ".join([argv[0]] + [shlex.quote(a) for a in argv[1:]])

    def upload(self, hub_path: str, spoke_path: str) -> None:
        with open(hub_path, "rb") as f:
            data = f.read()
        if ssh(self.alias, "mkdir -p %s && cat > %s" % (os.path.dirname(spoke_path), spoke_path),
               input=data, timeout=120) != 0:
            raise OSError("upload failed")

    def connect(self) -> int:
        """Run one ssh session; returns its exit status (255 = connection failure)."""
        if self.prepare_remote() != 0:
            return 255
        tcp_port = None
        if self.cfg["forward"] == "tcp":
            tcp_port = random.randint(40000, 59999)
            forward = "127.0.0.1:%d:%s" % (tcp_port, self.relay_path)
        else:
            forward = "%s/.local/state/herdr-machine0/sock/%s.sock:%s" % (self.home, self.slot, self.relay_path)
        argv = config.ssh_base() + ["-tt", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=4", "-R", forward, self.alias, self.remote_command(tcp_port)]
        dest = "%s/.local/state/herdr-machine0/paste" % self.home
        filt = paste.PasteFilter(lambda body: paste.rewrite(
            body, self.cfg["paste_dirs"], dest, self.upload, int(self.cfg["paste_max_bytes"])))

        import pty
        pid, master = pty.fork()
        if pid == 0:
            os.execvp(argv[0], argv)
        self.child, self.master = pid, master
        self.resize()
        self.raw()
        self.state("awake")
        try:
            while True:
                try:
                    r, _, _ = select.select([0, master], [], [])
                except InterruptedError:
                    continue
                if 0 in r:
                    data = os.read(0, 65536)
                    if not data:
                        raise Quit()
                    out = filt.feed(data)
                    if out:
                        os.write(master, out)
                if master in r:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    os.write(1, data)
        finally:
            os.close(master)
            self.master = -1
        _, status = os.waitpid(pid, 0)
        self.child = 0
        return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)

    def resize(self, *_: Any) -> None:
        if self.master < 0 or not os.isatty(0):
            return
        size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)
        if self.child:
            try:
                os.kill(self.child, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    # ---- main loop ---------------------------------------------------------------

    def run(self) -> int:
        signal.signal(signal.SIGWINCH, self.resize)
        signal.signal(signal.SIGTERM, _quit)
        signal.signal(signal.SIGHUP, _quit)
        self.start_relay()
        attempt = 0
        code = 0
        try:
            while True:
                try:
                    m = machine0.get(self.spoke)
                except machine0.Machine0Error as e:
                    self.say("\r\n  machine0: %s (retrying)\r\n" % e)
                    time.sleep(15)
                    continue
                st = machine0.status(m)
                if st == "MISSING":
                    self.say("\r\n  %s no longer exists on machine0.\r\n" % self.spoke)
                    return 1
                if st != machine0.RUNNING or not machine0.ip(m):
                    self.suspended_screen(st)
                    attempt = 0
                    continue
                sshconf.update(self.spoke, machine0.ip(m) or "")
                started = time.time()
                code = self.connect()
                if code != 255:
                    return code
                if time.time() - started > 60:
                    attempt = 0
                delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                attempt += 1
                self.state("reconnecting")
                self.cooked()
                self.say("\r\n  connection to %s lost; reconnecting in %ds\r\n" % (self.spoke, delay))
                time.sleep(delay)
        except Quit as q:
            self.close_pane = q.close_pane
            if self.child:
                try:
                    os.kill(self.child, signal.SIGHUP)
                except ProcessLookupError:
                    pass
            return 0
        finally:
            self.cooked()
            self.state(None)
            herdr.set_tokens(self.pane, {"spoke": None}, self.herdr_socket)
            if self.server is not None:
                self.server.close()
            try:
                os.unlink(self.relay_path)
            except OSError:
                pass


def attach(spoke: str, slot: str, harness: Optional[str], cwd: Optional[str], takeover: bool) -> int:
    if config.role() != "hub":
        print("spoke attach runs on the hub", file=sys.stderr)
        return 2
    if not os.environ.get("HERDR_PANE_ID"):
        print("spoke attach must run inside a herdr pane", file=sys.stderr)
        return 2
    entry = registry.get_slot(spoke, slot) or {}
    harness = harness or entry.get("harness") or config.settings()["default_harness"]
    cwd = cwd or entry.get("cwd") or "~"
    if harness not in config.HARNESSES:
        print("unknown harness %s" % harness, file=sys.stderr)
        return 2
    ensure_hint(harness)
    lock = registry.SlotLock(spoke, slot)
    try:
        lock.acquire(takeover=takeover)
    except registry.SlotBusy as e:
        print("%s/%s is already attached (pid %d); use --takeover" % (spoke, slot, e.pid), file=sys.stderr)
        return 1
    w = None
    try:
        registry.put_slot(spoke, slot, harness=harness, cwd=cwd, pane_id=os.environ["HERDR_PANE_ID"])
        w = Wrapper(spoke, slot, harness, cwd)
        code = w.run()
    finally:
        lock.release()
    if w is not None and w.close_pane:
        herdr.quiet("pane.close", {"pane_id": os.environ["HERDR_PANE_ID"]})
    return code

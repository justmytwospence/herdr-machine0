"""A live phase list with an overall bar, for long multi-step commands.

    New spoke amber-falcon   large · us-west · pi

    ✓ Create VM            1m38s
    ⠹ Boot and SSH            12s
    · Sync dotfiles
    · Start agent

    ━━━━━━━━━━━━━━━━━━━━━━──────────────   58%   1m50s

Each phase has an expected duration; the bar moves through a phase in
proportion to elapsed/expected and never claims a phase is done before it is.
Command output goes to a log file instead of the screen (`log` below); a failed
phase prints the log's tail. Without a terminal it degrades to one line per
phase.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from typing import IO, Dict, Iterator, List, Optional, Tuple

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
GREEN, RED, DIM, BOLD, RESET = "\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[1m", "\x1b[0m"
BAR_WIDTH = 36


def fmt(seconds: float) -> str:
    seconds = int(seconds)
    return "%dm%02ds" % divmod(seconds, 60) if seconds >= 60 else "%ds" % seconds


class Phase:
    def __init__(self, key: str, label: str, expected: float):
        self.key, self.label, self.expected = key, label, max(expected, 1.0)
        self.state = "pending"  # pending | running | done | failed | skipped
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self.note = ""

    def elapsed(self, now: float) -> float:
        if self.started is None:
            return 0.0
        return (self.ended or now) - self.started


class Progress:
    def __init__(self, title: str, subtitle: str, phases: List[Tuple[str, str, float]],
                 log_path: str, stream: IO[str] = sys.stderr):
        self.title, self.subtitle = title, subtitle
        self.phases: Dict[str, Phase] = {k: Phase(k, l, e) for k, l, e in phases}
        self.order = [k for k, _, _ in phases]
        self.stream = stream
        self.tty = stream.isatty() and os.environ.get("TERM") != "dumb"
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self.log: IO[str] = open(log_path, "a", buffering=1)
        self.started = time.time()
        self.lines = 0
        self.tick = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # ---- lifecycle -----------------------------------------------------------

    def __enter__(self) -> "Progress":
        if self.tty:
            self.stream.write("\x1b[?25l")  # hide the cursor
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        else:
            self.stream.write("%s  %s\n" % (self.title, self.subtitle))
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join()
        self.render()
        if self.tty:
            self.stream.write("\x1b[?25h")
        self.stream.flush()
        self.log.close()

    def _loop(self) -> None:
        while not self.stop.wait(0.1):
            self.tick += 1
            self.render()

    # ---- phases --------------------------------------------------------------

    def expect(self, key: str, seconds: float) -> None:
        self.phases[key].expected = max(seconds, 1.0)

    def skip(self, key: str, note: str = "") -> None:
        with self.lock:
            self.phases[key].state, self.phases[key].note = "skipped", note

    @contextlib.contextmanager
    def step(self, key: str) -> Iterator[Phase]:
        phase = self.phases[key]
        with self.lock:
            phase.state, phase.started = "running", time.time()
        self.log.write("\n==> %s\n" % phase.label)
        if not self.tty:
            self.stream.write("  ... %s\n" % phase.label)
            self.stream.flush()
        try:
            yield phase
        except BaseException:
            with self.lock:
                phase.state, phase.ended = "failed", time.time()
            raise
        with self.lock:
            phase.state, phase.ended = "done", time.time()
        if not self.tty:
            self.stream.write("  ok  %s (%s)\n" % (phase.label, fmt(phase.elapsed(time.time()))))
            self.stream.flush()

    def note(self, key: str, text: str) -> None:
        with self.lock:
            self.phases[key].note = text

    # ---- rendering -----------------------------------------------------------

    def fraction(self, now: float) -> float:
        total = sum(p.expected for p in self.phases.values() if p.state != "skipped") or 1.0
        done = 0.0
        for p in self.phases.values():
            if p.state == "done":
                done += p.expected
            elif p.state == "running":
                # Approach, never reach, the phase's share while it runs.
                done += p.expected * min(p.elapsed(now) / p.expected, 0.95)
        return min(done / total, 1.0)

    def frame(self, now: float) -> List[str]:
        out = ["", "  %s%s%s   %s%s%s" % (BOLD, self.title, RESET, DIM, self.subtitle, RESET), ""]
        width = max(len(p.label) for p in self.phases.values()) + 2
        for key in self.order:
            p = self.phases[key]
            if p.state == "done":
                mark, label = GREEN + "✓" + RESET, p.label
            elif p.state == "failed":
                mark, label = RED + "✗" + RESET, p.label
            elif p.state == "running":
                mark, label = SPINNER[self.tick % len(SPINNER)], BOLD + p.label + RESET
            elif p.state == "skipped":
                mark, label = DIM + "–" + RESET, DIM + p.label + RESET
            else:
                mark, label = DIM + "·" + RESET, DIM + p.label + RESET
            time_text = fmt(p.elapsed(now)) if p.state in ("running", "done", "failed") else ""
            pad = " " * (width - len(p.label))
            note = ("  " + DIM + p.note + RESET) if p.note else ""
            out.append("  %s %s%s%6s%s" % (mark, label, pad, time_text, note))
        frac = self.fraction(now)
        filled = int(round(frac * BAR_WIDTH))
        failed = any(p.state == "failed" for p in self.phases.values())
        color = RED if failed else GREEN
        bar = color + "━" * filled + RESET + DIM + "─" * (BAR_WIDTH - filled) + RESET
        out += ["", "  %s  %3d%%   %s" % (bar, int(frac * 100), fmt(now - self.started)), ""]
        return out

    def render(self) -> None:
        if not self.tty:
            return
        with self.lock:
            lines = self.frame(time.time())
            buf = []
            if self.lines:
                buf.append("\x1b[%dA" % self.lines)
            for line in lines:
                buf.append("\r\x1b[2K" + line + "\n")
            self.stream.write("".join(buf))
            self.stream.flush()
            self.lines = len(lines)

    def tail(self, n: int = 12) -> List[str]:
        try:
            with open(self.log_path, errors="replace") as f:
                return [l.rstrip("\n") for l in f.readlines()[-n:]]
        except OSError:
            return []


class NullProgress:
    """Same interface, no display: for callers that just want the work done."""

    log: Optional[IO[str]] = None

    def __enter__(self) -> "NullProgress":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def expect(self, key: str, seconds: float) -> None:
        pass

    def skip(self, key: str, note: str = "") -> None:
        pass

    def note(self, key: str, text: str) -> None:
        pass

    @contextlib.contextmanager
    def step(self, key: str) -> Iterator[None]:
        yield None

    def tail(self, n: int = 12) -> List[str]:
        return []

"""Tests run against a throwaway HOME so nothing touches real config, ssh or state.

Every test module imports this package first, so the sandbox holds however the
tests are run (discover with or without -t, a single module, pytest). Run from
anywhere but the repo root, `import tests` fails before anything is written.
"""

import os
import sys
import tempfile

if any(m == "herdr_machine0" or m.startswith("herdr_machine0.") for m in sys.modules):
    raise RuntimeError("herdr_machine0 was imported before the test sandbox; its paths point at the real HOME")

_HOME = tempfile.mkdtemp(prefix="herdr-machine0-test-")
os.environ["HOME"] = _HOME
os.environ["HERDR_MACHINE0_CONFIG_DIR"] = os.path.join(_HOME, ".config", "herdr-machine0")
os.environ["HERDR_MACHINE0_STATE_DIR"] = os.path.join(_HOME, ".local", "state", "herdr-machine0")
os.environ["HERDR_MACHINE0_ROLE"] = "hub"
for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_AGENT", "PI_CODING_AGENT_DIR", "CODEX_HOME"):
    os.environ.pop(key, None)

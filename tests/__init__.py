"""Tests run against a throwaway HOME so nothing touches real config, ssh or state."""

import os
import tempfile

_HOME = tempfile.mkdtemp(prefix="herdr-machine0-test-")
os.environ["HOME"] = _HOME
os.environ["HERDR_MACHINE0_CONFIG_DIR"] = os.path.join(_HOME, ".config", "herdr-machine0")
os.environ["HERDR_MACHINE0_STATE_DIR"] = os.path.join(_HOME, ".local", "state", "herdr-machine0")
os.environ["HERDR_MACHINE0_ROLE"] = "hub"
for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_AGENT", "PI_CODING_AGENT_DIR", "CODEX_HOME"):
    os.environ.pop(key, None)

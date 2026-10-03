#!/usr/bin/env python3
"""herdr-machine0 entry point: `spoke <command>` (linked to ~/.local/bin/spoke).

Also the plugin's command for herdr hooks and actions (see herdr-plugin.toml).
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from herdr_machine0.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

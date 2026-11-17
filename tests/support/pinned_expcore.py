"""`python -m expcore ARGS`, with the core's identity pinned as the suite pins it (`core_pin`).

A test that runs a command in a process of its own runs `<python> pinned_expcore.py ARGS`, so that the command sees
the core's identity the test's own process does, on a checkout with uncommitted changes too.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # this process only: it runs one command and exits

import core_pin

from expcore.cli import main

if __name__ == "__main__":
    core_pin.apply()
    sys.exit(main(sys.argv[1:]))

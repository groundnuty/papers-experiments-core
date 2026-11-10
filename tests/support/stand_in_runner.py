"""An interpreter for scripts/fresh_repo.py's own test: `python -m expcore` with stand-in wheels, or `python -c`.

The core is installed editable where the tests run, and packing the real environment's wheels on every run costs
minutes, so the fresh-repository test runs its study steps through this file: `<python> stand_in_runner.py -m expcore
ARGS` runs the command line with every vendored wheel a stand-in (`package_support.stand_in_vendor`), and
`<python> stand_in_runner.py -c CODE ARGS` runs the code as `python -c` would. The fresh-repo workflow runs the real
thing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from package_support import stand_in_vendor

from expcore.package import build


def main(argv: list[str]) -> int:
    if argv[:2] == ["-m", "expcore"]:
        from expcore.cli import main as expcore_main

        build.vendor_wheels = stand_in_vendor
        return expcore_main(argv[2:])
    if argv[:1] == ["-c"] and len(argv) >= 2:
        sys.argv = ["-c", *argv[2:]]
        exec(compile(argv[1], "<string>", "exec"), {"__name__": "__main__"})
        return 0
    print(f"stand_in_runner: expected `-m expcore ...` or `-c CODE ...`, got {argv[:2]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

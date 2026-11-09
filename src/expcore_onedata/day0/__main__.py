# Provenance: new
"""`python -m expcore_onedata.day0 <platform.toml> --out <directory>`: run the day-0 checks and write a dated report.

The target must be named: there is no default configuration and nothing is read from the environment to find one. The
report (JSON and Markdown) goes into the directory the caller names. The checks that change state run only with
`EXPCORE_LIVE_MUTATE=1` in the environment; without it they report unknown and send nothing.

Exit status: 0 every check passed, 1 a check failed, 3 nothing failed but something could not be measured, 2 a usage
or configuration error.
"""

import argparse
import sys
from collections.abc import Sequence

from expcore.config.loading import ConfigError
from expcore_onedata.day0.registry import REGISTRY
from expcore_onedata.day0.runner import EXIT_USAGE, run


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line, run, and print where the report is; return the exit status."""
    parser = argparse.ArgumentParser(prog="python -m expcore_onedata.day0", description=__doc__.split("\n\n")[0])
    parser.add_argument("platform", nargs="?", help="the platform.toml of the deployment to check (required to run)")
    parser.add_argument("--out", help="the directory the dated report is written to (required to run)")
    parser.add_argument("--only", action="append", metavar="CHECK", help="run only this check; repeatable")
    parser.add_argument("--list", action="store_true", help="list the registered checks and exit")
    args = parser.parse_args(argv)
    if args.list:
        for check in REGISTRY.values():
            print(f"{check.name}{' (changes state)' if check.mutates else ''}: {check.title}")
        return 0
    if not args.platform or not args.out:
        parser.print_usage(sys.stderr)
        print("error: name the platform.toml to check and the directory for the report (--out)", file=sys.stderr)
        return EXIT_USAGE
    try:
        code, json_path, md_path = run(args.platform, args.out, names=args.only)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    print(f"report: {json_path}\nreport: {md_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())

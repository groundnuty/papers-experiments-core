# Provenance: new
"""Freeze and check a condition's tool manifest from the server exactly as the launch starts it (O84, L13).

    python -m expcore_onedata.toolmanifest freeze PLATFORM CONDITION [OPTIONS]
    python -m expcore_onedata.toolmanifest check PLATFORM CONDITION [OPTIONS]

    --pass-env NAME   give the server this variable of your environment, by name (repeatable)
    --token-env NAME  take the server's token from this variable, by name (default: a fixed placeholder)
    --timeout S       seconds to wait for the server (default: the platform's `[toolserver] start_timeout_s`)

The frozen manifest hashes what the server shows, and the server shows what it is configured with: its allow-list, its
hosts, whether it reads a `.env` file, and the directory it starts in. A manifest read from a server the launch would
not have started (another environment, another directory) can freeze tools no run sees. So this command line builds
nothing of its own: it starts the server from `toolserver.server_start`, the one definition the launch uses too, with
the command and arguments of `[toolserver]` (the resolve launcher included), the environment `server_environment`
builds for the condition, and a new, empty directory of mode 0700 for each start, removed once the server has stopped.

`freeze` writes the manifest to the file the condition declares (`manifest` in `[toolserver.conditions]`), which need
not exist yet. `check` compares the live server with that file, and says so when it is missing. The platform loads
without its manifests, so one condition is frozen or checked before the others are frozen.

`--pass-env` is for the variables a study's server needs and the launch's caller passes (`extra_env`); it refuses every
variable the launch sets itself (`toolserver.MANAGED_ENV` and `toolserver.OVERRIDDEN_ENV`), the word that stops the
`.env` read among them, and a name the environment does not hold. The tool list is taken not to depend on the server's
token (only the stand-in server has been shown to behave so), so without `--token-env` both token settings hold a fixed
placeholder that is no token of any run; a server that checks its token when it starts takes the value of a variable
named by `--token-env` (never a value on the command line).

Each exits 0 on success, 1 on a mismatch (`check`, listed on standard error) and 2 on an error. It verifies the
pinned checkout first, as a launch does (when `[toolserver]` pins one), and refuses a clone at another commit or with a
local change, naming which, before anything is started or written. It starts the server and reads its tools; it
contacts no federation.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mcp import StdioServerParameters

from expcore.config.loading import ConfigError
from expcore.tools.manifest import ManifestError, check_server, freeze_server
from expcore_onedata.config import load_platform
from expcore_onedata.pin import PinError, verify_configured
from expcore_onedata.toolserver import MANAGED_ENV, OVERRIDDEN_ENV, server_start

FREEZE_TOKEN = "expcore-manifest-freeze-placeholder"  # not a credential of any run: the tools do not depend on it


class _Token:
    """A token value, which is all `server_environment` asks of one."""

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value


def _value(name: str, option: str) -> str:
    """The value of a variable the command line names, which must be set: a freeze with a variable missing is not the
    environment the launch builds."""
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{option} names {name}, which is not set or is empty in this environment")
    return value


def _run(args: argparse.Namespace) -> int:
    """Start the condition's server as the launch does and freeze or check its tools; return the exit status."""
    fixed = sorted(set(args.pass_env) & {*MANAGED_ENV, *OVERRIDDEN_ENV})
    if fixed:  # refused before anything is read or started
        raise ValueError(f"--pass-env cannot pass {', '.join(fixed)}: the launch sets these itself")
    extra = {name: _value(name, "--pass-env") for name in args.pass_env}
    token = _Token(FREEZE_TOKEN if args.token_env is None else _value(args.token_env, "--token-env"))
    config = load_platform(args.platform, require_secrets=False, require_manifests=False)  # one condition at a time
    condition = config.condition(args.condition)  # refuses a platform with no [toolserver], and an undeclared name
    server = config.toolserver
    if server is None:
        raise ValueError(f"{config.file}: has no [toolserver] section")
    timeout_s = server.start_timeout_s if args.timeout is None else args.timeout
    verify_configured(server)  # as a launch does first: never a manifest from a clone that is not the pinned one
    with server_start(config, condition, token, extra) as begun:
        params = StdioServerParameters(command=begun.command, args=begun.args, env=begun.env, cwd=begun.cwd)
        run = freeze_server if args.action == "freeze" else check_server
        return run(condition.manifest, params, timeout_s)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return 0 on success, 1 on a mismatch, 2 on an error."""
    parser = argparse.ArgumentParser(
        prog="python -m expcore_onedata.toolmanifest",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s {freeze,check} PLATFORM CONDITION [--pass-env NAME ...] [--token-env NAME] [--timeout S]",
    )
    parser.add_argument("action", choices=["freeze", "check"])
    parser.add_argument("platform", type=Path, help="the platform.toml whose [toolserver] declares the condition")
    parser.add_argument("condition", help="the condition's name, as [toolserver.conditions] declares it")
    parser.add_argument("--pass-env", action="append", default=[], metavar="NAME", help="give the server this variable")
    parser.add_argument("--token-env", metavar="NAME", help="take the server's token from this variable")
    parser.add_argument("--timeout", type=float, metavar="S", help="seconds to wait for the server")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (ConfigError, ManifestError, PinError, ValueError, OSError, TimeoutError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

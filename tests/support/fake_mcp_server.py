"""A tiny real MCP stdio server for the client tests, built with the SDK's server API.

    python fake_mcp_server.py --tools echo,add [--suffix TEXT] [--hang] [--pidfile PATH]

`--tools` is the server-side allow-list: only the named tools of the catalogue are registered. `--suffix` changes
every description. `--hang` makes the server never speak. `--pidfile` records the process id, so a test can see that
a failed start left no child behind.
"""

import argparse
import os
import sys
import time

from mcp.server import MCPServer


def echo(text: str) -> str:
    return text


def add(a: int, b: int) -> int:
    return a + b


def fail(message: str) -> str:
    raise RuntimeError(message)


def crash() -> str:
    os._exit(3)  # the connection closes under the caller


def extra() -> str:
    return "extra"


CATALOGUE = {
    "echo": (echo, "Echo the text back."),
    "add": (add, "Add two integers."),
    "fail": (fail, "Always fails with the given message."),
    "crash": (crash, "Kills the server."),
    "extra": (extra, "A tool nobody froze."),
}


def build(tools: list[str], suffix: str) -> MCPServer:
    server = MCPServer("fake-tools")
    for name in tools:
        function, description = CATALOGUE[name]
        server.tool(name=name, description=description + suffix)(function)
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tools", default="echo,add")
    parser.add_argument("--suffix", default="")
    parser.add_argument("--hang", action="store_true")
    parser.add_argument("--pidfile")
    args = parser.parse_args()
    if args.pidfile:
        with open(args.pidfile, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    if args.hang:
        time.sleep(3600)
        sys.exit(0)
    build([t for t in args.tools.split(",") if t], args.suffix).run("stdio")


if __name__ == "__main__":
    main()

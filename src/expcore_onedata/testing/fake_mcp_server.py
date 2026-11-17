# Provenance: new
"""A tiny real MCP stdio server for tests of a tool client, built with the SDK's server API.

    python -m expcore_onedata.testing.fake_mcp_server --tools echo,add [--suffix TEXT] [--hang]

`--tools` is the server-side allow-list: only the named tools of the catalogue are registered. `--suffix` changes
every description. `--hang` makes the server never speak.

Models: an MCP server over stdio that lists and calls tools, with a small catalogue (`CATALOGUE`: `echo`, `add`, `fail`
that raises, `crash` that kills the process, `extra`, a tool nobody froze, and `stall`, a call the server accepts and
never answers while it stays up), the allow-list, and a server that hangs. Does not model: any real server's tools, its
settings or its behaviour under load; the Onedata server is `fake_onedata_mcp`, which is configured from this one.
"""

import argparse
import asyncio
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


async def stall() -> str:
    await asyncio.sleep(3600)  # the server stays responsive; only this call never returns
    return "stalled"


CATALOGUE = {
    "echo": (echo, "Echo the text back."),
    "add": (add, "Add two integers."),
    "fail": (fail, "Always fails with the given message."),
    "crash": (crash, "Kills the server."),
    "extra": (extra, "A tool nobody froze."),
    "stall": (stall, "Accepts the call and never answers it."),
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
    args = parser.parse_args()
    if args.hang:
        time.sleep(3600)
        sys.exit(0)
    build([t for t in args.tools.split(",") if t], args.suffix).run("stdio")


if __name__ == "__main__":
    main()

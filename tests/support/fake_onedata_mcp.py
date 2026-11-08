"""A stand-in for the Onedata MCP server: the generic fake server, configured the way the real one is, by environment.

The allow-list comes from ONEDATA_MCP_TOOLS, as the real server reads it. When FAKE_MCP_RECORD names a file, the
server writes the Onedata settings it received there as JSON, so a test can see what the launcher passed it. The
values are test values.
"""

import json
import os
import sys

from fake_mcp_server import build

SETTINGS = (
    "ONEDATA_ONEZONE_HOST",
    "ONEDATA_ONEZONE_TOKEN",
    "ONEDATA_ONEPROVIDER_HOST",
    "ONEDATA_ONEPROVIDER_TOKEN",
    "ONEDATA_MCP_TOOLS",
    "ONEDATA_ALLOW_INSECURE_TLS",
)


def main() -> None:
    record = os.environ.get("FAKE_MCP_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as out:
            json.dump({name: os.environ.get(name) for name in SETTINGS}, out)
    tools = [t for t in os.environ.get("ONEDATA_MCP_TOOLS", "").split(",") if t]
    if "--hang" in sys.argv:
        import time

        time.sleep(3600)
    build(tools, "").run("stdio")


if __name__ == "__main__":
    main()

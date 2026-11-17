"""onedata-mcp's reading of `ONEDATA_MCP_NODE_NAMES` (`onedata_mcp.config.get_node_names`), for the tests that build it
(O70).

The value is a JSON object whose keys are real host names or addresses, each bare, `host:port` or `[ipv6]:port`, and
whose values are non-empty declared names; the server reads each key by its host (port and brackets off, lower case) and
refuses one host under two names.
"""

import json
import re

_BRACKETED = re.compile(r"\[(?P<host>[^\[\]]+)\](?::\d{1,5})?")
_HOST_PORT = re.compile(r"(?P<host>[^:]+):\d{1,5}")


def server_reads(value: str | dict[str, str]) -> dict[str, str]:
    """The `(host -> declared name)` the server builds from the value; AssertionError where the server refuses it."""
    names = json.loads(value) if isinstance(value, str) else value
    assert isinstance(names, dict)
    read: dict[str, str] = {}
    for key, declared in names.items():
        host = key.strip().lower()
        for pattern in (_BRACKETED, _HOST_PORT):
            found = pattern.fullmatch(key.strip())
            if found:
                host = found.group("host").lower()
                break
        assert host, "an empty key"
        assert not any(c.isspace() or c in "/\\\"'" for c in host), "a key the server refuses"
        assert isinstance(declared, str)
        assert declared.strip(), "an empty declared name"
        assert read.setdefault(host, declared) == declared, "one host under two names"
    return read

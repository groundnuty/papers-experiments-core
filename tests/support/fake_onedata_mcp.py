"""A stand-in for the Onedata MCP server: the generic fake server, configured the way the real one is, by environment.

The allow-list comes from ONEDATA_MCP_TOOLS, as the real server reads it. When FAKE_MCP_RECORD names a file, the
server writes the Onedata settings it received there as JSON, so a test can see what the launcher passed it. When
FAKE_MCP_TLS_PROBE names an HTTPS URL, the server first opens it as the real server opens the federation, an httpx
client that verifies unless ONEDATA_ALLOW_INSECURE_TLS is "true" and otherwise reads its CA bundle from the environment
as httpx does (SSL_CERT_FILE), and records the answer's status, or that the connection was refused, as `tls_probe`.
The values are test values.
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
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def probe_tls(url: str) -> str:
    """Open `url` with the real server's TLS choice; return the status, or "refused" and the error's class."""
    import httpx

    verify = os.environ.get("ONEDATA_ALLOW_INSECURE_TLS", "false").lower() != "true"
    try:
        with httpx.Client(verify=verify, timeout=10) as client:
            return str(client.get(url).status_code)
    except httpx.HTTPError as error:
        return f"refused ({type(error).__name__})"


def main() -> None:
    seen: dict[str, str | None] = {name: os.environ.get(name) for name in SETTINGS}
    probe = os.environ.get("FAKE_MCP_TLS_PROBE")
    if probe:
        seen["tls_probe"] = probe_tls(probe)
    record = os.environ.get("FAKE_MCP_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as out:
            json.dump(seen, out)
    tools = [t for t in os.environ.get("ONEDATA_MCP_TOOLS", "").split(",") if t]
    if "--hang" in sys.argv:
        import time

        time.sleep(3600)
    build(tools, "").run("stdio")


if __name__ == "__main__":
    main()

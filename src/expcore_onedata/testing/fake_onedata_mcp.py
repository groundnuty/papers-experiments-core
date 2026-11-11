# Provenance: new
"""A stand-in for the Onedata MCP server: the generic fake server, configured the way the real one is, by environment.

    python -m expcore_onedata.testing.fake_onedata_mcp [--hang]

The allow-list comes from ONEDATA_MCP_TOOLS, as the real server reads it. When FAKE_MCP_RECORD names a file, the
server writes the Onedata settings it received there as JSON, so a test can see what the launcher passed it, with the
SHA-256 and the mode of the file SSL_CERT_FILE names. The values are test values.

When FAKE_MCP_TLS_PROBE names HTTPS URLs (separated by commas), the server first opens each with an httpx client that
verifies unless ONEDATA_ALLOW_INSECURE_TLS is "true" and otherwise takes its CA bundle from the environment as httpx
does (SSL_CERT_FILE), and records each answer's status, or that the connection was refused, as `tls_probe`. That client
is this stand-in's own, written to open the federation the way the real server is known to (an httpx client whose
`verify` is that flag): it checks what the launcher passes, not the real server's behaviour, which the gated live test
in `tests/onedata/test_toolserver.py` checks (EXPCORE_LIVE_TOOLSERVER).

Models: how the real server is started and configured (its environment variables, its tool allow-list, its start
and its hang) and a small catalogue of tools behind it. Does not model: the real server's tools (`echo`, `add` and the
rest are not Onedata's), its answers, or what it does with the federation: a launcher that passes it a token learns
only that the token arrived.
"""

import hashlib
import json
import os
import stat
import sys

from expcore_onedata.testing.fake_mcp_server import build

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
    """Open `url` with the flag's TLS choice; return the status, or "refused" and the error's class."""
    import httpx

    verify = os.environ.get("ONEDATA_ALLOW_INSECURE_TLS", "false").lower() != "true"
    try:
        with httpx.Client(verify=verify, timeout=10) as client:
            return str(client.get(url).status_code)
    except httpx.HTTPError as error:
        return f"refused ({type(error).__name__})"


def bundle_facts(path: str | None) -> dict[str, str | None]:
    """The SHA-256 and the permission bits of the CA bundle the environment names, as the server found it."""
    if not path or not os.path.exists(path):
        return {"ssl_cert_file_sha256": None, "ssl_cert_file_mode": None}
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    return {"ssl_cert_file_sha256": digest, "ssl_cert_file_mode": oct(stat.S_IMODE(os.stat(path).st_mode))}


def main() -> None:
    seen: dict[str, str | None] = {name: os.environ.get(name) for name in SETTINGS}
    seen.update(bundle_facts(os.environ.get("SSL_CERT_FILE")))
    probe = os.environ.get("FAKE_MCP_TLS_PROBE")
    if probe:
        seen["tls_probe"] = ",".join(probe_tls(url) for url in probe.split(","))
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

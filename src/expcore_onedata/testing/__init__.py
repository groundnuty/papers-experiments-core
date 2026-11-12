# Provenance: new
"""Fakes of the Onedata platform, so a study tests its instruments, probes and scorers without a live federation.

Installed with the platform layer (the `onedata` extra) and imported only by tests: no runtime module of `expcore` or
`expcore_onedata` imports it, and `tests/test_import_boundary.py` holds that. It imports the core, the platform layer
and the standard library, never pytest or anything of a test tree, so a helper that needs pytest (a fixture) is the
study's own thin wrapper around a name here. `docs/interfaces.md` lists the public names with what each models and
what it does not; the study guide shows them in use ("Testing a study's instruments and scorers").

- `MockOnedata`, `Request`: the provider REST surface the fsspec client uses, over a real local HTTP socket.
- `FakeFederation`: an in-process Onezone (tokens, privileges, the scope of the study space).
- `FakeData`, `Node`, `Transfer`, `PROVIDER`, `ROOT_ID`, `ERRORS_25_0`, `ERRORS_LEGACY`: Onezone plus every provider's
  data API: files, per-provider block maps, per-reader visibility delays, pending deletes, transfers, and Onedata
  25.0's error shapes (or the older 404 shape).
- `Ticker` and the test world (`make_federation`, `write_platform`, `write_with_placement`, `tool_plane_replicates`
  and the names the default `platform.toml` holds).
- Two stand-in MCP servers, run as `python -m expcore_onedata.testing.fake_mcp_server` and `... .fake_onedata_mcp`.
  They are not imported here, so importing this package does not load the MCP SDK.
"""

from expcore_onedata.testing.fake_data import ERRORS_25_0, ERRORS_LEGACY, PROVIDER, ROOT_ID, FakeData, Node, Transfer
from expcore_onedata.testing.fake_federation import FakeFederation
from expcore_onedata.testing.fake_onedata import MockOnedata, Request
from expcore_onedata.testing.world import (
    ADMIN_ENV,
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    CEILING_S,
    CREDENTIALS_DIR,
    DATA,
    DATA_PATH_USER,
    MANAGE,
    NOW,
    SERVICE_USER,
    SETTINGS,
    SPACE_NAME,
    SPACE_PREFIX,
    TOOL_SERVER_ARGS,
    TOOL_SERVER_USER,
    USERS,
    Ticker,
    add_users,
    make_federation,
    platform_document,
    tool_plane_replicates,
    with_placement,
    write_platform,
    write_with_placement,
)

__all__ = [
    "ADMIN_ENV",
    "ALPHA_ID",
    "BASELINE",
    "BETA_ID",
    "CEILING_S",
    "CREDENTIALS_DIR",
    "DATA",
    "DATA_PATH_USER",
    "ERRORS_25_0",
    "ERRORS_LEGACY",
    "MANAGE",
    "NOW",
    "PROVIDER",
    "ROOT_ID",
    "SERVICE_USER",
    "SETTINGS",
    "SPACE_NAME",
    "SPACE_PREFIX",
    "TOOL_SERVER_ARGS",
    "TOOL_SERVER_USER",
    "USERS",
    "FakeData",
    "FakeFederation",
    "MockOnedata",
    "Node",
    "Request",
    "Ticker",
    "Transfer",
    "add_users",
    "make_federation",
    "platform_document",
    "tool_plane_replicates",
    "with_placement",
    "write_platform",
    "write_with_placement",
]

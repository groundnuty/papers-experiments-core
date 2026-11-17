"""O73: a client of the federation's services is built in one place, `hpcboot.service_client`, or is on the list below.

`hpcfed.py verify-data` built its REST client with no address map, so on a federation of declared names (which resolve
nowhere on the site) it could reach no service (F-B2-1, found live on 17 Nov 2026). Fixing that one call left the next
one to be written the same way, so this scan reads every module of `deploy/hpc-federation`, `deploy/compose-federation`
and `src` (`tests/support/rest_sites.py`) and fails on any construction of a client of a service outside the
constructor that is not allowed here, with the reason it is safe. The scanner is held by planted modules, and the list
by its own counts: an entry with no site, or a second site where one is allowed, fails too.

A client is safe when it needs no map: it dials a loopback address, a name that resolves, or the address it is given,
or it is built from the rendered `platform.toml`, whose `[addresses]` `up` writes once the root holds a map. The clients
the scan cannot see (a shell `curl` in `bundle/`) are no part of it."""

import ast
from pathlib import Path

from rest_sites import MODULE, audit, scan_source, scan_tree

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("deploy/hpc-federation", "deploy/compose-federation", "src")
HPCBOOT = "deploy/hpc-federation/hpcboot.py"
CONSTRUCTOR = (HPCBOOT, "service_client")  # the one place the login node's client of a federation's services is built

# Why a client needs no address map. The wording is the reason on each entry that cites it.
NODE = (
    "run on a service's own node by the bundle, dialling that node's loopback (127.0.0.1) only: no declared name is "
    "dialled and none could resolve there"
)
CELL = (
    "a probe run inside the agent's cell, as the agent would, to record what it can and cannot reach: it holds no "
    "token and builds no client of the login node's"
)
COMPOSE = (
    "a Compose federation's client: its services are at loopback ports (`Service.url`) and its names resolve on the "
    "project's network, so no root of it holds an address map"
)
PLATFORM_FILE = (
    "built from a loaded platform.toml: the file's `[addresses]` (which `up` writes once the root holds a map) is "
    "read with it and every call of the client dials through it (client._build_http); a Compose file has none, "
    "its names resolve"
)
OWN_TRANSPORT = (
    "the client's own transport, built from the address map it is given (or none); the class every login-side "
    "client is made from"
)

ALLOWED: dict[tuple[str, str, str], tuple[int, str]] = {
    # -- the factory the core's phase and verify-data hand to the constructor
    (HPCBOOT, "core_phase", "Rest"): (1, "the default of the `rest` parameter, which only `service_client` calls"),
    (HPCBOOT, "verify_ingested", "Rest"): (1, "the default of the `rest` parameter, which only `service_client` calls"),
    # -- on a service's node, loopback
    ("deploy/hpc-federation/bundle/hpcop-cb-init.py", "request", "http.client.HTTPConnection"): (
        1,
        f"Couchbase's admin on HOST = 127.0.0.1; {NODE}",
    ),
    ("deploy/hpc-federation/bundle/onboard.py", MODULE, "http.client.HTTPSConnection"): (
        1,
        f"LocalHTTPS connects to 127.0.0.1; {NODE}",
    ),
    ("deploy/hpc-federation/bundle/onboard.py", "Panel.req", "LocalHTTPS"): (1, f"the node's own panel; {NODE}"),
    ("deploy/hpc-federation/run/fed_provider.py", MODULE, "http.client.HTTPSConnection"): (
        1,
        f"LocalHTTPS connects to 127.0.0.1; {NODE}",
    ),
    ("deploy/hpc-federation/run/fed_provider.py", "Worker.call", "LocalHTTPS"): (
        1,
        f"the provider's own API; {NODE}",
    ),
    ("deploy/hpc-federation/run/fed_zone.py", MODULE, "http.client.HTTPSConnection"): (
        1,
        f"LocalHTTPS connects to 127.0.0.1; {NODE}",
    ),
    ("deploy/hpc-federation/run/fed_zone.py", "Zone.call", "LocalHTTPS"): (1, f"the zone's own API; {NODE}"),
    # -- in the agent's cell
    ("deploy/hpc-federation/run/agent.py", MODULE, "http.client.HTTPSConnection"): (
        1,
        f"Via connects to the gate on 127.0.0.1 by the provider's name; {CELL}",
    ),
    ("deploy/hpc-federation/run/agent.py", "main.gate", "Via"): (1, f"the gate, on the cell's loopback; {CELL}"),
    ("deploy/hpc-federation/run/agent.py", "direct.conn", "http.client.HTTPSConnection"): (
        1,
        f"the provider by the name the cell's arguments give, with no token, expected refused (not a loopback "
        f"address); {CELL}",
    ),
    ("deploy/hpc-federation/run/agent.py", "reachability", "http.client.HTTPConnection"): (
        1,
        f"a Couchbase login attempt where its port answers, on the hosts the cell is given; {CELL}",
    ),
    # -- the Compose federation
    ("deploy/compose-federation/federation.py", "up", "Rest"): (1, COMPOSE),
    ("deploy/compose-federation/federation.py", "reset_clean", "Rest"): (1, COMPOSE),
    ("deploy/compose-federation/federation.py", "reset_replicas", "Rest"): (1, COMPOSE),
    ("deploy/compose-federation/measure_dir_distribution.py", "main", "Rest"): (1, f"a measuring script; {COMPOSE}"),
    ("deploy/compose-federation/measure_granularity_and_eviction.py", "main", "Rest"): (
        1,
        f"a measuring script; {COMPOSE}",
    ),
    ("deploy/compose-federation/measure_partial_file.py", "main", "Rest"): (1, f"a measuring script; {COMPOSE}"),
    ("deploy/compose-federation/measure_transfer_shapes.py", "main", "Rest"): (1, f"a measuring script; {COMPOSE}"),
    ("deploy/compose-federation/measure_direct_vs_proxy.py", "measure", "OnezoneClient"): (
        1,
        f"a measuring script over a platform file derived for the project's network; {COMPOSE}",
    ),
    ("deploy/compose-federation/measure_mount_sweep.py", "run_setting", "OnezoneClient"): (
        1,
        f"a measuring script over a platform file derived for the project's network; {COMPOSE}",
    ),
    ("deploy/compose-federation/rehearse_shards.py", "Rehearsal.connect", "OnezoneClient"): (
        1,
        f"the Compose rehearsal over its rendered platform.toml; {COMPOSE}",
    ),
    # -- the platform layer's client, from the rendered platform.toml
    ("deploy/compose-federation/fedlinks.py", "check_after_up", "OnezoneClient"): (
        1,
        f"the link check, over the rendered platform.toml up writes; {PLATFORM_FILE}",
    ),
    ("deploy/compose-federation/fedimport.py", "verify_imports", "OnezoneClient"): (
        1,
        f"the import check, over the rendered platform.toml up writes; {PLATFORM_FILE}",
    ),
    ("src/expcore_onedata/day0/runner.py", "build_context", "OnezoneClient"): (
        1,
        f"the day-0 checks' client; {PLATFORM_FILE}",
    ),
    ("src/expcore_onedata/lease.py", "revoke", "OnezoneClient"): (1, f"a lease's revocation; {PLATFORM_FILE}"),
    # -- the clients' own transports
    ("deploy/compose-federation/fedrest.py", "Rest.__init__", "httpx.Client"): (2, OWN_TRANSPORT),
    ("deploy/compose-federation/fedrest.py", "Rest.__init__", "MappedTransport"): (1, OWN_TRANSPORT),
    ("src/expcore_onedata/client.py", "_build_http", "httpx.Client"): (2, OWN_TRANSPORT),
    ("src/expcore_onedata/client.py", "_build_http", "MappedTransport"): (1, OWN_TRANSPORT),
    # -- the gate and the fakes
    ("src/expcore_onedata/sandbox/gate.py", "_upstream_conn", "http.client.HTTPSConnection"): (
        1,
        "the gate's upstream, dialled at the host's `dial` address when it has one (O73): the address map the gate "
        "is given, not the login node's",
    ),
    ("src/expcore_onedata/sandbox/gate.py", "_upstream_conn", "http.client.HTTPConnection"): (
        1,
        "the gate's plain upstream, dialled at the host's `dial` address when it has one (O73)",
    ),
    ("src/expcore_onedata/testing/fake_federation.py", "FakeFederation.client_http", "httpx.Client"): (
        1,
        "an in-memory transport answering for the fake federation: no network",
    ),
    ("src/expcore_onedata/testing/fake_onedata_mcp.py", "probe_tls", "httpx.Client"): (
        1,
        "the fake server's TLS-flag probe of a URL a test gives it: a stand-in, no federation",
    ),
}


def names(sites) -> list[str]:
    return [f"{s.path}:{s.line} {s.function} {s.how} {s.name}" for s in sites]


def test_o73_no_client_of_a_federations_services_is_built_outside_the_constructor_but_the_listed():
    """O73: every construction of a client of a federation's services (`Rest(`, `rest(`, `OnezoneClient(`, a raw HTTP
    client, a transport) in the HPC and Compose federations' code and the core's is in `service_client` or on the list
    with its reason; a new one is a failure that names it."""
    sites = scan_tree(REPO, ROOTS)
    unlisted, problems = audit(sites, CONSTRUCTOR, ALLOWED)
    assert not unlisted, (
        "a client built beside the constructor (add it to the constructor, or to ALLOWED with why):\n"
        + "\n".join(names(unlisted))
    )
    assert not problems, "the allow-list is stale or has no reason:\n" + "\n".join(problems)


def test_o73_the_constructor_builds_the_client_and_both_phases_call_it():
    """O73: `service_client` is the one place that calls the factory, and `core_phase` and `verify_ingested` build
    their client by calling it, never the factory themselves."""
    sites = scan_tree(REPO, ("deploy/hpc-federation",))
    inside = [s for s in sites if (s.path, s.function) == CONSTRUCTOR]
    assert inside
    assert {s.name for s in inside} == {"rest"}
    tree = ast.parse((REPO / HPCBOOT).read_text(encoding="utf-8"))
    for phase in ("core_phase", "verify_ingested"):
        (function,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == phase]
        calls = [n.func.id for n in ast.walk(function) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert "service_client" in calls, phase
        assert "rest" not in calls, phase


PLANTED = """
import http.client
from http.client import HTTPSConnection as Https

import httpx
from fedrest import Rest
from expcore_onedata.client import OnezoneClient


def fetch(layout, rest=Rest):
    first = Rest(layout.ca_pem)
    second = rest(layout.ca_pem)
    third = OnezoneClient(layout)
    fourth = httpx.Client()
    fifth = http.client.HTTPSConnection("a-name.example", 8443)
    sixth = Https("a-name.example", 8443)
    return first, second, third, fourth, fifth, sixth


class Local(http.client.HTTPSConnection):
    pass


def reach():
    return Local("a-name.example", 8443)
"""

NOT_CLIENTS = '''
"""Rest( and OnezoneClient( in a docstring, and rest(x) in a comment."""
import http.client
from typing import Callable

from fedrest import Rest, RestError


def rest():
    return 1


def build(client: Rest, factory: Callable[[], "Rest"]) -> RestError:
    attempt: Rest = factory()
    return rest() + attempt.json(1)
'''


def test_o73_the_scanner_finds_every_planted_construction():
    """O73: a call, a factory handed in as `rest`, a kept class, a default, a raw client, an aliased connection class,
    a subclass of a connection class and a call of that subclass are all sites, keyed by function and name."""
    found = {(s.function, s.name, s.how) for s in scan_source(PLANTED, "deploy/hpc-federation/planted.py")}
    assert found == {
        ("fetch", "Rest", "value"),  # the default `rest=Rest`
        ("fetch", "Rest", "call"),
        ("fetch", "rest", "call"),
        ("fetch", "OnezoneClient", "call"),
        ("fetch", "httpx.Client", "call"),
        ("fetch", "http.client.HTTPSConnection", "call"),
        ("fetch", "http.client.connection", "call"),
        (MODULE, "http.client.HTTPSConnection", "subclass"),
        ("reach", "Local", "call"),
    }


def test_o73_the_scanner_passes_what_builds_no_client():
    """O73: an import, an annotation, a name in a docstring and a call of a module's own function named `rest` are no
    site."""
    assert scan_source(NOT_CLIENTS, "deploy/hpc-federation/not_a_client.py") == []


def test_o73_a_planted_raw_construction_in_the_tree_fails_the_audit(tmp_path):
    """O73: the audit over a tree that holds one raw construction of a client beside the real ones names it, and the
    constructor's own calls, an allowed entry and a stale one are told apart."""
    planted = tmp_path / "deploy" / "hpc-federation" / "hpcplanted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(PLANTED, encoding="utf-8")
    sites = scan_tree(tmp_path, ROOTS)
    unlisted, problems = audit(sites, CONSTRUCTOR, {("deploy/hpc-federation/hpcplanted.py", "fetch", "Rest"): (2, "x")})
    assert {(s.function, s.name) for s in unlisted} >= {("fetch", "rest"), ("fetch", "OnezoneClient")}
    assert not [s for s in unlisted if (s.function, s.name) == ("fetch", "Rest")]  # allowed, by key
    assert problems == []
    _, problems = audit(sites, CONSTRUCTOR, {("deploy/hpc-federation/hpcplanted.py", "fetch", "Rest"): (1, "x")})
    assert problems == ["('deploy/hpc-federation/hpcplanted.py', 'fetch', 'Rest'): allowed 1 site(s), found 2"]
    _, problems = audit(sites, CONSTRUCTOR, {("deploy/hpc-federation/gone.py", "fetch", "Rest"): (1, "x")})
    assert "('deploy/hpc-federation/gone.py', 'fetch', 'Rest'): allowed 1 site(s), found 0" in problems
    _, problems = audit(sites, CONSTRUCTOR, {("deploy/hpc-federation/hpcplanted.py", "fetch", "Rest"): (2, " ")})
    assert problems == ["('deploy/hpc-federation/hpcplanted.py', 'fetch', 'Rest'): no reason"]

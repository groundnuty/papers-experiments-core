"""O29: every read of the platform layer names whether a timeout is tried again, and a test holds each choice (#185).

The harness's own plumbing retries a read that timed out (`retry=` a `ReadRetry`); whatever measures the platform keeps
one attempt (`ONE_ATTEMPT`), so a stall stays the recorded stall it is. The choice is made at the call site, and this
file lists every site, by module, function and call, with the expression it passes. A site added, removed or changed
without this table changing fails here, so a later caller cannot turn a measurement into a retried read, or the other
way round, without a reviewer seeing it.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "expcore_onedata"
HELPERS = {"space_root", "wait_visible", "wait_transfers"}  # the consistency waits that make a client read themselves
SKIPPED = {"client.py", "sandbox/gate.py"}  # the client itself, and the data gate, a program of its own with no client

# The harness's own plumbing: a read that timed out is tried again, three attempts with pauses of 2 s and 5 s.
PLUMBING = {
    ("provision.py", "Provisioner._size_of", "client.get"): ["self._retry"],
    ("provision.py", "Provisioner.read_file", "client.get"): ["self._retry"],  # the metadata read, with the deadline
    ("provision.py", "Provisioner.await_visible", "client.get"): ["self._retry"],
    ("provision.py", "Provisioner.await_visible", "consistency.wait_visible"): ["self._retry"],
    ("ingestbase.py", "Baseline._times", "client.get"): ["self._p.retry"],
    ("ingestbase.py", "Baseline.confirm", "consistency.wait_visible"): ["self._p.retry"],
    ("reset.py", "SpaceReset._summary", "client.get"): ["self._p.retry"],
    ("reset.py", "SpaceReset._listed", "client.get"): ["self._p.retry"],
    ("reset.py", "SpaceReset._finish", "client.get"): ["self._p.retry"],
    ("reset.py", "SpaceReset._requirements", "consistency.space_root"): ["self._p.retry"],
    ("reset.py", "SpaceReset._wait_ended", "consistency.wait_transfers"): ["self._p.retry"],
    ("reset.py", "SpaceReset._occupancy", "physical_sizes"): ["self._p.retry"],
    ("day0/mutating.py", "_attempt_write", "consistency.space_root"): ["ctx.plumbing_retry()"],  # the setup read
}

# A read whose policy is its caller's: a parameter that the caller names, so each caller is a row above or below.
CALLER_CHOOSES = {
    ("consistency.py", "space_root", "client.get"): ["retry"],
    ("consistency.py", "wait_transfers.read", "client.get"): ["retry"],
    ("consistency.py", "wait_visible", "client.get"): ["retry"],
    ("occupancy.py", "physical_sizes", "client.get"): ["retry"],
    ("provision.py", "Provisioner._list_pages", "client.get"): ["reads"],  # `children` decides, below
}

# Plumbing unless it is an instrument's read: `children` and `root` say so by their own arguments.
INSTRUMENT_AWARE = {
    ("provision.py", "Provisioner.root", "consistency.space_root"): ["self._retry if wait else ONE_ATTEMPT"],
}

# What measures the platform or checks it, and a poll that already counts a failed read as one that did not match:
# one attempt, a timeout stays a timeout.
ONE_ATTEMPT_SITES = {
    # the health gate's checks: the token and privilege reads, the content-type measurement, the fault gate's probe
    ("tokens.py", "TokenIssuer._read_privileges", "client.get"): ["ONE_ATTEMPT"],
    ("tokenscope.py", "admin_privileges", "client.get"): ["ONE_ATTEMPT"],
    ("tokenscope.py", "effective_clusters", "client.get"): ["ONE_ATTEMPT"],
    ("precampaign.py", "_child", "client.get"): ["ONE_ATTEMPT"],
    ("precampaign.py", "measure_content_type", "consistency.space_root"): ["ONE_ATTEMPT"],
    ("fault_serves.py", "provider_serves.probe", "client.get"): ["ONE_ATTEMPT"],
    # the placement probes: the transfer records they record, the transfer log, the block map
    ("probes.py", "PlatformProbes._transfer_records", "client.get"): ["ONE_ATTEMPT"],
    ("transferlog.py", "TransferLog._observe", "client.get"): ["ONE_ATTEMPT"],
    ("provision.py", "Provisioner.blocks", "client.get"): ["ONE_ATTEMPT"],  # the probes' and the placement polls' read
    # day-0's checks, which record what each provider does
    ("day0/checks.py", "reachability", "client.get"): ["ONE_ATTEMPT", "ONE_ATTEMPT"],
    ("day0/checks.py", "_names", "client.get"): ["ONE_ATTEMPT"],
    ("day0/checks.py", "_space_listing", "consistency.space_root"): ["ONE_ATTEMPT"],
    ("day0/mutating.py", "_remove_and_verify", "client.get"): ["ONE_ATTEMPT"],
    # transfers' records, polled to their end: not in the plumbing the rule names
    ("provision.py", "Provisioner.replicate", "consistency.wait_transfers"): ["ONE_ATTEMPT"],
    ("provision.py", "Provisioner.evict", "consistency.wait_transfers"): ["ONE_ATTEMPT"],
    ("eviction.py", "_wait_ended", "consistency.wait_transfers"): ["ONE_ATTEMPT"],
    # a read inside a poll that counts a failed read as one that did not match: the poll is the retry
    ("provision.py", "Provisioner._gone", "client.get"): ["ONE_ATTEMPT"],
    ("ingestbase.py", "Baseline._absent_everywhere", "client.get"): ["ONE_ATTEMPT"],
    ("reset.py", "SpaceReset._qos_gone", "client.get"): ["ONE_ATTEMPT"],
}

# Modules whose every read measures: no site in them may ever name another policy.
MEASURING_MODULES = {
    "probes.py",
    "transferlog.py",
    "precampaign.py",
    "fault_serves.py",
    "tokens.py",
    "tokenscope.py",
    "health.py",
    "day0/checks.py",
}


def reads_of(path: Path) -> dict[tuple[str, str, str], list[str]]:
    """Every client read in a module: `(module, function, callee) -> the retry expression of each call`, or
    `<missing>` for a call that names none."""
    relative = path.relative_to(SRC).as_posix()
    found: dict[tuple[str, str, str], list[str]] = {}

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, [*stack, child.name])
                continue
            if isinstance(child, ast.Call):
                callee = callee_of(child)
                if callee:
                    named = {k.arg: ast.unparse(k.value) for k in child.keywords}
                    found.setdefault((relative, ".".join(stack), callee), []).append(named.get("retry", "<missing>"))
            visit(child, stack)

    visit(ast.parse(path.read_text(encoding="utf-8")), [])
    return {key: sorted(values) for key, values in found.items()}


def callee_of(call: ast.Call) -> str | None:
    """Name a call that reads through the client: its `get`, a consistency wait that does, or `physical_sizes`."""
    func = call.func
    if isinstance(func, ast.Attribute):
        receiver = ast.unparse(func.value)
        if func.attr == "get" and receiver.split(".")[-1].lstrip("_") == "client":
            return "client.get"
        if func.attr == "get" and any(k.arg == "provider" for k in call.keywords):
            return "client.get"  # a read of a provider, whatever the receiver is called
        if func.attr in HELPERS and receiver == "consistency":
            return f"consistency.{func.attr}"
    if isinstance(func, ast.Name) and func.id == "physical_sizes":
        return "physical_sizes"
    return None


def every_read() -> dict[tuple[str, str, str], list[str]]:
    found: dict[tuple[str, str, str], list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("testing/") or relative in SKIPPED:
            continue
        found.update(reads_of(path))
    return found


def test_o29_every_read_names_its_policy_and_the_table_lists_each_choice():
    """O29: no `client.get`, consistency wait or occupancy read leaves a module without a `retry=`, and the choice of
    each is the one this table records: a plumbing read with the policy of its owner, a caller's pass-through, an
    instrument's read, or `ONE_ATTEMPT`."""
    expected = {**PLUMBING, **CALLER_CHOOSES, **INSTRUMENT_AWARE, **ONE_ATTEMPT_SITES}
    assert len(expected) == len(PLUMBING) + len(CALLER_CHOOSES) + len(INSTRUMENT_AWARE) + len(ONE_ATTEMPT_SITES)
    found = every_read()
    assert not [site for site, named in found.items() if "<missing>" in named], "a read names no `retry=`"
    assert found == expected


def test_o29_the_measuring_call_sites_keep_one_attempt():
    """O29: the health gate's checks, the placement probes, the transfer log and day-0's checks never name a policy
    other than `ONE_ATTEMPT`, so a timeout there is the recorded timeout."""
    found = every_read()
    measuring = {site: named for site, named in found.items() if site[0] in MEASURING_MODULES}
    assert measuring, "the measuring modules read through the client"
    assert all(named == ["ONE_ATTEMPT"] * len(named) for named in measuring.values()), measuring


def test_o29_the_plumbing_call_sites_name_a_policy_of_their_owner():
    """O29: the provisioner, `read_file`, the reset, the ingest and day-0's setup pass their owner's policy, never
    `ONE_ATTEMPT` and never a fresh one built at the call, so the count of retries is the owner's."""
    found = every_read()
    for site, named in PLUMBING.items():
        assert found[site] == named
        assert all("ONE_ATTEMPT" not in expression and "ReadRetry(" not in expression for expression in named)


def test_o29_a_read_by_a_provider_is_a_call_site_whatever_its_receiver_is_called():
    """O29: the scan follows a `get` with a `provider=` as well as one on a `client`, so a client held under another
    name is not a way round the table."""
    tree = ast.parse("def f(c):\n    return c.get('/x', provider='p')\n")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert callee_of(call) == "client.get"

"""O29: every call of the platform layer that reaches a GET names whether a timeout is tried again, and a test holds it.

The harness's own plumbing retries a read that timed out (`retry=` a `ReadRetry`, `gate_retry=` for a mutation's or a
range read's gate); whatever measures the platform keeps one attempt (`ONE_ATTEMPT`), so a stall stays the recorded
stall it is. The choice is made at the call site, and this file lists every site, by module, function and call, with
the expression it passes. A site added, removed or changed without this table changing fails here, so a later caller
cannot turn a measurement into a retried read, or the other way round, without a reviewer seeing it.

What counts as a site is `tests/support/read_sites.py`'s: any `.get(` that takes a keyword or a path, whatever its
receiver is called; the client's gated and space reads (`mutate`, `read_range`, `read_content`, `space_name`,
`file_space_id`, `file_space_name`) on any receiver; the consistency waits and the occupancy read, whether called as
`consistency.x`, imported by name or handed to `functools.partial`; the client's internal calls (`client.py` is
scanned); the provisioner's helpers and the layer's functions that retry by default; and a method got by `getattr`
under a constant name. A call the scanner cannot classify fails the test, naming it.
"""

import functools
from pathlib import Path

import pytest
from read_sites import MISSING, Scan, retries, retrying_helper, scan_source

SRC = Path(__file__).resolve().parents[2] / "src" / "expcore_onedata"
SKIPPED = {"sandbox/gate.py"}  # the data gate, a program of its own with no client; the fakes in testing/ are no client

# The harness's own plumbing: a read that timed out is tried again, three attempts with pauses of 2 s and 5 s.
PLUMBING = {
    ("provision.py", "Provisioner._size_of", "client.get"): ["self._retry"],
    ("provision.py", "Provisioner.read_file", "client.get"): ["self._retry"],  # the metadata read, with the deadline
    ("provision.py", "Provisioner.await_visible", "client.get"): ["self._retry"],
    ("provision.py", "Provisioner.await_visible", "consistency.wait_visible"): ["self._retry"],
    ("provision.py", "Provisioner.replicate_file", "consistency.wait_transfers"): ["self._retry"],
    ("provision.py", "Provisioner.evict", "consistency.wait_transfers"): ["self._retry"],
    # the mutation gate's reads, and the gate of a range read and of a streamed read: the request itself is sent once
    ("provision.py", "Provisioner.mutate", "client.mutate"): ["self._retry"],
    ("provision.py", "Provisioner.verify_space", "client.space_name"): ["self._retry"],
    ("provision.py", "Provisioner.read_file", "client.read_content"): ["self._retry"],
    ("provision.py", "Provisioner.fetch", "client.read_range"): ["self._retry"],
    ("ingestbase.py", "Baseline._times", "client.get"): ["self._p.retry"],
    ("ingestbase.py", "Baseline.confirm", "consistency.wait_visible"): ["self._p.retry"],
    # the preallocated ingest (O44): its visibility wait, the block map before a part and the one its reset completes
    ("ingestsparse.py", "SparseBaseline.confirm_state", "consistency.wait_visible"): ["self._p.retry"],
    ("ingestsparse.py", "SparseBaseline._complete", "provisioner.blocks"): ["self._p.retry"],
    ("ingestsparse.py", "SparseBaseline.check_holdings", "provisioner.blocks"): ["self._p.retry"],  # before a removal
    ("ingestsparse.py", "SparseBaseline._restore_replicas", "provisioner.blocks"): ["self._p.retry"],
    ("reset.py", "SpaceReset._page", "client.get"): ["self._p.retry"],
    ("reset.py", "SpaceReset._dated_page", "client.get_dated"): ["self._p.retry"],  # the history's, with its Date
    ("reset.py", "SpaceReset._zone_clock", "client.get_dated"): [
        "self._p.retry"
    ],  # the global clock, Onezone's reading
    ("reset.py", "SpaceReset._finish", "client.get"): ["self._p.retry"],
    ("reset.py", "SpaceReset._wait_ended", "consistency.wait_transfers"): ["self._p.retry"],
    ("reset.py", "SpaceReset._occupancy", "physical_sizes"): ["self._p.retry"],
    ("reset.py", "SpaceReset._quiet", "physical_sizes"): ["self._p.retry"],
    # the reset's QoS stages (O33): the scans, each requirement's record, and the settle's and the quiet stage's
    # block maps, whose failure fails the stage (a read that fails is no poll that did not match)
    ("resetqos.py", "_summary", "client.get"): ["p.retry"],
    ("resetqos.py", "tree", "consistency.space_root"): ["p.retry"],
    ("resetqos.py", "listed", "consistency.space_root"): ["p.retry"],
    ("resetqos.py", "_listing_page", "client.get"): ["p.retry"],
    ("resetqos.py", "_requirement_entry", "client.get"): ["p.retry"],
    ("resetqos.py", "read_pairs", "provisioner.blocks"): ["p.retry"],
    # the check of a space provisioned by storage import (O41): its block maps are read by the harness, as a baseline's
    ("storageimport.py", "SpaceCheck._check_blocks.whole", "provisioner.blocks"): ["self._p.retry"],
    ("eviction.py", "_wait_ended", "consistency.wait_transfers"): ["provisioner.retry"],
    # the placement's block-map reads outside a poll: a stall there ends the placement
    ("ranges.py", "_held", "provisioner.blocks"): ["provisioner.retry"],
    ("ranges.py", "place_ranges", "provisioner.holds"): ["provisioner.retry"],
    # day-0's write check: the root it writes under, and its gate's reads, which the check does not measure
    ("day0/mutating.py", "_attempt_write", "consistency.space_root"): ["ctx.plumbing_retry()"],
    ("day0/mutating.py", "_attempt_write", "client.mutate"): ["ctx.plumbing_retry()"],
    ("day0/mutating.py", "_remove_and_verify", "client.mutate"): ["ctx.plumbing_retry()"],
    # day-0's outage check, before anything is taken down: the proof of who holds what is part of setting the scenario
    # up, and a stall there ends the preparation (the helpers it calls, below, read under the provisioner's own policy)
    ("day0/outage_run.py", "Run.prove_holdings", "provisioner.blocks"): ["self.provisioner.retry"],
}

# A read whose policy is its caller's: a parameter that the caller names, so each caller is a row above or below.
CALLER_CHOOSES = {
    ("consistency.py", "space_root", "client.get"): ["retry"],
    ("consistency.py", "wait_transfers.read", "client.get"): ["retry"],
    ("consistency.py", "wait_visible", "client.get"): ["retry"],
    ("occupancy.py", "physical_sizes", "client.get"): ["retry"],
    ("provision.py", "Provisioner._list_pages", "client.get"): ["reads"],  # `children` decides, below
    ("provision.py", "Provisioner.blocks", "client.get"): ["retry"],  # one attempt unless the caller says: place_ranges
    ("provision.py", "Provisioner.holds", "provisioner.blocks"): ["retry"],
    ("ingestsparse.py", "SparseBaseline.layout", "client.get"): ["retry"],  # a poll's one attempt, or the part's check
}

# Plumbing unless it is an instrument's read: `children` and `root` say so by their own arguments.
INSTRUMENT_AWARE = {
    ("provision.py", "Provisioner.root", "consistency.space_root"): ["self._retry if wait else ONE_ATTEMPT"],
}

# What measures the platform or checks it, and a poll that already counts a failed read as one that did not match:
# one attempt, a timeout stays a timeout. `<default>` is a call that names nothing where the default is one attempt.
ONE_ATTEMPT_SITES = {
    # the health gate's checks: the token and privilege reads, the content-type measurement, the fault gate's probe
    ("tokens.py", "TokenIssuer._read_privileges", "client.get"): ["ONE_ATTEMPT"],
    ("tokenscope.py", "admin_privileges", "client.get"): ["ONE_ATTEMPT"],
    ("tokenscope.py", "effective_clusters", "client.get"): ["ONE_ATTEMPT"],
    ("precampaign.py", "_child", "client.get"): ["ONE_ATTEMPT"],
    ("precampaign.py", "measure_content_type", "consistency.space_root"): ["ONE_ATTEMPT"],
    ("precampaign.py", "measure_content_type", "client.content_answer"): ["<one attempt>", "<one attempt>"],
    ("fault_serves.py", "ServingProbe.__call__", "client.get"): ["ONE_ATTEMPT"],
    # the link check (O35): its fetch is the measurement, and every read and write of setting its probe up is part of
    # the health gate's check, one attempt like the content-type check's
    ("linkcheck.py", "_Check._empty_on", "client.get"): ["ONE_ATTEMPT"],
    ("linkcheck.py", "_Check._evict", "consistency.wait_transfers"): ["ONE_ATTEMPT"],
    ("linkcheck.py", "_Check._holdings", "client.get"): ["ONE_ATTEMPT"],
    ("linkcheck.py", "_Check._schedule_eviction", "client.mutate"): ["ONE_ATTEMPT"],
    ("linkcheck.py", "_Check.prepare", "consistency.wait_visible"): ["ONE_ATTEMPT"],
    ("linkprobe.py", "ProbeFiles._create", "client.mutate"): ["ONE_ATTEMPT"],
    ("linkprobe.py", "ProbeFiles._children", "client.get"): ["ONE_ATTEMPT"],
    ("linkprobe.py", "ProbeFiles._root", "consistency.space_root"): ["ONE_ATTEMPT"],
    ("linkprobe.py", "ProbeFiles._write", "client.mutate"): ["ONE_ATTEMPT"],
    ("linkprobe.py", "ProbeFiles.delete", "client.mutate"): ["ONE_ATTEMPT"],
    ("linkprobe.py", "ProbeFiles.digest", "client.read_content"): ["ONE_ATTEMPT"],
    # the placement probes: the transfer records they record, the transfer log, the block map
    ("probes.py", "PlatformProbes._transfer_records", "client.get"): ["ONE_ATTEMPT"],
    ("probes.py", "PlatformProbes._read_maps", "provisioner.blocks"): ["<default>"],
    ("transferlog.py", "TransferLog._record", "client.get"): ["ONE_ATTEMPT"],
    ("transferlog.py", "TransferLog._dated_page", "client.get_dated"): ["ONE_ATTEMPT"],
    ("transferlog.py", "TransferLog._zone_dated", "client.get_dated"): ["ONE_ATTEMPT"],  # the global clock
    # day-0's checks, which record what each provider does
    ("day0/checks.py", "reachability", "client.get"): ["ONE_ATTEMPT", "ONE_ATTEMPT"],
    ("day0/checks.py", "_names", "client.get"): ["ONE_ATTEMPT"],
    ("day0/checks.py", "_space_listing", "consistency.space_root"): ["ONE_ATTEMPT"],
    ("day0/mutating.py", "_remove_and_verify", "client.get"): ["ONE_ATTEMPT"],
    # the outage check's cells: the reads and listings during the outage and in the all-up baseline, whose outcome,
    # errno and latency are the record, and the target's direct answer. A retry there would hide what is measured.
    ("day0/outage_cells.py", "Cells.read", "client.read_content"): ["ONE_ATTEMPT"],
    ("day0/outage_cells.py", "Cells._names", "client.get"): ["ONE_ATTEMPT"],
    ("day0/outage_cells.py", "Cells.target_answers", "client.get"): ["ONE_ATTEMPT"],
    # a read inside a poll that counts a failed read as one that did not match: the poll is the retry
    ("provision.py", "Provisioner._gone", "client.get"): ["ONE_ATTEMPT"],
    ("provision.py", "Provisioner.placement_report", "provisioner.blocks"): ["<default>"],
    ("provision.py", "Provisioner.require_empty", "provisioner.holds"): ["<default>"],
    ("eviction.py", "_converge_empty", "provisioner.blocks"): ["<default>"],
    ("ingestbase.py", "Baseline._absent_everywhere", "client.get"): ["ONE_ATTEMPT"],
    # the reset's removal (O33): the wait for a requirement to be fulfilled, and the deletion barrier
    ("resetremove.py", "_record", "client.get"): ["ONE_ATTEMPT"],
    ("resetremove.py", "_gone", "client.get"): ["ONE_ATTEMPT"],
    ("resetremove.py", "_processed", "client.get"): ["ONE_ATTEMPT"],
}

# The client's own calls: a request it sends, the gate's reads and what each call into the gate says about them. The
# reads of the gate are `reads.retry` (a `_GateReads`, which each public entry builds from the caller's `gate_retry` or
# `retry`, one attempt when it names none); a plain read and a dated read pass on their caller's `retry`; a range read's
# own request, a streamed read and the head of a content read are one request by construction, and a write is passed on
# with no policy at all.
CLIENT_INTERNAL = {
    ("client.py", "OnezoneClient.get", "self._send(GET)"): ["retry"],
    ("client.py", "OnezoneClient.get_dated", "self._request(GET)"): ["retry"],
    # the Onepanel reads of an imported space (O41): a plain GET under the caller's policy, and the forced scan's gate
    ("client.py", "OnezoneClient.panel_get", "self._send(GET)"): ["retry"],
    ("client.py", "OnezoneClient.force_scan", "self._space_name"): ["_GateReads(gate_retry)"],
    ("client.py", "OnezoneClient._send", "self._request(<method>)"): ["retry"],
    ("client.py", "_exchange", "http.send"): ["<transport>"],  # every request, a retry's one-shot client's included
    ("client.py", "OnezoneClient.mutate", "self._send(<method>)"): ["<none>"],  # a write: never retried
    ("client.py", "OnezoneClient.mutate", "self._target"): ["_GateReads(gate_retry)"],
    ("client.py", "OnezoneClient.read_range", "self._target"): ["_GateReads(gate_retry)"],
    ("client.py", "OnezoneClient.read_range", "self._request(GET)"): ["ONE_ATTEMPT"],  # it may have fetched bytes
    # a revocation's confirmation: a failed read writes no revoked line, so the slot stays held (fail closed)
    ("client.py", "OnezoneClient.own_temporary_generation", "self._send(GET)"): ["ONE_ATTEMPT"],
    ("client.py", "OnezoneClient.read_content", "self._target"): ["gate", "gate"],  # bounded by the deadline left
    ("client.py", "OnezoneClient.content_answer", "self._target"): ["_ONE_ATTEMPT"],  # the pre-campaign measurement
    # a streamed range and a content head both go through `_streamed`, which sends once and releases the answer (O47)
    ("client.py", "_streamed", "http.send"): ["<transport>"],
    ("client.py", "OnezoneClient.space_name", "self._space_name"): ["_GateReads(retry, deadline_s)"],
    ("client.py", "OnezoneClient.file_space_id", "self._file_space_id"): ["_GateReads(retry, deadline_s)"],
    ("client.py", "OnezoneClient.file_space_name", "self._space_name"): ["reads"],
    ("client.py", "OnezoneClient.file_space_name", "self._file_space_id"): ["reads"],
    ("client.py", "OnezoneClient._space_name", "self._send(GET)"): ["reads.retry"],
    ("client.py", "OnezoneClient._space_roots", "self._space_root"): ["reads"],
    ("client.py", "OnezoneClient._space_root", "self._send(GET)"): ["reads.retry"],
    ("client.py", "OnezoneClient._parent_of", "self._send(GET)"): ["reads.retry"],
    ("client.py", "OnezoneClient._file_space_id", "self._space_roots"): ["reads"],
    ("client.py", "OnezoneClient._file_space_id", "self._walk"): ["reads", "reads"],
    ("client.py", "OnezoneClient._walk", "self._parent_of"): ["reads"],
    ("client.py", "OnezoneClient._record_space", "self._send(GET)"): ["reads.retry"],
    ("client.py", "OnezoneClient._record_space", "self._file_space_id"): ["reads"],
    ("client.py", "OnezoneClient._record_space", "self._space_name"): ["reads"],
    ("client.py", "OnezoneClient._target", "self._space_name"): ["reads", "reads"],
    ("client.py", "OnezoneClient._target", "self._record_space"): ["reads"],
    ("client.py", "OnezoneClient._target", "self._file_space_id"): ["reads"],
}

# The modules that call the provisioner's helpers and the layer's functions that retry (`place_ranges`, `evict_ids`,
# `evict_tree`), which retry by default, and which they call. A module that begins to, or a helper it begins to call, is
# a change a reviewer sees here. Each is plumbing, but for the instrument's switch.
HELPER_USERS = {
    "day0/mutating.py": {
        "provisioner.verify_space",
        "provisioner.put_file",
        "provisioner.fetch",
        "provisioner.evict",
        "provisioner.teardown",
        "provisioner.resolve",
        "layer.place_ranges",  # the layer's functions that retry are listed with the helpers
    },
    "eviction.py": {
        "provisioner.verify_space",
        "provisioner.schedule_transfer",
        "provisioner.resolve",
        "provisioner.walk",
    },
    "day0/outage_run.py": {  # the outage check's setup, which writes and places its scratch files before the outage
        "provisioner.verify_space",
        "provisioner.put_file",
        "provisioner.resolve",
        "provisioner.await_visible",
        "provisioner.replicate",
    },
    "ingest.py": {"provisioner.verify_space"},
    "ingestsparse.py": {
        "provisioner.resolve",
        "provisioner.read_file",
        "provisioner.mutate",
        "provisioner.replicate_file",  # the reset's replica of a completed file
        "provisioner.await_visible",  # the mirror sees the completed file before its block map is read
    },
    "ingestbase.py": {
        "provisioner.resolve",
        "provisioner.mutate",
        "provisioner.read_file",
        "provisioner.upload",
    },
    "occupancy.py": {"provisioner.root"},
    "probes.py": {"provisioner.resolve"},  # `resolve(path, wait=False)`: the instrument's form, checked below
    "provision.py": {
        "provisioner.verify_space",
        "provisioner.root",
        "provisioner.children",
        "provisioner.resolve",
        "provisioner.mutate",
        "provisioner.make_dir",
        "provisioner.read_file",
        "provisioner.await_visible",
        "provisioner.fetch",
        "provisioner.schedule_transfer",
        "provisioner.replicate_file",  # `replicate` by path, through the one by id
    },
    "ranges.py": {
        "provisioner.verify_space",
        "provisioner.resolve_many",
        "provisioner.await_visible",
        "provisioner.read_through",
    },
    "reset.py": {
        "provisioner.verify_space",
        "provisioner.mutate",
        "provisioner.resolve_many",
        "layer.evict_ids",
        "layer.evict_tree",
    },
    "resetqos.py": {"provisioner.children"},
    "resetremove.py": {"provisioner.mutate"},
    "storageimport.py": {  # the check of a space provisioned by storage import (O41): plumbing, as a baseline's
        "provisioner.root",
        "provisioner.children",
        "provisioner.read_file",
    },
}

# Calls that have a provisioner helper's name and are the method of another object (module, function, receiver).
NOT_THE_PROVISIONERS = {
    ("ingest.py", "ProgressiveIngest._perform", "self._base"),  # the ingest's baseline writes through the provisioner
    ("ingestbase.py", "Baseline._write_back", "self"),
    ("transferlog.py", "_placement", "edges"),  # a window of transfer edges, not a block map
    ("transferwindow.py", "Edges.bears_on", "self"),
    # the agent's tool `schedule_file_replication` through the tool server (MCP), which reaches no Onezone read of ours
    ("day0/outage_run.py", "Run.agent_baseline", "self.tools"),
    ("day0/outage_run.py", "Run.during", "tools"),
}

# Modules whose every read measures: no site in them may ever name another policy, or call a helper that retries.
MEASURING_MODULES = {
    "probes.py",
    "transferlog.py",
    "precampaign.py",
    "linkcheck.py",
    "linkprobe.py",
    "fault_serves.py",
    "tokens.py",
    "tokenscope.py",
    "health.py",
    "day0/checks.py",
    "day0/outage_cells.py",  # every cell it makes is a measurement; the run's setup is in outage_run.py
}


@functools.cache
def every_scan() -> dict[str, Scan]:
    """The scan of every module of the layer, by its path, but for the skipped ones. Read-only: it is shared."""
    scans: dict[str, Scan] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("testing/") or relative in SKIPPED:
            continue
        scans[relative] = scan_source(path.read_text(encoding="utf-8"), relative, NOT_THE_PROVISIONERS)
    return scans


def every_read() -> dict[tuple[str, str, str], list[str]]:
    return {site: sorted(shown) for scan in every_scan().values() for site, shown in scan.reads.items()}


def helper_calls() -> dict[str, set[str]]:
    users: dict[str, set[str]] = {}
    for module, scan in every_scan().items():
        for _, _, callee in scan.helpers:
            users.setdefault(module, set()).add(callee)
    return users


def measuring_breaches(scan: Scan) -> list[str]:
    """What in a module that measures retries: a read naming a policy, or a helper called in its retrying form."""
    reads = [f"{site}: {', '.join(shown)}" for site, shown in scan.reads.items() if any(retries(s) for s in shown)]
    helpers = [
        f"{site}: {', '.join(shown)}" for site, shown in scan.helpers.items() if any(map(retrying_helper, shown))
    ]
    return reads + helpers


# -- the layer's own call sites --------------------------------------------------------------------------------


def test_o29_every_call_that_reaches_a_get_is_one_the_scanner_classifies():
    """O29: a call the scanner cannot place (a helper's name on a receiver it does not know, a read handed on as a
    value) fails here, named, so none is a way round the table."""
    unclassified = [line for scan in every_scan().values() for line in scan.unclassified]
    assert unclassified == []


def test_o29_every_read_names_its_policy_and_the_table_lists_each_choice():
    """O29: no read, wait, gate call or occupancy read leaves a module without naming its policy, the client's own
    calls included, and the choice of each is the one this table records: a plumbing read with the policy of its
    owner, a caller's pass-through, an instrument's read, or one attempt."""
    expected = {**PLUMBING, **CALLER_CHOOSES, **INSTRUMENT_AWARE, **ONE_ATTEMPT_SITES, **CLIENT_INTERNAL}
    tables = (PLUMBING, CALLER_CHOOSES, INSTRUMENT_AWARE, ONE_ATTEMPT_SITES, CLIENT_INTERNAL)
    assert len(expected) == sum(len(table) for table in tables)
    found = every_read()
    assert not [site for site, named in found.items() if MISSING in named], "a read names no policy"
    assert found == expected


def test_o29_the_client_is_scanned_and_a_get_of_its_own_that_names_no_policy_would_fail():
    """O29: `client.py` is no longer skipped: its internal GETs are rows of the table, and one that named no `retry` is
    `<missing>`."""
    assert "client.py" in every_scan()
    assert any(site[0] == "client.py" for site in every_read())
    bare = "class C:\n    def get(self, path):\n        return self._send('GET', path)\n"
    assert scan_source(bare, "client.py").reads == {("client.py", "C.get", "self._send(GET)"): [MISSING]}


def test_o29_the_provisioner_helpers_that_retry_are_called_by_the_modules_the_table_lists():
    """O29: `children`, `resolve`, `walk`, `root` and the rest retry unless told not to; the modules that call them are
    the ones listed, each with the helpers it calls, so a new caller is seen."""
    assert helper_calls() == HELPER_USERS


def test_o29_the_measuring_modules_keep_one_attempt_in_every_read_and_every_helper():
    """O29: the health gate's checks, the placement probes, the transfer log, the fault gate's probe, day-0's checks and
    the outage check's cells never name a policy other than one attempt and never call a provisioner helper in its
    retrying form, so a timeout there is the recorded timeout."""
    scans = every_scan()
    assert set(scans) >= MEASURING_MODULES
    assert {module: measuring_breaches(scans[module]) for module in MEASURING_MODULES} == {
        module: [] for module in MEASURING_MODULES
    }
    assert any(scans[module].reads for module in MEASURING_MODULES), "the measuring modules read through the client"
    probe_helpers = scans["probes.py"].helpers
    assert probe_helpers
    assert all(shown == ["False"] for shown in probe_helpers.values())  # `resolve(path, wait=False)`


def test_o29_the_plumbing_call_sites_name_a_policy_of_their_owner():
    """O29: the provisioner, `read_file`, the reset, the ingest and day-0's setup pass their owner's policy, never
    `ONE_ATTEMPT` and never a fresh one built at the call, so the count of retries is the owner's."""
    found = every_read()
    for site, named in PLUMBING.items():
        assert found[site] == named
        assert all("ONE_ATTEMPT" not in expression and "ReadRetry(" not in expression for expression in named)


# -- the scanner finds what the old table could not see -----------------------------------------------------------


def reads_of(text: str, module: str = "probes.py") -> dict[tuple[str, str, str], list[str]]:
    return scan_source(text, module).reads


def test_o29_a_get_with_a_policy_is_a_site_whatever_its_receiver_is_called():
    """O29: an Onezone read made through a `zone` or any other name, with no `provider=`, is a client read, found by its
    keyword; a measuring module that gives it a policy breaches."""
    text = "def f(zone, policy):\n    return zone.get('/spaces/x', retry=policy)\n"
    scan = scan_source(text, "probes.py")
    assert scan.reads == {("probes.py", "f", "client.get"): ["policy"]}
    assert measuring_breaches(scan) != []
    assert measuring_breaches(scan_source(text.replace("policy)\n", "ONE_ATTEMPT)\n"), "probes.py")) == []


@pytest.mark.parametrize("receiver", ["zone", "self._zone", "thing", "ctx.anything.at_all"])
def test_o29_a_get_of_a_path_that_names_no_policy_is_missing_whatever_the_receiver(receiver):
    """O29: a path in the call, a string or an f-string, makes it a read; one that names nothing is `<missing>`."""
    for path in ("'/spaces/x'", "f'/data/{x}'"):
        found = reads_of(f"def f(thing, zone, ctx, x):\n    return {receiver}.get({path})\n")
        assert found == {("probes.py", "f", "client.get"): [MISSING]}


def test_o29_a_get_with_unpacked_keywords_names_no_policy_the_scanner_can_see():
    """O29: `get(path, **options)` may carry any policy, so it is a read that is `<missing>` and fails the table."""
    assert reads_of("def f(z, o):\n    return z.get(p, **o)\n") == {("probes.py", "f", "client.get"): [MISSING]}


def test_o29_a_mappings_get_is_no_site():
    """O29: a key, a key and a default, and a key held in a name take no keyword, so none can carry a policy."""
    assert reads_of("def f(d, k):\n    return d.get('size'), d.get(k, 0), d.get(k), os.environ.get('X', '')\n") == {}


CONSISTENCY = "from expcore_onedata.consistency import"
WAIT_SOURCES = [
    f"{CONSISTENCY} space_root\n\n\ndef f(c, p):\n    return space_root(c, p, 's', retry=POLICY)\n",
    f"{CONSISTENCY} space_root as root\n\n\ndef f(c, p):\n    return root(c, p, 's', retry=POLICY)\n",
    "from expcore_onedata import consistency as k\n\n\ndef f(c):\n    return k.space_root(c, 'p', 's', retry=POLICY)\n",
    f"import functools\n\n{CONSISTENCY} space_root\n\n\n"
    "def f(c, p):\n    return functools.partial(space_root, c, p, 's', retry=POLICY)()\n",
]


@pytest.mark.parametrize("source", WAIT_SOURCES)
def test_o29_a_consistency_wait_is_a_site_however_it_is_imported_or_bound(source):
    """O29: `space_root` named by an import (an alias included) or bound by `functools.partial` reads through the
    client, with the policy it names; a measuring module that names a retrying one breaches."""
    scan = scan_source(source, "probes.py")
    (shown,) = scan.reads.values()
    assert shown == ["POLICY"]
    assert measuring_breaches(scan) != []


def test_o29_a_wait_handed_on_as_a_value_is_unclassified():
    """O29: a read passed to another call, which the scanner cannot follow, fails closed, naming module and function."""
    source = (
        "from expcore_onedata.consistency import wait_visible\n\n\n"
        "def f(items):\n    return list(map(wait_visible, items))\n"
    )
    assert scan_source(source, "probes.py").unclassified == [
        "probes.py:5 f: `wait_visible` is used as a value, so its calls cannot be followed"
    ]
    assert scan_source("def f(client, xs):\n    return map(client.get, xs)\n", "probes.py").unclassified != []


@pytest.mark.parametrize(
    "call",
    ["resolve(path)", "children(node)", "walk()", "root()", "resolve(path, wait=True)", "children(a, retry=True)"],
)
def test_o29_a_measuring_module_that_calls_a_retrying_provisioner_helper_breaches(call):
    """O29: the provisioner's helpers retry by default, so a probe that called `resolve(path)` would retry unseen; each
    is a site, and breaches in a measuring module unless its instrument's switch is a literal False."""
    scan = scan_source(f"def f(self, path, node, a):\n    return self._provisioner.{call}\n", "probes.py")
    assert measuring_breaches(scan) != []


@pytest.mark.parametrize(
    "call", ["resolve(path, wait=False)", "children(node, retry=False)", "root(wait=False)", "blocks(file, provider)"]
)
def test_o29_the_instruments_form_of_a_helper_does_not_breach(call):
    """O29: `wait=False`, `retry=False`, and a block map read with no policy (one attempt) are what a probe may do."""
    scan = scan_source(f"def f(self, path, node, file, provider):\n    return self._provisioner.{call}\n", "probes.py")
    assert measuring_breaches(scan) == []


def test_o29_a_helper_name_on_an_object_the_scanner_does_not_know_is_unclassified():
    """O29: `thing.upload(x)` could be the provisioner's through another name; it fails closed, naming the call, until
    a test says what it is. A path's `resolve()`, and a helper's name on a call's result or the standard library, are
    not."""
    found = scan_source("def f(thing, x):\n    return thing.upload(x)\n", "ingest.py").unclassified
    assert found == ["ingest.py:2 f: `thing.upload` has a provisioner helper's name, a receiver unknown"]
    quiet = "import os\n\n\ndef f(x):\n    return Path(x).resolve(), os.walk(x), make(x).walk(), x.resolve()\n"
    assert scan_source(quiet, "ingest.py").unclassified == []


def test_o29_a_gate_call_that_names_no_policy_is_missing():
    """O29: `client.mutate(...)`, `read_range` and `read_content` name their gate's policy by `gate_retry=`; one that
    names none is `<missing>`, and one in a measuring module that names a policy breaches."""
    source = (
        "def f(client, p):\n    client.mutate('PUT', '/x')\n"
        "    client.read_range('/x', provider=p, offset=0, length=1)\n"
    )
    assert reads_of(source) == {
        ("probes.py", "f", "client.mutate"): [MISSING],
        ("probes.py", "f", "client.read_range"): [MISSING],
    }
    named = scan_source("def f(client, p):\n    client.mutate('PUT', '/x', gate_retry=p)\n", "probes.py")
    assert measuring_breaches(named) != []


# -- a gated or space read is a client call whatever its receiver is called ------------------------------------


# Each of these reads through the client under a policy, in a module that measures, behind a receiver the old scanner
# did not know for a client: none was a site, so none was a breach.
RENAMED_RECEIVERS = {
    "read_range": (
        "def f(self, p):\n"
        "    return self._onezone.read_range('/data/x/content', provider=p, offset=0, length=1,"
        " gate_retry=self._provisioner.retry)\n"
    ),
    "space_name": "def f(oz, sid, r):\n    return oz.space_name(sid, retry=r)\n",
    "read_content": (
        "def f(conn, r):\n"
        "    return conn.read_content('/data/x/content', provider='p', size=1, chunk_bytes=1, deadline_s=1,"
        " gate_retry=r)\n"
    ),
    "file_space_id": "def f(oz, r):\n    return oz.file_space_id('x', 'p', retry=r)\n",
    "file_space_name": "def f(self, r):\n    return self.zone_api.file_space_name('x', 'p', retry=r)\n",
}


@pytest.mark.parametrize("name", sorted(RENAMED_RECEIVERS))
def test_o29_a_gated_read_with_a_policy_is_a_site_whatever_its_receiver_is_called(name):
    """O29: `read_range`, `read_content`, `space_name`, `file_space_id` and `file_space_name` are the client's whatever
    the receiver is named (`self._onezone`, `oz`, `conn`): the policy they name is read, and a measuring module that
    names a retrying one breaches."""
    scan = scan_source(RENAMED_RECEIVERS[name], "probes.py")
    assert [site[2] for site in scan.reads] == [f"client.{name}"]
    assert scan.unclassified == []
    assert measuring_breaches(scan) != []


@pytest.mark.parametrize("name", sorted(RENAMED_RECEIVERS))
def test_o29_a_gated_read_of_a_renamed_receiver_that_names_one_attempt_does_not_breach(name):
    """O29: the instrument's form, `ONE_ATTEMPT`, is no breach however the receiver is named."""
    source = RENAMED_RECEIVERS[name]
    quiet = source.replace("self._provisioner.retry", "ONE_ATTEMPT").replace("retry=r", "retry=ONE_ATTEMPT")
    quiet = quiet.replace("gate_retry=r", "gate_retry=ONE_ATTEMPT")
    assert quiet != source
    scan = scan_source(quiet, "probes.py")
    assert [site[2] for site in scan.reads] == [f"client.{name}"]  # seen, and named one attempt
    assert measuring_breaches(scan) == []


@pytest.mark.parametrize("name", sorted(RENAMED_RECEIVERS))
def test_o29_a_gated_read_of_a_renamed_receiver_that_names_no_policy_is_unclassified(name):
    """O29: with no `gate_retry=` or `retry=` there is nothing to read, and the receiver is no client's by name: the
    call fails closed, named, rather than passing as no read."""
    source = RENAMED_RECEIVERS[name]
    bare = source.replace(", gate_retry=self._provisioner.retry", "").replace(", gate_retry=r", "")
    bare = bare.replace(", retry=r", "")
    assert bare != source
    found = scan_source(bare, "probes.py").unclassified
    assert len(found) == 1
    assert f".{name}`" in found[0]


def test_o29_a_gated_read_with_unpacked_keywords_names_no_policy_the_scanner_can_see():
    """O29: `read_range(..., **options)` may carry any `gate_retry`: it is `<missing>` on a client's receiver and
    unclassified on another, never no read."""
    assert reads_of("def f(client, o):\n    return client.read_range('/x', **o)\n") == {
        ("probes.py", "f", "client.read_range"): [MISSING]
    }
    assert scan_source("def f(oz, o):\n    return oz.read_range('/x', **o)\n", "probes.py").unclassified != []


def test_o29_the_provisioners_mutate_is_still_its_helper_and_not_a_gated_client_call():
    """O29: `provisioner.mutate(...)` names no `gate_retry` (the provisioner passes its own): it stays the helper the
    table lists, and another receiver of that name is unclassified as before."""
    scan = scan_source("def f(self, p):\n    return self._provisioner.mutate('PUT', '/x')\n", "ingestbase.py")
    assert scan.reads == {}
    assert list(scan.helpers) == [("ingestbase.py", "f", "provisioner.mutate")]
    assert scan.unclassified == []
    other = scan_source("def f(thing):\n    return thing.mutate('PUT', '/x')\n", "ingestbase.py")
    assert other.reads == {}
    assert other.unclassified != []


# -- a layer function that retries, and a method got by name -------------------------------------------------


LAYER_CALLS = [
    "from expcore_onedata.ranges import place_ranges\n\n\ndef f(p, rows):\n    return place_ranges(p, rows)\n",
    "from expcore_onedata.eviction import evict_ids as gone\n\n\ndef f(p, ids):\n    return gone(p, ids, 'a')\n",
    "from expcore_onedata.eviction import evict_tree\n\n\ndef f(p, e):\n    return evict_tree(p, e, 'a')\n",
    "from expcore_onedata import eviction\n\n\ndef f(p, e):\n    return eviction.evict_tree(p, e, 'a')\n",
    "from expcore_onedata import ranges as r\n\n\ndef f(p, rows):\n    return r.place_ranges(p, rows)\n",
    "from expcore_onedata.provision import replicate\n\n\ndef f(p):\n    return replicate(p, '/x', 'a')\n",
    "from expcore_onedata.provision import evict\n\n\ndef f(p):\n    return evict(p, '/x', 'a')\n",
]


@pytest.mark.parametrize("source", LAYER_CALLS)
def test_o29_a_layer_function_that_retries_is_a_site_and_breaches_in_a_measuring_module(source):
    """O29: `place_ranges`, `evict_ids`, `evict_tree`, `replicate` and `evict` retry the reads they make under their
    provisioner's policy, so a call of one, however imported, is a helper in its retrying form."""
    scan = scan_source(source, "probes.py")
    assert len(scan.helpers) == 1
    assert scan.unclassified == []
    assert measuring_breaches(scan) != []


def test_o29_a_layer_function_that_retries_handed_on_as_a_value_is_unclassified():
    """O29: `map(evict_ids, ...)` and a `functools.partial` of it that is never called here cannot be followed; the
    partial that is called is the call it stands for."""
    value = "from expcore_onedata.eviction import evict_ids\n\n\ndef f(xs):\n    return list(map(evict_ids, xs))\n"
    assert scan_source(value, "probes.py").unclassified != []
    bound = (
        "import functools\n\nfrom expcore_onedata.eviction import evict_ids\n\n\n"
        "def f(p, ids):\n    return functools.partial(evict_ids, p, ids)('a')\n"
    )
    scan = scan_source(bound, "probes.py")
    assert (len(scan.helpers), scan.unclassified) == (1, [])


def test_o29_a_name_that_is_no_import_of_the_layer_is_no_layer_call():
    """O29: a function of that name defined here, or taken from the standard library, is not the layer's."""
    local = "def evict(path):\n    return path\n\n\ndef f(p):\n    return evict(p)\n"
    assert scan_source(local, "probes.py").helpers == {}


GETATTR_CALLS = {
    "get": ("def f(oz, r):\n    return getattr(oz, 'get')('/spaces/x', retry=r)\n", "client.get"),
    "read_range": (
        "def f(oz, p, r):\n    return getattr(oz, 'read_range')('/x', provider=p, offset=0, length=1, gate_retry=r)\n",
        "client.read_range",
    ),
}


@pytest.mark.parametrize("name", ["get", "read_range"])
def test_o29_a_method_got_by_getattr_under_a_constant_name_is_the_call_it_stands_for(name):
    """O29: `getattr(oz, "get")(path, retry=r)` is `oz.get(path, retry=r)`: a site with that policy, a breach in a
    measuring module."""
    source, callee = GETATTR_CALLS[name]
    scan = scan_source(source, "probes.py")
    assert [site[2] for site in scan.reads] == [callee]
    assert measuring_breaches(scan) != []


def test_o29_a_method_got_by_getattr_under_a_name_the_scanner_cannot_read_is_unclassified():
    """O29: `getattr(oz, name)(...)` may be any read of the client, and a watched method got by `getattr` and kept as
    a value cannot be followed; both fail closed. An attribute that is none of the layer's reads is no concern."""
    dynamic = "def f(oz, name):\n    return getattr(oz, name)('/spaces/x', retry=POLICY)\n"
    assert scan_source(dynamic, "probes.py").unclassified != []
    kept = "def f(oz):\n    g = getattr(oz, 'get')\n    return g('/spaces/x', retry=POLICY)\n"
    assert scan_source(kept, "probes.py").unclassified != []
    harmless = "def f(error):\n    return getattr(error, 'status', None), getattr(error, 'args')\n"
    assert scan_source(harmless, "probes.py").unclassified == []


@pytest.mark.parametrize("name", sorted(RENAMED_RECEIVERS))
def test_o29_a_bypass_added_to_the_real_probes_module_is_caught(name):
    """O29: the layer's own `probes.py`, a module that measures, breaches nothing; with one of the renamed-receiver
    reads added to it, it does, so the table fails on the change a reviewer must see."""
    real = (SRC / "probes.py").read_text(encoding="utf-8")
    assert measuring_breaches(scan_source(real, "probes.py", NOT_THE_PROVISIONERS)) == []
    mutated = scan_source(real + "\n\n" + RENAMED_RECEIVERS[name], "probes.py", NOT_THE_PROVISIONERS)
    assert measuring_breaches(mutated) != []

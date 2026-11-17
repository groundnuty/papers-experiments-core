"""O89: every write to a run's `infrastructure_facts` goes through one helper, which merges and never drops a key.

Three times on 16 Nov 2026 one feature replaced the dict and dropped what another had put there (the work disk's proof
dropped `provider_storage`, then `node` and `federation_window`; a Docker start dropped the window). The helper
(`expcore_onedata.sandbox.infrafacts`) has no way to replace or remove, and refuses (a bug of the writer) to overwrite a
key another writer owns. The scanner at the end fails for any source that still writes the dict by itself.
"""

import ast
import threading
from pathlib import Path

import pytest

from expcore_onedata.sandbox import cellhost, cellresolv, fedwindow, hostfacts, infrafacts
from expcore_onedata.sandbox.apptainer import facts as apptainer_facts
from expcore_onedata.sandbox.infrafacts import FactsConflict, InfrastructureFacts

SRC = Path(__file__).resolve().parents[3] / "src"
HELPER = SRC / "expcore_onedata" / "sandbox" / "infrafacts.py"
LITERAL = "infrastructure_facts"
PRIVATE = "_TABLE"  # the helper's own name for it


def handle(meta=None):
    meta = {} if meta is None else meta
    return InfrastructureFacts(meta, threading.RLock()), meta


def test_o89_a_run_has_no_facts_until_one_is_written_and_the_first_write_makes_the_table():
    """O89: a Docker run's record without a window or a node keeps no empty `infrastructure_facts` (as before)."""
    facts, meta = handle()
    assert LITERAL not in meta
    facts.write("another_feature", {"fact": 1})
    assert meta[LITERAL] == {"fact": 1}


def test_o89_writers_with_different_keys_merge_and_every_key_stays():
    """O89: the class of the three drops: a second writer's keys join the first's, never take their place."""
    facts, meta = handle()
    facts.write("work_disk", {"facts": ["a"], "provider_storage": "tmpfs"})
    facts.write("node", {"node": {"kernel": "k"}})
    facts.write("federation_window", {"federation_window": {"verdict": "unknown"}})
    facts.write("work_disk", {"facts": ["a", "b"], "provider_storage": "tmpfs"})  # the proof sets its own again
    assert meta[LITERAL] == {
        "facts": ["a", "b"],
        "provider_storage": "tmpfs",
        "node": {"kernel": "k"},
        "federation_window": {"verdict": "unknown"},
    }


def test_o89_a_writer_overwriting_the_key_of_another_is_refused_and_nothing_of_its_update_is_written():
    """O89: the key stays the first writer's, and an update that names one such key writes none of its other keys."""
    facts, meta = handle()
    facts.write("node", {"node": {"kernel": "k"}})
    with pytest.raises(FactsConflict, match="node"):
        facts.write("work_disk", {"facts": ["new"], "node": {"kernel": "other"}})
    assert meta[LITERAL] == {"node": {"kernel": "k"}}


def test_o89_a_writer_updating_its_own_key_replaces_the_value():
    """O89: the window is recorded pending, then closed; both are the platform's, so the second replaces the first."""
    facts, meta = handle()
    facts.write("federation_window", {"federation_window": "pending"})
    facts.write("federation_window", {"federation_window": "closed"})
    assert meta[LITERAL] == {"federation_window": "closed"}


def test_o89_the_sandboxs_own_keys_belong_to_their_writers_before_any_of_them_has_written():
    """O89: a stray write of `node` or `limits` is the one refused, never the sandbox's own that comes after it."""
    facts, meta = handle()
    with pytest.raises(FactsConflict, match="node"):
        facts.write("a_stray_feature", {"node": "stray"})
    assert LITERAL not in meta
    facts.write("node", {"node": {"kernel": "k"}})  # the legitimate writer is not refused
    assert meta[LITERAL]["node"] == {"kernel": "k"}


def test_o89_a_key_nobody_declared_belongs_to_its_first_writer():
    """O89: a feature that adds a fact (`record_infrastructure_fact`) owns it, and no other writer may take it."""
    facts, meta = handle()
    facts.write("feature_a", {"fact": 1})
    facts.write("feature_a", {"fact": 2})
    with pytest.raises(FactsConflict, match="feature_a"):
        facts.write("feature_b", {"fact": 3})
    assert meta[LITERAL] == {"fact": 2}


def test_o89_a_refusal_names_the_key_and_both_writers_and_never_a_value():
    facts, _ = handle()
    facts.write("feature_a", {"fact": "a secret value"})
    with pytest.raises(FactsConflict) as refused:
        facts.write("feature_b", {"fact": "another value"})
    text = str(refused.value)
    assert "fact" in text
    assert "feature_a" in text
    assert "feature_b" in text
    assert "value" not in text


def test_o89_a_table_that_something_else_wrote_is_refused_for_its_keys():
    """O89: a key in the table that no writer of the helper claimed was written around it: never overwritten quietly."""
    facts, meta = handle({LITERAL: {"fact": 1}})
    with pytest.raises(FactsConflict, match="fact"):
        facts.write("feature_a", {"fact": 2})
    assert meta[LITERAL] == {"fact": 1}


def test_o89_a_snapshot_gets_the_facts_made_at_the_read_beside_the_ones_the_run_holds():
    """O89: `metadata()` adds `limits`, `memory` and the like to its copy: the copy keeps every stored key, and the
    run's own table is not touched."""
    facts, meta = handle()
    facts.write("node", {"node": {"kernel": "k"}})
    snapshot = {LITERAL: dict(meta[LITERAL])}
    facts.annotate(snapshot, "metadata", {"limits": {"cpus": "set"}})
    facts.annotate(snapshot, "metadata", {"limits": {"cpus": "unknown"}})  # a second read of the same run
    assert snapshot[LITERAL] == {"node": {"kernel": "k"}, "limits": {"cpus": "unknown"}}
    assert meta[LITERAL] == {"node": {"kernel": "k"}}


def test_o89_a_snapshot_never_overwrites_a_stored_key_of_another_writer():
    facts, meta = handle()
    facts.write("node", {"node": {"kernel": "k"}})
    snapshot = {LITERAL: dict(meta[LITERAL])}
    with pytest.raises(FactsConflict, match="node"):
        facts.annotate(snapshot, "metadata", {"node": "overwritten"})
    assert snapshot[LITERAL] == {"node": {"kernel": "k"}}


def test_o89_a_snapshot_of_a_run_with_no_table_makes_it_for_the_facts_it_adds():
    facts, _ = handle()
    snapshot: dict = {}
    facts.annotate(snapshot, "a_feature", {"a_fact_made_at_the_read": "kept"})  # an undeclared key: its writer's own
    assert snapshot == {LITERAL: {"a_fact_made_at_the_read": "kept"}}


def test_o89_a_fact_made_at_the_read_cannot_later_be_stored_by_another_writer():
    facts, _ = handle()
    facts.annotate({}, "a_feature", {"a_fact_made_at_the_read": "kept"})  # an undeclared key: its writer's own
    with pytest.raises(FactsConflict, match="a_fact_made_at_the_read"):
        facts.write("another_feature", {"a_fact_made_at_the_read": "other"})


def test_o89_a_fact_read_back_is_the_stored_one_or_the_default():
    facts, _ = handle()
    assert facts.get("node") is None
    assert facts.get("node", "fallback") == "fallback"
    facts.write("node", {"node": {"kernel": "k"}})
    assert facts.get("node") == {"kernel": "k"}


def test_o89_a_record_cannot_replace_the_whole_table():
    """O89: `sandbox.record(**fields)` updates the metadata: a field named for the table would replace it."""
    infrafacts.refuse_replacement({"lease": 1})
    with pytest.raises(FactsConflict, match="whole"):
        infrafacts.refuse_replacement({"lease": 1, LITERAL: {}})


def test_o89_writers_on_several_threads_lose_none_of_their_keys():
    """O89: a cell's deadline writes from its timer's thread: the lock is the sandbox's own, held for each write."""
    facts, meta = handle()
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            for i in range(50):
                facts.write(f"feature_{n}", {f"fact_{n}_{i}": i})
        except BaseException as error:  # a failure on a thread is a failure of the test
            errors.append(error)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(meta[LITERAL]) == 8 * 50


def test_o89_the_declared_owners_hold_disjoint_keys_that_the_features_actually_write():
    """O89: the helper's table of the sandbox's own keys agrees with the modules that make them, so a key a module adds
    without declaring it here is found by this test and not by a run."""
    keys = [key for owned in infrafacts.OWNED.values() for key in owned]
    assert len(keys) == len(set(keys)), "a key is declared for two writers"
    for work_disk in apptainer_facts.WORK_DISK_KEYS:
        record = apptainer_facts.record(work_disk, mount_refused=True, provider_storage="tmpfs")
        assert set(record) <= infrafacts.OWNED[infrafacts.WORK_DISK]
    assert {fedwindow.KEY} == infrafacts.OWNED[infrafacts.WINDOW]
    assert {"node", "tool_server_host"} == infrafacts.OWNED[infrafacts.NODE]
    assert hostfacts.of_node(lambda: hostfacts.readings()) is not None  # the readings the node's key carries
    # the two facts the cell host name feature adds when the metadata is read (O73): declared, so a stray write of
    # either key is the one refused, and the helper's literal for the first is the feature's own constant
    assert {cellhost.FACT, "own_node_scan"} <= infrafacts.OWNED[infrafacts.METADATA]
    assert cellhost.FACT == "cell_hostname"
    # the hash of the resolv.conf a cell read (O56) is written once, at the start, by its own owner
    assert {cellresolv.FACT} == infrafacts.OWNED[infrafacts.CELL_RESOLV]
    assert cellresolv.FACT == "resolv_conf_sha256"


# ---------------------------------------------------------------------------------------------- the scanner


def offences(source: str) -> list[int]:
    """The lines of a source that reach the table: its key as a string, as a keyword argument, or as an identifier (a
    name, an attribute, a parameter, a field or a definition: `dataclasses.asdict` would make it a key), or the helper's
    private name for it (as a name, an attribute, an import or a string). A docstring or a comment that talks about the
    table is a longer string, or no string, and is not one."""
    found = []
    tree = ast.parse(source)
    documentation = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in (LITERAL, PRIVATE) and id(node) not in documentation:
            found.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == LITERAL:
            found.append(node.value.lineno)
        elif (
            (isinstance(node, ast.Name) and node.id in (LITERAL, PRIVATE))
            or (isinstance(node, ast.Attribute) and node.attr in (LITERAL, PRIVATE))
            or (isinstance(node, ast.arg) and node.arg == LITERAL)
            or (isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == LITERAL)
            or (isinstance(node, ast.ImportFrom) and any(alias.name in (LITERAL, PRIVATE) for alias in node.names))
        ):
            found.append(node.lineno)
    return sorted(found)


@pytest.mark.parametrize(
    "source",
    [
        'meta["infrastructure_facts"]["node"] = 1',
        'meta["infrastructure_facts"] = {}',
        'meta.setdefault("infrastructure_facts", {})["node"] = 1',
        'meta.update({"infrastructure_facts": {}})',
        "meta.update(infrastructure_facts={})",
        'facts = meta["infrastructure_facts"]\nfacts["node"] = 1',
        'facts = meta.get("infrastructure_facts")',
        'x = {"infrastructure_facts": 1}',
        "meta[infrafacts._TABLE] = {}",
        "from expcore_onedata.sandbox.infrafacts import _TABLE",
        "from expcore_onedata.sandbox.infrafacts import _TABLE as table",
        'name = getattr(infrafacts, "_TABLE")',
        "meta.setdefault(_TABLE, {})",
        "@dataclass\nclass Start:\n    infrastructure_facts: dict",
        "def f(infrastructure_facts):\n    return 1",
        "self.infrastructure_facts = {}",
        "infrastructure_facts = {}",
        "def infrastructure_facts():\n    return 1",
        "from somewhere import infrastructure_facts",
    ],
)
def test_o89_the_scanner_finds_each_way_of_reaching_the_table(source):
    assert offences(source), source


def test_o89_the_scanner_passes_a_source_that_only_talks_about_the_table():
    source = '''
"""The run's `infrastructure_facts` hold the node's readings."""

def f():
    """Adds one fact to `infrastructure_facts` and nothing else."""
    # the run's infrastructure_facts
    return "the infrastructure_facts table"
'''
    assert offences(source) == []


def test_o89_no_source_of_the_core_writes_the_table_but_the_helper():
    """O89: the literal appears in `src` only in the helper. A new writer has to be the helper's, or this fails with the
    line, and with it the next feature that would have dropped another's key. (A source that reads the table by the
    literal fails too: a reader asks the sandbox's `metadata()`.)"""
    assert getattr(infrafacts, PRIVATE) == LITERAL, "the helper's private name for the table is not the one scanned for"
    assert offences(HELPER.read_text(encoding="utf-8")), "the scanner no longer sees the helper's own key"
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        if path == HELPER:
            continue
        lines = offences(path.read_text(encoding="utf-8"))
        if lines:
            found[path.relative_to(SRC).as_posix()] = lines
    assert not found, f"writes the run's infrastructure facts outside expcore_onedata.sandbox.infrafacts: {found}"

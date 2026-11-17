"""O63: what a shard keeps beside its archive, copied back with it. A shard's spend journals (a new file per step:
what a killed step spent, which `--resume` counts as `lost_in_kill`), its archive manifest `<label>.out.json` and the
newest snapshot of its `out/` that is newer than the verified archive (a step resumed from the archive and killed took
it: it holds runs the archive lacks) are listed by `shards.py collect` in `archives.sha256.json` and checked by
`verify-copy`, because the site's scratch is cleared after the pull. Snapshots that are not newer than the archive are
resume material and not listed. Every path and name here is invented."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest
from campaign_support import NOW, finished, make_manifest, make_schema
from copy_back_support import JOURNALS, MANIFEST, SCRIPT, archived, make_plan, plan_file, pull, pulled, sha_of, verify

from expcore.campaign import out_snapshot
from expcore.campaign.markers import write_marker
from expcore.campaign.store import Store

SCHEMA = make_schema()
ROWS = make_manifest(SCHEMA).rows
KEYS = [row.key.text for row in ROWS]


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_copy_back_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_copy_back_evidence"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_copy_back_evidence", None)


def _evidence(plan):
    return json.loads((plan.results / MANIFEST).read_text(encoding="utf-8"))["evidence"]


def _names(plan):
    return [entry["name"] for entry in _evidence(plan)]


def _journal_names(label):
    return [f"{label}/journals/{name}" for name in JOURNALS[label]]


# ------------------------------------------------------------------------------------------------ a live out/
def _live(tmp_path, label):
    """A shard's out/ with a real store, as a dispatcher leaves it (the core's own schema and markers)."""
    out = tmp_path / "live" / label / "out"
    (out / "runs").mkdir(parents=True)
    (out / "campaign.lock").write_text("")
    store = Store(out / "state.sqlite", SCHEMA)
    store.sync_manifest(ROWS)
    return store, out


def _run(store, out, key):
    """One attempt, begun and ended: its record and its marker in its directory, its row finished."""
    digest = store.q("SELECT digest FROM runs WHERE run_key=?", (key,))[0]["digest"]
    run_dir = out / "runs" / digest / "try1"
    run_dir.mkdir(parents=True)
    begun = store.begin_attempt(key, NOW, 4321, str(run_dir))
    (run_dir / "record.json").write_text(json.dumps({"run": key}))
    result = finished(input_tokens=1000, output_tokens=200)
    write_marker(run_dir, key, begun.id, result)
    store.finish_attempt(begun.id, result, 3)


def _snapshot(tmp_path, store, out, label, base, index=1, count=2, stamp="20261116T150000Z"):
    """The core's own snapshot of `out` into the shard's snapshots directory, naming the archive it started from."""
    facts = {"shard": {"index": index, "count": count, "label": label}, "base": base}
    into = tmp_path / "results" / label / "snapshots"
    return out_snapshot.take(out, into, scratch=tmp_path / "scratch", facts=facts, live=True, now=stamp)


def _archive_sha(tmp_path, label):
    return json.loads((tmp_path / "results" / f"{label}.out.json").read_text(encoding="utf-8"))["sha256"]


def _relative(path, root):
    return path.relative_to(root).as_posix()


# ------------------------------------------------------------------------------------------------ collect's evidence
def test_the_manifest_lists_each_shards_journals_and_archive_manifest_with_their_hashes(shards, tmp_path):
    """O63: every file under `<label>/journals/` and `<label>.out.json`, with its relative name, size and SHA-256, the
    names sorted, taken from the files and never from a step's claim."""
    plan = make_plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    wanted = sorted(["fed-a.out.json", "fed-b.out.json", *_journal_names("fed-a"), *_journal_names("fed-b")])
    assert _evidence(plan) == [
        {"name": name, "bytes": (plan.results / name).stat().st_size, "sha256": sha_of(plan.results / name)}
        for name in wanted
    ]


def test_the_manifest_with_its_evidence_is_the_same_for_every_collect_of_unchanged_results(shards, tmp_path):
    """O63: no timestamp and no directory in it; a journal that grows between two collects changes it, as it must."""
    plan = make_plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "first")
    first = (plan.results / MANIFEST).read_bytes()
    shards.collect(plan, tmp_path / "second")
    assert (plan.results / MANIFEST).read_bytes() == first
    assert str(tmp_path) not in first.decode()
    journal = plan.results / "fed-a" / "journals" / next(iter(JOURNALS["fed-a"]))
    journal.write_bytes(journal.read_bytes() + b'{"spent": 9}\n')
    shards.collect(plan, tmp_path / "third")
    assert (plan.results / MANIFEST).read_bytes() != first


def test_a_shard_with_no_journals_directory_lists_its_archive_manifest_alone(shards, tmp_path):
    """O63: no journal is not an error; the archive manifest is listed whatever else is there."""
    plan = shards.load_plan(plan_file(tmp_path))
    archived(shards, tmp_path, "fed-a", 1, journals=False)
    archived(shards, tmp_path, "fed-b", 2)
    shards.collect(plan, tmp_path / "merge")
    assert _names(plan) == sorted(["fed-a.out.json", "fed-b.out.json", *_journal_names("fed-b")])


def test_a_journal_in_a_subdirectory_is_listed_by_its_relative_name(shards, tmp_path):
    """O63: every file under journals/ is evidence, wherever it lies."""
    plan = make_plan(shards, tmp_path)
    (plan.results / "fed-a" / "journals" / "old").mkdir()
    (plan.results / "fed-a" / "journals" / "old" / "one.jsonl").write_text("{}\n")
    shards.collect(plan, tmp_path / "merge")
    assert "fed-a/journals/old/one.jsonl" in _names(plan)


@pytest.mark.parametrize("kind", ["file link", "directory link", "directory of links", "journals is a link"])
def test_a_link_among_the_journals_refuses_the_collect_whole_naming_the_shard(shards, tmp_path, kind):
    """O63: a link is never followed to be hashed, so the pull could not reproduce it: the collect is refused, naming
    the shard, with no manifest and no directory left."""
    plan = make_plan(shards, tmp_path)
    journals = plan.results / "fed-a" / "journals"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.jsonl").write_text("{}\n")
    if kind == "file link":
        (journals / "link.jsonl").symlink_to(elsewhere / "x.jsonl")
    elif kind == "directory link":
        (journals / "dir").symlink_to(elsewhere)
    elif kind == "directory of links":
        (journals / "sub").mkdir()
        (journals / "sub" / "link.jsonl").symlink_to(elsewhere / "x.jsonl")
    else:
        shutil.rmtree(journals)
        journals.symlink_to(elsewhere)
    with pytest.raises(shards.ShardsError, match="fed-a"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()
    assert not (tmp_path / "merge").exists()


def test_an_archive_manifest_that_no_longer_names_the_verified_archive_refuses_the_collect(
    shards, tmp_path, monkeypatch
):
    """O63: the `.out.json` listed is the one of the archive hashed. A step that published another between the two
    reads (collect run while a step ends) would have the manifest vouch for a pair that never existed."""
    plan = make_plan(shards, tmp_path)
    real = shards.verified

    def verified_then_replaced(results, shard, scratch):
        found = real(results, shard, scratch)
        own = results / f"{shard.label}.out.json"
        own.write_text(json.dumps(json.loads(own.read_text()) | {"sha256": "0" * 64}))
        return found

    monkeypatch.setattr(shards, "verified", verified_then_replaced)
    with pytest.raises(shards.ShardsError, match="fed-a"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()


# ------------------------------------------------------------------------------------------------ the snapshots
def _with_a_newer_snapshot(shards, tmp_path, label="fed-a", index=1):
    """Both shards archived; `label` was resumed from its archive and killed after a run it took a snapshot of."""
    plan = make_plan(shards, tmp_path)
    store, out = _live(tmp_path, label)
    _run(store, out, KEYS[0])
    snapshot = _snapshot(tmp_path, store, out, label, _archive_sha(tmp_path, label), index=index)
    store.close()
    return plan, snapshot


def test_a_snapshot_newer_than_the_verified_archive_is_listed_with_every_file_and_warned_of(shards, tmp_path, capsys):
    """O63: its `facts.base` is the archive's SHA-256, so a step started from the archive and was killed: the snapshot
    holds a run the merge will not. Every file of it (store, runs/, its manifest) is listed with its hash, and the
    collect says so, naming the shard and the snapshot (it does not refuse: the merge is of the archive)."""
    plan, snapshot = _with_a_newer_snapshot(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    seen = capsys.readouterr().err
    assert "fed-a" in seen
    assert snapshot.name in seen
    assert "newer than its archive" in seen
    assert "fed-b" not in seen
    files = sorted(_relative(path, plan.results) for path in snapshot.rglob("*") if path.is_file())
    assert files
    assert f"fed-a/snapshots/{snapshot.name}/snapshot.json" in files
    assert f"fed-a/snapshots/{snapshot.name}/state.sqlite" in files
    assert any("/runs/" in name for name in files)
    listed = [entry for entry in _evidence(plan) if "/snapshots/" in entry["name"]]
    assert listed == [
        {"name": name, "bytes": (plan.results / name).stat().st_size, "sha256": sha_of(plan.results / name)}
        for name in files
    ]


def test_a_snapshot_taken_before_the_archive_is_resume_material_and_not_listed(shards, tmp_path, capsys):
    """O63: a snapshot that names no archive (the shard's first step) or an older one was taken before the archive and
    holds nothing it lacks: not listed, and nothing said."""
    plan = make_plan(shards, tmp_path)
    store, out = _live(tmp_path, "fed-a")
    _run(store, out, KEYS[0])
    _snapshot(tmp_path, store, out, "fed-a", None)  # the first step's own: its base is none
    _snapshot(tmp_path, store, out, "fed-b", "0" * 64, index=2, stamp="20261116T150100Z")  # one of an earlier archive
    store.close()
    shards.collect(plan, tmp_path / "merge")
    assert not [name for name in _names(plan) if "/snapshots/" in name]
    assert "newer" not in capsys.readouterr().err


def test_only_the_newest_snapshot_that_verifies_is_listed(shards, tmp_path):
    """O63: the one `--resume` would restore (`restore_newest`, with the archive named as the base): of two that name
    the archive, the newer, and its older sibling is left out."""
    plan = make_plan(shards, tmp_path)
    store, out = _live(tmp_path, "fed-a")
    base = _archive_sha(tmp_path, "fed-a")
    _run(store, out, KEYS[0])
    older = _snapshot(tmp_path, store, out, "fed-a", base)
    _run(store, out, KEYS[1])
    newer = _snapshot(tmp_path, store, out, "fed-a", base, stamp="20261116T150100Z")
    store.close()
    shards.collect(plan, tmp_path / "merge")
    snapshots = {name.split("/")[2] for name in _names(plan) if "/snapshots/" in name}
    assert snapshots == {newer.name} != {older.name}


def test_a_newest_snapshot_that_does_not_verify_falls_back_to_the_older_one_and_says_so(shards, tmp_path, capsys):
    """O63: a snapshot a resume would not use (a file changed since it was taken) is never listed as the evidence."""
    plan = make_plan(shards, tmp_path)
    store, out = _live(tmp_path, "fed-a")
    base = _archive_sha(tmp_path, "fed-a")
    _run(store, out, KEYS[0])
    older = _snapshot(tmp_path, store, out, "fed-a", base)
    _run(store, out, KEYS[1])
    newer = _snapshot(tmp_path, store, out, "fed-a", base, stamp="20261116T150100Z")
    store.close()
    record = next(newer.rglob("record.json"))
    record.write_text('{"changed": true}')
    shards.collect(plan, tmp_path / "merge")
    snapshots = {name.split("/")[2] for name in _names(plan) if "/snapshots/" in name}
    assert snapshots == {older.name}
    assert f"snapshot {newer.name} is not used" in capsys.readouterr().err


def test_a_snapshot_of_another_shard_refuses_the_collect_naming_it_as_a_resume_does(shards, tmp_path):
    """O63: `--resume` refuses it (O62), so `collect` picks the same and does the same."""
    plan = make_plan(shards, tmp_path)
    store, out = _live(tmp_path, "fed-a")
    _run(store, out, KEYS[0])
    facts = {"shard": {"index": 2, "count": 2, "label": "fed-b"}, "base": _archive_sha(tmp_path, "fed-a")}
    out_snapshot.take(
        out, tmp_path / "results" / "fed-a" / "snapshots", scratch=tmp_path / "scratch", facts=facts, live=True, now="x"
    )
    store.close()
    with pytest.raises(shards.ShardsError, match=r"fed-a.*snap-000001"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()


def test_the_check_of_a_snapshot_leaves_nothing_in_the_directory_collected_into(shards, tmp_path):
    """O63: the snapshot is read through a restore into a private directory under the target, which is removed: the
    merge, run there, sees the shards' directories and nothing else."""
    plan, _ = _with_a_newer_snapshot(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    assert sorted(os.listdir(tmp_path / "merge")) == ["fed-a", "fed-b"]


def test_a_collect_refused_after_a_snapshot_was_read_leaves_nothing_behind(shards, tmp_path, monkeypatch):
    """O63: the manifest cannot be written (the results directory is full) once the snapshot has been read through its
    restore: refused whole, and the target, with that private directory, is gone."""
    plan, _ = _with_a_newer_snapshot(shards, tmp_path)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shards, "_write_whole", full)
    with pytest.raises(shards.ShardsError, match="manifest"):
        shards.collect(plan, tmp_path / "merge")
    assert not (tmp_path / "merge").exists()


# ------------------------------------------------------------------------------------------------ verify-copy
def _evidence_file(local, label="fed-a"):
    return local / label / "journals" / next(iter(JOURNALS[label]))


def test_verify_copy_passes_on_an_exact_copy_of_archives_journals_and_manifests(shards, tmp_path, capsys):
    """O63: the whole results directory pulled (archives, `.out.json` files, journals and the manifest): exit 0, and
    the message says how many archives and how many files beside them were checked."""
    manifest, local, given = pulled(shards, tmp_path)
    assert verify(shards, manifest, local, given) == 0
    seen = capsys.readouterr()
    assert "2 archives" in seen.out
    assert "5 evidence files" in seen.out
    assert seen.err == ""


def test_verify_copy_passes_on_an_exact_copy_with_the_newer_snapshot(shards, tmp_path, capsys):
    """O63: the listed snapshot's files are in the pull and pass; one of them gone is named."""
    plan, snapshot = _with_a_newer_snapshot(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    manifest, local, given = pull(plan, tmp_path)
    assert verify(shards, manifest, local, given) == 0
    capsys.readouterr()
    gone = local / "fed-a" / "snapshots" / snapshot.name / "state.sqlite"
    gone.unlink()
    assert verify(shards, manifest, local, given) == 1
    seen = capsys.readouterr().err
    assert f"fed-a/snapshots/{snapshot.name}/state.sqlite: missing" in seen


def test_verify_copy_fails_naming_a_missing_journal_and_no_other(shards, tmp_path, capsys):
    """O63: the spend journal of a killed step, which `--resume` counts as lost, is not in the pull: exit 1, named."""
    manifest, local, given = pulled(shards, tmp_path)
    _evidence_file(local).unlink()
    assert verify(shards, manifest, local, given) == 1
    seen = capsys.readouterr()
    name = f"fed-a/journals/{next(iter(JOURNALS['fed-a']))}"
    assert f"{name}: missing" in seen.err
    assert "fed-b" not in seen.err
    assert "tar" not in seen.err
    assert str(tmp_path) not in seen.err + seen.out


def test_verify_copy_fails_naming_a_journal_with_one_byte_changed(shards, tmp_path, capsys):
    """O63: the same size and one different byte is caught by the SHA-256."""
    manifest, local, given = pulled(shards, tmp_path)
    path = _evidence_file(local)
    data = bytearray(path.read_bytes())
    data[3] ^= 0x01
    path.write_bytes(bytes(data))
    assert verify(shards, manifest, local, given) == 1
    assert f"{next(iter(JOURNALS['fed-a']))}: SHA-256 differs" in capsys.readouterr().err


def test_verify_copy_fails_naming_a_truncated_journal(shards, tmp_path, capsys):
    """O63: a journal cut short (a pull that stopped) is caught by its size."""
    manifest, local, given = pulled(shards, tmp_path)
    path = _evidence_file(local)
    path.write_bytes(path.read_bytes()[:-3])
    assert verify(shards, manifest, local, given) == 1
    assert f"{next(iter(JOURNALS['fed-a']))}: size differs" in capsys.readouterr().err


def test_verify_copy_fails_naming_a_missing_or_changed_archive_manifest(shards, tmp_path, capsys):
    """O63: `<label>.out.json` is checked like an archive."""
    manifest, local, given = pulled(shards, tmp_path)
    (local / "fed-a.out.json").unlink()
    (local / "fed-b.out.json").write_text("{}")
    assert verify(shards, manifest, local, given) == 1
    seen = capsys.readouterr().err
    assert "fed-a.out.json: missing" in seen
    assert "fed-b.out.json" in seen


def test_verify_copy_names_every_failure_across_archives_and_evidence_in_one_run(shards, tmp_path, capsys):
    """O63: an archive, a journal and a manifest at once: all three are named, with the count."""
    manifest, local, given = pulled(shards, tmp_path)
    (local / "fed-a.out.tar").write_bytes(b"x")
    _evidence_file(local).unlink()
    (local / "fed-b.out.json").unlink()
    assert verify(shards, manifest, local, given) == 1
    seen = capsys.readouterr().err
    assert "3 of 7" in seen
    assert "fed-a.out.tar" in seen
    assert "fed-a/journals/" in seen
    assert "fed-b.out.json" in seen


def test_verify_copy_does_not_take_a_directory_or_a_link_for_a_journal(shards, tmp_path, capsys):
    """O63: only a regular file is a journal."""
    manifest, local, given = pulled(shards, tmp_path)
    first, second = (local / "fed-a" / "journals" / name for name in JOURNALS["fed-a"])
    first.unlink()
    first.mkdir()
    second.rename(tmp_path / "elsewhere.jsonl")
    second.symlink_to(tmp_path / "elsewhere.jsonl")
    assert verify(shards, manifest, local, given) == 1
    seen = capsys.readouterr().err
    assert seen.count("not a regular file") == 2


def test_verify_copy_does_not_follow_a_link_that_stands_for_a_directory_of_the_path(shards, tmp_path, capsys):
    """O63: `fed-a/journals` a link to a directory that holds the very same files is no pull of them: each file under
    it is named as failing, and nothing is read through the link."""
    manifest, local, given = pulled(shards, tmp_path)
    shutil.move(local / "fed-a" / "journals", tmp_path / "moved")
    (local / "fed-a" / "journals").symlink_to(tmp_path / "moved")
    assert verify(shards, manifest, local, given) == 1
    seen = capsys.readouterr().err
    assert all(f"fed-a/journals/{name}" in seen for name in JOURNALS["fed-a"])
    assert "fed-b" not in seen


def test_verify_copy_warns_of_an_extra_journal_or_archive_manifest_and_still_passes(shards, tmp_path, capsys):
    """O63: a journal the manifest does not name (a step that ran after `collect`), or a `.out.json` of another
    campaign, is warned of and never a failure."""
    manifest, local, given = pulled(shards, tmp_path)
    (local / "fed-a" / "journals" / "20261117T000000Z-9-dddd0004.jsonl").write_text("{}\n")
    (local / "fed-z.out.json").write_text("{}")
    assert verify(shards, manifest, local, given) == 0
    seen = capsys.readouterr().err
    assert seen.count("WARNING") == 2
    assert "fed-a/journals/20261117T000000Z-9-dddd0004.jsonl" in seen
    assert "fed-z.out.json" in seen


def _rewrite(manifest, mutate):
    """The manifest after `mutate(data)`, and the hash of the file now: what an operator given that hash would use."""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    mutate(data)
    manifest.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def _entry(**changes):
    return {"name": "fed-a.out.json", "bytes": 1, "sha256": "0" * 64} | changes


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(None, id="none"),
        pytest.param([], id="empty"),
        pytest.param("fed-a.out.json", id="str"),
        pytest.param([_entry(name="../fed-a.out.json")], id="up"),
        pytest.param([_entry(name="/fed-a.out.json")], id="absolute"),
        pytest.param([_entry(name="fed-a//journals/x")], id="empty-part"),
        pytest.param([_entry(name="fed-a/./journals/x")], id="dot-part"),
        pytest.param([_entry(name="fed-a/../x")], id="dotdot-part"),
        pytest.param([_entry(name="fed-a\\journals\\x")], id="backslash"),
        pytest.param([_entry(name="fed-a/journals/\u0000x")], id="nul"),
        pytest.param([_entry(name="")], id="no-name"),
        pytest.param([_entry(name="fed-a/")], id="trailing-slash"),
        pytest.param([_entry(name=7)], id="name-not-str"),
        pytest.param([_entry(bytes=-1)], id="negative"),
        pytest.param([_entry(bytes=True)], id="bool"),
        pytest.param([_entry(bytes="1")], id="bytes-str"),
        pytest.param([_entry(sha256="short")], id="short-hash"),
        pytest.param([{"name": "fed-a.out.json", "bytes": 1}], id="no-hash"),
        pytest.param([_entry(), _entry()], id="twice"),
        pytest.param([_entry(name="fed-a.out.tar")], id="an-archive-name"),
        pytest.param(["fed-a.out.json"], id="entry-not-object"),
    ],
)
def test_verify_copy_refuses_evidence_it_cannot_read_as_such_before_it_reads_any_file(shards, tmp_path, evidence):
    """O63: a manifest whose hash is right but whose evidence is malformed (no list, none named, a name that leaves the
    directory, an entry without its size and hash, a name given twice) is exit 2, never a pass on the archives alone."""
    manifest, local, _ = pulled(shards, tmp_path)
    given = _rewrite(manifest, lambda data: data.update(evidence=evidence))
    assert verify(shards, manifest, local, given) == 2


def test_verify_copy_refuses_a_manifest_with_no_evidence_section_as_one_it_cannot_use(shards, tmp_path, capsys):
    """O63: a manifest of the archives alone (an earlier tip's) must not be passed by a verifier that no longer looks
    at what lies beside them: exit 2, naming the evidence."""
    manifest, local, _ = pulled(shards, tmp_path)
    given = _rewrite(manifest, lambda data: data.pop("evidence"))
    assert verify(shards, manifest, local, given) == 2
    assert "evidence" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ no core on the host
def _on_a_bare_interpreter(*args):
    """`shards.py` run as the operator's host runs it: isolated (`-I`) and without site-packages (`-S`), where the
    core, which this environment has installed, cannot be imported."""
    return subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), *args], capture_output=True, text=True, check=False, timeout=120
    )


def test_verify_copy_checks_the_evidence_on_a_host_without_the_core(shards, tmp_path):
    """O63: standard library alone: an exact pull passes, a journal changed fails naming it, the core not importable."""
    manifest, local, given = pulled(shards, tmp_path)
    args = ("verify-copy", str(manifest), str(local), "--manifest-sha256", given)
    passed = _on_a_bare_interpreter(*args)
    assert passed.returncode == 0, passed.stderr
    assert "2 archives" in passed.stdout
    assert "5 evidence files" in passed.stdout
    _evidence_file(local).write_bytes(b"x")
    failed = _on_a_bare_interpreter(*args)
    assert failed.returncode == 1
    assert next(iter(JOURNALS["fed-a"])) in failed.stderr
    assert "Traceback" not in failed.stderr

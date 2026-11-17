"""O63: `shards.py verify-copy` over the evidence beside the archives (the journals, the archive manifests and a
snapshot newer than the archive): it fails, naming each file, when a pulled one is missing or differs, passes an exact
pull, and refuses a manifest whose evidence it cannot read. What `collect` lists is in
`test_hpc_shards_copy_back_evidence.py`. Every path and name here is invented."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys

import pytest
from copy_back_support import JOURNALS, SCRIPT, make_plan, pull, pulled, verify, with_newer_snapshot


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_verify_copy_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_verify_copy_evidence"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_verify_copy_evidence", None)


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
    plan, snapshot = with_newer_snapshot(shards, tmp_path)
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


def test_verify_copy_warns_once_of_an_extra_file_under_a_nested_journals_directory(shards, tmp_path, capsys):
    """O63: a journals directory is walked once, whatever lies under it, so one extra file is one warning."""
    plan = make_plan(shards, tmp_path)
    (plan.results / "fed-a" / "journals" / "old").mkdir()
    (plan.results / "fed-a" / "journals" / "old" / "listed.jsonl").write_text("{}\n")
    shards.collect(plan, tmp_path / "merge")
    manifest, local, given = pull(plan, tmp_path)
    (local / "fed-a" / "journals" / "old" / "later.jsonl").write_text("{}\n")
    assert verify(shards, manifest, local, given) == 0
    seen = capsys.readouterr().err
    assert seen.count("WARNING") == 1
    assert "fed-a/journals/old/later.jsonl" in seen


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

"""O63: `shards.py collect` archives each federation's ROOT evidence (what `down` removes with the root and the study's
post-check and the facts read: the state, `platform.toml`, `facts.json`, `link-check.json`, `render-identity.json`, the
fault journal and the fault relay's journal) to `<results>/<label>.root.tar` with a manifest of its own, lists both in
`archives.sha256.json` for `verify-copy`, and writes a collected-evidence marker into the root for `down`. The
archive is an allow-list of evidence files: nothing from `secrets/`, no file holding a token, no link followed. Every
path, name and token here is invented."""

import argparse
import dataclasses
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

import pytest
from copy_back_support import MANIFEST, SCRIPT, make_plan, plan_file, pull, sha_of, verify
from root_evidence_support import EVIDENCE, NOT_EVIDENCE, PASSPHRASE, TOKEN, make_root, mode_of

DEPLOY = SCRIPT.parent


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_root_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_root_evidence"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_root_evidence", None)


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def _collect(shards, tmp_path, into="merge"):
    plan = make_plan(shards, tmp_path)
    shards.collect(plan, tmp_path / into)
    return plan


# ------------------------------------------------------------------------------------------------ the allow-list
def test_the_root_archive_holds_exactly_the_evidence_allow_list(shards, tmp_path):
    """O63: every file of the allow-list that the root holds, by its name relative to the root, and nothing else: the
    relay's heartbeat and lock, the CA, the shim and the providers' data are not evidence."""
    plan = _collect(shards, tmp_path)
    for shard in plan.shards:
        assert _members(plan.results / f"{shard.label}.root.tar") == EVIDENCE


def test_no_secret_reaches_a_root_archive(shards, tmp_path):
    """O63: nothing of `secrets/`, no file whose mode is tight because it holds a token, key or password, and no file
    whose content holds a planted token or passphrase: by construction (an allow-list), and checked here on bytes."""
    plan = _collect(shards, tmp_path)
    for shard in plan.shards:
        archive = plan.results / f"{shard.label}.root.tar"
        members = _members(archive)
        assert not [name for name in members if name.startswith("secrets/") or name.endswith((".token", ".key"))]
        for content in members.values():
            assert TOKEN.encode() not in content
            assert PASSPHRASE.encode() not in content
        assert TOKEN.encode() not in archive.read_bytes()
        assert TOKEN.encode() not in (plan.results / f"{shard.label}.root.json").read_bytes()
    for name in NOT_EVIDENCE:  # the root itself is untouched: the evidence is copied, never moved
        assert (plan.shards[0].root / name).exists()


@pytest.mark.parametrize(
    "name", ["facts.json", "fault-journal/injections.jsonl", f"fault-relay/answers/{'0' * 32}.json"]
)
def test_a_token_inside_an_evidence_file_refuses_the_collect_naming_the_file_and_never_the_token(
    shards, tmp_path, capsys, name
):
    """O63: an allow-listed file that holds the content of a secret is refused whole, never archived and never dropped
    silently: the message names the shard and the file, not the value, and nothing is published."""
    plan = make_plan(shards, tmp_path)
    leaking = plan.shards[1].root / name
    leaking.parent.mkdir(parents=True, exist_ok=True)
    leaking.write_text(f'{{"admin": "{TOKEN}"}}\n')
    before = sorted(p.name for p in plan.results.iterdir())
    with pytest.raises(shards.ShardsError, match=rf"fed-b.*{name}") as refused:
        shards.collect(plan, tmp_path / "merge")
    assert TOKEN not in str(refused.value)
    assert sorted(p.name for p in plan.results.iterdir()) == before  # no root archive, no manifest
    assert not (tmp_path / "merge").exists()
    assert not (plan.shards[0].root / "evidence-collected.json").exists()


@pytest.mark.parametrize(
    "name",
    [
        "fault-relay/answers/" + "1" * 32 + ".json",
        "fault-journal/injections.jsonl",
        "fault-journal/federation.lock",
        "facts.json",
    ],
)
def test_a_link_among_the_evidence_refuses_the_collect_and_is_never_followed(shards, tmp_path, name):
    """O63: a link where an evidence file is (here into `secrets/`) is refused, naming it; its target is never read into
    an archive."""
    plan = make_plan(shards, tmp_path)
    link = plan.shards[0].root / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(plan.shards[0].root / "secrets" / "admin.token")
    with pytest.raises(shards.ShardsError, match=rf"fed-a.*{name}"):
        shards.collect(plan, tmp_path / "merge")
    assert not list(plan.results.glob("*.root.*"))
    assert not (plan.results / MANIFEST).exists()


def test_the_fault_journal_is_archived_whole_and_a_lock_in_it_is_read_never_taken(shards, tmp_path):
    """O63: every file of `fault-journal/` at any depth, the lock files the pilot's heal flocks included, as they are;
    one held under an exclusive lock by another process of the operator's (here this one, on a lock file) does not stop
    the collect, which only reads."""
    plan = make_plan(shards, tmp_path)
    lock = plan.shards[0].root / "fault-journal" / "federation.lock"
    with open(lock, "wb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        shards.collect(plan, tmp_path / "merge")
        fcntl.flock(held, fcntl.LOCK_UN)
    members = _members(plan.results / "fed-a.root.tar")
    assert {name for name in members if name.startswith("fault-journal/")} == {
        name for name in EVIDENCE if name.startswith("fault-journal/")
    }
    assert members["fault-journal/old/one.jsonl"] == EVIDENCE["fault-journal/old/one.jsonl"]
    assert members["fault-journal/federation.lock"] == b""


@pytest.mark.parametrize("what", ["a link in a nested directory", "a name that is not plain", "a tree nested too deep"])
def test_a_fault_journal_with_a_link_an_odd_name_or_a_deep_tree_refuses_the_collect(shards, tmp_path, what):
    """O63: the journal is archived whole or not at all: nothing in it is skipped silently or followed."""
    plan = make_plan(shards, tmp_path)
    journal = plan.shards[0].root / "fault-journal"
    if what == "a link in a nested directory":
        (journal / "old" / "link.jsonl").symlink_to(plan.shards[0].root / "secrets" / "admin.token")
    elif what == "a name that is not plain":
        (journal / "a b.jsonl").write_text("{}\n")
    else:
        deep = journal.joinpath(*"abcdef")
        deep.mkdir(parents=True)
        (deep / "x.jsonl").write_text("{}\n")
    with pytest.raises(shards.ShardsError, match=r"fed-a.*fault-journal"):
        shards.collect(plan, tmp_path / "merge")
    assert not list(plan.results.glob("*.root.*"))


def test_the_pre_step_guards_claim_files_are_evidence_when_present(shards, tmp_path):
    """O63: `shard-claim.json` and `shard-claim.lock` (who held the root: a resume dispute needs it) are archived
    beside the state, and a root without them is as it was."""
    plan = make_plan(shards, tmp_path)
    for name in ("shard-claim.json", "shard-claim.lock"):
        (plan.shards[1].root / name).unlink()
    shards.collect(plan, tmp_path / "merge")
    assert {"shard-claim.json", "shard-claim.lock"} <= set(_members(plan.results / "fed-a.root.tar"))
    assert not {"shard-claim.json", "shard-claim.lock"} & set(_members(plan.results / "fed-b.root.tar"))


def test_a_link_in_place_of_a_directory_of_the_allow_list_is_refused(shards, tmp_path):
    """O63: `fault-relay` itself a link (to the secrets) is refused, not followed."""
    plan = make_plan(shards, tmp_path)
    root = plan.shards[0].root
    relay = root / "fault-relay"
    for child in sorted(relay.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    relay.rmdir()
    relay.symlink_to(root / "secrets")
    with pytest.raises(shards.ShardsError, match=r"fed-a.*fault-relay"):
        shards.collect(plan, tmp_path / "merge")
    assert not list(plan.results.glob("*.root.*"))


def test_a_file_the_allow_list_does_not_name_is_left_out_even_when_it_sits_in_an_evidence_directory(shards, tmp_path):
    """O63: an allow-listed directory is no licence: `fault-relay/notes.txt`, a dot file, a nested directory and an
    `answers/` file that is not a request id are not evidence."""
    plan = make_plan(shards, tmp_path)
    relay = plan.shards[0].root / "fault-relay"
    (relay / "notes.txt").write_text(TOKEN)
    (relay / ".draft").write_text(TOKEN)
    (relay / "answers" / "nested").mkdir()
    (relay / "answers" / "nested" / f"{'2' * 32}.json").write_text(TOKEN)
    (relay / "answers" / "not-an-id.json").write_text(TOKEN)
    shards.collect(plan, tmp_path / "merge")
    assert _members(plan.results / "fed-a.root.tar") == EVIDENCE


def test_a_root_with_only_what_up_always_writes_is_archived_without_the_optional_files(shards, tmp_path):
    """O63: a federation that ran no faults and has no render identity (an `up` before O74, a layout with no platform
    template) holds the state and the facts alone: those are the archive."""
    plan = make_plan(shards, tmp_path)
    keep = {name: EVIDENCE[name] for name in ("hpc-federation.json", "facts.json")}
    for shard in plan.shards:
        shutil.rmtree(shard.root)
        make_root(shard.root, evidence=keep, extras={})
    shards.collect(plan, tmp_path / "merge")
    assert _members(plan.results / "fed-a.root.tar") == keep


@pytest.mark.parametrize("missing", ["hpc-federation.json", "facts.json"])
def test_a_root_without_its_state_or_its_facts_refuses_the_collect_naming_the_shard_and_file(shards, tmp_path, missing):
    """O63: the state and the facts are what `up` always writes: a root without one is not the evidence the post-check
    needs, and a collect that left it out would be a pass on missing evidence."""
    plan = make_plan(shards, tmp_path)
    (plan.shards[1].root / missing).unlink()
    with pytest.raises(shards.ShardsError, match=rf"fed-b.*{missing}"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()
    assert not list(plan.results.glob("*.root.*"))


def test_a_root_that_is_gone_refuses_the_collect_unless_an_earlier_root_archive_verifies(shards, tmp_path, capsys):
    """O63: after `down --discard-evidence` (or a down that followed a collect) the root is gone: a collect of the
    archives is refused naming the shard, unless the root archive an earlier collect wrote is still there and verifies
    against its manifest, in which case it is listed again as it is; one that does not verify is refused."""
    plan = _collect(shards, tmp_path, "first")
    first = (plan.results / "fed-a.root.tar").read_bytes()
    shutil.rmtree(plan.shards[0].root)
    capsys.readouterr()
    shards.collect(plan, tmp_path / "second")  # the earlier archive of fed-a verifies: kept, listed
    assert "WARNING: shard fed-a: its root is gone" in capsys.readouterr().err
    assert (plan.results / "fed-a.root.tar").read_bytes() == first
    listed = {e["name"] for e in json.loads((plan.results / MANIFEST).read_text())["evidence"]}
    assert {"fed-a.root.tar", "fed-a.root.json"} <= listed
    (plan.results / "fed-a.root.tar").write_bytes(first + b"x")
    with pytest.raises(shards.ShardsError, match=r"fed-a.*root"):
        shards.collect(plan, tmp_path / "third")
    (plan.results / "fed-a.root.tar").unlink()
    with pytest.raises(shards.ShardsError, match=r"fed-a.*root"):
        shards.collect(plan, tmp_path / "fourth")


# ------------------------------------------------------------------------------------------------ the manifests
def test_the_root_archive_has_a_manifest_of_its_own_with_no_path_and_no_timestamp(shards, tmp_path):
    """O63: `<label>.root.json`: format, label, the archive's SHA-256 and size, and each file's name, size and SHA-256;
    two collects of an unchanged root write the same bytes."""
    plan = _collect(shards, tmp_path, "first")
    own = json.loads((plan.results / "fed-a.root.json").read_text())
    assert own["format"] == "expcore-hpc-root-evidence/1"
    assert own["label"] == "fed-a"
    assert own["sha256"] == sha_of(plan.results / "fed-a.root.tar")
    assert own["bytes"] == (plan.results / "fed-a.root.tar").stat().st_size
    assert own["files"] == [
        {"name": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in sorted(EVIDENCE.items())
    ]
    assert str(tmp_path) not in (plan.results / "fed-a.root.json").read_text()
    first = [(plan.results / n).read_bytes() for n in ("fed-a.root.tar", "fed-a.root.json", MANIFEST)]
    shards.collect(plan, tmp_path / "second")
    assert [(plan.results / n).read_bytes() for n in ("fed-a.root.tar", "fed-a.root.json", MANIFEST)] == first


def test_the_archives_manifest_lists_each_root_archive_and_its_manifest_with_their_hashes(shards, tmp_path):
    """O63: in `evidence`, beside the journals, as name, size and SHA-256 of the bytes written."""
    plan = _collect(shards, tmp_path)
    evidence = {e["name"]: e for e in json.loads((plan.results / MANIFEST).read_text())["evidence"]}
    for label in ("fed-a", "fed-b"):
        for suffix in ("root.tar", "root.json"):
            path = plan.results / f"{label}.{suffix}"
            assert evidence[path.name] == {"name": path.name, "bytes": path.stat().st_size, "sha256": sha_of(path)}


@pytest.mark.parametrize("name", ["fed-a.root.tar", "fed-b.root.json"])
def test_verify_copy_fails_on_a_root_archive_that_is_missing(shards, tmp_path, name, capsys):
    """O63: the pull lacks the root archive (or its manifest): verify-copy exits 1 naming it."""
    plan = _collect(shards, tmp_path)
    manifest, local, given = pull(plan, tmp_path)
    (local / name).unlink()
    assert verify(shards, manifest, local, given) == 1
    assert f"{name}: missing" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["fed-a.root.tar", "fed-b.root.json"])
@pytest.mark.parametrize("change", ["same size", "other size"])
def test_verify_copy_fails_on_a_root_archive_that_changed(shards, tmp_path, name, change, capsys):
    """O63: a pulled root archive of other bytes (the same length or another) fails the check, naming it."""
    plan = _collect(shards, tmp_path)
    manifest, local, given = pull(plan, tmp_path)
    changed = bytearray((local / name).read_bytes())
    if change == "same size":
        changed[-1] ^= 1
    else:
        changed += b"x"
    (local / name).write_bytes(bytes(changed))
    assert verify(shards, manifest, local, given) == 1
    assert f"{name}: " in capsys.readouterr().err


def test_verify_copy_passes_on_an_intact_pull_that_holds_the_root_archives(shards, tmp_path, capsys):
    """O63: the root archives are checked like the journals, and a pull that holds every file passes."""
    plan = _collect(shards, tmp_path)
    manifest, local, given = pull(plan, tmp_path)
    assert verify(shards, manifest, local, given) == 0
    assert "ok" in capsys.readouterr().out


def test_verify_copy_warns_of_a_root_archive_the_manifest_does_not_name(shards, tmp_path, capsys):
    """O63: like an unlisted `.out.tar`, a `.root.tar` the manifest does not name is a warning, never a failure."""
    plan = _collect(shards, tmp_path)
    manifest, local, given = pull(plan, tmp_path)
    (local / "fed-c.root.tar").write_bytes(b"another campaign's")
    assert verify(shards, manifest, local, given) == 0
    assert "fed-c.root.tar is not named by the manifest" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ a shard that moved
def _moved(plan, tmp_path, label="fed-a"):
    """The plan after `label` moved to a fresh federation: the same shard, another root (a fleet's `plan` gives it)."""
    fresh = make_root(tmp_path / "fed" / f"{label}-moved")
    shard = next(s for s in plan.shards if s.label == label)
    others = tuple(s for s in plan.shards if s.label != label)
    return dataclasses.replace(
        plan, shards=tuple(sorted((*others, dataclasses.replace(shard, root=fresh)), key=lambda s: s.index))
    )


def _aside(plan):
    return sorted(p.name for p in plan.results.glob("fed-a.*.root.*"))


def test_a_shard_that_moved_to_another_federation_does_not_overwrite_its_first_federations_root_archive(
    shards, tmp_path
):
    """O63: the archive is named by the shard's label, and a fleet's shard moves between federations: the collect
    that finds `<label>.root.*` made of another root puts it aside (`<label>.<12 hex digits>.root.*`, the same bytes),
    never overwrites it, and lists both pairs in `archives.sha256.json` for `verify-copy`."""
    plan = _collect(shards, tmp_path, "first")
    first = {name: (plan.results / name).read_bytes() for name in ("fed-a.root.tar", "fed-a.root.json")}
    moved = _moved(plan, tmp_path)
    shards.collect(moved, tmp_path / "second")
    aside = _aside(plan)
    assert len(aside) == 2
    assert all(re.fullmatch(r"fed-a\.[0-9a-f]{12}\.root\.(tar|json)", name) for name in aside)
    assert [(plan.results / name).read_bytes() for name in aside] == [first["fed-a.root.json"], first["fed-a.root.tar"]]
    own = json.loads((plan.results / "fed-a.root.json").read_text())
    assert own["root_id"] != json.loads(first["fed-a.root.json"])["root_id"]
    listed = {e["name"] for e in json.loads((plan.results / MANIFEST).read_text())["evidence"]}
    assert {"fed-a.root.tar", "fed-a.root.json", *aside} <= listed
    assert str(tmp_path) not in first["fed-a.root.json"].decode() + (plan.results / "fed-a.root.json").read_text()


def test_a_federation_brought_up_again_at_the_same_root_does_not_overwrite_the_first_ones_archive(shards, tmp_path):
    """O63: the same shard, the same root path, a new `up` (its allocation's name is new): the evidence of the first is
    put aside like a moved shard's, never overwritten by the second's."""
    plan = _collect(shards, tmp_path, "first")
    first = {name: (plan.results / name).read_bytes() for name in ("fed-a.root.tar", "fed-a.root.json")}
    state = plan.shards[0].root / "hpc-federation.json"
    document = json.loads(state.read_text())
    document["allocation"] = {"name": "y"}
    state.write_text(json.dumps(document))
    shards.collect(plan, tmp_path / "second")
    aside = _aside(plan)
    assert len(aside) == 2
    assert [(plan.results / name).read_bytes() for name in aside] == [first["fed-a.root.json"], first["fed-a.root.tar"]]
    assert (plan.results / "fed-a.root.tar").read_bytes() != first["fed-a.root.tar"]
    assert '"y"' not in (plan.results / "fed-a.root.json").read_text()  # a hash of the name, not the name


def test_the_set_aside_archive_is_checked_by_verify_copy_and_a_second_collect_finds_it_again(shards, tmp_path, capsys):
    """O63: pulled with the directory, the set-aside pair is checked like the others; a collect that follows lists it as
    before and sets nothing more aside."""
    plan = _collect(shards, tmp_path, "first")
    moved = _moved(plan, tmp_path)
    shards.collect(moved, tmp_path / "second")
    before = _aside(plan)
    shards.collect(moved, tmp_path / "third")
    assert _aside(plan) == before
    manifest, local, given = pull(moved, tmp_path)
    assert verify(shards, manifest, local, given) == 0
    capsys.readouterr()
    (local / before[0]).unlink()
    assert verify(shards, manifest, local, given) == 1
    assert f"{before[0]}: missing" in capsys.readouterr().err


def test_a_set_aside_pair_that_does_not_verify_refuses_the_collect(shards, tmp_path):
    """O63: an archive put aside that is no longer the one its manifest names is refused, not listed as it is."""
    plan = _collect(shards, tmp_path, "first")
    moved = _moved(plan, tmp_path)
    shards.collect(moved, tmp_path / "second")
    tar = next(p for p in plan.results.glob("fed-a.*.root.tar"))
    tar.write_bytes(tar.read_bytes() + b"x")
    with pytest.raises(shards.ShardsError, match=r"fed-a.*another|root archive put aside"):
        shards.collect(moved, tmp_path / "third")


def test_a_root_archive_of_another_federation_is_not_taken_for_a_gone_roots_own(shards, tmp_path):
    """O63: the shard moved and its new root is gone too: the archive under its label is the first federation's, so the
    collect refuses instead of listing it as this root's."""
    plan = _collect(shards, tmp_path, "first")
    moved = _moved(plan, tmp_path)
    shutil.rmtree(moved.shards[0].root)
    with pytest.raises(shards.ShardsError, match=r"fed-a.*another federation"):
        shards.collect(moved, tmp_path / "second")


def test_the_root_archive_and_its_manifest_are_private_files(shards, tmp_path):
    """O63: they name the site's nodes and paths: mode 0600 whatever the umask."""
    plan = _collect(shards, tmp_path)
    for name in ("fed-a.root.tar", "fed-a.root.json", "fed-b.root.tar", "fed-b.root.json"):
        assert mode_of(plan.results / name) == 0o600


def test_a_shard_marked_running_is_warned_of_and_still_collected(shards, tmp_path, capsys):
    """O63: a step that runs across the collect is not seen by the marker, which notices only later starts: the
    collect says so, naming the shard."""
    plan = make_plan(shards, tmp_path)
    (plan.results / "fed-b").mkdir(exist_ok=True)
    (plan.results / "fed-b" / "running.json").write_text("{}")
    shards.collect(plan, tmp_path / "merge")
    err = capsys.readouterr().err
    assert "WARNING: shard fed-b: a step of it is marked running" in err
    assert "shard fed-a: a step of it is marked running" not in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_secrets_directory_that_cannot_be_read_refuses_the_collect_rather_than_skipping_the_check(shards, tmp_path):
    """O63: with `secrets/` unreadable there is nothing to check the archive against: refused, naming the directory."""
    plan = make_plan(shards, tmp_path)
    secrets = plan.shards[0].root / "secrets"
    secrets.chmod(0)
    try:
        with pytest.raises(shards.ShardsError, match=r"fed-a.*secrets/ cannot be read"):
            shards.collect(plan, tmp_path / "merge")
    finally:
        secrets.chmod(0o700)
    assert not list(plan.results.glob("*.root.*"))


# ------------------------------------------------------------------------------------------------ collect --roots-only
def _roots_only(shards, tmp_path, *flags):
    return shards.main(["collect", "--manifest", str(tmp_path / "shards.toml"), "--roots-only", *flags])


def test_roots_only_archives_and_marks_one_shards_root_without_any_shard_archive(shards, tmp_path, capsys):
    """O63: for a shard about to move while the others are not final: `collect --roots-only --shard L` needs no archive,
    no `--into`, and writes no `archives.sha256.json`; it publishes `<L>.root.tar`/`.json` (the hash on stderr) and the
    marker, and `down` then goes."""
    plan = shards.load_plan(plan_file(tmp_path))  # no shard has an archive
    plan.results.mkdir()
    assert _roots_only(shards, tmp_path, "--shard", "fed-a") == 0
    err = capsys.readouterr().err
    assert (
        f"shard fed-a: root evidence archived (fed-a.root.tar sha256 {sha_of(plan.results / 'fed-a.root.tar')})" in err
    )
    assert not (plan.results / "fed-b.root.tar").exists()
    assert not (plan.results / MANIFEST).exists()
    assert json.loads((plan.shards[0].root / "evidence-collected.json").read_text())["root_archive_sha256"] == sha_of(
        plan.results / "fed-a.root.tar"
    )
    assert not (plan.shards[1].root / "evidence-collected.json").exists()


def test_roots_only_without_a_shard_takes_every_root(shards, tmp_path):
    """O63: no `--shard`: every shard's root."""
    plan = shards.load_plan(plan_file(tmp_path))
    plan.results.mkdir()
    assert _roots_only(shards, tmp_path) == 0
    assert sorted(p.name for p in plan.results.glob("*.root.tar")) == ["fed-a.root.tar", "fed-b.root.tar"]


@pytest.mark.parametrize(
    "flags", [["--shard", "nope"], ["--into", "x"], ["--run"]], ids=["an unknown shard", "an --into", "a --run"]
)
def test_roots_only_refuses_what_does_not_go_with_it(shards, tmp_path, capsys, flags):
    """O63: exit 2 and nothing written."""
    plan = shards.load_plan(plan_file(tmp_path))
    assert _roots_only(shards, tmp_path, *flags) == 2
    assert not plan.results.exists() or not list(plan.results.glob("*.root.*"))
    assert "ERROR" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [[], ["--shard", "fed-a"]], ids=["no --into", "--shard without --roots-only"])
def test_a_full_collect_needs_its_into_and_takes_no_shard(shards, tmp_path, flags):
    """O63: `--shard` belongs to `--roots-only`; a full collect without `--into` is refused."""
    make_plan(shards, tmp_path)
    argv = ["collect", "--manifest", str(tmp_path / "shards.toml"), *flags]
    assert shards.main(argv) == 2


# ------------------------------------------------------------------------------------------------ the marker
def test_a_collect_writes_the_marker_into_each_root_naming_the_root_archive_s_hash(shards, tmp_path):
    """O63: `<root>/evidence-collected.json` (mode 0600, no path): the root archive's SHA-256 and the digest of the
    step records it was collected against, which `down` reads."""
    plan = _collect(shards, tmp_path)
    for shard in plan.shards:
        marker = shard.root / "evidence-collected.json"
        assert mode_of(marker) == 0o600
        found = json.loads(marker.read_text())
        assert found["format"] == "expcore-hpc-evidence-collected/1"
        assert found["label"] == shard.label
        assert found["root_archive_sha256"] == sha_of(plan.results / f"{shard.label}.root.tar")
        assert len(found["steps_sha256"]) == 64
        assert str(tmp_path) not in marker.read_text()


def test_the_marker_names_each_archived_file_by_the_hash_the_archive_manifest_lists(shards, tmp_path):
    """O63: `down` cannot see the results directory, so the marker carries the hash of each file as it was archived
    (the same bytes the archive's own manifest lists): that is what tells a file a step wrote from one the federation
    rewrote."""
    plan = _collect(shards, tmp_path)
    for shard in plan.shards:
        found = json.loads((shard.root / "evidence-collected.json").read_text())
        listed = json.loads((plan.results / f"{shard.label}.root.json").read_text())["files"]
        assert found["files"] == {entry["name"]: entry["sha256"] for entry in listed}


def test_the_marker_is_not_itself_evidence(shards, tmp_path):
    """O63: a second collect does not archive the first's marker: the archive of an unchanged root is the same."""
    plan = _collect(shards, tmp_path, "first")
    first = (plan.results / "fed-a.root.tar").read_bytes()
    shards.collect(plan, tmp_path / "second")
    assert (plan.results / "fed-a.root.tar").read_bytes() == first
    assert "evidence-collected.json" not in _members(plan.results / "fed-a.root.tar")


def test_a_refused_collect_writes_no_marker_and_no_root_archive(shards, tmp_path):
    """O63: the marker says the evidence was collected: a collect that refused (a shard without its lock) leaves none
    in any root, and no root archive."""
    plan = make_plan(shards, tmp_path)
    (plan.results / "fed-b.out.tar").write_bytes(b"not the archive its manifest names")
    with pytest.raises(shards.ShardsError, match="fed-b"):
        shards.collect(plan, tmp_path / "merge")
    assert not [s.root for s in plan.shards if (s.root / "evidence-collected.json").exists()]
    assert not list(plan.results.glob("*.root.*"))


# ------------------------------------------------------------------------------------------------ the step's record
def _step_args(tmp_path, label="fed-a", resume=False):
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=1, count=2, label=label, tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume)  # fmt: skip


def _dispatcher(seen):
    def run(argv, env, cwd):
        record = Path(env["EXPCORE_FED_ROOT"]) / "shard-steps" / "fed-a.json"
        seen.append(record.read_bytes() if record.is_file() else None)  # what the root holds when the dispatcher starts
        out = Path(argv[argv.index("run") + 1]) / "out"
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "campaign.lock").write_text("")
        return 0

    return run


def test_a_step_records_in_its_federation_s_root_that_it_ran_before_its_dispatcher_starts(
    shards, tmp_path, monkeypatch
):
    """O63: `down` cannot see the results directory, so the step leaves a record in the root (no path, no host, no
    secret): the shard's spec and how many times a step of it started. It is there when the dispatcher starts, and a
    resume counts again."""
    root = make_root(tmp_path / "fed" / "fed-a")
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(root))
    seen: list = []
    assert shards.step(_step_args(tmp_path), runner=_dispatcher(seen), core_found=lambda path: True) == 0
    record = json.loads(seen[0])
    assert record == {"format": "expcore-hpc-shard-step-record/1", "shard": {"index": 1, "count": 2, "label": "fed-a"},
                      "starts": 1}  # fmt: skip
    assert str(tmp_path) not in seen[0].decode()
    assert mode_of(root / "shard-steps") == 0o700
    assert shards.step(_step_args(tmp_path, resume=True), runner=_dispatcher(seen), core_found=lambda path: True) == 0
    assert json.loads(seen[1])["starts"] == 2


def test_a_step_refused_before_it_starts_leaves_no_record(shards, tmp_path, monkeypatch):
    """O63: a step whose interpreter imports another core runs nothing and says nothing in the root."""
    root = make_root(tmp_path / "fed" / "fed-a")
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(root))
    seen: list = []
    assert shards.step(_step_args(tmp_path), runner=_dispatcher(seen), core_found=lambda path: False) != 0
    assert not (root / "shard-steps" / "fed-a.json").exists()


def test_a_step_that_cannot_write_its_record_does_not_start(shards, tmp_path, monkeypatch):
    """O63: no record means `down` would not know a step ran: the step is refused and its dispatcher never runs."""
    root = make_root(tmp_path / "fed" / "fed-a")
    shutil.rmtree(root / "shard-steps")
    (root / "shard-steps").write_text("a file where the directory should be")
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(root))
    seen: list = []
    with pytest.raises(shards.ShardsError, match="record"):
        shards.step(_step_args(tmp_path), runner=_dispatcher(seen), core_found=lambda path: True)
    assert not seen
    assert not list((tmp_path / "tmpfs").iterdir())  # and nothing is left on the node's tmpfs


def test_a_step_with_no_federation_root_writes_no_record(shards, tmp_path, monkeypatch):
    """O63: a step run without `EXPCORE_FED_ROOT` (a stand-in dispatcher test, a study with no federation) is as it
    was."""
    monkeypatch.delenv("EXPCORE_FED_ROOT", raising=False)
    assert shards.step(_step_args(tmp_path), runner=lambda argv, env, cwd: 0, core_found=lambda path: True) == 0
    assert not list(tmp_path.rglob("shard-steps"))


# ------------------------------------------------------------------------------------------------ the names
def test_the_names_shards_and_hpcevidence_share_are_the_same(shards):
    """O63: `shards.py` runs alone on a node and keeps its own copy of the step record's names; `hpcevidence` (which
    `down` and `collect` use) is their source. A test compares them, as for the render identity."""
    spec = importlib.util.spec_from_file_location("hpc_evidence_names", DEPLOY / "hpcevidence.py")
    evidence = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = evidence
    try:
        spec.loader.exec_module(evidence)
        assert (shards.STEP_RECORDS, shards.STEP_RECORD_FORMAT) == (evidence.STEPS_DIR, evidence.STEP_FORMAT)
        assert shards.PLATFORM in evidence.EVIDENCE_FILES
    finally:
        sys.modules.pop(spec.name, None)

"""O63: `unpack` of deploy/hpc-federation/shards.py (what `collect` and `--resume` extract a shard's archive with)
takes only an `out/` that stays inside `out/`: a member name with a `..` component, an absolute name, a name outside
`out/`, a link whose target leaves `out/`, and a member written through a link are each refused for the whole archive,
before anything is extracted. The archives here are built by hand, since the step's own `archive` writes from `out/`
alone and cannot carry such members. Every name is invented and nothing runs srun."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
MARKER = "marker-of-a-hostile-name"  # in every hostile name or target below; never to be echoed by a refusal

DIR, FILE, SYMLINK, HARDLINK = "dir", "file", "symlink", "hardlink"
OUT = ("out", DIR, None)


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_unpack", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_unpack"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_unpack", None)


def _write(path: Path, members) -> list[str]:
    """Write a tar of `(name, kind, target)` members to `path`; the names it holds, read back from the file, so a test
    can show that the hostile name is really in the archive."""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, target in members:
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if kind == DIR:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind in (SYMLINK, HARDLINK):
                info.type = tarfile.SYMTYPE if kind == SYMLINK else tarfile.LNKTYPE
                info.linkname, info.mode = target, 0o644
                tar.addfile(info)
            else:
                data = b"content"
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
    with tarfile.open(path) as tar:
        return tar.getnames()


def _tree(root: Path) -> list[str]:
    """Everything under `root`, links not followed."""
    found = []
    for base, dirs, files in os.walk(root):
        found += [Path(base, name).relative_to(root).as_posix() for name in (*dirs, *files)]
    return sorted(found)


HOSTILE = {
    "dotdot": [OUT, ("out/../x", FILE, None)],
    "dotdot-back-in": [OUT, ("out/../out/x", FILE, None)],  # `normpath` turns it into out/x: refused on the raw name
    "dotdot-below": [OUT, ("out/sub/../../x", FILE, None)],
    "absolute": [OUT, ("/out/x", FILE, None)],
    "outside-out": [OUT, ("x", FILE, None)],
    "beside-out": [OUT, ("out-other/x", FILE, None)],
    "symlink-up": [OUT, ("out/link", SYMLINK, "../x")],
    "symlink-up-deep": [OUT, ("out/a", DIR, None), ("out/a/link", SYMLINK, "../../x")],
    "symlink-absolute": [OUT, ("out/link", SYMLINK, "/out/x")],
    "symlink-interior-dotdot": [
        OUT,
        ("out/link", SYMLINK, "sub/../x"),
    ],  # in bounds lexically, not once `sub` is a link
    "hardlink-outside": [OUT, ("out/file", FILE, None), ("out/link", HARDLINK, "x")],
    "hardlink-dotdot": [OUT, ("out/file", FILE, None), ("out/link", HARDLINK, "out/../x")],
    "hardlink-absolute": [OUT, ("out/file", FILE, None), ("out/link", HARDLINK, "/out/file")],
    # `out/d` is a link to out/, `out/d/s` is then written into out/ as a link to its parent, the shard's directory,
    # and `out/s/leak` is written through it beside out/: every name and target is inside out/ taken by itself
    "through-a-link": [OUT, ("out/d", SYMLINK, "."), ("out/d/s", SYMLINK, ".."), ("out/s/leak", FILE, None)],
    "through-a-directory-link": [OUT, ("out/d", SYMLINK, "."), ("out/d/file", FILE, None)],
    # `tarfile` keeps the trailing `/` of a link's name and drops it when it makes the link: the link is at out/d
    "symlink-named-with-a-slash": [OUT, ("out/d/", SYMLINK, "..")],
    "symlink-named-out": [("out/", SYMLINK, "x")],
    "file-named-out": [("out", FILE, None)],
    "empty": [],
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_an_archive_with_a_member_that_leaves_out_is_refused_before_anything_is_extracted(shards, tmp_path, case):
    """O63: the whole archive is refused and nothing is made, not the directory it was to be unpacked into."""
    archive = tmp_path / "shard.tar"
    names = _write(archive, HOSTILE[case])
    assert names == [name for name, _, _ in HOSTILE[case]]  # the hostile names are really in it, slashes and all
    before = _tree(tmp_path)
    with pytest.raises(shards.ShardsError):
        shards.unpack(archive, tmp_path / "into")
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("name", ["out/../x", "/out/x", "x", "out/../out/x", "out/sub/../../x"])
def test_the_hostile_names_are_in_the_archives_the_tests_build(tmp_path, name):
    """O63: the archives the refusals are asserted on carry the names they claim to."""
    assert name in _write(tmp_path / "shard.tar", [OUT, (name, FILE, None)])


@pytest.mark.parametrize(
    "members",
    [
        [OUT, (f"out/../{MARKER}", FILE, None)],
        [OUT, (f"/{MARKER}/x", FILE, None)],
        [OUT, (f"{MARKER}/x", FILE, None)],
        [OUT, ("out/link", SYMLINK, f"../{MARKER}")],
        [OUT, ("out/file", FILE, None), ("out/link", HARDLINK, f"{MARKER}")],
        [OUT, ("out/d", SYMLINK, "."), (f"out/d/{MARKER}", FILE, None)],
    ],
    ids=["dotdot", "absolute", "outside-out", "symlink", "hardlink", "through-a-link"],
)
def test_the_refusal_names_the_shard_and_the_kind_of_problem_never_the_member(shards, tmp_path, members):
    """O63: the message says which shard and which kind of member, and carries nothing a hostile archive wrote."""
    archive = tmp_path / "shard.tar"
    _write(archive, members)
    with pytest.raises(shards.ShardsError) as refused:
        shards.unpack(archive, tmp_path / "into", label="fed-a")
    message = str(refused.value)
    assert message.startswith("shard fed-a: its archive ")
    assert MARKER not in message
    assert len(message) < 100


def test_a_normal_archive_unpacks_as_before_links_inside_out_kept(shards, tmp_path):
    """O63: files, directories, a link inside `out/` (the merge reads one as evidence and refuses it, R04), a link up to
    a sibling inside `out/` and a hard link are all there, in `into/out`."""
    archive = tmp_path / "shard.tar"
    _write(
        archive,
        [
            OUT,
            ("out/campaign.lock", FILE, None),
            ("out/runs", DIR, None),
            ("out/runs/r.json", FILE, None),
            ("out/runs/newest", SYMLINK, "r.json"),
            ("out/runs/sibling", SYMLINK, "../campaign.lock"),
            ("out/runs/same", HARDLINK, "out/runs/r.json"),
        ],
    )
    out = shards.unpack(archive, tmp_path / "into")
    assert out == tmp_path / "into" / "out"
    assert (out / "campaign.lock").read_bytes() == b"content"
    assert os.readlink(out / "runs" / "newest") == "r.json"
    assert os.readlink(out / "runs" / "sibling") == "../campaign.lock"
    assert os.path.samefile(out / "runs" / "same", out / "runs" / "r.json")
    assert _tree(tmp_path / "into") == sorted(
        ["out", "out/campaign.lock", "out/runs", "out/runs/r.json", "out/runs/newest", "out/runs/sibling",
         "out/runs/same"]
    )  # fmt: skip


def test_an_into_that_exists_is_still_refused_whatever_the_archive_holds(shards, tmp_path):
    """O63: `into` must be new, as before."""
    archive = tmp_path / "shard.tar"
    _write(archive, [OUT, ("out/campaign.lock", FILE, None)])
    (tmp_path / "into").mkdir()
    with pytest.raises(FileExistsError):
        shards.unpack(archive, tmp_path / "into")


def test_collect_refuses_a_shard_whose_archive_has_a_member_outside_out_and_extracts_none_of_it(shards, tmp_path):
    """O63: through `collect`: an archive that passes its manifest's hash and still carries `out/../x`."""
    results = tmp_path / "results"
    results.mkdir()
    out = tmp_path / "src-out" / "out"
    out.mkdir(parents=True)
    (out / "campaign.lock").write_text("")
    shards.archive(out, results, {"index": 1, "count": 1, "label": "fed-a"}, 0)
    _write(results / "fed-a.out.tar", [OUT, ("out/campaign.lock", FILE, None), ("out/../x", FILE, None)])
    manifest = json.loads((results / "fed-a.out.json").read_text())
    manifest["sha256"] = hashlib.sha256((results / "fed-a.out.tar").read_bytes()).hexdigest()
    (results / "fed-a.out.json").write_text(json.dumps(manifest))
    (tmp_path / "hpc-federation.json").write_text("{}")  # the root's state and facts, which a collect archives
    (tmp_path / "facts.json").write_text("{}")
    plan = types.SimpleNamespace(
        study=tmp_path / "study",
        results=results,
        shards=[shards.Shard(1, 1, "fed-a", tmp_path)],
        post_run=None,
        pass_env=(),
    )  # what `collect` reads of a plan, as landing A and the pass_env PR made it read more than this test's base did
    with pytest.raises(shards.ShardsError, match=r"shard fed-a: its archive "):
        shards.collect(plan, tmp_path / "merge")
    assert not (tmp_path / "merge" / "fed-a").exists()  # nothing of the shard was unpacked
    assert "x" not in [Path(name).name for name in _tree(tmp_path)]  # and the member is nowhere, beside out/ or not


LOCAL_LOG = b"a withheld start diagnostic, redacted with what the core knows\n"


@pytest.fixture
def loose_umask():
    """The umask of a host whose files are readable by its group and by others, so that a private mode is the
    code's doing and not the process's."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _archive_with_log(path: Path, *, directory: int, regular: int) -> None:
    """An `out/` archive whose `logs/` and the two files in it were recorded with these modes: what an earlier core, or
    a node whose umask was loose, wrote."""
    members = [
        ("out", tarfile.DIRTYPE, 0o755, b""),
        ("out/campaign.lock", tarfile.REGTYPE, 0o644, b""),
        ("out/logs", tarfile.DIRTYPE, directory, b""),
        ("out/logs/.gitignore", tarfile.REGTYPE, regular, b"*\n"),
        ("out/logs/harness-errors.log", tarfile.REGTYPE, regular, LOCAL_LOG),
    ]
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, mode, data in members:
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.mtime, info.size = kind, mode, 0, len(data)
            tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def _private_logs(out: Path) -> None:
    logs = out / "logs"
    assert _mode(logs) == 0o700, oct(_mode(logs))
    for name in ("harness-errors.log", ".gitignore"):
        assert _mode(logs / name) == 0o600, (name, oct(_mode(logs / name)))
    assert (logs / "harness-errors.log").read_bytes() == LOCAL_LOG  # the text is kept, only its mode is the unpack's


@pytest.mark.parametrize(
    ("directory", "regular"),
    [(0o755, 0o644), (0o777, 0o666), (0o750, 0o640), (0o700, 0o600), (0o755, 0o444)],
    ids=["loose", "writable-by-all", "group-readable", "already-private", "read-only"],
)
def test_l08_the_local_log_comes_out_of_a_shards_archive_private_whatever_the_archive_and_the_umask(
    shards, tmp_path, loose_umask, directory, regular
):
    """L08: `out/logs/` (the campaign's local log, which holds the text of a withheld start diagnostic and a harness
    traceback) is unpacked on the operator's host as mode 0700, its files 0600, whatever modes the archive recorded
    and whatever the host's umask is; what `collect` does next (`_read_only`) never gives anyone else a bit."""
    archive = tmp_path / "shard.tar"
    _archive_with_log(archive, directory=directory, regular=regular)
    out = shards.unpack(archive, tmp_path / "into")
    _private_logs(out)
    shards._read_only(out)
    for path in (out / "logs", out / "logs" / "harness-errors.log", out / "logs" / ".gitignore"):
        assert _mode(path) & 0o077 == 0, (path.name, oct(_mode(path)))


def test_l08_the_local_log_the_core_wrote_and_the_step_archived_is_private_when_collect_unpacks_it(
    shards, tmp_path, loose_umask
):
    """L08: end to end under a loose umask: the core's own writer (`defect_log.write_note`), the step's `archive` of the
    copy's `out/`, and the unpack `collect` and `--resume` extract it with."""
    from expcore.campaign.defect_log import LOG_DIR, log_path, write_note
    from expcore.records.redact import Redactor

    out = tmp_path / "node" / "out"
    out.mkdir(parents=True)
    (out / "campaign.lock").write_text("")
    write_note(
        out,
        title="k attempt 1: a diagnostic withheld",
        stamp="T",
        text=LOCAL_LOG.decode(),
        redactor=lambda: Redactor([]),
    )
    assert log_path(out).is_file()
    assert shards.LOCAL_LOGS == LOG_DIR  # the script has no import of the core, so the name is pinned here
    results = tmp_path / "results"
    results.mkdir()
    shards.archive(out, results, {"index": 1, "count": 1, "label": "fed-a"}, 0)
    unpacked = shards.unpack(results / "fed-a.out.tar", tmp_path / "into", label="fed-a")
    assert LOCAL_LOG.decode().strip() in (unpacked / "logs" / "harness-errors.log").read_text(encoding="utf-8")
    assert _mode(unpacked / "logs") == 0o700, oct(_mode(unpacked / "logs"))
    for name in ("harness-errors.log", ".gitignore"):
        assert _mode(unpacked / "logs" / name) == 0o600, (name, oct(_mode(unpacked / "logs" / name)))


SPEC = {"index": 1, "count": 1, "label": "fed-a"}
PUBLISHED = ("fed-a.out.tar", "fed-a.out.json")


def _out_with_local_log(tmp_path: Path) -> Path:
    """An `out/` as a step leaves it: the lock the merge needs and the local log with a withheld diagnostic."""
    out = tmp_path / "node" / "out"
    (out / "logs").mkdir(parents=True)
    (out / "campaign.lock").write_text("")
    (out / "logs" / "harness-errors.log").write_bytes(LOCAL_LOG)
    return out


def _archived_under(shards, out: Path, results: Path, umask: int) -> dict:
    previous = os.umask(umask)
    try:
        return shards.archive(out, results, SPEC, 0)
    finally:
        os.umask(previous)


@pytest.mark.parametrize("umask", [0o022, 0o002, 0o000], ids=["022", "002", "000"])
def test_l08_the_shards_archive_and_its_manifest_are_written_private_whatever_the_umask(
    shards, tmp_path, monkeypatch, umask
):
    """L08: `<label>.out.tar` carries `out/logs` (the withheld start diagnostic) to the operator's host and the operator
    pulls it with `rsync -a`, which keeps modes: the archive and its manifest are mode 0600 whatever the step's umask,
    and so is the draft each is written through, so that no moment of the write leaves the bytes readable. Nothing but
    the two files is left, and the archive is still the one the manifest hashes."""
    out = _out_with_local_log(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    replaced = []
    real = os.replace

    def spy(source, target):
        replaced.append((Path(target).name, _mode(Path(source))))
        real(source, target)

    monkeypatch.setattr(shards.os, "replace", spy)
    manifest = _archived_under(shards, out, results, umask)
    assert sorted(path.name for path in results.iterdir()) == sorted(PUBLISHED)
    for name in PUBLISHED:
        assert _mode(results / name) == 0o600, (name, oct(_mode(results / name)))
    assert sorted(replaced) == sorted((name, 0o600) for name in PUBLISHED), replaced
    on_disk = json.loads((results / "fed-a.out.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert hashlib.sha256((results / "fed-a.out.tar").read_bytes()).hexdigest() == manifest["sha256"]
    unpacked = shards.unpack(results / "fed-a.out.tar", tmp_path / "into", label="fed-a")
    assert (unpacked / "logs" / "harness-errors.log").read_bytes() == LOCAL_LOG


def test_l08_a_world_readable_archive_of_an_earlier_tip_is_replaced_by_a_private_one(shards, tmp_path, loose_umask):
    """L08: a `<label>.out.tar` and manifest an earlier tip left at 0644 are not kept at that mode: the step's archive
    replaces both with files of 0600 (the rename gives the published name the draft's mode, not the old file's)."""
    out = _out_with_local_log(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    for name in PUBLISHED:
        (results / name).write_bytes(b"an earlier archive")
        os.chmod(results / name, 0o644)
    manifest = _archived_under(shards, out, results, 0o022)
    for name in PUBLISHED:
        assert _mode(results / name) == 0o600, (name, oct(_mode(results / name)))
    assert hashlib.sha256((results / "fed-a.out.tar").read_bytes()).hexdigest() == manifest["sha256"]


def test_l08_archive_makes_the_results_directories_it_creates_private_and_leaves_one_it_found(
    shards, tmp_path, loose_umask
):
    """L08: a results directory that is not there yet is made 0700 with every missing parent (`mkdir(mode=, parents=)`
    gives the parents the umask's mode); one the operator made is left as it was found, since it may be shared."""
    out = _out_with_local_log(tmp_path)
    fresh = tmp_path / "a" / "b" / "results"
    shards.archive(out, fresh, SPEC, 0)
    for directory in (tmp_path / "a", tmp_path / "a" / "b", fresh):
        assert _mode(directory) == 0o700, (directory.name, oct(_mode(directory)))
    found = tmp_path / "shared"
    found.mkdir()
    os.chmod(found, 0o755)
    shards.archive(out, found, SPEC, 0)
    assert _mode(found) == 0o755
    assert _mode(found / "fed-a.out.tar") == 0o600


def test_l08_a_step_that_makes_the_results_directory_leaves_it_and_the_shards_directory_private(
    shards, tmp_path, loose_umask
):
    """L08: the first thing a step makes under `--results` is the shard's directory (its running mark), and `results`
    with it when it is not there: both, the journals directory beside the snapshots, and then the archive and its
    manifest are private under a loose umask. (Every other step test makes `results` first.)"""
    study, tmpfs, results = tmp_path / "study", tmp_path / "tmpfs", tmp_path / "results"
    study.mkdir()
    (study / "study.toml").write_text("[study]\n")
    tmpfs.mkdir()
    args = argparse.Namespace(
        study=study, index=1, count=2, label="fed-a", tmpfs=tmpfs, results=results, core_src=tmp_path, resume=False
    )

    def dispatcher(argv, env, cwd):
        out = Path(argv[argv.index("run") + 1]) / "out"
        (out / "logs").mkdir(parents=True, exist_ok=True)
        (out / "logs" / "harness-errors.log").write_bytes(LOCAL_LOG)
        (out / "campaign.lock").write_text("")
        return 0

    assert shards.step(args, runner=dispatcher, core_found=lambda path: True) == 0
    for directory in (results, results / "fed-a", results / "fed-a" / shards.JOURNALS):
        assert _mode(directory) == 0o700, (directory.name, oct(_mode(directory)))
    for name in PUBLISHED:
        assert _mode(results / name) == 0o600, (name, oct(_mode(results / name)))

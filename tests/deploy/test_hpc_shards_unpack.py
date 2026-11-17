"""O63: `unpack` of deploy/hpc-federation/shards.py (what `collect` and `--resume` extract a shard's archive with)
takes only an `out/` that stays inside `out/`: a member name with a `..` component, an absolute name, a name outside
`out/`, a link whose target leaves `out/`, and a member written through a link are each refused for the whole archive,
before anything is extracted. The archives here are built by hand, since the step's own `archive` writes from `out/`
alone and cannot carry such members. Every name is invented and nothing runs srun."""

import hashlib
import importlib.util
import io
import json
import os
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

"""O63: the copy back of a campaign's shard archives from the cluster's scratch to the operator's host, checked without
a cluster: the manifest `shards.py collect` writes beside the archives, and `shards.py verify-copy`, which the operator
runs on their own host (standard library only, the core not installed) against what they pulled. Every path and name
here is invented."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
MANIFEST = "archives.sha256.json"


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_copy_back", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_copy_back"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_copy_back", None)


def _plan_file(tmp_path, labels=("fed-a", "fed-b")):
    lines = [
        f'study = "{tmp_path / "study"}"',
        f'python = "{tmp_path / "venv" / "python"}"',
        f'core_src = "{tmp_path / "core" / "src"}"',
        f'results = "{tmp_path / "results"}"',
        f'tmpfs = "{tmp_path / "tmpfs"}"',
        'step_path = "/usr/bin:/bin"',
        "cpus = 6",
        'memory = "18g"',
    ]
    for label in labels:
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{tmp_path / "fed" / label}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _archived(shards, tmp_path, label, index, count=2, lock=True):
    out = tmp_path / "src-out" / label / "out"
    (out / "runs").mkdir(parents=True)
    (out / "runs" / "r.json").write_text(json.dumps({"label": label, "pad": label * 200}))
    if lock:
        (out / "campaign.lock").write_text("")
    (tmp_path / "results").mkdir(exist_ok=True)
    shards.archive(out, tmp_path / "results", {"index": index, "count": count, "label": label}, 0)


def _plan(shards, tmp_path):
    """A plan of two shards, each with its archive in `<tmp>/results`."""
    plan = shards.load_plan(_plan_file(tmp_path))
    for shard in plan.shards:
        _archived(shards, tmp_path, shard.label, shard.index)
    return plan


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pulled(shards, tmp_path):
    """What an operator holds after `collect` and a pull: `(manifest, local dir, the hash collect printed)`."""
    plan = _plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    local = tmp_path / "local"
    local.mkdir()
    for name in ("fed-a.out.tar", "fed-b.out.tar", MANIFEST):
        shutil.copy2(plan.results / name, local / name)
    return local / MANIFEST, local, _digest(plan.results / MANIFEST)


def _verify(shards, manifest, local, digest):
    return shards.main(["verify-copy", str(manifest), str(local), "--manifest-sha256", digest])


# ------------------------------------------------------------------------------------------------ collect's manifest
def test_collect_writes_a_manifest_of_the_archives_it_read_beside_them(shards, tmp_path):
    """O63: version, plan id, and each archive's relative name, size and SHA-256, computed from the archive itself."""
    plan = _plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "merge")
    manifest = json.loads((plan.results / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["format"] == "expcore-hpc-archives/1"
    assert len(manifest["plan_id"]) == 64
    assert set(manifest["plan_id"]) <= set("0123456789abcdef")
    assert manifest["archives"] == [
        {"name": name, "bytes": (plan.results / name).stat().st_size, "sha256": _digest(plan.results / name)}
        for name in ("fed-a.out.tar", "fed-b.out.tar")
    ]
    assert set(manifest) == {"format", "plan_id", "archives"}


def test_the_manifest_takes_the_size_of_the_bytes_read_not_the_one_a_step_claimed(shards, tmp_path):
    """O63: a shard's own manifest that gives another size changes nothing: the size is the archive's."""
    plan = _plan(shards, tmp_path)
    own = plan.results / "fed-a.out.json"
    claimed = json.loads(own.read_text(encoding="utf-8"))
    claimed["bytes"] = 7
    own.write_text(json.dumps(claimed))
    shards.collect(plan, tmp_path / "merge")
    entry = json.loads((plan.results / MANIFEST).read_text(encoding="utf-8"))["archives"][0]
    assert entry["bytes"] == (plan.results / "fed-a.out.tar").stat().st_size != 7


def test_the_manifest_is_the_same_for_every_collect_and_names_no_path(shards, tmp_path):
    """O63: no timestamp and no directory in it, so a second collect writes the same bytes and prints the same hash."""
    plan = _plan(shards, tmp_path)
    shards.collect(plan, tmp_path / "first")
    first = (plan.results / MANIFEST).read_bytes()
    shards.collect(plan, tmp_path / "second")
    assert (plan.results / MANIFEST).read_bytes() == first
    assert str(tmp_path) not in first.decode()


def test_a_collect_that_refuses_a_shard_writes_no_manifest(shards, tmp_path):
    """O63: the manifest says what was verified whole, so a refused collect leaves none."""
    plan = shards.load_plan(_plan_file(tmp_path))
    _archived(shards, tmp_path, "fed-a", 1)
    _archived(shards, tmp_path, "fed-b", 2, lock=False)
    with pytest.raises(shards.ShardsError, match="fed-b"):
        shards.collect(plan, tmp_path / "merge")
    assert not (plan.results / MANIFEST).exists()
    assert not list(plan.results.glob("*.part"))


def test_a_results_directory_that_cannot_take_the_manifest_refuses_the_collect_whole(shards, tmp_path, monkeypatch):
    """O63: no unpacked tree and no private copy is left when the manifest cannot be written."""
    plan = _plan(shards, tmp_path)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shards, "_write_whole", full)
    with pytest.raises(shards.ShardsError, match="manifest"):
        shards.collect(plan, tmp_path / "merge")
    assert not (tmp_path / "merge").exists()


def test_the_collect_command_prints_the_manifest_hash_apart_from_the_merge_line(shards, tmp_path, capsys):
    """O63: stderr names the SHA-256 of the manifest as written; stdout is still the merge line alone."""
    _plan(shards, tmp_path)
    assert shards.main(["collect", "--manifest", str(_plan_file(tmp_path)), "--into", str(tmp_path / "merge")]) == 0
    seen = capsys.readouterr()
    assert len(seen.out.strip().splitlines()) == 1
    assert "merge" in seen.out
    assert f"sha256 {_digest(tmp_path / 'results' / MANIFEST)}" in seen.err
    assert MANIFEST in seen.err


# ------------------------------------------------------------------------------------------------ verify-copy
def test_verify_copy_passes_on_an_exact_copy(shards, tmp_path, capsys):
    """O63: every archive present with its size and SHA-256, the manifest the one collect printed: exit 0."""
    manifest, local, digest = _pulled(shards, tmp_path)
    assert _verify(shards, manifest, local, digest) == 0
    seen = capsys.readouterr()
    assert "2 archives" in seen.out
    assert seen.err == ""


def test_verify_copy_accepts_the_hash_in_capitals(shards, tmp_path):
    """O63: a hash retyped or pasted in capitals is the same hash."""
    manifest, local, digest = _pulled(shards, tmp_path)
    assert _verify(shards, manifest, local, digest.upper()) == 0


def test_verify_copy_fails_naming_a_missing_archive(shards, tmp_path, capsys):
    """O63: exit 1, and the name of the archive that is not there: no other, no path."""
    manifest, local, digest = _pulled(shards, tmp_path)
    (local / "fed-b.out.tar").unlink()
    assert _verify(shards, manifest, local, digest) == 1
    seen = capsys.readouterr()
    assert "fed-b.out.tar" in seen.err
    assert "missing" in seen.err
    assert "fed-a.out.tar" not in seen.err
    assert str(tmp_path) not in seen.err + seen.out


def test_verify_copy_fails_naming_an_archive_with_one_byte_changed(shards, tmp_path, capsys):
    """O63: the same size and one different byte is caught by the SHA-256."""
    manifest, local, digest = _pulled(shards, tmp_path)
    data = bytearray((local / "fed-a.out.tar").read_bytes())
    data[len(data) // 2] ^= 0xFF
    (local / "fed-a.out.tar").write_bytes(bytes(data))
    assert _verify(shards, manifest, local, digest) == 1
    seen = capsys.readouterr()
    assert "fed-a.out.tar" in seen.err
    assert "SHA-256" in seen.err
    assert "fed-b.out.tar" not in seen.err


def test_verify_copy_fails_naming_a_truncated_archive(shards, tmp_path, capsys):
    """O63: an archive cut short (a pull that stopped) is caught by its size."""
    manifest, local, digest = _pulled(shards, tmp_path)
    kept = (local / "fed-b.out.tar").read_bytes()[:-512]
    (local / "fed-b.out.tar").write_bytes(kept)
    assert _verify(shards, manifest, local, digest) == 1
    seen = capsys.readouterr()
    assert "fed-b.out.tar" in seen.err
    assert "size" in seen.err
    assert "fed-a.out.tar" not in seen.err


def test_verify_copy_names_every_archive_that_fails_not_the_first(shards, tmp_path, capsys):
    """O63: one run lists them all, so one pull's every problem is seen at once."""
    manifest, local, digest = _pulled(shards, tmp_path)
    (local / "fed-a.out.tar").write_bytes(b"x")
    (local / "fed-b.out.tar").unlink()
    assert _verify(shards, manifest, local, digest) == 1
    seen = capsys.readouterr()
    assert "fed-a.out.tar" in seen.err
    assert "fed-b.out.tar" in seen.err


def test_verify_copy_warns_of_an_extra_archive_and_still_passes(shards, tmp_path, capsys):
    """O63: an archive the manifest does not name (another campaign's, or a stale one) is warned of, never a failure."""
    manifest, local, digest = _pulled(shards, tmp_path)
    (local / "fed-z.out.tar").write_bytes(b"another campaign's")
    (local / "fed-a.out.json").write_text("{}")  # a shard's own manifest is not an archive
    assert _verify(shards, manifest, local, digest) == 0
    seen = capsys.readouterr()
    assert "WARNING" in seen.err
    assert "fed-z.out.tar" in seen.err
    assert "fed-a.out.json" not in seen.err
    assert "fed-a.out.tar" not in seen.err


def test_verify_copy_does_not_take_a_directory_or_a_link_for_an_archive(shards, tmp_path, capsys):
    """O63: only a regular file is an archive, so a link or a directory of the right name never reads or hangs."""
    manifest, local, digest = _pulled(shards, tmp_path)
    (local / "fed-a.out.tar").unlink()
    (local / "fed-a.out.tar").mkdir()
    (local / "fed-b.out.tar").rename(tmp_path / "elsewhere.tar")
    (local / "fed-b.out.tar").symlink_to(tmp_path / "elsewhere.tar")
    assert _verify(shards, manifest, local, digest) == 1
    seen = capsys.readouterr()
    assert "fed-a.out.tar" in seen.err
    assert "fed-b.out.tar" in seen.err
    assert "regular file" in seen.err


def test_verify_copy_refuses_a_manifest_whose_hash_is_not_the_one_collect_printed(shards, tmp_path, capsys):
    """O63: a manifest edited to match a swapped archive passes its own entries but not the printed hash: exit 2."""
    manifest, local, digest = _pulled(shards, tmp_path)
    swapped = b"another campaign's archive"
    (local / "fed-a.out.tar").write_bytes(swapped)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["archives"][0] |= {"bytes": len(swapped), "sha256": hashlib.sha256(swapped).hexdigest()}
    manifest.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    assert _verify(shards, manifest, local, digest) == 2
    seen = capsys.readouterr()
    assert "manifest" in seen.err
    assert "SHA-256" in seen.err
    assert str(tmp_path) not in seen.err


def test_verify_copy_needs_the_hash_collect_printed(shards, tmp_path):
    """O63: with no hash to compare the manifest to, nothing is checked: the argument is required."""
    manifest, local, _ = _pulled(shards, tmp_path)
    with pytest.raises(SystemExit) as stopped:
        shards.main(["verify-copy", str(manifest), str(local)])
    assert stopped.value.code not in (0, None)


@pytest.mark.parametrize("given", ["", "abc", "z" * 64, "0" * 63])
def test_verify_copy_refuses_a_hash_that_is_not_a_sha256(shards, tmp_path, given, capsys):
    """O63: a hash cut short by a copy and paste is a mistake to name, not a manifest to call altered."""
    manifest, local, _ = _pulled(shards, tmp_path)
    assert _verify(shards, manifest, local, given) == 2
    assert "SHA-256" in capsys.readouterr().err


def _rewritten(manifest, **changes):
    """The manifest with `changes` applied, and the hash of the file now: what an operator given that hash would use."""
    data = json.loads(manifest.read_text(encoding="utf-8")) | changes
    manifest.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"format": "expcore-hpc-archives/2"},
        {"archives": []},
        {"archives": [{"name": "../fed-a.out.tar", "bytes": 1, "sha256": "0" * 64}]},
        {"archives": [{"name": "sub/fed-a.out.tar", "bytes": 1, "sha256": "0" * 64}]},
        {"archives": [{"name": "/fed-a.out.tar", "bytes": 1, "sha256": "0" * 64}]},
        {"archives": [{"name": "fed-a.out.tar", "bytes": -1, "sha256": "0" * 64}]},
        {"archives": [{"name": "fed-a.out.tar", "bytes": True, "sha256": "0" * 64}]},
        {"archives": [{"name": "fed-a.out.tar", "bytes": 1, "sha256": "short"}]},
        {"archives": [{"name": "fed-a.out.tar", "bytes": 1}]},
        {"archives": [{"name": "fed-a.out.tar", "bytes": 1, "sha256": "0" * 64}] * 2},
        {"archives": "fed-a.out.tar"},
    ],
    ids=[
        "format",
        "none",
        "up",
        "subdirectory",
        "absolute",
        "negative",
        "bool",
        "short-hash",
        "no-hash",
        "twice",
        "str",
    ],
)
def test_verify_copy_refuses_a_manifest_it_cannot_read_as_one_before_it_reads_any_file(shards, tmp_path, changes):
    """O63: a manifest whose hash is right but that names no archive, or a path out of the directory, or an entry
    without its size and hash verifies nothing: exit 2, and a manifest that names none is never a pass."""
    manifest, local, _ = _pulled(shards, tmp_path)
    assert _verify(shards, manifest, local, _rewritten(manifest, **changes)) == 2


def test_verify_copy_refuses_a_manifest_that_is_not_json_or_a_directory_that_is_not_there(shards, tmp_path):
    """O63: exit 2 for either, a message and no traceback."""
    manifest, local, digest = _pulled(shards, tmp_path)
    assert _verify(shards, manifest, tmp_path / "nowhere", digest) == 2
    manifest.write_text("not json")
    assert _verify(shards, manifest, local, hashlib.sha256(b"not json").hexdigest()) == 2
    assert _verify(shards, tmp_path / "absent.json", local, digest) == 2


# ------------------------------------------------------------------------------------------------ no core on the host
def _on_a_bare_interpreter(*args):
    """`shards.py` run as the operator's host runs it: isolated (`-I`) and without site-packages (`-S`), where the
    core, which this environment has installed, cannot be imported."""
    return subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), *args], capture_output=True, text=True, check=False, timeout=120
    )


def test_the_premise_of_the_bare_interpreter_holds_the_core_is_not_importable_there():
    """O63: the check below proves something only if `-I -S` cannot import the core."""
    found = subprocess.run(
        [sys.executable, "-I", "-S", "-c", "import expcore"], capture_output=True, text=True, check=False, timeout=120
    )
    assert found.returncode != 0


def test_verify_copy_runs_on_a_host_without_the_core(shards, tmp_path):
    """O63: standard library alone: it passes an exact copy and fails a changed one with the core not importable."""
    manifest, local, digest = _pulled(shards, tmp_path)
    args = ("verify-copy", str(manifest), str(local), "--manifest-sha256", digest)
    passed = _on_a_bare_interpreter(*args)
    assert passed.returncode == 0, passed.stderr
    assert "2 archives" in passed.stdout
    (local / "fed-a.out.tar").write_bytes(b"x")
    failed = _on_a_bare_interpreter(*args)
    assert failed.returncode == 1
    assert "fed-a.out.tar" in failed.stderr
    assert "Traceback" not in failed.stderr

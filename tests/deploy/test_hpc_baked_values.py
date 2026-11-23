"""The HPC bundle's check for the Couchbase values the images ship (deploy/hpc-federation/bundle/baked.py, lib.sh).

lib.sh lists those values by length and SHA-256 only, and baked.py finds them by hash wherever grep -F would have
found the plain value. The real values never appear here: these tests use a stand-in value and its hash.
"""

import errno
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle"
BAKED = BUNDLE / "baked.py"
STAND_IN = b"qzjtwkcpmvbnrhsl"  # 16 lowercase letters, the cookies' form; not a real value
STAND_IN_HEX = b"0f1e2d3c4b5a69788796a5b4c3d2e1f0"  # 32 hex digits, the memcached password's form
ENTRIES = [f"{len(v)}:{hashlib.sha256(v).hexdigest()}" for v in (STAND_IN, STAND_IN_HEX)]


def baked(top: Path, *entries: str) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-I", str(BAKED), str(top), *(entries or ENTRIES)]
    return subprocess.run(argv, capture_output=True, timeout=60, check=False)


def lib(command: str, *args: str) -> subprocess.CompletedProcess:
    """Run a bundle function in a fresh bash that sources lib.sh, with this interpreter as the bundle's Python."""
    script = f'source "{BUNDLE}/lib.sh"; {command} "$@"'
    env = {**os.environ, "HPCOP_PY": sys.executable}
    return subprocess.run(
        ["bash", "-c", script, "lib", *args], capture_output=True, text=True, timeout=60, check=False, env=env
    )


@pytest.mark.parametrize(
    "content",
    [
        STAND_IN,  # a cookie file: the value alone
        b"\n" + STAND_IN + b"\n",
        b'{"cookie": "' + STAND_IN + b'"}',
        b"d\x00\x10" + STAND_IN + b"jh\x02",  # an Erlang term: a letter tag right after the value
        b"xy" + STAND_IN + b"z",  # inside a longer run of letters, off its start
        b"\x00" * 100 + b"ab" + STAND_IN_HEX + b"cd" + b"\x00" * 100,  # binary, inside a longer hex run
    ],
)
def test_a_shipped_value_is_found_by_its_hash_wherever_grep_would_find_it(tmp_path, content):
    """Each place grep -F found the plain value, the hash comparison finds it too: the path is named, the value not."""
    nested = tmp_path / "var" / "lib" / "couchbase"
    nested.mkdir(parents=True)
    (nested / "config.dat").write_bytes(content)
    (tmp_path / "clean.txt").write_bytes(b"nothing shipped here\n")
    result = baked(tmp_path)
    assert result.returncode == 1, result.stderr
    assert result.stdout.decode().splitlines() == [str(nested / "config.dat")]
    assert STAND_IN not in result.stdout + result.stderr
    assert STAND_IN_HEX not in result.stdout + result.stderr


def test_a_tree_without_a_shipped_value_passes(tmp_path):
    """A near miss (one character off, or cut short) is not the value."""
    (tmp_path / "a").write_bytes(STAND_IN[:-1] + b"x")
    (tmp_path / "b").write_bytes(STAND_IN[:-1])
    (tmp_path / "empty").write_bytes(b"")
    result = baked(tmp_path)
    assert (result.returncode, result.stdout) == (0, b""), result.stderr


def test_a_link_is_not_followed(tmp_path):
    """As grep -r does: a link inside the tree to a file outside it is not read."""
    outside = tmp_path / "outside"
    outside.write_bytes(STAND_IN)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    assert baked(tree).returncode == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_file_that_cannot_be_read_fails_the_scan(tmp_path):
    """The check fails closed: an unreadable file is an error (2), never a pass."""
    locked = tmp_path / "locked"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        result = baked(tmp_path)
    finally:
        locked.chmod(0o600)
    assert result.returncode == 2
    assert b"Permission denied" in result.stderr


def test_an_error_reading_a_files_metadata_fails_the_scan(tmp_path, monkeypatch):
    """A stat that fails (EIO) is an error, not a file skipped as not regular, which would pass a tree unread."""
    spec = importlib.util.spec_from_file_location("baked", BAKED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "cookie").write_bytes(STAND_IN)
    real_lstat = os.lstat

    def failing_lstat(path, *args, **kwargs):
        if str(path).endswith("cookie"):
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "lstat", failing_lstat)
    assert module.main([str(tmp_path), *ENTRIES]) == 2


@pytest.mark.parametrize("entries", [["16"], ["16:" + "A" * 64], ["0:" + "a" * 64], ["16:abc"]])
def test_a_malformed_hash_list_is_an_error(tmp_path, entries):
    assert baked(tmp_path, *entries).returncode == 2


def test_a_missing_directory_is_an_error(tmp_path):
    assert baked(tmp_path / "missing").returncode == 2


def test_lib_sh_lists_the_shipped_values_by_hash_only():
    """Two 16-letter cookies and the 32-hex memcached password, as LENGTH:SHA256: no literal value in the bundle."""
    result = lib("printf '%s\\n' \"${BAKED_CB_SHA256[@]}\"")
    assert result.returncode == 0, result.stderr
    entries = result.stdout.split()
    assert len(entries) == 3
    assert all(re.fullmatch(r"(16|32):[0-9a-f]{64}", e) for e in entries)
    assert sorted(e.split(":")[0] for e in entries) == ["16", "16", "32"]
    for path in (p for p in BUNDLE.iterdir() if p.is_file()):
        assert "BAKED_CB_VALUES" not in path.read_text(encoding="utf-8", errors="replace"), path


def test_no_baked_passes_a_clean_tree_and_fails_one_holding_a_value(tmp_path):
    """lib.sh's no_baked, which prep and check-defaults.sh call: true on a clean tree; false, with the file named on
    stderr, on one holding a listed value."""
    (tmp_path / "clean").write_bytes(b"fresh cookie\n")
    override = f"BAKED_CB_SHA256=({' '.join(ENTRIES)}); no_baked"
    assert lib(override, str(tmp_path)).returncode == 0
    (tmp_path / "couchbase-server.cookie").write_bytes(STAND_IN)
    result = lib(override, str(tmp_path))
    assert result.returncode == 1
    assert f"a shipped Couchbase value is in {tmp_path / 'couchbase-server.cookie'}" in result.stderr
    assert STAND_IN.decode() not in result.stderr


def test_no_baked_fails_when_the_scan_cannot_run(tmp_path):
    """A scan that does not complete (here, a directory that does not exist) is a failure, not a pass."""
    assert lib("no_baked", str(tmp_path / "missing")).returncode == 2


def test_prep_and_the_checks_use_the_hash_comparison():
    """prep (after removing the shipped state), check-defaults.sh's file check and its live check all call no_baked."""
    patch = (BUNDLE / "patch.sh").read_text(encoding="utf-8")
    checks = (BUNDLE / "check-defaults.sh").read_text(encoding="utf-8")
    assert 'no_baked "$R/volumes" || die' in patch
    assert 'no_baked "$R/volumes"' in checks
    assert 'no_baked "$CB"' in checks
    assert "grep -rlF" not in patch + checks


# -- O98: a restore does not read the provider's storage ---------------------------------------------------------------


def baked_pruned(top: Path, *options: str) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-I", str(BAKED), *options, str(top), *ENTRIES]
    return subprocess.run(argv, capture_output=True, timeout=60, check=False)


def volumes(tmp_path: Path) -> Path:
    """A provider's /volumes as the rootfs holds it: the persistence and its backup, the posix storage, and an imported
    tree's mountpoint, all real directories."""
    top = tmp_path / "volumes"
    for name in ("persistence", "persistence_backup", "posix", "import/shared-tree", "import/second-tree"):
        (top / name).mkdir(parents=True)
    return top


def test_o98_the_posix_storage_and_each_import_mountpoint_are_left_out_of_the_scan_and_nothing_else_is(tmp_path):
    """O98: a value in the study's data under volumes/posix, or under an import entry, is not read (a tmpfs storage is
    the provider's whole data, ten minutes a restore); the same value in the persistence, its backup, a file beside
    them is found, so the scan of the Couchbase state stays whole."""
    top = volumes(tmp_path)
    (top / "posix" / "data.bin").write_bytes(STAND_IN)
    (top / "posix" / "deep" / "er").mkdir(parents=True)
    (top / "posix" / "deep" / "er" / "more.bin").write_bytes(STAND_IN)
    (top / "import" / "shared-tree" / "file").write_bytes(STAND_IN)
    (top / "import" / "second-tree" / "file").write_bytes(STAND_IN)
    assert baked(top).returncode == 1  # without the prune, as before: the data is read
    result = baked_pruned(top, "--prune", "posix", "--prune-each", "import")
    assert (result.returncode, result.stdout) == (0, b""), result.stderr
    for kept in ("persistence/cookie", "persistence_backup/etc/cookie", "loose"):
        (top / kept).parent.mkdir(parents=True, exist_ok=True)
        (top / kept).write_bytes(STAND_IN)
        found = baked_pruned(top, "--prune", "posix", "--prune-each", "import")
        assert found.returncode == 1, kept
        assert found.stdout.decode().splitlines() == [str(top / kept)]
        (top / kept).unlink()


def test_o98_an_entry_directly_below_import_is_pruned_but_import_itself_is_not_a_prune_path_of_its_own(tmp_path):
    """O98: `--prune-each import` leaves out what is below `import`, a file as much as a mountpoint, and nothing of its
    parent; `--prune posix` leaves out `posix` and not a directory of that name elsewhere."""
    top = volumes(tmp_path)
    (top / "import" / "a-plain-file").write_bytes(STAND_IN)
    (top / "persistence" / "posix").mkdir()
    (top / "persistence" / "posix" / "cookie").write_bytes(STAND_IN)
    found = baked_pruned(top, "--prune", "posix", "--prune-each", "import")
    assert found.returncode == 1
    assert found.stdout.decode().splitlines() == [str(top / "persistence" / "posix" / "cookie")]


def test_o98_a_prune_path_that_is_not_there_prunes_nothing_a_zone_has_no_storage(tmp_path):
    """O98: a zone has no posix and no import: the paths are missing, which is nothing to prune and no error."""
    (tmp_path / "persistence").mkdir()
    (tmp_path / "persistence" / "clean").write_bytes(b"fresh\n")
    assert baked_pruned(tmp_path, "--prune", "posix", "--prune-each", "import").returncode == 0
    (tmp_path / "persistence" / "cookie").write_bytes(STAND_IN)
    assert baked_pruned(tmp_path, "--prune", "posix", "--prune-each", "import").returncode == 1


@pytest.mark.parametrize(
    "options",
    [
        ("--prune", "posix"),
        ("--prune-each", "import"),
        ("--prune", "import/shared-tree"),
        ("--prune-each", "import/shared-tree"),
    ],
)
def test_o98_a_prune_path_that_is_a_link_is_refused_and_nothing_is_scanned_as_pruned(tmp_path, options):
    """O98: a link where a prune path is, or an entry below a `--prune-each` path, is an error (exit 2): what it stands
    for is not known to be what was meant, so the scan fails closed rather than leave it out."""
    top = tmp_path / "volumes"
    (top / "persistence").mkdir(parents=True)
    (top / "persistence" / "cookie").write_bytes(STAND_IN)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (top / "import").mkdir()
    for target in ("posix",):
        (top / target).symlink_to(elsewhere, target_is_directory=True)
    (top / "import" / "shared-tree").symlink_to(elsewhere, target_is_directory=True)
    result = baked_pruned(top, *options)
    assert result.returncode == 2, result.stdout
    assert b"link" in result.stderr


def test_o98_a_link_among_the_parents_of_a_prune_path_is_refused_too(tmp_path):
    """O98: a prune path below a link is no path of the tree: `lstat` of the whole would follow the link out of it."""
    top = tmp_path / "volumes"
    outside = tmp_path / "outside"
    (outside / "posix").mkdir(parents=True)
    top.mkdir()
    (top / "linked").symlink_to(outside, target_is_directory=True)
    assert baked_pruned(top, "--prune", "linked/posix").returncode == 2


def test_o98_a_prune_option_that_is_no_plain_relative_path_or_has_no_value_is_an_error(tmp_path):
    """O98: an absolute path, `..`, an empty one, an unknown option and an option with no value fail the scan closed."""
    for options in (("--prune", "/etc"), ("--prune", "../elsewhere"), ("--prune", ""), ("--prune-each", "a//b"),
                    ("--unknown", "x"), ("--prune",)):  # fmt: skip
        assert baked_pruned(tmp_path, *options).returncode == 2, options


def test_o98_a_prune_each_path_that_is_a_file_is_an_error(tmp_path):
    """O98: `--prune-each` names a directory whose entries are left out; a file there is no such directory."""
    (tmp_path / "import").write_bytes(b"not a directory")
    assert baked_pruned(tmp_path, "--prune-each", "import").returncode == 2


def test_o98_lib_sh_passes_the_options_before_the_directory_and_the_restore_check_prunes_the_storage(tmp_path):
    """O98: `no_baked <dir> --prune posix --prune-each import` scans the rest of the directory; check-defaults.sh's
    /volumes check is that call, and prep's, and the live check's, keep the whole scan."""
    top = volumes(tmp_path)
    (top / "posix" / "data.bin").write_bytes(STAND_IN)
    override = f"BAKED_CB_SHA256=({' '.join(ENTRIES)}); no_baked"
    assert lib(override, str(top)).returncode == 1
    assert lib(override, str(top), "--prune", "posix", "--prune-each", "import").returncode == 0
    (top / "persistence" / "cookie").write_bytes(STAND_IN)
    assert lib(override, str(top), "--prune", "posix", "--prune-each", "import").returncode == 1
    checks = (BUNDLE / "check-defaults.sh").read_text(encoding="utf-8")
    patch = (BUNDLE / "patch.sh").read_text(encoding="utf-8")
    assert 'no_baked "$R/volumes" --prune posix --prune-each import' in checks
    assert 'no_baked "$CB"' in checks
    assert 'no_baked "$R/volumes" || die' in patch  # prep scans the rootfs whole, before any storage exists
    assert "--prune" not in patch

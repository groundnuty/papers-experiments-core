"""O63: the operator's key files reach a shard step's dispatcher (deploy/hpc-federation/shards.py). A study's
models.toml names `key_file = "~/..."` paths, and the dispatcher's HOME is a private directory on the node's tmpfs, so
without this every model call fails at its first request. The manifest's optional `home_files` names an operator
directory laid out as it would sit under a home; `step` copies it into the dispatcher's home before the dispatcher
starts and removes it with the step's directory. Everything here is invented: the tree, the file names and the keys;
nothing runs srun, and the dispatcher is a stand-in."""

import argparse
import importlib.util
import json
import os
import shlex
import signal
import stat
import sys
from pathlib import Path

import pytest
from fingerprint_support import standin_python

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
# Names and contents no record, archive, log or line may ever hold: each carries "zq".
FILES = {".zqsecrets/alpha-zq/key-zq": "sk-zq-first-1111\n", ".zqkeys/beta-zq/gamma-zq/lk-zq": "lk-zq-second-2222\n"}
MARKERS = (b"zq", b"1111", b"2222")
HOME_NAMES = {"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPTAINER_CONFIGDIR",
              "APPTAINER_CACHEDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL",
              "GIT_CONFIG_NOSYSTEM", "TMPDIR"}  # fmt: skip


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_home_files", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_home_files"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_home_files", None)


def key_tree(tmp_path: Path, files=FILES) -> Path:
    """The operator's tree: directories 0700, files 0600, as an operator's private key tree is."""
    root = tmp_path / "optree"
    root.mkdir(mode=0o700, parents=True)
    for name, text in files.items():
        path = root / name
        for parent in reversed(path.relative_to(root).parents):
            (root / parent).mkdir(mode=0o700, exist_ok=True)
            (root / parent).chmod(0o700)
        path.write_text(text)
        path.chmod(0o600)
    return root


def make_study(tmp_path: Path) -> Path:
    study = tmp_path / "study"
    study.mkdir(parents=True, exist_ok=True)
    study.chmod(0o755)
    (study / "study.toml").write_text("[study]\n")
    (study / "campaign.toml").write_text("[scheduler]\nworkers = 1\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return study


def manifest_file(tmp_path: Path, extra: str = "") -> Path:
    make_study(tmp_path)
    lines = [f'study = "{tmp_path / "study"}"', f'python = "{standin_python(tmp_path / "venv")}"',
             f'core_src = "{tmp_path / "core" / "src"}"', f'results = "{tmp_path / "results"}"',
             f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin:/bin"', "cpus = 6",
             'memory = "18g"']  # fmt: skip
    for n, name in enumerate(("a", "b"), 1):
        root = tmp_path / "fed" / name
        root.mkdir(parents=True, exist_ok=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked",
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')
        (root / "facts.json").write_text("{}")
    if extra:
        lines.append(extra)
    for label, name in (("fed-a", "a"), ("fed-b", "b")):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{tmp_path / "fed" / name}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def printed(shards, tmp_path, capsys, command, extra="") -> list[str]:
    assert shards.main([command, "--manifest", str(manifest_file(tmp_path, extra))]) == 0
    return capsys.readouterr().out.splitlines()


def declared(tree: Path) -> str:
    return f'home_files = "{tree}"'


def step_args(tmp_path, home_files=None, *, label="fed-a"):
    """The step's arguments as its line gives them; `home_files=None` is a line with no such option."""
    study = make_study(tmp_path)
    extra = {} if home_files is None else {"home_files": str(home_files)}
    return argparse.Namespace(study=study, index=1, count=2, label=label, tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False, archive_left=False,
                              snapshot_every=3600.0, **extra)  # fmt: skip


def dispatcher():
    """The stand-in dispatcher: what its home held (relative paths, modes, contents), its env and the step's record."""
    seen = {}

    def run(argv, env, cwd):
        home = Path(env["HOME"])
        seen.update(env=dict(env), owner=json.loads((Path(cwd) / "step.json").read_text()), home=home)
        seen["files"] = {str(p.relative_to(home)): (stat.S_IMODE(p.lstat().st_mode), p.read_text())
                         for p in sorted(home.rglob("*")) if p.is_file() and not p.is_symlink()}  # fmt: skip
        seen["dirs"] = {str(p.relative_to(home)): stat.S_IMODE(p.lstat().st_mode)
                        for p in sorted(home.rglob("*")) if p.is_dir()}  # fmt: skip
        copy = Path(argv[argv.index("run") + 1])
        (copy / "out" / "runs").mkdir(parents=True, exist_ok=True)
        (copy / "out" / "runs" / "r1.json").write_text("{}")
        (copy / "out" / "campaign.lock").write_text("")
        return 0

    return run, seen


def never_run(argv, env, cwd):
    raise AssertionError("a refused step must never start its dispatcher")


def run_step(shards, args, runner):
    return shards.step(args, runner=runner, core_found=lambda path: True)


def holds_nothing_of_the_tree(root: Path, extra=()) -> None:
    """No file under `root` (names included) holds a marker: not a name, not a content."""
    for path in [root, *root.rglob("*")]:
        text = str(path.relative_to(root)).encode() if path != root else b""
        body = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        for marker in (*MARKERS, *extra):
            assert marker not in text, "a name of the tree is in a record"
            assert marker not in body, "a name or content of the tree is in a record"


# -- the manifest and the line ---------------------------------------------------------------------------------------


def test_o63_home_files_rides_on_every_line_and_nothing_else_changes(shards, tmp_path, capsys):
    """O63: `lines` ends each line in `--home-files PATH` (the path, as the manifest's other paths ride on it) and is
    otherwise the line a manifest without the key prints, word for word; `plan` says only that the shard has one."""
    tree = key_tree(tmp_path / "with")
    plain = printed(shards, tmp_path / "plain", capsys, "lines")
    lines = printed(shards, tmp_path / "with", capsys, "lines", declared(tree))
    for line, base in zip(lines, plain, strict=True):
        assert line.endswith(f" --home-files {shlex.quote(str(tree))}")
        stripped = line.removesuffix(f" --home-files {shlex.quote(str(tree))}")
        assert stripped.replace(str(tmp_path / "with"), "ROOT") == base.replace(str(tmp_path / "plain"), "ROOT")
    plans = [json.loads(p) for p in printed(shards, tmp_path / "with", capsys, "plan", declared(tree))]
    assert [p["home_files"] for p in plans] == [True, True]
    assert all(p["argv"][-2:] == ["--home-files", str(tree)] for p in plans)  # the path rides on the line only
    assert all("home_files" not in json.loads(p) for p in printed(shards, tmp_path / "plain", capsys, "plan"))


@pytest.mark.parametrize("value", ['"relative/path"', '"/a/../b"', '"/a/./b"', '"/a//b"', '"/a/b/"', "7", "true",
                                   '["/a"]', '""', '"/a/b\\nc"', '"/a/b\\tc"', '"/a/b\\u007fc"'])  # fmt: skip
def test_o63_a_home_files_that_is_not_a_plain_absolute_path_refuses_the_plan(shards, tmp_path, capsys, value):
    """O63: the manifest's path is absolute with no `.`, `..`, empty or control-character part (a newline would make
    two lines of one), as a string: `plan` and `lines` refuse anything else naming the key and print nothing."""
    path = manifest_file(tmp_path, f"home_files = {value}")
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "home_files" in captured.err


def damaged_trees(tmp_path: Path):
    """Each way the operator's tree can be wrong, as a function that damages a good tree: (name, damage, reason)."""

    def link_inside(root):
        (root / ".zqsecrets" / "alpha-zq" / "link-zq").symlink_to("key-zq")

    def link_to_dir(root):
        (root / "dirlink-zq").symlink_to(root / ".zqkeys")

    def group_readable(root):
        (root / ".zqsecrets" / "alpha-zq" / "key-zq").chmod(0o640)

    def executable_file(root):
        (root / ".zqsecrets" / "alpha-zq" / "key-zq").chmod(0o700)

    def open_dir(root):
        (root / ".zqkeys").chmod(0o750)

    def open_root(root):
        root.chmod(0o755)

    def fifo(root):
        os.mkfifo(root / "fifo-zq", 0o600)

    def too_many(root):
        for n in range(shards_limit + 1):
            (root / f"f{n}-zq").write_text("x")
            (root / f"f{n}-zq").chmod(0o600)

    def too_big(root):
        (root / "big-zq").write_bytes(b"x" * ((1 << 20) + 1))
        (root / "big-zq").chmod(0o600)

    def too_deep(root):
        deep = root
        for n in range(12):
            deep = deep / f"d{n}-zq"
            deep.mkdir(mode=0o700)
        (deep / "k-zq").write_text("x")
        (deep / "k-zq").chmod(0o600)

    shards_limit = 64
    return [("a link to a file", link_inside), ("a link to a directory", link_to_dir),
            ("a file open to the group", group_readable), ("a file that is executable", executable_file),
            ("a directory open to the group", open_dir), ("a root open to others", open_root),
            ("a file that is not regular", fifo), ("too many files", too_many), ("too many bytes", too_big),
            ("a tree too deep", too_deep)]  # fmt: skip


@pytest.mark.parametrize("n", range(10))
def test_o63_a_damaged_tree_refuses_plan_lines_and_step_naming_the_key_and_no_file(shards, tmp_path, capsys, n):
    """O63: a link inside, a file or directory open to group or other, a file that is not regular, too many files or
    bytes or too deep a tree refuses `plan`, `lines` and the step (73, nothing made, no dispatcher), the message naming
    `home_files` and never a path or a name of the tree."""
    tree = key_tree(tmp_path)
    _, damage = damaged_trees(tmp_path)[n]
    damage(tree)
    path = manifest_file(tmp_path, declared(tree))
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "home_files" in captured.err
        assert "zq" not in captured.err
        assert str(tree) not in captured.err
    assert not list((tmp_path / "tmpfs").iterdir())
    capsys.readouterr()
    assert run_step(shards, step_args(tmp_path, tree), never_run) == shards.PRE_STEP_REFUSED
    err = capsys.readouterr().err
    assert "home_files" in err
    assert "zq" not in err
    assert str(tree) not in err
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not list((tmp_path / "results").iterdir())


def test_o63_a_tree_that_is_not_this_users_is_refused(shards, tmp_path):
    """O63: a directory or a file of another user's is refused (the user is a parameter, as a test cannot chown)."""
    tree = key_tree(tmp_path)
    with pytest.raises(shards.ShardsError, match="home_files") as raised:
        shards.read_home_files(str(tree), uid=os.geteuid() + 1)
    assert "not this user" in str(raised.value)
    assert "zq" not in str(raised.value)
    assert shards.read_home_files(str(tree)) is not None  # and the user's own passes


def test_o63_a_link_on_the_way_to_the_tree_is_refused_and_the_message_says_to_give_the_resolved_path(shards, tmp_path):
    """O63: no link on the way to the directory is followed: the operator gives the resolved path."""
    tree = key_tree(tmp_path)
    (tmp_path / "alias").symlink_to(tree)
    with pytest.raises(shards.ShardsError, match="home_files") as raised:
        shards.read_home_files(str(tmp_path / "alias"))
    assert "link" in str(raised.value)
    assert "resolved" in str(raised.value)
    with pytest.raises(shards.ShardsError, match="link"):
        shards.read_home_files(str(tmp_path / "alias" / ".zqkeys"))


@pytest.mark.parametrize("what", ["missing", "empty"])
def test_o63_a_missing_or_empty_tree_is_refused(shards, tmp_path, what):
    """O63: a directory that does not exist, or holds no file, is a mistake that would fail every model call."""
    tree = tmp_path / "none"
    if what == "empty":
        tree.mkdir(mode=0o700)
    with pytest.raises(shards.ShardsError, match="home_files"):
        shards.read_home_files(str(tree))


def test_o63_a_file_that_is_a_file_not_a_directory_is_no_tree(shards, tmp_path):
    """O63: the manifest's path names a directory."""
    path = tmp_path / "afile"
    path.write_text("x")
    path.chmod(0o600)
    with pytest.raises(shards.ShardsError, match="home_files"):
        shards.read_home_files(str(path))


# -- the step -------------------------------------------------------------------------------------------------------


def test_o63_the_tree_is_in_the_dispatchers_home_with_the_same_paths_modes_and_contents(shards, tmp_path, capsys):
    """O63 [fails before]: `~/...` in a study's models.toml resolves, in the dispatcher, to the operator's files: the
    same relative paths and contents, files 0600 and every directory 0700; the step's record says `home_files` and
    how many files, and holds nothing of them; the step says how many went in (a count) and names none."""
    tree = key_tree(tmp_path)
    run, seen = dispatcher()
    assert run_step(shards, step_args(tmp_path, tree), run) == 0
    assert {name: text for name, (_, text) in seen["files"].items()} == FILES
    assert {mode for mode, _ in seen["files"].values()} == {0o600}
    assert set(seen["dirs"].values()) == {0o700}  # the step's own home directories and the tree's
    assert seen["owner"]["home_files"] is True
    assert seen["owner"]["home_files_count"] == len(FILES)
    err = capsys.readouterr().err
    assert f"{len(FILES)} file" in err
    assert "zq" not in err


def test_o63_the_core_reads_the_key_through_the_home_the_step_gave_it(shards, tmp_path, monkeypatch):
    """O63 [fails before]: the study's model entry, read in a process whose HOME is the dispatcher's, finds its key;
    in the same home with no `home_files` it cannot be read (the defect)."""
    from expcore.config.models import ConfigError, ModelSpec

    spec = ModelSpec(key="m1", backend="openai_compat", api_id="m", status="draft", base_url="http://127.0.0.1:9/v1",
                     key_file="~/.zqsecrets/alpha-zq/key-zq", free=True)  # fmt: skip
    keys = {}

    def run(argv, env, cwd):
        monkeypatch.setenv("HOME", env["HOME"])
        try:
            keys["read"] = spec.read_key()
        except ConfigError:
            keys["read"] = None
        copy = Path(argv[argv.index("run") + 1])
        (copy / "out" / "runs").mkdir(parents=True, exist_ok=True)
        (copy / "out" / "runs" / "r1.json").write_text("{}")
        (copy / "out" / "campaign.lock").write_text("")
        return 0

    tree = key_tree(tmp_path / "with")
    assert run_step(shards, step_args(tmp_path / "with", tree), run) == 0
    assert keys["read"] == "sk-zq-first-1111"
    assert run_step(shards, step_args(tmp_path / "without", None), run) == 0
    assert keys["read"] is None


def test_o63_the_copies_vanish_with_the_step_and_no_record_archive_or_log_holds_the_tree(shards, tmp_path, capsys):
    """O63: after the step the tmpfs is empty (the copies went with its directory); the archive, its manifest, the
    results directory and what the step printed hold no name of the tree and no content of a file."""
    tree = key_tree(tmp_path)
    run, seen = dispatcher()
    assert run_step(shards, step_args(tmp_path, tree), run) == 0
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not seen["home"].exists()
    holds_nothing_of_the_tree(tmp_path / "results", extra=(str(tree).encode(),))
    out = capsys.readouterr()
    assert "zq" not in out.out + out.err
    import tarfile

    with tarfile.open(tmp_path / "results" / "fed-a.out.tar") as archive:
        for member in archive.getmembers():
            assert b"zq" not in member.name.encode()
            body = archive.extractfile(member)
            assert body is None or not any(m in body.read() for m in MARKERS)


def test_o63_a_step_without_home_files_is_what_it_was(shards, tmp_path):
    """O63: no `home_files`: the dispatcher's home holds only the step's own directories, the environment is the
    allowlist and the home variables, the record has no `home_files`, and the archive's manifest none."""
    run, seen = dispatcher()
    assert run_step(shards, step_args(tmp_path), run) == 0
    assert seen["files"] == {}
    assert set(seen["dirs"]) == {name.rstrip("/") for name in shards.HOME_DIRS}
    assert set(seen["env"]) - set(shards.ALLOWLIST) - HOME_NAMES <= {"PYTHONDONTWRITEBYTECODE"}
    assert "home_files" not in seen["owner"]
    assert "home_files_count" not in seen["owner"]
    manifest = json.loads((tmp_path / "results" / "fed-a.out.json").read_text())
    assert not any("home_files" in key for key in manifest)


def test_o63_the_archive_manifest_never_says_anything_of_the_files(shards, tmp_path):
    """O63: the archive's manifest of a step that had files is the manifest of one that had none, key by key."""
    run, _ = dispatcher()
    assert run_step(shards, step_args(tmp_path / "a", None), run) == 0
    assert run_step(shards, step_args(tmp_path / "b", key_tree(tmp_path / "b")), run) == 0
    plain = json.loads((tmp_path / "a" / "results" / "fed-a.out.json").read_text())
    with_files = json.loads((tmp_path / "b" / "results" / "fed-a.out.json").read_text())
    assert set(plain) == set(with_files)


def test_o63_a_file_that_collides_with_the_steps_own_home_refuses_the_step_before_its_dispatcher(
    shards, tmp_path, capsys
):
    """O63: a tree whose top-level `.cache` is a FILE would sit where the step made a directory: the step is refused
    (73) before its dispatcher starts, nothing is left on the tmpfs, and the message names the key and no file."""
    tree = key_tree(tmp_path, {".cache": "lk-zq-3333\n"})
    capsys.readouterr()
    assert run_step(shards, step_args(tmp_path, tree), never_run) == shards.PRE_STEP_REFUSED
    err = capsys.readouterr().err
    assert "home_files" in err
    assert "zq" not in err
    assert ".cache" not in err.replace("home_files", "")
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_a_tree_that_shares_a_directory_with_the_steps_home_is_merged(shards, tmp_path):
    """O63: a tree with `.config/<app>/key` lands beside the step's own `.config` (made 0700 first), mode 0600."""
    tree = key_tree(tmp_path, {".config/zqapp/key-zq": "sk-zq-4444\n"})
    run, seen = dispatcher()
    assert run_step(shards, step_args(tmp_path, tree), run) == 0
    assert seen["files"][".config/zqapp/key-zq"] == (0o600, "sk-zq-4444\n")
    assert seen["dirs"][".config"] == 0o700
    assert seen["dirs"][".config/zqapp"] == 0o700


def test_o63_a_tree_that_changed_after_the_lines_were_made_is_the_tree_the_step_reads(shards, tmp_path):
    """O63: the step reads the tree itself, once, at its start: a key rotated since `lines` is the one that arrives,
    and a link planted since is refused by the step (no line pins a tree)."""
    tree = key_tree(tmp_path)
    (tree / ".zqsecrets" / "alpha-zq" / "key-zq").write_text("sk-zq-rotated-9999\n")
    run, seen = dispatcher()
    assert run_step(shards, step_args(tmp_path, tree), run) == 0
    assert seen["files"][".zqsecrets/alpha-zq/key-zq"][1] == "sk-zq-rotated-9999\n"


def test_o63_the_archive_failing_leaves_no_key_in_the_directory_the_step_keeps(shards, tmp_path, monkeypatch):
    """O63: a step whose out/ cannot be archived keeps its directory (exit 75) for `--archive-left`: the key files
    are gone from it by then, and nothing of them is in what stays."""
    tree = key_tree(tmp_path)

    def broken(*args, **kwargs):
        raise OSError(28, "no space")

    monkeypatch.setattr(shards, "archive", broken)
    run, seen = dispatcher()
    assert run_step(shards, step_args(tmp_path, tree), run) == shards.ARCHIVE_FAILED
    kept = list((tmp_path / "tmpfs").iterdir())
    assert len(kept) == 1
    holds_nothing_of_the_tree(kept[0])
    assert not (seen["home"] / ".zqsecrets").exists()


def test_o63_a_term_while_the_files_are_staged_removes_them_and_restores_the_handlers(shards, tmp_path, monkeypatch):
    """O63: a TERM that arrives after the copy and before the dispatcher relays it (the step's own default would end
    it outright, leaving the directory) ends the step through its cleanup: nothing is left on the tmpfs, and the
    handlers are the ones the process had."""
    tree = key_tree(tmp_path)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    real = shards.stage_home_files

    def staged_then_stopped(*args, **kwargs):
        result = real(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(shards, "stage_home_files", staged_then_stopped)
    with pytest.raises(SystemExit) as stopped:
        run_step(shards, step_args(tmp_path, tree), never_run)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert not list((tmp_path / "tmpfs").iterdir())
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_o63_a_step_that_ends_normally_leaves_the_handlers_it_found_when_it_stages_files(shards, tmp_path):
    """O63: the stop handlers armed for the copy are the process's own again once the step is over."""
    tree = key_tree(tmp_path)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    run, _ = dispatcher()
    assert run_step(shards, step_args(tmp_path, tree), run) == 0
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_o63_an_unset_home_files_arms_no_handler_and_reads_no_tree(shards, tmp_path, monkeypatch):
    """O63: with no `home_files` the step reads nothing and arms nothing."""
    monkeypatch.setattr(shards, "read_home_files", lambda *a, **k: pytest.fail("a tree was read"))
    monkeypatch.setattr(shards, "_unwind_on_stop", lambda: pytest.fail("a handler was armed"))
    run, _ = dispatcher()
    assert run_step(shards, step_args(tmp_path), run) == 0


# -- what a review found: signals, the study's copy, the path ----------------------------------------------------------


def test_o63_a_hang_up_while_the_files_are_staged_ends_the_step_through_its_cleanup(shards, tmp_path, monkeypatch):
    """O63: a HUP, whose default would end the step outright and leave the copies, is handled as a stop is: exit 129
    through the cleanup, nothing left on the tmpfs, the handlers given back."""
    tree = key_tree(tmp_path)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    real = shards.stage_home_files

    def staged_then_hung_up(*args, **kwargs):
        result = real(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGHUP)
        return result

    monkeypatch.setattr(shards, "stage_home_files", staged_then_hung_up)
    with pytest.raises(SystemExit) as stopped:
        run_step(shards, step_args(tmp_path, tree), never_run)
    assert stopped.value.code == 128 + signal.SIGHUP
    assert not list((tmp_path / "tmpfs").iterdir())
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_o63_a_hang_up_while_the_dispatcher_runs_removes_the_copies_too(shards, tmp_path):
    """O63: the stop handlers stay armed through the dispatcher's run (HUP is not one `run_dispatcher` takes over): a
    HUP that arrives while it runs ends the step through its cleanup, and the home is gone."""
    tree = key_tree(tmp_path)
    seen = {}

    def hung_up_while_running(argv, env, cwd):
        seen["home"] = Path(env["HOME"])
        os.kill(os.getpid(), signal.SIGHUP)
        raise AssertionError("the hang-up must have ended the step")

    with pytest.raises(SystemExit) as stopped:
        run_step(shards, step_args(tmp_path, tree), hung_up_while_running)
    assert stopped.value.code == 128 + signal.SIGHUP
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not seen["home"].exists()


def test_o63_a_stop_that_arrives_during_the_cleanup_waits_until_the_keys_are_gone(shards, tmp_path, monkeypatch):
    """O63: a staging that wrote a key and then refused a colliding file starts its cleanup; a TERM that arrives while
    the home is being removed is held back until it is gone (the step then ends as a TERM ends it), so it can never
    leave the first key on the disk."""
    tree = key_tree(tmp_path, {".config/zq-first": "sk-zq-5555\n", ".zqmore/x-zq": "x\n", ".zzcache": "y\n"})
    (tree / ".zzcache").unlink()
    (tree / ".cache").write_text("sk-zq-collides\n")  # a file where the step's own `.cache` directory is
    (tree / ".cache").chmod(0o600)
    real, order = shards.remove_tree, []

    def stopped_while_removing(path):
        order.append(Path(path).name)
        if Path(path).name == "home":
            os.kill(os.getpid(), signal.SIGTERM)  # held back by the step: delivered after the cleanup
        real(path)

    monkeypatch.setattr(shards, "remove_tree", stopped_while_removing)
    with pytest.raises(SystemExit) as stopped:
        run_step(shards, step_args(tmp_path, tree), never_run)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert order[0] == "home"
    assert not list((tmp_path / "tmpfs").iterdir())


@pytest.mark.parametrize("way", ["normal", "dispatcher_failed", "no_out", "archive_failed"])
def test_o63_the_step_removes_the_copies_explicitly_on_every_way_out(shards, tmp_path, monkeypatch, way):
    """O63: whatever the way out (a finished shard, a dispatcher that failed, one that left no out/, an archive that
    could not be written), the key copies are removed by name before the step's directory is, not left to it."""
    tree = key_tree(tmp_path)
    real, removed = shards.remove_tree, []

    def noting(path):
        removed.append((Path(path).name, (Path(path) / ".zqsecrets").exists() or Path(path).name != "home"))
        real(path)

    monkeypatch.setattr(shards, "remove_tree", noting)
    if way == "archive_failed":
        monkeypatch.setattr(shards, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "no space")))
    run, _ = dispatcher()

    def runner(argv, env, cwd):
        if way == "no_out":
            return 3
        status = run(argv, env, cwd)
        return 3 if way == "dispatcher_failed" else status

    run_step(shards, step_args(tmp_path, tree), runner)
    names = [name for name, _ in removed]
    assert "home" in names
    assert any(name.startswith(shards.WORK_PREFIX) for name in names) or way == "archive_failed"
    if any(name.startswith(shards.WORK_PREFIX) for name in names):
        assert names.index("home") < min(i for i, n in enumerate(names) if n.startswith(shards.WORK_PREFIX))
    for left in (tmp_path / "tmpfs").rglob("*"):
        assert "zq" not in left.name


def test_o63_a_tree_inside_the_study_is_refused_because_the_step_copies_the_study(shards, tmp_path, capsys):
    """O63: the step copies the whole study to its tmpfs, and a failed archive keeps that copy: a key tree inside the
    study would be copied a second time into it, so `plan`, `lines` and the step refuse one."""
    make_study(tmp_path)
    tree = key_tree(tmp_path / "study")
    path = manifest_file(tmp_path, declared(tree))
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "home_files" in captured.err
        assert "overlap" in captured.err
        assert "zq" not in captured.err
    assert run_step(shards, step_args(tmp_path, tree), never_run) == shards.PRE_STEP_REFUSED
    assert "overlap" in capsys.readouterr().err
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_a_study_inside_the_tree_is_refused_by_the_step(shards, tmp_path, capsys):
    """O63: a tree that holds the study is no private tree: the step refuses it (73, nothing made)."""
    tree = key_tree(tmp_path)
    study = tmp_path / "optree" / "study"
    study.mkdir(mode=0o700)
    (study / "study.toml").write_text("[study]\n")
    (study / "campaign.toml").write_text("[scheduler]\nworkers = 1\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    args = argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False, archive_left=False,
                              snapshot_every=3600.0, home_files=str(tree))  # fmt: skip
    capsys.readouterr()
    assert run_step(shards, args, never_run) == shards.PRE_STEP_REFUSED
    assert "overlap" in capsys.readouterr().err
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_a_tree_below_a_directory_that_only_allows_a_search_is_read(shards, tmp_path):
    """O63: a directory above the tree needs search permission, not a read (a shared `0711` directory on a cluster):
    the tree below it is read by its known path."""
    shared = tmp_path / "shared"
    shared.mkdir()
    tree = key_tree(shared)
    shared.chmod(0o311)  # this user's: write and search, no read
    try:
        assert len(shards.read_home_files(str(tree)).files) == len(FILES)
    finally:
        shared.chmod(0o700)


@pytest.mark.parametrize("name", ["SIGUSR1", "SIGUSR2", "SIGQUIT", "SIGALRM"])
def test_o63_any_signal_that_would_end_the_step_ends_it_through_its_cleanup_while_keys_are_staged(
    shards, tmp_path, monkeypatch, name
):
    """O63: a scheduler's warning signal (USR1 and the like), whose default action ends the process outright, is a stop
    too while the key files are in the step's home: exit 128 + the signal, nothing left on the tmpfs."""
    sig = getattr(signal, name)
    tree = key_tree(tmp_path)
    before = {s: signal.getsignal(s) for s in shards.STOP_SIGNALS}
    real = shards.stage_home_files

    def staged_then_signalled(*args, **kwargs):
        result = real(*args, **kwargs)
        os.kill(os.getpid(), sig)
        return result

    monkeypatch.setattr(shards, "stage_home_files", staged_then_signalled)
    with pytest.raises(SystemExit) as stopped:
        run_step(shards, step_args(tmp_path, tree), never_run)
    assert stopped.value.code == 128 + sig
    assert not list((tmp_path / "tmpfs").iterdir())
    assert {s: signal.getsignal(s) for s in before} == before


def test_o63_a_stop_that_lands_before_the_cleanup_holds_the_signals_still_leaves_no_key(shards, tmp_path, monkeypatch):
    """O63: on the way out of a shard that left no out/, a HUP that arrives before the cleanup has blocked the signals
    would cut the cleanup short; the step makes it whole before the stop goes on."""
    import contextlib

    tree = key_tree(tmp_path)

    @contextlib.contextmanager
    def late_hold():
        os.kill(os.getpid(), signal.SIGHUP)  # lands at once, before anything is held
        yield

    monkeypatch.setattr(shards, "_stop_held", late_hold)
    with pytest.raises(SystemExit) as stopped:
        run_step(shards, step_args(tmp_path, tree), lambda argv, env, cwd: 3)
    assert stopped.value.code == 128 + signal.SIGHUP
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_a_home_the_dispatcher_replaced_by_a_link_is_unlinked_never_walked(shards, tmp_path):
    """O63: a dispatcher (or a post-run script) that moves its home away and leaves a link in its place: the step's
    cleanup unlinks the link, does not walk it (and chmod what it leads to), and still removes its own directory."""
    tree = key_tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"

    def moves_its_home(argv, env, cwd):
        home = Path(env["HOME"])
        home.rename(elsewhere)
        home.symlink_to(elsewhere)
        return 3

    run_step(shards, step_args(tmp_path, tree), moves_its_home)
    assert not list((tmp_path / "tmpfs").iterdir())
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o700  # what the link led to was not touched


# -- a kept step directory: the keys of a step killed after its dispatcher ended ---------------------------------------


def kept_with_keys(shards, tmp_path, monkeypatch, *, gone=True):
    """The directory a step leaves when its archive could not be written (phase `archiving`, out/ whole), as a step
    killed in the window before it removed its own home would leave it: its record says `home_files`, and the home
    holds a key. The step is shown gone (its recorded start is not any process's), or, with `gone=False`, still alive
    (this process)."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path, key_tree(tmp_path))
    run, _ = dispatcher()
    with monkeypatch.context() as patch:
        patch.setattr(shards, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
        assert run_step(shards, args, run) == shards.ARCHIVE_FAILED
    (kept,) = (tmp_path / "tmpfs").glob("expcore-shard-fed-a.*")
    owner = json.loads((kept / "step.json").read_text())
    assert owner["phase"] == shards.ARCHIVING
    assert owner["home_files"] is True
    start = "0" if gone else shards._start_of(os.getpid())
    (kept / "step.json").write_text(json.dumps({**owner, "start": start, "pid": owner["pid"] if gone else os.getpid()}))
    (kept / "home" / ".zqsecrets" / "alpha-zq").mkdir(parents=True)
    (kept / "home" / ".zqsecrets" / "alpha-zq" / "key-zq").write_text("sk-zq-left-behind\n")
    return kept, args


def assert_keys_gone_out_kept(kept: Path) -> None:
    assert not (kept / "home").exists()
    assert (kept / "study" / "out" / "campaign.lock").exists()  # the only whole out/ is untouched
    assert (kept / "step.json").exists()
    assert json.loads((kept / "step.json").read_text())["phase"] == "archiving"


def test_o63_sweep_removes_the_keys_of_a_kept_step_directory_and_keeps_its_out(shards, tmp_path, monkeypatch, capsys):
    """O63: a step killed after its dispatcher ended and before it removed its home leaves a directory of phase
    `archiving` whose home holds the keys; `sweep` (the federation's down), its step shown gone, removes the home and
    keeps the directory, its record and its out/ (still waiting for `--archive-left`)."""
    kept, _ = kept_with_keys(shards, tmp_path, monkeypatch)
    capsys.readouterr()
    assert shards.sweep(tmp_path / "tmpfs", "/fed/root") == 0
    out = capsys.readouterr()
    assert f"{kept.name} (its out/ was not archived" in out.out  # still named as kept, as before
    assert "zq" not in out.out + out.err
    assert_keys_gone_out_kept(kept)


def test_o63_a_resume_removes_the_keys_of_a_kept_directory_and_still_refuses(shards, tmp_path, monkeypatch):
    """O63: `--resume`'s look at the node refuses a kept out/ as before, and removes its key copies first."""
    kept, args = kept_with_keys(shards, tmp_path, monkeypatch)
    spec = {"index": 1, "count": 2, "label": "fed-a"}
    with pytest.raises(shards.ShardsError, match="was not archived and is kept on this node"):
        shards.clear_leftovers(tmp_path / "tmpfs", spec, "/fed/root", args.results)
    assert_keys_gone_out_kept(kept)


def test_o63_archive_left_removes_the_keys_before_it_archives(shards, tmp_path, monkeypatch):
    """O63: `--archive-left` removes the key copies as soon as its step is shown gone, so that an archive that fails
    again leaves none on the node."""
    kept, args = kept_with_keys(shards, tmp_path, monkeypatch)
    monkeypatch.setattr(shards, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
    with pytest.raises(OSError, match="No space left"):
        shards.archive_left(argparse.Namespace(**{**vars(args), "archive_left": True}))
    assert_keys_gone_out_kept(kept)


def test_o63_a_link_in_the_kept_homes_place_is_unlinked_never_walked(shards, tmp_path, monkeypatch):
    """O63: a link where the home was is unlinked; what it leads to is not walked or changed."""
    kept, _ = kept_with_keys(shards, tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    (kept / "home").rename(elsewhere)
    (kept / "home").symlink_to(elsewhere)
    assert shards.sweep(tmp_path / "tmpfs", "/fed/root") == 0
    assert not os.path.lexists(kept / "home")
    assert (elsewhere / ".zqsecrets" / "alpha-zq" / "key-zq").exists()  # not followed
    assert (kept / "study" / "out" / "campaign.lock").exists()


def test_o63_the_keys_of_a_step_that_may_still_run_are_not_touched(shards, tmp_path, monkeypatch):
    """O63: the same proof that lets a whole directory go is needed: a step shown alive keeps its home, and a record
    that does not say `home_files` has no home removed."""
    kept, _ = kept_with_keys(shards, tmp_path, monkeypatch, gone=False)
    assert shards.sweep(tmp_path / "tmpfs", "/fed/root") == 0
    assert (kept / "home" / ".zqsecrets" / "alpha-zq" / "key-zq").exists()
    owner = json.loads((kept / "step.json").read_text())
    del owner["home_files"]
    (kept / "step.json").write_text(json.dumps({**owner, "start": "0"}))  # the step is shown gone now
    assert shards.sweep(tmp_path / "tmpfs", "/fed/root") == 0
    assert (kept / "home" / ".zqsecrets" / "alpha-zq" / "key-zq").exists()  # no `home_files`: not ours to remove

"""O19: what a run left in its sandbox is copied out as root without following or keeping a link (#129).

The tests make real links, FIFOs, sockets and odd names in the test's own directories. The fake host runs the real
`find`, `cp` and `chown` on them, as this user and without sudo, and one test copies with the real host's own commands
(`DockerHost` with no privilege command). Nothing outside an output may be copied, changed or readable through the copy.
"""

import dataclasses
import json
import os
import socket
import stat
from pathlib import Path

import pytest
from campaign_support import STUDY_TOML, write_study
from sandbox_support import make_spec, start_options

from expcore.campaign.attempts import classify
from expcore.campaign.executor import failure_result
from expcore.campaign.factors import load_factor_schema
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.sandbox import reap
from expcore_onedata.sandbox.hostops import CmdResult, DockerHost
from expcore_onedata.sandbox.sandbox import DockerSandbox

KIND = "unsafe_output"
SECRET = "host-only secret text"
ENDED = "2026-01-01T00:00:00Z"


def start(world, level="isolated"):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


@pytest.fixture
def outsider(tmp_path) -> Path:
    """A host file outside every sandbox, which nothing may copy, change or make readable."""
    path = tmp_path / "outside" / "private.txt"
    path.parent.mkdir()
    path.write_text(SECRET, encoding="utf-8")
    return path


def kinds_under(root: Path) -> dict[str, str]:
    """Every entry under `root` by its relative path and kind, by lstat: what a later reader of the copy finds."""
    found = {}
    for top, dirs, files in os.walk(root):  # never follows a link
        for name in dirs + files:
            mode = (Path(top) / name).lstat().st_mode
            kind = "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"
            found[str((Path(top) / name).relative_to(root))] = kind
    return found


def secret_readable(root: Path) -> bool:
    """Whether the secret can be read from anything under `root`, following every link as a careless reader would."""
    for top, _, files in os.walk(root, followlinks=True):
        for name in files:
            try:
                if SECRET in (Path(top) / name).read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
    return False


def refused_paths(sbx) -> list[tuple[str, str]]:
    return [(r["path"], r["what"]) for r in sbx.metadata()["unsafe_output"]["refused"]]


def refuse(world, sbx, dest, outputs):
    """Collect, require the set-aside, and return the error."""
    with pytest.raises(InfraError) as caught:
        sbx.collect(dest, outputs=outputs)
    assert caught.value.kind == KIND
    assert sbx.keep_evidence is True  # the disks stay, links and all, for an operator
    return caught.value


def test_o19_a_symbolic_link_to_a_host_file_is_refused_and_named_by_its_path_never_its_target(
    world, outsider, tmp_path
):
    """O19: a symbolic link left in an output and pointing at a host file is refused before anything is copied: the
    collection raises InfraError of kind unsafe_output, the record names the link by its path in the working directory
    and what it is, never its target, and nothing of the host file reaches the copy."""
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    results.mkdir()
    (results / "report.json").write_text("{}", encoding="utf-8")
    (results / "leakage").symlink_to(outsider)
    dest = tmp_path / "evidence"
    error = refuse(world, sbx, dest, ["results"])
    assert not (dest / "work").exists()  # nothing of the output was copied
    assert not secret_readable(dest)
    assert refused_paths(sbx) == [("results/leakage", "symbolic link")]
    written = (dest / "metadata.json").read_text(encoding="utf-8")
    assert json.loads(written)["unsafe_output"] == sbx.metadata()["unsafe_output"]
    for text in (written, str(error)):
        assert str(outsider) not in text
        assert "private.txt" not in text
    assert outsider.read_text(encoding="utf-8") == SECRET
    sbx.stop()
    assert (world.s.work_root / "_kept_evidence" / sbx.sid).is_dir()


@pytest.mark.parametrize("where", ["inside", "itself"])
def test_o19_a_link_to_another_directory_is_refused_in_the_output_or_as_the_output(world, tmp_path, where):
    """O19: a symbolic link to another run's directory, inside an output or as the output itself, is refused, and
    nothing of that directory is readable through the copy."""
    another = tmp_path / "another-run"
    another.mkdir()
    (another / "answer.json").write_text(SECRET, encoding="utf-8")
    sbx = start(world)
    work = sbx.dir / "work"
    if where == "inside":
        (work / "results").mkdir()
        (work / "results" / "neighbour").symlink_to(another, target_is_directory=True)
        expected = "results/neighbour"
    else:
        (work / "results").symlink_to(another, target_is_directory=True)
        expected = "results"
    dest = tmp_path / "evidence"
    refuse(world, sbx, dest, ["results"])
    assert refused_paths(sbx) == [(expected, "symbolic link")]
    assert not secret_readable(dest)
    sbx.stop()


def test_o19_a_link_on_the_way_to_an_output_is_refused_and_not_followed(world, tmp_path):
    """O19: a directory on the way to a named output that is a symbolic link would make the output's path lead out of
    the working directory: it is refused, never followed, and never reported as a missing output."""
    another = tmp_path / "another-run"
    another.mkdir()
    (another / "answer.json").write_text(SECRET, encoding="utf-8")
    sbx = start(world)
    (sbx.dir / "work" / "results").symlink_to(another, target_is_directory=True)
    dest = tmp_path / "evidence"
    refuse(world, sbx, dest, ["results/answer.json"])
    assert refused_paths(sbx) == [("results", "symbolic link")]
    assert not secret_readable(dest)
    sbx.stop()


def test_o19_a_dangling_link_as_an_output_is_refused_not_reported_missing(world, tmp_path):
    """O19: an output that is a link to nothing is refused like any link; it is not an output the agent never wrote."""
    sbx = start(world)
    (sbx.dir / "work" / "report.json").symlink_to(tmp_path / "nowhere")
    refuse(world, sbx, tmp_path / "evidence", ["report.json"])
    assert refused_paths(sbx) == [("report.json", "symbolic link")]
    sbx.stop()


@pytest.mark.parametrize("target", ["host", "scratch"])
def test_o19_a_hard_link_to_a_file_outside_the_output_is_refused_and_one_inside_it_is_not(
    world, outsider, tmp_path, target
):
    """O19: a hard link in an output to a file outside it, a host file or the agent's own scratch, is refused, and the
    file it links to is left as it was. Two names of one file, both in the output, are no link out of it: each is
    copied as a file of its own, with one link."""
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    results.mkdir()
    (results / "first.txt").write_text("shared", encoding="utf-8")
    os.link(results / "first.txt", results / "second.txt")
    got = sbx.collect(tmp_path / "evidence", outputs=["results"])
    assert got["outputs"] == {"results": True}
    for name in ("first.txt", "second.txt"):
        copied = tmp_path / "evidence" / "work" / "results" / name
        assert copied.read_text(encoding="utf-8") == "shared"
        assert copied.lstat().st_nlink == 1
    sbx.stop()

    sbx = start(world)
    work = sbx.dir / "work"
    (work / "results").mkdir()
    if target == "host":
        source = outsider
    else:
        (work / "scratch").mkdir()
        source = work / "scratch" / "notes.txt"
        source.write_text(SECRET, encoding="utf-8")
    before = source.stat()
    os.link(source, work / "results" / "borrowed.txt")
    dest = tmp_path / "second"
    refuse(world, sbx, dest, ["results"])
    assert refused_paths(sbx) == [("results/borrowed.txt", "hard link to a file outside the output")]
    assert not secret_readable(dest)
    after = source.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    sbx.stop()


def test_o19_a_fifo_is_refused_and_the_collection_does_not_wait_on_it(world, tmp_path):
    """O19: a FIFO in an output is refused, before anything opens it."""
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    results.mkdir()
    os.mkfifo(results / "pipeline")
    refuse(world, sbx, tmp_path / "evidence", ["results"])
    assert refused_paths(sbx) == [("results/pipeline", "FIFO")]
    sbx.stop()


def test_o19_a_socket_is_refused(world, tmp_path, monkeypatch):
    """O19: a socket in an output is refused (a real one where the test may make it; the listing test below shows one
    everywhere)."""
    try:
        listener = socket.socket(socket.AF_UNIX)
    except PermissionError:
        pytest.skip("this environment forbids unix sockets; test_o19_a_device_or_socket_is_refused covers the kind")
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    results.mkdir()
    monkeypatch.chdir(results)  # a socket's path is short; bind it by its name in the directory
    try:
        listener.bind("listener")
        refuse(world, sbx, tmp_path / "evidence", ["results"])
    finally:
        listener.close()
    assert refused_paths(sbx) == [("results/listener", "socket")]
    sbx.stop()


def listed_as(host, name: str, kind: str) -> None:
    """Make the real listing show the entry called `name` as another kind: a device, which a test cannot make. The
    listing gives each entry's kind first and its name last."""

    def find(argv):
        listing = host.real(argv)
        records = [kind + r[1:] if r.endswith(" " + name) else r for r in listing.stdout.split("\0")]
        return CmdResult(listing.returncode, "\0".join(records), listing.stderr)

    host.run_handlers["find"] = find


@pytest.mark.parametrize(
    ("kind", "what"), [("c", "character device"), ("b", "block device"), ("s", "socket"), ("D", "door")]
)
def test_o19_a_device_or_socket_is_refused(world, tmp_path, kind, what):
    """O19: a device node, a socket or a door in an output is refused (the listing is made to show one: a test cannot
    make a device, nor everywhere a socket)."""
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    results.mkdir()
    (results / "console").write_text("", encoding="utf-8")
    listed_as(world.host, "console", kind)
    dest = tmp_path / "evidence"
    refuse(world, sbx, dest, ["results"])
    assert refused_paths(sbx) == [("results/console", what)]
    assert not (dest / "work").exists()
    sbx.stop()


def test_o19_a_deep_tree_is_copied_whole_and_a_path_too_long_to_copy_is_refused(world, tmp_path):
    """O19: a tree a hundred directories deep is copied whole, every entry a plain directory or file; a path longer
    than the copy allows is refused, never cut short or half copied."""
    sbx = start(world)
    work = sbx.dir / "work"
    deep = work / "results" / Path(*["level"] * 100)
    deep.mkdir(parents=True)
    (deep / "bottom.txt").write_text("found", encoding="utf-8")
    got = sbx.collect(tmp_path / "evidence", outputs=["results"])
    assert got["outputs"] == {"results": True}
    copied = kinds_under(tmp_path / "evidence" / "work" / "results")
    assert len(copied) == 101
    assert set(copied.values()) == {"dir", "file"}
    assert (tmp_path / "evidence" / "work" / "results" / Path(*["level"] * 100) / "bottom.txt").read_text() == "found"
    sbx.stop()

    sbx = start(world)
    longest = sbx.dir / "work" / "lengthy" / Path(*["n" * 200] * 12)
    longest.mkdir(parents=True)
    (longest / "bottom.txt").write_text("found", encoding="utf-8")
    refuse(world, sbx, tmp_path / "second", ["lengthy"])
    assert {what for _, what in refused_paths(sbx)} == {"path longer than 2048 bytes"}
    assert not (tmp_path / "second" / "work").exists()
    sbx.stop()


def test_o19_names_with_newlines_and_leading_dashes_are_copied_exactly_and_nothing_else(world, outsider, tmp_path):
    """O19: a name with a newline, or one that starts with dashes and reads as an option, is copied under its own name
    with its own content, in an output whose own name starts with a dash; nothing outside is touched."""
    sbx = start(world)
    output = sbx.dir / "work" / "-dashed"
    (output / "-R").mkdir(parents=True)
    names = ["-rf", "--help", "--no-preserve=all", "line\nbreak.txt", "-R/--target-directory=elsewhere"]
    for name in names:
        (output / name).write_text(f"content of {name}", encoding="utf-8")
    got = sbx.collect(tmp_path / "evidence", outputs=["-dashed"])
    assert got["outputs"] == {"-dashed": True}
    copied = tmp_path / "evidence" / "work" / "-dashed"
    assert kinds_under(copied) == {"-R": "dir", **dict.fromkeys(names, "file")}
    for name in names:
        assert (copied / name).read_text(encoding="utf-8") == f"content of {name}"
    assert outsider.read_text(encoding="utf-8") == SECRET
    assert sorted(p.name for p in outsider.parent.iterdir()) == ["private.txt"]
    sbx.stop()

    sbx = start(world)  # an odd name is no cover for a link: it is refused under its exact name
    (sbx.dir / "work" / "-dashed").mkdir()
    (sbx.dir / "work" / "-dashed" / "--line\nlink").symlink_to(outsider)
    refuse(world, sbx, tmp_path / "second", ["-dashed"])
    assert refused_paths(sbx) == [("-dashed/--line\nlink", "symbolic link")]
    assert not secret_readable(tmp_path / "second")
    sbx.stop()


def test_o19_a_sparse_file_bigger_than_the_working_disk_is_refused(world, tmp_path):
    """O19: an output whose bytes exceed the working disk's quota can only be sparse: it is refused before a copy
    writes them out, so a reader of the copy never meets a file bigger than the disk the agent had."""
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    results.mkdir()
    with open(results / "inflated.bin", "wb") as handle:
        handle.truncate(world.s.limits.work_disk_bytes + 1)
    refuse(world, sbx, tmp_path / "evidence", ["results"])
    assert refused_paths(sbx) == [("results", "more bytes than the working disk holds")]
    assert not (tmp_path / "evidence" / "work").exists()
    sbx.stop()


def test_o19_a_clean_output_is_the_drivers_own_and_no_command_dereferences(world, tmp_path):
    """O19: a clean output is copied, owned by the driver, every file with one link, and an output named inside it is
    copied with it; the listing, the copy and the change of owner are each told never to follow a link."""
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    (results / "nested").mkdir(parents=True)
    (results / "nested" / "report.json").write_text("{}", encoding="utf-8")
    got = sbx.collect(tmp_path / "evidence", outputs=["results/nested/report.json", "results"])
    assert got["outputs"] == {"results/nested/report.json": True, "results": True}
    copied = tmp_path / "evidence" / "work" / "results"
    for path in (copied, copied / "nested", copied / "nested" / "report.json"):
        info = path.lstat()
        assert (info.st_uid, info.st_gid) == (os.getuid(), os.getgid())
    assert (copied / "nested" / "report.json").lstat().st_nlink == 1
    commands = {argv[0]: argv for argv in world.host.sudo_commands if argv[0] in ("find", "cp", "chown")}
    assert "-P" in commands["find"]
    assert {"-R", "-P"} <= set(commands["cp"])
    assert {"-R", "-P", "-h"} <= set(commands["chown"])
    assert "--dereference" not in commands["chown"]
    sbx.stop()


def test_o19_a_copy_that_holds_a_link_after_all_is_removed_and_refused(world, outsider, tmp_path):
    """O19: the copy is verified after it is made: one that holds a link however it came there (here a copy command
    that leaves one) is removed and refused, never handed on."""
    sbx = start(world)
    results = sbx.dir / "work" / "results"
    results.mkdir()
    (results / "report.json").write_text("{}", encoding="utf-8")

    def copies_and_links(argv):
        done = world.host.real(argv)
        (Path(argv[-1]) / "planted").symlink_to(outsider)
        return done

    world.host.run_handlers["cp"] = copies_and_links
    dest = tmp_path / "evidence"
    refuse(world, sbx, dest, ["results"])
    assert not (dest / "work" / "results").exists()
    assert not secret_readable(dest)
    sbx.stop()


def test_o19_a_gate_log_replaced_by_a_link_is_refused_too(world, tmp_path):
    """O19: the gate's log, copied out as root like the outputs, is held to the same rule: a link there is refused and
    the file it points at is not copied."""
    elsewhere = tmp_path / "outside" / "elsewhere.jsonl"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps({"note": SECRET}) + "\n", encoding="utf-8")
    sbx = start(world, "via-gate")
    log = sbx.dir / "gatelog" / "requests.jsonl"
    log.unlink()
    log.symlink_to(elsewhere)
    dest = tmp_path / "evidence"
    refuse(world, sbx, dest, [])
    assert sbx.metadata()["unsafe_output"]["refused"] == [
        {"of": "gate log", "path": "requests.jsonl", "what": "symbolic link"}
    ]
    assert not (dest / "gate-requests.jsonl").exists()
    assert not secret_readable(dest)
    sbx.stop()


def test_o19_c01_a_refused_output_is_set_aside_even_where_sandbox_failures_are_the_treatment(world, outsider, tmp_path):
    """O19, C01: a refused collection says nothing about the agent or the manipulation: under a condition that declares
    `sandbox` its treatment the run is still infrastructure, set aside and never scored."""
    sbx = start(world)
    (sbx.dir / "work" / "report.json").symlink_to(outsider)
    error = refuse(world, sbx, tmp_path / "evidence", ["report.json"])
    declared = ("sandbox",)
    result = dataclasses.replace(
        failure_result(error, ended_utc=ENDED, wall_s=1.0, treatment_kinds=declared), model_turns=3
    )
    assert classify(result, declared) == ("infra_fail", True, False)
    sbx.stop()


def test_o19_declaring_unsafe_output_as_treatment_is_refused_at_load(tmp_path):
    """O19: an agent can leave a link on purpose, so no condition may declare the kind its refusal ends in."""
    import expcore_onedata.sandbox.kinds  # noqa: F401  the platform registers its kinds when it is imported

    declared = STUDY_TOML.replace(
        '[conditions.api-none]\ndata_path = "api"\ntools = "none"',
        f'[conditions.api-none]\ndata_path = "api"\ntools = "none"\ninfrastructure_is_treatment = ["{KIND}"]',
    )
    assert declared != STUDY_TOML
    with pytest.raises(ConfigError) as caught:
        load_factor_schema(write_study(tmp_path, declared))
    assert f"names infrastructure kind '{KIND}', which can never be a treatment" in str(caught.value)


@pytest.fixture
def dead_run(world):
    """A directory of a run whose owner is gone: a work disk with an output, a gate log, and no container."""
    directory = world.s.work_root / "lessee-0123abcd"
    (directory / "work" / "results").mkdir(parents=True)
    (directory / "work" / "results" / "report.json").write_text("{}", encoding="utf-8")
    (directory / "gatelog").mkdir()
    (directory / "gatelog" / "requests.jsonl").write_text("{}\n", encoding="utf-8")
    os.utime(directory, (1_000_000_000, 1_000_000_000))
    return directory


@pytest.mark.parametrize("planted", ["symlink", "fifo"])
def test_o19_the_reaper_refuses_a_dead_runs_link_or_fifo_and_keeps_its_disks(
    world, dead_run, outsider, tmp_path, planted
):
    """O19: the reaper copies a dead run's outputs out as root too, under the same rule: a link or a FIFO in them is
    refused, nothing reaches the kept copy, and the run's disks stay for an operator."""
    results = dead_run / "work" / "results"
    if planted == "symlink":
        (results / "leakage").symlink_to(outsider)
    else:
        os.mkfifo(results / "pipeline")
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    assert reap.reap_run(world.host, world.s, "lessee", log_dir=log_dir, outputs=["results"]) is False
    assert dead_run.is_dir()
    assert not secret_readable(log_dir)
    assert "other" not in kinds_under(log_dir).values()


def test_o19_the_real_hosts_commands_copy_a_clean_tree_and_refuse_a_link(tmp_path, outsider):
    """O19: with the real host's own commands (`DockerHost`, here with no privilege command), a clean tree with odd
    names is copied exactly and a tree holding a link is refused with nothing copied."""
    from expcore_onedata.sandbox import copyout

    host = DockerHost(sudo_command=())
    root = tmp_path / "root"
    (root / "results" / "-R").mkdir(parents=True)
    (root / "results" / "line\nbreak.txt").write_text("odd", encoding="utf-8")
    (root / "results" / "-R" / "--help").write_text("dashed", encoding="utf-8")
    dest = tmp_path / "dest"
    got = copyout.copy_tree(host, root, [("results", dest / "results")], max_bytes=1 << 20)
    assert got == {"results": {"files": 2, "bytes": 9}}
    assert kinds_under(dest / "results") == {"-R": "dir", "-R/--help": "file", "line\nbreak.txt": "file"}
    (root / "linked").mkdir()
    (root / "linked" / "leakage").symlink_to(outsider)
    with pytest.raises(copyout.UnsafeOutput) as caught:
        copyout.copy_tree(host, root, [("linked", dest / "linked")], max_bytes=1 << 20)
    assert [(r.path, r.what) for r in caught.value.refused] == [("linked/leakage", "symbolic link")]
    assert not (dest / "linked").exists()

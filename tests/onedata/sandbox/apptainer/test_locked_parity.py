"""O71: what the agent locked in `/work` is collected the same on Docker and on the Apptainer backend, on both of its
work disks (the directory and the image).

On Docker the copy helper runs as root, which reads a mode-000 entry whatever its mode, so the output is collected
whole. On the Apptainer backend the helper runs as the agent's own uid, which gives the owner back the read bit of what
it owns first. The same tree must give the same outcome and the same collected bytes on all three. Where an entry
cannot be read at all (here, a helper that is not the owner), the three refuse it the same way, for the scorers.
"""

import os
from pathlib import Path

import pytest
from apptainer_support import make_settings
from apptainer_support import start as start_apptainer
from fake_node import FakeNode
from locked_work import as_root, lock, locked_afterwards, needs_unprivileged, not_the_owner
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import copyout
from expcore_onedata.sandbox.apptainer import workdisk
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

pytestmark = needs_unprivileged

TREE = {
    "out/ok.txt": "ok\n",
    "out/secret.txt": "secret\n",
    "out/locked/inner.txt": "inner\n",
    "out/locked/deep/x.txt": "x\n",
}  # the agent locks secret.txt, the directory locked and the directory deep in it


@pytest.fixture(autouse=True)
def _unlocked_afterwards(tmp_path):
    yield from locked_afterwards(lambda: tmp_path)


def _leave_locked(work: Path, linked: bool = False) -> None:
    """What the agent leaves in `/work`: the tree, with a file and two directories locked; with `linked`, a readable
    file and a locked one that has a second name instead."""
    if linked:
        (work / "out").mkdir(parents=True, exist_ok=True)
        (work / "out" / "ok.txt").write_text("ok\n", encoding="utf-8")
        (work / "out" / "a").write_text("a\n", encoding="utf-8")
        os.link(work / "out" / "a", work / "out" / "b")
        lock(work / "out" / "a")
        return
    for rel, text in TREE.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    lock(work / "out" / "secret.txt")
    lock(work / "out" / "locked" / "deep")
    lock(work / "out" / "locked")


def _collected(dest: Path) -> dict[str, bytes]:
    """The bytes of every file collected under `out`, by relative path."""
    root = dest / "work"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _outcome(done: dict, dest: Path) -> dict:
    return {"outputs": done["outputs"], "refused_output": done.get("refused_output"), "bytes": _collected_or_none(dest)}


def _collected_or_none(dest: Path) -> dict[str, bytes] | None:
    return _collected(dest) if (dest / "work").exists() else None


def _docker(world, tmp_path, scenario: str) -> dict:
    sbx = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    try:
        _leave_locked(sbx.dir / "work", scenario == "linked")
        with pytest.MonkeyPatch.context() as patch:
            source = copyout._helper_source()
            if scenario == "unreadable":
                patch.setattr(copyout, "_helper_source", lambda: not_the_owner(source))
            elif scenario == "linked":  # Docker's helper is root
                patch.setattr(copyout, "_helper_source", lambda: as_root(source))
            done = sbx.collect(tmp_path / "docker", outputs=["out"])
        return _outcome(done, tmp_path / "docker")
    finally:
        sbx.stop()


def _apptainer_directory(tmp_path, scenario: str) -> dict:
    (tmp_path / "directory").mkdir()
    box = start_apptainer(FakeNode(), make_settings(tmp_path / "directory"))
    try:
        _leave_locked(box.dir / "work", scenario == "linked")
        with pytest.MonkeyPatch.context() as patch:
            if scenario == "unreadable":
                source = copyout._helper_source()
                patch.setattr(copyout, "_helper_source", lambda: not_the_owner(source))
            done = box.collect(tmp_path / "apptainer-directory", ["out"])
        return _outcome(done, tmp_path / "apptainer-directory")
    finally:
        box.stop()


def _apptainer_image(tmp_path, scenario: str) -> dict:
    (tmp_path / "image").mkdir()
    node = FakeNode()
    box = start_apptainer(node, make_settings(tmp_path / "image", work_disk="image"))
    try:
        node.cell_handler = lambda cell: (_leave_locked(cell.work_dir(), scenario == "linked"), CmdResult(0, ""))[1]
        box.exec("leave()", 30)
        with pytest.MonkeyPatch.context() as patch:
            if scenario == "unreadable":
                source = workdisk.wrapper_source()
                patch.setattr(workdisk, "wrapper_source", lambda: not_the_owner(source))
            done = box.collect(tmp_path / "apptainer-image", ["out"])
        return _outcome(done, tmp_path / "apptainer-image")
    finally:
        box.stop()


def _all_three(world, tmp_path, scenario: str) -> dict[str, dict]:
    return {
        "docker": _docker(world, tmp_path, scenario),
        "apptainer directory": _apptainer_directory(tmp_path, scenario),
        "apptainer image": _apptainer_image(tmp_path, scenario),
    }


def test_o71_the_same_locked_tree_is_collected_whole_the_same_on_docker_and_on_both_apptainer_work_disks(
    world, tmp_path
):
    """O71: a mode-000 file and a mode-000 directory with entries: the same outcome (collected, nothing refused) and
    the same bytes on Docker, the Apptainer directory disk and its image, which is what Docker's root helper gives."""
    found = _all_three(world, tmp_path, "whole")
    expected = {
        "outputs": {"out": True},
        "refused_output": None,
        "bytes": {rel: text.encode() for rel, text in TREE.items()},
    }
    assert found == {"docker": expected, "apptainer directory": expected, "apptainer image": expected}


def test_o71_what_no_helper_can_read_is_refused_the_same_on_docker_and_on_both_apptainer_work_disks(world, tmp_path):
    """O71, O19: where the helper cannot give the owner's bits back, each backend names the same entries for the
    scorers as `entry that cannot be read`, copies none of the outputs and ends normally (never a set-aside)."""
    found = _all_three(world, tmp_path, "unreadable")
    refused = {
        "refused": [
            {"path": "out/locked", "what": copyout.UNREADABLE},
            {"path": "out/secret.txt", "what": copyout.UNREADABLE},
        ],
        "count": 2,
        "by_what": {copyout.UNREADABLE: 2},
    }
    expected = {"outputs": {"out": False}, "refused_output": refused, "bytes": None}
    assert found == {"docker": expected, "apptainer directory": expected, "apptainer image": expected}


def test_o71_a_locked_file_with_another_link_is_refused_the_same_on_docker_and_on_both_apptainer_work_disks(
    world, tmp_path
):
    """O71, O19: a mode change belongs to the inode, which another name may hold outside `/work`, so the Apptainer
    helper never restores a file with another link. Docker's root helper could read it, so the same file is refused
    there by its mode and links: the same outcome on all three, the agent's `entry that cannot be read`, none of the
    outputs copied, never a set-aside."""
    found = _all_three(world, tmp_path, "linked")
    refused = {
        "refused": [
            {"path": "out/a", "what": copyout.UNREADABLE},
            {"path": "out/b", "what": copyout.UNREADABLE},
        ],
        "count": 2,
        "by_what": {copyout.UNREADABLE: 2},
    }
    expected = {"outputs": {"out": False}, "refused_output": refused, "bytes": None}
    assert found == {"docker": expected, "apptainer directory": expected, "apptainer image": expected}

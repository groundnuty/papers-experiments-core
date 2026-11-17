"""O63: the collected-evidence marker (`hpcevidence.write_marker`, read by `down` through `check_collected`) names each
archived file by its root-relative path and accepts every name the archiver (`archive_root`) can produce: its own bounds
are a depth of `MAX_DEPTH` below a tree and names of up to 200 plain characters, so the longest path it holds is the
fault journal's, four nested directories and a file, 1,018 characters. A marker that refused one made `down` refuse
unchanged, successfully collected evidence, and collecting again could not help. Every name here is invented."""

import json

import pytest
from hpc_fed_support import hpc_on_path
from root_evidence_support import make_root, record_step

LABEL = "shard-1"
LONG = "d" * 200  # the longest plain name the archiver takes: `_PLAIN_NAME`
FILE_NAME = "f" * 200


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcevidence

        modules.evidence = hpcevidence
        yield modules


def _root_with(tmp_path, relative):
    """A collectable root (a step record and the evidence `make_root` leaves) with one more journal file."""
    root = make_root(tmp_path / "root")
    record_step(root, LABEL)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"event": "deep"}\n')
    return root


def test_o63_a_marker_accepts_the_longest_name_the_archiver_holds(hpc, tmp_path):
    """O63: from the archive of a root holding the longest fault-journal file the archiver allows (the tree, four
    directories of 200 characters and a file of 200, 1,018 characters), through `write_marker` to `check_collected`: the
    marker is current, so `down` is not refused for evidence that is collected and unchanged."""
    evidence = hpc.evidence
    relative = "/".join(["fault-journal", *([LONG] * evidence.MAX_DEPTH), FILE_NAME])
    assert len(relative) == 1018
    root = _root_with(tmp_path, relative)
    held = evidence.archive_root(root, LABEL)
    assert relative in held.hashes  # the archiver took it
    evidence.write_marker(root, LABEL, held.sha256, held.steps_sha256, held.hashes)
    check = evidence.check_collected(root)
    assert check.marked
    assert not check.refused


def test_o63_the_marker_of_such_a_root_is_refused_once_the_file_changes(hpc, tmp_path):
    """O63: the longer limit lets the name in, not the change: a strict file rewritten after the collect is named."""
    evidence = hpc.evidence
    relative = "/".join(["fault-journal", *([LONG] * evidence.MAX_DEPTH), FILE_NAME])
    root = _root_with(tmp_path, relative)
    held = evidence.archive_root(root, LABEL)
    evidence.write_marker(root, LABEL, held.sha256, held.steps_sha256, held.hashes)
    (root / relative).write_bytes(b'{"event": "changed after the collect"}\n')
    check = evidence.check_collected(root)
    assert check.marked
    assert check.refused
    assert check.changed == (relative,)


@pytest.mark.parametrize("depth", [5, 8])
def test_o63_a_marker_still_refuses_a_name_beyond_what_the_archiver_could_hold(hpc, tmp_path, depth):
    """O63: the limit is the archiver's own bound and no wider: a name nested deeper than `MAX_DEPTH` (which no archive
    holds) makes the marker unreadable, so a hand-edited marker cannot smuggle in an unbounded path."""
    evidence = hpc.evidence
    root = _root_with(tmp_path, "fault-journal/injections.jsonl")
    held = evidence.archive_root(root, LABEL)
    too_deep = "/".join(["fault-journal", *([LONG] * depth), FILE_NAME])
    evidence.write_marker(root, LABEL, held.sha256, held.steps_sha256, {**held.hashes, too_deep: "0" * 64})
    assert not evidence.check_collected(root).marked
    assert json.loads((root / evidence.COLLECTED).read_text())["files"][too_deep] == "0" * 64  # it was written as given

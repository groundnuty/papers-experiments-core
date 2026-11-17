"""The private working directory a tool server starts in (O84): new, empty, owner-only, gone with what is left in it."""

import os
import stat

import pytest
from expcore.tools.workdir import private_workdir


def test_o84_a_private_directory_is_new_empty_and_owner_only_whatever_the_umask():
    """O84: each use makes a directory that holds nothing, belongs to the caller and has mode 0700 even when the
    process's umask would allow more, and that is not the working directory."""
    before = os.umask(0)
    try:
        with private_workdir() as path:
            info = path.stat()
            assert list(path.iterdir()) == []
            assert stat.S_IMODE(info.st_mode) == stat.S_IRWXU
            assert info.st_uid == os.getuid()
            assert path.resolve() != os.path.realpath(os.getcwd())
    finally:
        os.umask(before)


def test_o84_each_use_gets_a_directory_of_its_own_and_it_is_removed_with_what_was_left_in_it():
    """O84: a second use is another directory, and leaving removes the directory with a file and a subdirectory the
    server wrote in it."""
    made = []
    for _ in range(2):
        with private_workdir() as path:
            (path / "left.txt").write_text("x", encoding="utf-8")
            (path / "sub").mkdir()
            (path / "sub" / "deeper").write_text("y", encoding="utf-8")
            made.append(path)
    assert made[0] != made[1]
    assert not any(path.exists() for path in made)


def test_o84_the_directory_is_removed_when_the_body_fails():
    """O84: an error in the body does not leave the directory behind, and is not swallowed."""
    made = []

    def fail_inside():
        with private_workdir() as path:
            made.append(path)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        fail_inside()
    assert made
    assert not made[0].exists()

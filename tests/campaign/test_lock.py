"""One dispatcher per store, by a non-blocking exclusive lock (C07)."""

import signal
import subprocess
import sys
import textwrap

import pytest

from expcore.campaign.lock import (
    CampaignLocked,
    DispatchLock,
    NetworkFilesystemWarning,
    filesystem_type,
    is_network_fs,
)


def test_c07_a_second_dispatcher_on_the_same_store_is_refused_at_once(tmp_path):
    """C07: a second dispatcher on the same store exits, without waiting for the first."""
    with DispatchLock(tmp_path), pytest.raises(CampaignLocked, match="one dispatcher per store"):
        DispatchLock(tmp_path).acquire()
    again = DispatchLock(tmp_path).acquire()  # released on exit, so a later dispatcher may take it
    assert again.held
    again.release()
    again.release()  # releasing twice does nothing
    assert not again.held


def test_c07_a_killed_dispatchers_lock_is_dropped_by_the_kernel(tmp_path):
    """C07: a dispatcher killed without releasing leaves nothing to clean up."""
    code = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from expcore.campaign.lock import DispatchLock
        DispatchLock(Path(sys.argv[1])).acquire()
        print("held", flush=True)
        sys.stdin.read()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(CampaignLocked):
            DispatchLock(tmp_path).acquire()
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        DispatchLock(tmp_path).acquire().release()
    finally:
        child.kill()
        child.wait()


def test_c07_a_store_on_a_network_file_system_starts_with_a_warning(tmp_path):
    """C07: warn on network file systems, where the lock is unreliable across hosts."""
    mounts = tmp_path / "mounts"
    real = str(tmp_path.resolve())
    mounts.write_text(f"rootfs / ext4 rw 0 0\nserver:/export {real} nfs4 rw 0 0\n", encoding="utf-8")
    assert filesystem_type(tmp_path / "campaign.db", mounts) == "nfs4"
    with pytest.warns(NetworkFilesystemWarning, match="nfs4"), DispatchLock(tmp_path, mounts=mounts):
        pass


def test_c07_a_local_file_system_gives_no_warning(tmp_path, recwarn):
    """C07: no warning on a local file system."""
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/disk {tmp_path.resolve()} ext4 rw 0 0\n", encoding="utf-8")
    with DispatchLock(tmp_path, mounts=mounts):
        pass
    assert not [w for w in recwarn if issubclass(w.category, NetworkFilesystemWarning)]


@pytest.mark.parametrize(
    ("fs_type", "network"),
    [
        ("nfs", True),
        ("nfs4", True),
        ("ceph", True),
        ("fuse.ceph-fuse", True),
        ("ext4", False),
        ("tmpfs", False),
        (None, False),
    ],
)
def test_c07_which_file_systems_count_as_network(fs_type, network):
    """C07: the network types are named, and an unknown type is not guessed to be one."""
    assert is_network_fs(fs_type) is network


def test_c07_an_unreadable_mount_table_gives_no_type(tmp_path):
    """C07: a missing table is not an error and not a network file system."""
    assert filesystem_type(tmp_path, tmp_path / "absent") is None

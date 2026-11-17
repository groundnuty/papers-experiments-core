"""`down`'s removal of an HPC federation's root on a shared filesystem (deploy/hpc-federation/hpcdown.py and
hpclaunch.StateStore.lifecycle_path, DESIGN.md section 5). On NFS a file held open while it is removed becomes a
silly-rename file (`.nfsXXXX`) until its last close, so a directory holding one is not empty: the first live `down`
held its lifecycle lock inside the root it removed, failed with ENOTEMPTY once the marker was gone, and a second `down`
refused the unmarked root. The lock now lies beside the root, the removal waits a bounded time for silly files to go
and keeps the marker to the end, and a `down` over the remains of a removal cut short finishes it. `NfsStandIn` gives
this host's filesystem an NFS client's unlink, from the file handles this process really holds."""

import errno
import os
import secrets
import stat
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


class NfsStandIn:
    """An NFS client's unlink and rmdir on this host's own filesystem. A regular file that a descriptor of this
    process still holds is renamed to `.nfs<hex>` in its directory instead of removed, and goes at its last close (here:
    at the next call, or the next sleep, that finds it held no more); unlinking such a file while it is held fails with
    EBUSY; a directory holding one is not empty. `sleep` advances a clock and runs `on_sleep`, as time passing does."""

    def __init__(self) -> None:
        self.silly: list[str] = []
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = lambda count: None

    @staticmethod
    def held(path) -> bool:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        for fd in os.listdir("/proc/self/fd"):
            try:
                mine = os.fstat(int(fd))
            except OSError:
                continue
            if (mine.st_dev, mine.st_ino) == (st.st_dev, st.st_ino):
                return True
        return False

    def collect(self) -> None:
        """Every silly file whose last handle has closed is gone, as the client removes it at that close."""
        for path in list(self.silly):
            if not self.held(path):
                Path(path).unlink(missing_ok=True)
                self.silly.remove(path)

    def unlink(self, path) -> None:
        self.collect()
        path = os.fspath(path)
        if self.held(path):
            if os.path.basename(path).startswith(".nfs"):
                raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), path)
            renamed = os.path.join(os.path.dirname(path), f".nfs{secrets.token_hex(12)}")
            os.rename(path, renamed)
            self.silly.append(renamed)
            return
        os.unlink(path)

    def rmdir(self, path) -> None:
        self.collect()
        os.rmdir(path)

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        self.on_sleep(self.sleeps)
        self.collect()

    def ops(self) -> dict:
        return {"unlink": self.unlink, "rmdir": self.rmdir, "sleep": self.sleep, "clock": lambda: self.now}


class Held:
    """A file held open by a descriptor of this process until `close`, which may come more than once."""

    def __init__(self, path: Path) -> None:
        self.fd: int | None = os.open(path, os.O_RDONLY)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Rig:
    """A driver over the fake Slurm, the core's phase recorded instead of run, its root's removal on NFS."""

    def __init__(self, hpc, tmp_path, site) -> None:
        self.hpc = hpc
        self.fake = FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.site = site
        self.nfs = NfsStandIn()
        self.driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
        self.driver.sleep = self.driver.slurm.sleep = lambda s: None
        self.driver.poll_s = self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False
        self.driver.removal = self.nfs.ops()

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    @property
    def lock(self) -> Path:
        return self.driver.store.lifecycle_path


def test_the_lifecycle_lock_lies_beside_the_root_and_down_on_nfs_removes_the_root(hpc, tmp_path, site):
    """O57: up's and down's lifecycle lock is `<root>.lifecycle.lock` beside the root, never in it: on NFS, down then
    holds nothing open in the root it removes, which goes whole; the lock file stays, never unlinked (a lock unlinked
    while held would let the next up or down lock another file of the same name)."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    assert rig.lock == rig.root.parent / "root.lifecycle.lock"
    assert rig.lock.is_file()
    assert not list(rig.root.glob("*lifecycle*"))
    assert "is gone, and so is the root" in rig.driver.down()
    assert not rig.root.exists()
    assert rig.nfs.silly == []
    assert rig.lock.is_file()


def test_a_lock_held_inside_the_root_is_what_failed_live_and_a_second_down_finishes(hpc, tmp_path, site, monkeypatch):
    """O57: with the lock back inside the root, as the first live slot ran it, the stand-in fails as the site did:
    the held lock becomes a silly file and the root is not empty. The removal waits its bound, then says the job is gone
    and keeps the marker, so a second down (the lock beside the root again) finishes the removal instead of refusing."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    store = type(rig.driver.store)
    beside = store.lifecycle_path
    monkeypatch.setattr(store, "lifecycle_path", property(lambda s: s.root / "hpc-federation.lifecycle.lock"))
    cut_short = r"^the held job 4242 is gone \(shards: sweep: .*\), but the root could not be removed"
    with pytest.raises(RuntimeError, match=cut_short) as raised:
        rig.driver.down()
    assert "1 NFS silly-rename file(s)" in str(raised.value)
    assert rig.fake.cancelled == ["4242"]
    assert rig.driver.layout.owned  # the marker went last, so the root still reads as this federation's
    monkeypatch.setattr(store, "lifecycle_path", beside)
    assert "no job of up's is left to cancel; the root removed" in rig.driver.down()
    assert not rig.root.exists()
    assert rig.fake.cancelled == ["4242"]


def test_a_file_another_process_holds_in_the_root_is_waited_for_and_the_root_goes(hpc, tmp_path, site):
    """O57: a file of the root held open elsewhere while down removes it (a lock file of the journal, say: one with
    nothing in it, which is no evidence a run left, O63) becomes a silly file; the removal waits for its holder to close
    it, then removes its directory and the root."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    journal = rig.driver.layout.journal / "held.lock"
    journal.write_text("", encoding="utf-8")
    handle = Held(journal)
    rig.nfs.on_sleep = lambda count: handle.close() if count == 3 else None
    try:
        rig.driver.down()
    finally:
        handle.close()
    assert not rig.root.exists()
    assert rig.nfs.sleeps == 3


def test_down_over_an_unmarked_root_of_nothing_but_silly_files_finishes_its_removal(hpc, tmp_path, site):
    """O57: a removal cut short after its marker (the root itself not yet removable) leaves an unmarked root of nothing
    but silly files: a down beside its lifecycle lock removes the root once they go, and removes nothing in it."""
    rig = Rig(hpc, tmp_path, site)
    rig.root.mkdir()
    rig.lock.touch()
    held = rig.root / "state.part"
    held.write_text("x", encoding="utf-8")
    handle = Held(held)
    rig.nfs.unlink(held)
    (silly,) = rig.nfs.silly
    rig.nfs.on_sleep = lambda count: handle.close() if count == 2 else None
    rig.driver.removal["unlink"] = lambda path: pytest.fail(f"a remains' file was removed: {path}")
    try:
        assert "is removed" in rig.driver.down()
    finally:
        handle.close()
    assert not rig.root.exists()
    assert not Path(silly).exists()


@pytest.mark.parametrize("left", ["a file", "nothing, and no lock"])
def test_down_still_refuses_an_unmarked_root_that_is_not_a_removals_remains(hpc, tmp_path, site, left):
    """O57: an unmarked root holding anything but silly files, or one with no lifecycle lock beside it (never this
    driver's), is refused as before, and nothing in it is touched."""
    rig = Rig(hpc, tmp_path, site)
    rig.root.mkdir()
    if left == "a file":
        rig.lock.touch()
        (rig.root / "data.bin").write_bytes(b"not ours")
    with pytest.raises(RuntimeError, match="not marked as this federation's"):
        rig.driver.down()
    assert rig.root.is_dir()
    assert [p.name for p in rig.root.iterdir()] == (["data.bin"] if left == "a file" else [])

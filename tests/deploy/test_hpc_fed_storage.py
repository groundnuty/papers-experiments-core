"""The provider storage an HPC federation's `up` makes on the cluster's shared scratch filesystem, and `down`'s removal
of it (deploy/hpc-federation/hpcstorage.py, DESIGN.md section 11). `up` makes one directory of its own below the site's
`HPC_STORAGE_ROOT` and one in it per provider, each by an exclusive `mkdir` that never adopts what is there, with an
owner marker in the first, and records them in the state file before it makes any; `down` removes exactly those, only
once the job and every service are gone, and shows by a count that nothing of them remains, failing loudly otherwise.
Nothing `up` did not make is ever removed. Over the fake Slurm of hpc_fed_support and a temporary directory for
scratch."""

import json
import os
import stat
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from hpc_import_support import IMPORT_LAYOUT, make_environ

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]
PATTERN = r"inodes used (?P<used>[0-9,]+) of (?P<quota>[0-9,]+)"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs, scratch = tmp_path / "images", tmp_path / "logs", tmp_path / "scratch"
    for directory in (images, logs, scratch, tmp_path / "fed"):
        directory.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    return {
        **SITE,
        **agent_site(tmp_path / "agent"),
        "SH": str(images),
        "L": str(logs),
        "HPC_STORAGE_ROOT": str(scratch),
        "HPC_QUOTA_CHECK": "off",  # the guard has its own tests (test_hpc_inode_guard.py)
    }


class Rig:
    """A driver over the fake Slurm with the core's phase recorded instead of run."""

    def __init__(self, hpc, tmp_path, site, layout=LAYOUT, environ=None):
        self.hpc, self.site, self.fake = hpc, site, FakeSlurm()
        self.root, self.scratch = tmp_path / "fed" / "root", Path(site.get("HPC_STORAGE_ROOT", tmp_path / "scratch"))
        given = {} if environ is None else {"environ": environ}
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake, **given)
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False
        self.said = []
        self.driver.say = self.said.append

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def submitted(self):
        return any(call[0] == "sbatch" for call in self.fake.calls)

    def state(self):
        return self.driver.store.read()

    def record(self):
        return self.state()["storage"]

    def parent(self):
        return Path(self.record()["parent"])

    def provider_dirs(self):
        return {name: Path(path) for name, path in self.record()["providers"].items()}

    def edit_state(self, change):
        """Change the state file on disk, as a corrupted or hand-edited one."""
        path = self.root / "hpc-federation.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        change(state)
        path.write_text(json.dumps(state), encoding="utf-8")


def listing(directory: Path) -> set:
    return {entry.name for entry in directory.iterdir()}


def populate(rig, outside: Path) -> int:
    """What a provider's storage holds once the federation ran: files and nested directories in each provider's
    directory, a read-only directory, and a link to a directory outside the federation. Returns the entries made."""
    made = 0
    for directory in rig.provider_dirs().values():
        nested = directory / "space-id" / "records" / "day"
        nested.mkdir(parents=True)
        for index in range(3):
            (nested / f"file-{index}.bin").write_bytes(b"x" * 64)
        (directory / "space-id" / "records").chmod(0o500)  # a read-only directory, as an ingest may leave
        (directory / "link-out").symlink_to(outside)
        (directory / "link-file").symlink_to(outside / "keep.txt")
        made += 8
    return made


@pytest.fixture
def outside(tmp_path):
    """A directory beside the federation that a link inside a provider's storage points at: it must come out whole."""
    directory = tmp_path / "elsewhere"
    directory.mkdir()
    (directory / "keep.txt").write_text("not the federation's\n", encoding="utf-8")
    (directory / "sub").mkdir()
    (directory / "sub" / "deeper.txt").write_text("nor this\n", encoding="utf-8")
    return directory


def untouched(outside: Path) -> bool:
    return (
        (outside / "keep.txt").read_text(encoding="utf-8") == "not the federation's\n"
        and (outside / "sub" / "deeper.txt").read_text(encoding="utf-8") == "nor this\n"
        and listing(outside) == {"keep.txt", "sub"}
    )


# -- up makes it ---------------------------------------------------------------------------------------------------


def test_up_makes_one_directory_of_its_own_and_one_per_provider_below_the_scratch_root(hpc, tmp_path, site):
    """O57: below HPC_STORAGE_ROOT `up` makes one directory named for its allocation, 0700, with an owner marker and
    one directory per provider, 0700; the state records them, and nothing else is made on
    scratch."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    record, name = rig.record(), rig.state()["allocation"]["name"]
    assert record["root"] == site["HPC_STORAGE_ROOT"]
    assert record["parent"] == f"{site['HPC_STORAGE_ROOT']}/{name}"
    assert listing(rig.scratch) == {name}
    assert listing(rig.parent()) == {".hpcop-storage-owner", *PROVIDERS}
    assert set(record["providers"]) == set(PROVIDERS)
    for path in (rig.parent(), *rig.provider_dirs().values()):
        assert stat.S_IMODE(path.lstat().st_mode) == 0o700
        assert path.is_dir()
        assert not path.is_symlink()
    marker = json.loads((rig.parent() / ".hpcop-storage-owner").read_text(encoding="utf-8"))
    assert marker == {"allocation": name, "token": record["token"]}
    for provider, directory in rig.provider_dirs().items():  # each provider's directory proves itself, too
        assert listing(directory) == {".hpcop-storage-owner"}
        held = json.loads((directory / ".hpcop-storage-owner").read_text(encoding="utf-8"))
        assert held == {**marker, "provider": provider}
    assert rig.state()["site"]["storage_root"] == site["HPC_STORAGE_ROOT"]


def test_the_state_names_the_storage_before_up_makes_any_of_it(hpc, tmp_path, site, monkeypatch):
    """O57: the record is written before the first `mkdir`, so a crash after it leaves enough for `down`."""
    rig = Rig(hpc, tmp_path, site)
    seen = []
    real = os.mkdir

    def mkdir(path, *args, **kwargs):
        if str(path).startswith(site["HPC_STORAGE_ROOT"]):
            seen.append((Path(path).name, (rig.state() or {}).get("storage")))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(hpc.storage.os, "mkdir", mkdir)
    rig.up()
    assert seen
    assert all(record is not None and record["parent"] for _, record in seen)
    assert seen[0][0].startswith("hpcop-")
    assert seen[0][1]["providers"].keys() == set(PROVIDERS)


def test_the_storage_is_made_before_the_job_is_submitted(hpc, tmp_path, site):
    """O57: a failure to make the storage leaves no held job billing."""
    rig = Rig(hpc, tmp_path, site)
    real = rig.fake.run
    seen = []

    def run(argv, timeout):
        if argv[0] == "sbatch":
            seen.append(listing(rig.scratch))
        return real(argv, timeout)

    rig.fake.run = run
    rig.up()
    assert seen
    assert len(seen[0]) == 1


def test_up_never_adopts_a_directory_that_is_already_there_and_down_never_removes_it(hpc, tmp_path, site, monkeypatch):
    """O57: a directory already at the storage's path (the allocation's random part taken) is refused, not adopted,
    and `down` finds no owner marker in it and removes nothing, saying why."""
    real = hpc.driver.secrets.token_hex
    monkeypatch.setattr(hpc.driver.secrets, "token_hex", lambda n=32: "deadbeef" if n == 4 else real(n))
    rig = Rig(hpc, tmp_path, site)
    taken = rig.scratch / "hpcop-expcore-hpc-pilot-deadbeef"
    taken.mkdir()
    (taken / "somebody-elses.dat").write_text("data\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="already exists"):
        rig.up()
    assert not rig.submitted()
    with pytest.raises(RuntimeError, match="no owner marker"):
        rig.driver.down()
    assert (taken / "somebody-elses.dat").read_text(encoding="utf-8") == "data\n"
    assert rig.root.exists()  # kept, with its state, for whoever looks at it


def test_a_site_without_a_scratch_root_makes_no_storage_and_its_state_records_none(hpc, tmp_path, site):
    """O57: with no HPC_STORAGE_ROOT nothing is made on scratch, and `down` is what it was; a state file from before
    this key (no `storage`, no `storage_root`) goes down as it did."""
    plain = {k: v for k, v in site.items() if not k.startswith("HPC_STORAGE") and not k.startswith("HPC_QUOTA")}
    rig = Rig(hpc, tmp_path, plain)
    rig.up()
    assert rig.state()["storage"] is None
    rig.edit_state(lambda s: (s.pop("storage"), s["site"].pop("storage_root", None)))  # as an older state file
    assert "down: the held job" in rig.driver.down()
    assert not rig.root.exists()
    assert listing(rig.scratch) == set()


def test_a_marker_that_cannot_be_written_for_want_of_inodes_leaves_no_unproven_directory(
    hpc, tmp_path, site, monkeypatch
):
    """O57: the quota the guard watches may run out at the `mkdir` itself. A failure after the storage's own directory
    was made and before its marker is written removes that empty directory (this call made it), so `down` is not left
    refusing a directory it cannot prove."""
    import errno

    rig = Rig(hpc, tmp_path, site)

    def full(content, **kwargs):
        raise OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))

    monkeypatch.setattr(hpc.storage, "_write_marker", full)
    with pytest.raises(RuntimeError, match="could not be made"):
        rig.up()
    assert listing(rig.scratch) == set()
    monkeypatch.undo()
    assert "down: " in rig.driver.down()
    assert not rig.root.exists()


def test_a_provider_directory_that_cannot_be_made_leaves_what_down_removes(hpc, tmp_path, site, monkeypatch):
    """O57: a failure after the marker (here the third `mkdir` of the storage) leaves the marked directory and the
    provider directories made so far, all recorded, and `down` removes them and shows none remain."""
    import errno

    rig = Rig(hpc, tmp_path, site)
    real, made = os.mkdir, []

    def mkdir(path, *args, **kwargs):
        if str(path).startswith(site["HPC_STORAGE_ROOT"]):
            made.append(path)
            if len(made) == 3:
                raise OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(hpc.storage.os, "mkdir", mkdir)
    with pytest.raises(RuntimeError, match="could not be made"):
        rig.up()
    monkeypatch.undo()
    assert len(listing(rig.scratch)) == 1
    assert len(listing(rig.parent())) == 2
    assert "0 entries remain" in rig.driver.down()
    assert listing(rig.scratch) == set()
    assert not rig.root.exists()


# -- the preflight of the scratch root ---------------------------------------------------------------------------


def test_a_scratch_root_that_is_not_a_directory_or_is_not_absolute_is_refused_before_anything_is_made(
    hpc, tmp_path, site
):
    """O57: a missing directory, a file, and a relative path are refused, naming HPC_STORAGE_ROOT."""
    for bad, why in (
        (str(tmp_path / "missing"), "is not a directory"),
        (str(tmp_path / "images" / "onezone.tar"), "is not a directory"),
    ):
        rig = Rig(hpc, tmp_path, {**site, "HPC_STORAGE_ROOT": bad})
        with pytest.raises(RuntimeError, match=f"HPC_STORAGE_ROOT.*{why}"):
            rig.up()
        assert not rig.root.exists()
        assert not any(c[0] == "sbatch" for c in rig.fake.calls)
    with pytest.raises(hpc.settings.SettingsError, match="HPC_STORAGE_ROOT"):
        hpc.settings.site_from_environment({**site, "HPC_STORAGE_ROOT": "relative/path"})


def test_a_scratch_root_that_is_the_federations_root_even_through_a_link_is_refused(hpc, tmp_path, site):
    """O57: `down` empties the root, so a scratch root that is the root, by its own path or through a link, would be
    emptied too: refused, naming the variable, before anything is made."""
    rig = Rig(hpc, tmp_path, site)
    rig.root.mkdir()
    (tmp_path / "root-link").symlink_to(rig.root)
    for named in (rig.root, tmp_path / "root-link"):
        rig.site = {**site, "HPC_STORAGE_ROOT": str(named)}
        with pytest.raises(RuntimeError, match=r"HPC_STORAGE_ROOT.*federation's root"):
            rig.up()
        assert not any(c[0] == "sbatch" for c in rig.fake.calls)
        assert list(rig.root.iterdir()) == []


def test_a_scratch_root_that_is_or_holds_or_lies_in_a_shared_tree_is_refused_naming_the_variable(hpc, tmp_path, site):
    """O57: nothing may be written to a shared tree, so a scratch root that is, holds or lies inside one is refused,
    naming the tree's variable."""
    environ = make_environ(tmp_path)
    tree = Path(environ["EXPCORE_FED_IMPORT_ALPHA"])
    for bad in (tree, tree.parent, tree / "inside"):
        bad.mkdir(exist_ok=True)
        rig = Rig(hpc, tmp_path, {**site, "HPC_STORAGE_ROOT": str(bad)}, layout=IMPORT_LAYOUT, environ=environ)
        with pytest.raises((RuntimeError, ValueError), match=r"HPC_STORAGE_ROOT.*EXPCORE_FED_IMPORT_"):
            rig.up()
        assert not rig.root.exists()


# -- down removes exactly it -------------------------------------------------------------------------------------


def test_down_removes_exactly_the_recorded_storage_and_shows_nothing_of_it_remains(hpc, tmp_path, site, outside):
    """O57: after the job and every service are gone, `down` removes the recorded directories whole (a read-only one,
    nested files and links included, a link never followed), counts what remains of them, says it is none, and then
    removes the root; a sibling of the scratch root's own and the target of a link inside are untouched."""
    (Path(site["HPC_STORAGE_ROOT"]) / "another-federations-storage").mkdir()
    (Path(site["HPC_STORAGE_ROOT"]) / "another-federations-storage" / "data.bin").write_bytes(b"y")
    (Path(site["HPC_STORAGE_ROOT"]) / "notes.txt").write_text("mine\n", encoding="utf-8")
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    assert populate(rig, outside) > 0
    out = rig.driver.down()
    assert "provider storage: 5 directories removed, 0 entries remain" in out
    assert listing(rig.scratch) == {"another-federations-storage", "notes.txt"}
    assert (rig.scratch / "another-federations-storage" / "data.bin").read_bytes() == b"y"
    assert untouched(outside)
    assert not rig.root.exists()


def test_the_storage_is_removed_after_the_job_is_gone_and_before_the_root_and_its_state(hpc, tmp_path, site):
    """O57: down cancels the job and ends every client first, and removes the storage while the state that names it
    still exists."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    order = []
    real_cancel = rig.driver.slurm.cancel
    real_remove = hpc.storage.remove

    def cancel(job):
        order.append("cancel")
        return real_cancel(job)

    def remove(record, state, **ops):
        order.append(("remove", rig.root.exists() and (rig.root / "hpc-federation.json").exists()))
        return real_remove(record, state, **ops)

    rig.driver.slurm.cancel = cancel
    hpc.storage.remove = remove
    try:
        rig.driver.down()
    finally:
        hpc.storage.remove = real_remove
    assert order == ["cancel", ("remove", True)]


def test_down_removes_the_storage_of_a_job_that_had_already_ended(hpc, tmp_path, site):
    """O57: scratch is seen from the login node, so a held job that ended on its own does not keep its storage."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    rig.fake.state = "GONE"
    rig.driver.down()
    assert listing(rig.scratch) == set()
    assert not rig.root.exists()


def test_down_that_could_not_verify_a_service_stopped_keeps_the_storage_for_the_second_down(hpc, tmp_path, site):
    """O57: a service that may still write is no reason to remove its storage: a down that cannot verify every service
    clean keeps the storage, the root and the state, and the second down removes all."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    rig.fake.teardown_rc = 1
    with pytest.raises(RuntimeError, match="could not be verified clean"):
        rig.driver.down()
    assert rig.parent().is_dir()
    assert all(p.is_dir() for p in rig.provider_dirs().values())
    rig.fake.teardown_rc, rig.fake.state = 0, "GONE"
    rig.driver.down()
    assert listing(rig.scratch) == set()
    assert not rig.root.exists()


def test_a_removal_that_fails_keeps_the_root_and_the_state_and_the_second_down_finishes_it(
    hpc, tmp_path, site, outside
):
    """O57: a removal that fails (here an unlink that is refused) is said, with the job gone all the same; the root and
    its state stay, and a second down removes what is left of the storage, which is idempotent."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    populate(rig, outside)
    real = os.unlink
    calls = []

    def unlink(path, **kwargs):
        calls.append(path)
        if len(calls) == 4:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, **kwargs)

    rig.driver.removal = {"unlink": unlink}
    with pytest.raises(RuntimeError, match="provider storage on scratch could not be removed"):
        rig.driver.down()
    assert rig.root.exists()
    assert rig.parent().exists()
    rig.driver.removal = {}
    rig.fake.state = "GONE"
    assert "0 entries remain" in rig.driver.down()
    assert listing(rig.scratch) == set()
    assert not rig.root.exists()
    assert untouched(outside)


def test_a_removal_that_reports_success_but_leaves_entries_fails_on_the_count(hpc, tmp_path, site):
    """O57: what remains is counted, not assumed: with file calls that do nothing the removal raises nothing, and the
    count of what is left still fails it, naming the number."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    (rig.provider_dirs()["aspen"] / "kept.dat").write_bytes(b"z")
    rig.driver.removal = {"unlink": lambda *args, **kwargs: None, "rmdir": lambda *args, **kwargs: None}
    with pytest.raises(RuntimeError, match=r"[0-9]+ entries of the provider storage remain"):
        rig.driver.down()
    assert rig.root.exists()


# -- nothing outside the recorded paths ---------------------------------------------------------------------------


def test_an_entry_beside_the_providers_directories_is_never_removed_and_fails_down_loudly(hpc, tmp_path, site):
    """O57: something `up` did not make inside the storage's own directory is kept, with the directory and its
    marker, and down fails naming how many such entries there are; the providers' directories, which `up` made, go."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    (rig.parent() / "someone-elses.dat").write_text("data\n", encoding="utf-8")
    (rig.parent() / "someone-elses-dir").mkdir()
    with pytest.raises(RuntimeError, match=r"2 entries .* were not made by up"):
        rig.driver.down()
    assert listing(rig.parent()) == {".hpcop-storage-owner", "someone-elses.dat", "someone-elses-dir"}
    assert rig.root.exists()
    (rig.parent() / "someone-elses.dat").unlink()
    (rig.parent() / "someone-elses-dir").rmdir()
    rig.fake.state = "GONE"
    rig.driver.down()
    assert listing(rig.scratch) == set()


def test_a_providers_directory_that_is_a_link_is_refused_and_nothing_is_removed(hpc, tmp_path, site, outside):
    """O57: a recorded path that is no longer a directory `up` made (here a link to a directory elsewhere) stops the
    removal before any directory is touched: no provider's storage is removed, and the link's target is whole."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    populate(rig, outside)
    target = rig.provider_dirs()["rowan"]
    target.rename(target.with_name("rowan-moved"))
    target.symlink_to(outside)
    with pytest.raises(RuntimeError, match=r"rowan.*not a directory up made"):
        rig.driver.down()
    assert untouched(outside)
    assert target.is_symlink()
    assert target.with_name("rowan-moved").is_dir()
    assert all((d / "space-id").is_dir() for n, d in rig.provider_dirs().items() if n != "rowan")


def test_a_storage_directory_replaced_by_a_link_is_refused(hpc, tmp_path, site, outside):
    """O57: the storage's own directory swapped for a link to another directory is refused, never followed."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    parent = rig.parent()
    moved = parent.with_name("moved-away")
    parent.rename(moved)
    parent.symlink_to(outside)
    with pytest.raises(RuntimeError, match="not a directory up made"):
        rig.driver.down()
    assert untouched(outside)
    assert parent.is_symlink()
    assert moved.is_dir()


def test_a_providers_directory_swapped_for_another_of_this_users_is_not_removed(hpc, tmp_path, site, outside):
    """O57: the storage's own directory holds this up's marker, but that proves nothing of each child: a provider's
    directory replaced by an unrelated directory of this user (no marker of this up for it) is refused before any
    removal, and the replacement's files are whole."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    target = rig.provider_dirs()["larch"]
    target.rename(target.with_name("larch-moved"))
    target.mkdir()
    (target / "unrelated.dat").write_text("not up's\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"larch.*holds no owner marker"):
        rig.driver.down()
    assert (target / "unrelated.dat").read_text(encoding="utf-8") == "not up's\n"
    assert all((d / ".hpcop-storage-owner").is_file() for n, d in rig.provider_dirs().items() if n != "larch")
    assert rig.root.exists()


def test_a_directory_swapped_for_a_link_after_it_was_proved_is_never_followed(hpc, tmp_path, site, outside):
    """O57: what is removed is the proved directory (held open, its contents removed through its descriptor), whatever
    its path names a moment later: swapped for a link to another directory between the proof and the removal, the
    link's target is whole, the link stays, and `down` fails saying the directory could not be removed."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    populate(rig, outside)
    real, swapped = hpc.storage.remove_tree, []

    def remove_tree(directory, **kwargs):
        if not swapped:
            target = Path(os.readlink(directory))  # the path the held descriptor was opened by
            swapped.append(target)
            target.rename(target.with_name(target.name + "-moved"))
            target.symlink_to(outside)
        return real(directory, **kwargs)

    hpc.storage.remove_tree = remove_tree
    try:
        with pytest.raises(RuntimeError, match="could not be removed"):
            rig.driver.down()
    finally:
        hpc.storage.remove_tree = real
    assert untouched(outside)
    assert swapped[0].is_symlink()
    assert swapped[0].with_name(swapped[0].name + "-moved").is_dir()


def test_a_directory_that_cannot_be_retired_gets_its_marker_back_and_the_second_down_finishes(hpc, tmp_path, site):
    """O57: a provider's directory emptied but not removed (its `rmdir` refused) is left holding its marker again, so
    the second `down` can still prove it up's and finish."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    refused = []

    def rmdir(path, **kwargs):
        if kwargs.get("dir_fd") is not None and not refused:
            refused.append(path)
            raise PermissionError(13, "Permission denied")
        return os.rmdir(path, **kwargs)

    rig.driver.removal = {"rmdir": rmdir}
    with pytest.raises(RuntimeError, match="its owner marker is kept"):
        rig.driver.down()
    held = [d for d in rig.provider_dirs().values() if d.exists()]
    assert [listing(d) for d in held if d.name == refused[0]] == [{".hpcop-storage-owner"}]
    rig.driver.removal = {}
    rig.fake.state = "GONE"
    assert "0 entries remain" in rig.driver.down()
    assert listing(rig.scratch) == set()


@pytest.mark.parametrize(
    "edit", [lambda s: s.pop("storage"), lambda s: s.update(storage=None)], ids=["deleted", "null"]
)
def test_a_state_that_names_a_scratch_root_but_no_storage_is_refused_not_read_as_nothing_to_remove(
    hpc, tmp_path, site, edit
):
    """O57: `up` always records the storage it makes, so a state with the site's scratch root and no `storage` (the key
    deleted, or null) is not one that made none: `down` refuses, saying nothing on scratch was removed, and keeps the
    root and the state."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    parent = rig.parent()
    rig.edit_state(edit)
    with pytest.raises(RuntimeError, match="records a scratch root and no provider storage"):
        rig.driver.down()
    assert parent.is_dir()
    assert all(d.is_dir() for d in parent.iterdir() if d.name != ".hpcop-storage-owner")
    assert rig.root.exists()


def test_a_provider_marker_that_cannot_be_written_removes_that_empty_directory(hpc, tmp_path, site, monkeypatch):
    """O57: a provider's marker that cannot be written (the quota ran out) removes the empty directory just made; the
    marked directories before it are the state's, and `down` removes them."""
    import errno

    rig = Rig(hpc, tmp_path, site)
    real, calls = hpc.storage._write_marker, []

    def write(content, **kwargs):
        calls.append(kwargs)
        if len(calls) == 3:  # the storage's own, one provider's, then this one
            raise OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))
        return real(content, **kwargs)

    monkeypatch.setattr(hpc.storage, "_write_marker", write)
    with pytest.raises(RuntimeError, match="could not be made"):
        rig.up()
    monkeypatch.undo()
    assert len(listing(rig.parent())) == 2  # its marker and the one provider's directory, the third removed
    assert "0 entries remain" in rig.driver.down()
    assert listing(rig.scratch) == set()


def test_a_marker_that_is_not_this_ups_is_refused(hpc, tmp_path, site):
    """O57: the owner marker holds this up's token; a directory with another's (or a link in its place) is not
    removed."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    marker = rig.parent() / ".hpcop-storage-owner"
    marker.write_text(json.dumps({"allocation": "other", "token": "0" * 32}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no owner marker"):
        rig.driver.down()
    assert all(p.is_dir() for p in rig.provider_dirs().values())
    marker.unlink()
    marker.symlink_to(tmp_path / "images" / "onezone.tar")
    with pytest.raises(RuntimeError, match="no owner marker"):
        rig.driver.down()
    assert all(p.is_dir() for p in rig.provider_dirs().values())


@pytest.mark.parametrize(
    "change",
    [
        lambda r, s: s["storage"].update(parent="/"),
        lambda r, s: s["storage"].update(parent=str(Path(r["root"]).parent)),
        lambda r, s: s["storage"].update(parent=r["root"]),
        lambda r, s: s["storage"].update(parent=r["root"] + "/hpcop-expcore-hpc-pilot-zzzzzzzz"),
        lambda r, s: s["storage"].update(parent=r["root"] + "/../escape"),
        lambda r, s: s["storage"]["providers"].update(aspen=r["root"]),
        lambda r, s: s["storage"]["providers"].update(aspen=r["parent"] + "/../aspen"),
        lambda r, s: s["storage"]["providers"].update(aspen=r["parent"] + "/aspen/deeper"),
        lambda r, s: s["storage"]["providers"].update(**{"..": r["parent"] + "/.."}),
        lambda r, s: s["storage"]["providers"].update(extra=r["parent"] + "/extra"),
        lambda r, s: s["storage"].update(root="/"),
        lambda r, s: s["storage"].update(providers=[]),
        lambda r, s: s.update(storage="/"),
    ],
)
def test_a_recorded_path_that_is_not_the_form_up_makes_is_refused_and_nothing_is_removed(hpc, tmp_path, site, change):
    """O57: `down` removes only paths of the exact form `up` records (the site's recorded scratch root, the allocation's
    own name, a provider of the layout): a state edited to name anything else (the root's parent, `/`, a path with a
    `..`, another name) is refused before any removal, and the directories `up` made are still there."""
    decoy = tmp_path / "scratch" / "decoy"
    decoy.mkdir()
    (decoy / "data.bin").write_bytes(b"d")
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    before = rig.record()
    rig.edit_state(lambda s: change(before, s))
    with pytest.raises(RuntimeError, match="not what up records"):
        rig.driver.down()
    assert (decoy / "data.bin").read_bytes() == b"d"
    assert rig.root.exists()
    assert Path(before["parent"]).is_dir()
    assert all(Path(p).is_dir() for p in before["providers"].values())

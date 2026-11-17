"""A provider's posix storage on the cluster's shared scratch filesystem (deploy/hpc-federation, O57): the node-side
script that checks the directory `up` made for the provider and binds it onto `/volumes/posix`
(`bundle/posixstorage.py`), and the shell wiring that passes it from the provider's job to its service, at the first
start and at every start again. Without a storage the service is started exactly as before, on its rootfs's own
`/volumes/posix` (the node's tmpfs). Nothing here needs a cluster: the scratch tree and the work directory are temporary
directories, the mount tables are text, and the service is the bundle's stand-in apptainer (tests/deploy/hpcnode.py)."""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BUNDLE = HERE.parents[1] / "deploy" / "hpc-federation" / "bundle"
SCRIPT = BUNDLE / "posixstorage.py"
ALLOCATION = "hpcop-expcore-hpc-pilot-0123abcd"
TOKEN = "0123456789abcdef0123456789abcdef"

node_spec = importlib.util.spec_from_file_location("hpcop_node_tests", HERE / "hpcnode.py")
node = sys.modules.get("hpcop_node_tests") or importlib.util.module_from_spec(node_spec)
sys.modules["hpcop_node_tests"] = node
node_spec.loader.exec_module(node)


def load():
    spec = importlib.util.spec_from_file_location("hpcop_posixstorage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpcop_posixstorage"] = module
    spec.loader.exec_module(module)
    return module


ps = load()
NO_MOUNTS = ""  # a mount table that names nothing: the storage is on no node-local memory


def cli(*args):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, timeout=60, check=False
    )


@pytest.fixture
def base(tmp_path):
    return tmp_path.resolve()


def make_storage(base, provider="aspen", allocation=ALLOCATION, marker=None):
    """What `up` makes on scratch for one provider (hpcstorage.create): the allocation's directory and the provider's,
    each 0700 with its owner marker, and in the provider's the empty directory its service binds."""
    parent = base / "scratch" / "hpcop-storage" / allocation
    directory = parent / provider
    (directory / "posix").mkdir(parents=True)
    for path in (parent, directory, directory / "posix"):
        path.chmod(0o700)
    owner = {"allocation": allocation, "token": TOKEN}
    node.put(parent / ".hpcop-storage-owner", json.dumps(owner, sort_keys=True) + "\n")
    held = {**owner, "provider": provider} if marker is None else marker
    node.put(directory / ".hpcop-storage-owner", json.dumps(held, sort_keys=True) + "\n")
    return directory / "posix"


@pytest.fixture
def storage(base):
    return make_storage(base)


@pytest.fixture
def workdir(base):
    """A provider's private work directory with an unpacked rootfs, as node-init.sh leaves it: its /volumes/posix is
    the image's empty mountpoint."""
    path = base / "tmp" / "hpcop-op.abc123"
    (path / "rootfs" / "volumes" / "posix").mkdir(parents=True)
    path.chmod(0o700)
    (path / "rootfs.complete").touch()
    return path


def prepare(workdir, storage, provider="aspen", mounts=NO_MOUNTS):
    return ps.prepare(str(workdir), provider, f"--storage={storage}", mountinfo=mounts)


def bind(workdir, storage):
    """binds as run_service calls it at a start that names `storage` (the federation's state's own)."""
    return ps.binds(str(workdir), f"--storage={storage}")


def refused(match, workdir, storage, provider="aspen", mounts=NO_MOUNTS):
    with pytest.raises(ps.StorageRefused, match=match) as failure:
        prepare(workdir, storage, provider, mounts)
    assert str(storage) not in str(failure.value)
    assert not (workdir / "posix-storage").exists()
    return failure


# -- the directory -----------------------------------------------------------------------------------------------------


def test_o57_prepare_records_the_storage_and_binds_name_it_writable_over_the_posix_mountpoint(workdir, storage):
    """O57: the directory up made for this provider is recorded in the work directory (0600) and bound, writable, onto
    /volumes/posix, the path finish.sh declares the posix storage at, so the cell sees its storage where it always
    did."""
    prepare(workdir, storage)
    record = workdir / "posix-storage"
    assert record.read_text(encoding="utf-8") == f"aspen\t{storage}\n"
    assert record.stat().st_mode & 0o777 == 0o600
    assert bind(workdir, storage) == ["--bind", f"{storage}:/volumes/posix:rw"]


def test_o57_a_start_that_names_a_storage_the_work_directory_does_not_record_is_refused(workdir, storage):
    """O57: a start given a storage (the federation's state names one) whose work directory records none was never
    prepared for it: refused, never started on the node's tmpfs instead."""
    with pytest.raises(ps.StorageRefused, match="records none") as failure:
        bind(workdir, storage)
    assert str(storage) not in str(failure.value)


def test_o57_a_record_edited_to_name_another_federations_storage_is_refused_at_the_start(base, workdir, storage):
    """O57: the start is given its own storage from the federation's state, and binds only when the work directory
    records exactly that: a record edited to another federation's directory for the same provider (whose marker would
    pass every other check) is refused, as is one naming another provider of this federation."""
    prepare(workdir, storage)
    other = make_storage(base / "other-federation", allocation="hpcop-expcore-hpc-pilot-feedf00d")
    node.put(workdir / "posix-storage", f"aspen\t{other}\n")
    with pytest.raises(ps.StorageRefused, match="another storage than") as failure:
        bind(workdir, storage)
    assert str(other) not in str(failure.value)
    sibling = make_storage(base, provider="larch")
    node.put(workdir / "posix-storage", f"larch\t{sibling}\n")
    with pytest.raises(ps.StorageRefused, match="another storage than"):
        bind(workdir, storage)


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda base: make_storage(base).parent, "not of the form up makes"),  # the provider's directory itself
        (lambda base: make_storage(base, provider="larch"), "not of the form up makes"),  # another provider's
        (lambda base: make_storage(base, allocation="other-0123abcd"), "not of the form up makes"),
        (lambda base: make_storage(base).parent / "elsewhere", "not of the form up makes"),
    ],
)
def test_o57_a_storage_that_is_not_this_providers_directory_below_an_allocations_is_refused(base, workdir, make, why):
    """O57: only `<allocation's directory>/<this provider>/posix` is bound: never the provider's directory (which holds
    the marker), another provider's, or a directory outside an allocation's."""
    target = make(base)
    target.mkdir(parents=True, exist_ok=True)
    refused(why, workdir, target)


def test_o57_a_path_that_is_not_plain_and_absolute_or_holds_a_bind_separator_is_refused(workdir, storage):
    """O57: '..', '//', a relative path and a ':' or ',' (which would end the bind's source) are refused, unechoed."""
    for bad in (f"{storage}/../posix", str(storage).replace("/scratch/", "//scratch/"), "relative/aspen/posix"):
        with pytest.raises(ps.StorageRefused):
            ps.prepare(str(workdir), "aspen", f"--storage={bad}", mountinfo=NO_MOUNTS)
    for bad in (f"{storage}:/etc", f"{storage},ro"):
        with pytest.raises(ps.StorageRefused, match="absolute path of"):
            ps.prepare(str(workdir), "aspen", f"--storage={bad}", mountinfo=NO_MOUNTS)


def test_o57_a_link_on_the_way_to_the_storage_is_refused(base, workdir):
    """O57: a link anywhere on the path could be re-pointed between the check and the bind: refused."""
    real = make_storage(base / "real")
    linked = base / "linked"
    linked.mkdir()
    (linked / "scratch").symlink_to(real.parents[2].parent)
    refused("symbolic link", workdir, linked / "scratch" / "hpcop-storage" / ALLOCATION / "aspen" / "posix")
    real.rmdir()
    real.symlink_to(base / "real")  # the bound directory itself a link
    refused("symbolic link", workdir, real)


def test_o57_a_storage_that_is_missing_is_refused(workdir, storage):
    """O57: a directory that is not there is refused, unechoed."""
    storage.rmdir()
    refused("does not exist", workdir, storage)


@pytest.mark.parametrize("which", [0, 1, 2])
def test_o57_each_of_the_three_directories_must_be_this_users_alone_0700(workdir, storage, which):
    """O57: the provider runs as this user in its cell and needs no other: the bound directory, its provider's and its
    allocation's are each mode 0700 (a group or other bit is refused, as is a mode without the owner's write)."""
    [storage, storage.parent, storage.parents[1]][which].chmod(0o750)
    refused("mode 0700", workdir, storage)


def test_o57_a_setgid_bit_that_a_projects_scratch_passes_down_is_accepted(workdir, storage):
    """O57: a project's scratch often has its setgid bit set, and every directory made in it inherits it: no access for
    anyone, so the permission bits alone are compared and 02700 is taken as 0700."""
    for path in (storage, storage.parent, storage.parents[1]):
        path.chmod(0o2700)
        if not path.stat().st_mode & 0o2000:
            pytest.skip("this filesystem keeps no setgid bit on a directory")
    prepare(workdir, storage)
    assert bind(workdir, storage) == ["--bind", f"{storage}:/volumes/posix:rw"]


def test_o57_a_storage_this_user_does_not_own_is_refused(workdir, storage, monkeypatch):
    """O57: a directory of another uid is refused, whatever its mode."""
    monkeypatch.setattr(ps.os, "getuid", lambda: os.stat(storage).st_uid + 1)
    refused("of this user", workdir, storage)


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ({"allocation": ALLOCATION, "token": TOKEN, "provider": "larch"}, "owner marker"),
        ({"allocation": "hpcop-expcore-hpc-pilot-ffffffff", "token": TOKEN, "provider": "aspen"}, "owner marker"),
        ({"allocation": ALLOCATION, "token": "not a token", "provider": "aspen"}, "owner marker"),
        ({"allocation": ALLOCATION, "token": TOKEN}, "owner marker"),
    ],
)
def test_o57_a_provider_directory_without_ups_marker_for_this_provider_is_refused(base, workdir, marker, why):
    """O57: the provider's directory must hold up's owner marker naming this provider and this allocation (the marker
    down proves the directory by): a marker of another provider, another allocation or no token is refused."""
    refused(why, workdir, make_storage(base, marker=marker))


def test_o57_a_marker_that_is_missing_a_link_or_not_json_is_refused(base, workdir, storage):
    """O57: no marker, a link in its place, and a marker that is not JSON are refused; a link is never read through."""
    marker = storage.parent / ".hpcop-storage-owner"
    text = marker.read_text(encoding="utf-8")
    marker.unlink()
    refused("owner marker", workdir, storage)
    elsewhere = base / "elsewhere-marker"
    node.put(elsewhere, text)
    marker.symlink_to(elsewhere)
    refused("owner marker", workdir, storage)
    marker.unlink()
    node.put(marker, "not json\n")
    refused("owner marker", workdir, storage)


def test_o57_the_marker_stays_beside_the_bound_directory_so_the_provider_never_sees_it(workdir, storage):
    """O57: down proves the provider's directory by its marker, which is in that directory, beside the one the service
    binds: the bind exposes only the empty `posix` directory to the provider."""
    prepare(workdir, storage)
    assert sorted(p.name for p in storage.parent.iterdir()) == [".hpcop-storage-owner", "posix"]
    assert list(storage.iterdir()) == []


def test_o57_a_storage_that_holds_files_at_the_first_start_is_refused_but_a_restart_binds_it_with_its_data(
    workdir, storage
):
    """O57: at the first start (prepare) the storage up just made is empty, and one that is not was not made for this
    start; at every start again (binds) it holds what the provider stored, and is bound with it."""
    (storage / "early.bin").write_bytes(b"x")
    refused("holds files", workdir, storage)
    (storage / "early.bin").unlink()
    prepare(workdir, storage)
    (storage / "space-id").mkdir()
    (storage / "space-id" / "data.bin").write_bytes(b"what the provider stored")
    assert bind(workdir, storage) == ["--bind", f"{storage}:/volumes/posix:rw"]


def test_o57_a_storage_on_node_local_memory_is_refused_since_it_is_there_to_keep_the_data_off_the_node(
    base, workdir, storage
):
    """O57: a directory on tmpfs or ramfs is the node's memory, which the bind exists to leave: refused."""
    table = f"36 25 0:32 / {base} rw,nosuid shared:1 - tmpfs tmpfs rw\n"
    refused("node-local memory", workdir, storage, mounts=table)


def test_o57_a_storage_inside_or_holding_the_work_directory_is_refused(base, workdir):
    """O57: the work directory is removed at teardown: a storage inside it, or holding it, is refused."""
    inside = make_storage(workdir)
    refused("work directory", workdir, inside)


def test_o57_a_rootfs_mountpoint_that_is_missing_a_link_or_holds_files_is_refused(workdir, storage, base):
    """O57: the bind covers the rootfs's /volumes/posix: a mountpoint that holds files (they would be hidden), is a
    link or is missing is refused, at the first start and at every start again."""
    point = workdir / "rootfs" / "volumes" / "posix"
    (point / "stale.bin").write_bytes(b"x")
    refused("holds files", workdir, storage)
    (point / "stale.bin").unlink()
    prepare(workdir, storage)
    (point / "stale.bin").write_bytes(b"x")
    with pytest.raises(ps.StorageRefused, match="holds files"):
        bind(workdir, storage)
    shutil.rmtree(point)
    point.symlink_to(base)
    with pytest.raises(ps.StorageRefused, match="link or not a directory"):
        bind(workdir, storage)
    point.unlink()
    with pytest.raises(ps.StorageRefused, match="no /volumes/posix"):
        bind(workdir, storage)


def test_o57_a_work_directory_that_is_not_a_providers_private_one_is_refused(storage, base):
    """O57: the record is written only in a provider's private work directory with its rootfs unpacked."""
    zone = base / "tmp" / "hpcop-oz.abc123"
    (zone / "rootfs" / "volumes" / "posix").mkdir(parents=True)
    zone.chmod(0o700)
    (zone / "rootfs.complete").touch()
    with pytest.raises(ps.StorageRefused, match="private work directory"):
        prepare(zone, storage)
    assert not (zone / "posix-storage").exists()


def test_o57_a_storage_that_became_a_link_or_lost_its_marker_after_prepare_is_refused_at_the_start(
    workdir, storage, base
):
    """O57: binds checks the recorded directory again, since a start may come long after prepare."""
    prepare(workdir, storage)
    marker = storage.parent / ".hpcop-storage-owner"
    saved = marker.read_text(encoding="utf-8")
    marker.unlink()
    with pytest.raises(ps.StorageRefused, match="owner marker"):
        bind(workdir, storage)
    node.put(marker, saved)
    storage.rename(storage.parent / "moved")
    (storage.parent / "moved").chmod(0o700)
    storage.symlink_to(storage.parent / "moved")
    with pytest.raises(ps.StorageRefused, match="symbolic link"):
        bind(workdir, storage)


def test_o57_a_record_that_was_edited_is_a_link_or_is_readable_by_others_is_refused(workdir, storage, base):
    """O57: the record is read as strictly as the argument: another provider's line, a link, a mode for others."""
    prepare(workdir, storage)
    record = workdir / "posix-storage"
    node.put(record, f"larch\t{storage}\n")
    with pytest.raises(ps.StorageRefused, match="not of the form up makes"):
        bind(workdir, storage)
    node.put(record, f"aspen\t{storage}\n", mode=0o644)
    with pytest.raises(ps.StorageRefused, match="readable by no one else"):
        bind(workdir, storage)
    record.unlink()
    node.put(base / "other-record", f"aspen\t{storage}\n")
    record.symlink_to(base / "other-record")
    with pytest.raises(ps.StorageRefused, match="cannot be opened"):
        bind(workdir, storage)
    record.unlink()
    node.put(record, "aspen\n")
    with pytest.raises(ps.StorageRefused, match="NAME<TAB>PATH"):
        bind(workdir, storage)


def test_o57_the_record_is_never_written_through_a_link_and_is_replaced_whole(workdir, storage, base):
    """O57: prepare writes a draft by exclusive create, never through a link, and replaces the record with it."""
    victim = base / "victim"
    node.put(victim, "kept\n")
    (workdir / "posix-storage").symlink_to(victim)
    prepare(workdir, storage)
    assert victim.read_text(encoding="utf-8") == "kept\n"
    assert not (workdir / "posix-storage").is_symlink()
    assert [p.name for p in workdir.iterdir() if p.name.startswith("posix-storage")] == ["posix-storage"]


def test_o57_the_command_line_prepares_and_binds_and_a_refusal_names_no_path(workdir, storage):
    """O57: the two verbs, as configure.sh and run_service call them; a refusal exits 1 on stderr, naming no path. (The
    accepted prepare is the function's, given a mount table: this host's own may be a tmpfs.)"""
    bad = storage.parent
    done = cli("prepare", "--workdir", str(workdir), "--provider", "aspen", f"--storage={bad}")
    assert done.returncode == 1
    assert "posixstorage: ERROR" in done.stderr
    assert str(bad) not in done.stderr + done.stdout
    assert not (workdir / "posix-storage").exists()
    prepare(workdir, storage)
    done = cli("binds", "--workdir", str(workdir), f"--storage={storage}")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["--bind", f"{storage}:/volumes/posix:rw"]
    assert cli("binds", "--workdir", str(workdir)).returncode == 1  # a start that names its storage, always
    assert cli("binds", "--workdir", str(workdir), "--provider", "aspen", f"--storage={storage}").returncode == 1
    assert cli("prepare", "--workdir", str(workdir), "--provider", "Aspen", f"--storage={storage}").returncode == 1


def test_o57_the_script_never_removes_renames_or_links_and_writes_only_its_record():
    """O57: the storage is read and named as a bind source: nothing in the script can remove, rename, link or truncate
    a path, and the one write is the record, by exclusive create, in `write_record` alone."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    forbidden = {"rmtree", "remove", "unlink", "rmdir", "rename", "renames", "symlink", "link", "truncate", "chmod"}
    where = {}
    for function in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        for call in (n for n in ast.walk(function) if isinstance(n, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
            where.setdefault(name, set()).add(function.name)
            modes = [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name in ("open", "fdopen") and any(m[:1] in ("w", "a", "x") for m in modes):
                where.setdefault("open-for-write", set()).add(function.name)
    assert not forbidden & set(where)
    assert where.get("replace", set()) == {"write_record"}
    assert where.get("open-for-write", set()) == {"write_record"}
    assert not {"mkdir", "makedirs"} & set(where)
    assert "O_EXCL" in SCRIPT.read_text(encoding="utf-8")


# -- the shell wiring --------------------------------------------------------------------------------------------------


def shell(snippet, *args):
    """A snippet in a fresh bash that sources lib.sh first, its Python helpers run by this interpreter; `$@` is
    `args`."""
    script = f'source "{BUNDLE}/lib.sh"; {snippet}'
    environ = {**os.environ, "HPCOP_PY": sys.executable}
    return subprocess.run(
        ["bash", "-c", script, "lib", *args], capture_output=True, text=True, timeout=30, check=False, env=environ
    )


def lib(command, *args):
    """One bundle function, called with `args`."""
    return shell(f'{command} "$@"', *args)


def test_o57_the_arguments_are_split_into_the_one_storage_and_the_rest():
    """O57: a provider's job takes one `--storage=PATH` among its peers and imports; configure.sh takes it out."""
    split = 'split_storage STORAGE REST "$@"; printf "S=%s\\n" "$STORAGE"; printf "R=%s\\n" "${REST[@]}"'
    done = shell(split, "10.0.0.1", "--storage=/s/a/posix", "--import=a=/x", "10.0.0.2")
    assert done.stdout.split() == ["S=--storage=/s/a/posix", "R=10.0.0.1", "R=--import=a=/x", "R=10.0.0.2"]
    done = shell(split, "10.0.0.1")
    assert done.stdout.split() == ["S=", "R=10.0.0.1"]
    twice = shell(split, "--storage=/s/a/posix", "--storage=/s/b/posix")
    assert twice.returncode != 0
    assert "given twice" in twice.stderr


def test_o57_the_service_binds_the_storage_its_start_names_and_one_that_names_none_binds_nothing(workdir, storage):
    """O57: storage_binds prints the bind of the storage the start names, as recorded; a start that names none binds
    nothing where nothing is recorded (tmpfs, as before), and refuses where a storage is recorded (a link in its place
    included), so a provider bound onto scratch is never started on tmpfs for want of an argument."""
    done = lib("storage_binds", str(workdir), "")
    assert (done.returncode, done.stdout) == (0, "")
    prepare(workdir, storage)
    done = lib("storage_binds", str(workdir), f"--storage={storage}")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["--bind", f"{storage}:/volumes/posix:rw"]
    done = lib("storage_binds", str(workdir), "")
    assert done.returncode != 0
    assert "names none" in done.stderr
    (workdir / "posix-storage").unlink()
    (workdir / "posix-storage").symlink_to(workdir / "no-such-file")
    assert lib("storage_binds", str(workdir), "").returncode != 0


def read(name):
    return (BUNDLE / name).read_text(encoding="utf-8")


def test_o57_configure_prepares_the_providers_storage_and_the_zone_takes_none():
    """O57: configure.sh splits out the storage, refuses one for the zone, and prepares it for the provider by name."""
    configure = read("configure.sh")
    assert 'split_storage STORAGE ARGS "$@"' in configure
    assert "the zone has no posix storage" in configure
    assert re.search(r'posixstorage\.py" prepare --workdir "\$W" --provider "\$NAME" "\$STORAGE"', configure)


def test_o57_the_storage_is_declared_to_onedata_as_it_always_was():
    """O57: the cell sees its storage at the same path, so the provider's posix storage is declared the same way:
    `posix-local` at /volumes/posix, owned by the job's uid and gid (finish.sh), with or without the bind."""
    finish = read("finish.sh")
    assert '{"posix-local": {"type": "posix", "mountPoint": "/volumes/posix", "rootUid": %d, "rootGid": %d' in finish
    assert '"$(id -u)" "$(id -g)"' in finish


# -- the service's argv, with the stand-in apptainer -------------------------------------------------------------------


def provider_work(holder, **kwargs):
    """A provider's work directory as node-init.sh and configure.sh leave it, its rootfs's /volumes/posix empty."""
    work = node.make_work(holder, **kwargs)
    (work / "rootfs" / "volumes" / "posix").mkdir(parents=True)
    (work / "rootfs" / "etc").mkdir()
    (work / "rootfs" / "etc" / "hpcop-erlang-env").write_text("ERL_EPMD_PORT=21500\n", encoding="utf-8")
    return work


def stand_in_env(holder, tmp_path):
    """The node's environment with the stand-in apptainer first on PATH, recording the service's argv and ending."""
    bin_dir = node.install_fakes(tmp_path / "bin")
    return node.node_env(
        holder, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_RUN_ARGS=str(tmp_path / "run-args"),
        FAKE_LOG=str(tmp_path / "fake-log"), HPCOP_STOP_SECONDS="30",
    )  # fmt: skip


def started(tmp_path):
    """The argv the stand-in service was started with, one word per line; None when it never was."""
    args = tmp_path / "run-args"
    return args.read_text(encoding="utf-8").splitlines() if args.exists() else None


def run_node(holder, tmp_path, *, record=None, given=None):
    """run-node.sh over a provider's work directory: `record` is what configure.sh's prepare recorded first, `given`
    the storage this start names. (The work directory, the script's end, the service's argv or None.)"""
    work = provider_work(holder)
    if record is not None:
        prepare(work, record)
    named = [f"--storage={given}"] if given is not None else []
    done = node.script("run-node.sh", "--workdir", str(work), *named, env=stand_in_env(holder, tmp_path))
    return work, done, started(tmp_path)


def service_argv(holder, tmp_path, storage=None):
    """The service's argv at a start that names `storage`, as recorded at the first (none: neither)."""
    work, done, argv = run_node(holder, tmp_path, record=storage, given=storage)
    assert done.returncode == 0, done.stderr
    return work, argv


@pytest.fixture
def holder(tmp_path):
    path = (tmp_path / "tmpfs").resolve()
    path.mkdir(mode=0o700)
    return path


@node.needs_pidfd
def test_o57_without_a_storage_the_service_is_started_exactly_as_before(holder, tmp_path):
    """O57: no HPC_STORAGE_ROOT, no record: the service's argv is word for word what it was, with no bind, so the
    provider stores in its rootfs's /volumes/posix on the node's tmpfs."""
    work, argv = service_argv(holder, tmp_path)
    assert argv == [
        "exec", "--writable", "--containall", "--no-home", "--pwd", "/root", "--workdir", f"{work}/work",
        f"{work}/rootfs", "/root/hpcop-start.sh",
    ]  # fmt: skip


@node.needs_pidfd
def test_o57_with_a_storage_the_service_binds_it_onto_the_posix_mountpoint_before_the_image(holder, tmp_path, base):
    """O57: with the record, the one bind is the storage onto /volumes/posix, writable, placed before the rootfs; the
    rest of the argv is unchanged."""
    storage = make_storage(base)
    work, argv = service_argv(holder, tmp_path, storage)
    assert argv == [
        "exec", "--writable", "--containall", "--no-home", "--pwd", "/root", "--workdir", f"{work}/work",
        "--bind", f"{storage}:/volumes/posix:rw", f"{work}/rootfs", "/root/hpcop-start.sh",
    ]  # fmt: skip


def test_o57_the_run_takes_its_storage_bind_from_the_function_alone_and_a_failure_stops_the_start():
    """O57: run_service binds the storage only through storage_binds, after the imports' binds and before the rootfs,
    and dies when it fails: a provider never starts on tmpfs because its storage could not be bound."""
    run = read("lib.sh")
    body = run[run.index("run_service() {") :]
    body = body[: body.index("\n}\n")]
    assert 'storage_binds "$W" "$storage") || die' in body
    assert body.index('import_binds "$W"') < body.index('storage_binds "$W"') < body.index("exec ")
    exec_line = body[
        body.index('\n  exec "$HPCOP_PY"') :
    ]  # the command itself: the hosts bind of the declared names (O73)
    assert '"${naming[@]}"' in exec_line  # is an array of its own, set above, never a word of this line
    assert exec_line.index('"${binds[@]}"') < exec_line.index('"$R"')
    assert not re.search(r"--bind[ =]", exec_line)


@node.needs_pidfd
@pytest.mark.parametrize(("recorded", "named"), [(True, False), (False, True)], ids=["record-only", "named-only"])
def test_o57_a_start_whose_storage_and_record_disagree_never_starts_the_service(
    holder, tmp_path, base, recorded, named
):
    """O57: a work directory that records a storage started with none named (the state's argument lost), and a start
    that names one the work directory never recorded, are both refused before the service: never on tmpfs instead."""
    storage = make_storage(base)
    _, done, argv = run_node(holder, tmp_path, record=storage if recorded else None, given=storage if named else None)
    assert done.returncode != 0
    assert argv is None


def test_o57_the_provider_job_passes_its_storage_to_its_service_and_configuration():
    """O57: provider.sbatch takes the one --storage= of its arguments for its service's start, and configure.sh gets
    every argument as before."""
    batch = read("provider.sbatch")
    assert 'split_storage STORAGE REST "$@"' in batch
    assert 'bash "$B/run-node.sh" --workdir "$W" ${STORAGE:+"$STORAGE"} &' in batch
    assert 'configure.sh" op --workdir "$W" "$NAME" "$ZONE" "$MAIL" "$@"' in batch


# -- a start again (restart.sh), with the stand-in apptainer -----------------------------------------------------------


def kept(holder, kind="op"):
    """A provider's work directory a kept stop left (teardown.sh --keep): the keep marks and the identity."""
    work = provider_work(holder, kind=kind)
    node.put(work / "stop-requested", "keep\n")
    node.put(work / "keep-until", f"{int(time.time()) + 3600}\n")
    node.put(work / "identity.json", json.dumps({"kind": kind, "providerId": "pid-1", "domain": "a.invalid"}) + "\n")
    return work


def restart(holder, tmp_path, work, *args, kind="op"):
    return node.script(
        "restart.sh", kind, "--workdir", str(work), *args, env=stand_in_env(holder, tmp_path), timeout=120
    )


@node.needs_pidfd
def test_o57_a_start_again_binds_the_storage_it_is_given_with_the_providers_data_in_it(holder, tmp_path, base):
    """O57: restart.sh, given the storage by `hpcfed.py start`, starts the service with the same bind, the provider's
    data in it. (The stand-in service ends at once, so the restart then fails its readiness: only the start is read.)"""
    storage = make_storage(base)
    work = kept(holder)
    prepare(work, storage)
    (storage / "space-id").mkdir()
    (storage / "space-id" / "data.bin").write_bytes(b"what the provider stored")
    restart(holder, tmp_path, work, f"--storage={storage}")
    argv = started(tmp_path)
    assert argv is not None
    assert argv[argv.index("--bind") : argv.index("--bind") + 2] == ["--bind", f"{storage}:/volumes/posix:rw"]
    assert argv.index("--bind") < argv.index(f"{work}/rootfs")


def test_o57_a_start_again_without_the_storage_its_work_directory_records_changes_nothing(holder, tmp_path, base):
    """O57: restart.sh named no storage over a work directory that records one is refused before it takes the
    directory over: its owner and its kept stop are as they were, and no service was started."""
    storage = make_storage(base)
    work = kept(holder)
    prepare(work, storage)
    owner = (work / "owner.pid").read_text(encoding="utf-8")
    done = restart(holder, tmp_path, work)
    assert done.returncode != 0
    assert "names none" in done.stderr
    assert started(tmp_path) is None
    assert (work / "owner.pid").read_text(encoding="utf-8") == owner
    assert (work / "stop-requested").read_text(encoding="utf-8") == "keep\n"


def test_o57_a_start_again_of_the_zone_takes_no_storage(holder, tmp_path, base):
    """O57: the zone has no posix storage to bind: restart.sh refuses one, naming why, before anything."""
    work = kept(holder, kind="oz")
    done = restart(holder, tmp_path, work, f"--storage={make_storage(base)}", kind="oz")
    assert done.returncode != 0
    assert "the zone has no posix storage" in done.stderr
    assert started(tmp_path) is None

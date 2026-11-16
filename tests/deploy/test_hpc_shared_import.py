"""A shared imported tree on the HPC providers (deploy/hpc-federation, O56): the node-side script that checks and binds
it read-only (`bundle/importstorage.py`), the shell wiring that passes it to the service, and the read-only proof from
the mounts the running service holds. Nothing here needs a cluster: the trees are temporary directories, the mount
tables are text, and the service's processes are a fake proc directory."""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
BUNDLE = HPC / "bundle"
SCRIPT = BUNDLE / "importstorage.py"


def load():
    spec = importlib.util.spec_from_file_location("hpcop_importstorage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpcop_importstorage"] = module
    spec.loader.exec_module(module)
    return module


imp = load()
NO_MOUNTS = ""  # a mount table that names nothing: no tree is on node-local memory


def cli(*args, check=False):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, timeout=60, check=check
    )


@pytest.fixture
def base(tmp_path):
    return tmp_path.resolve()


@pytest.fixture
def tree(base):
    """A shared tree, as a directory with a file in it."""
    path = base / "shared" / "alpha"
    path.mkdir(parents=True)
    (path / "day-01.csv").write_text("01,ok\n", encoding="utf-8")
    return path


@pytest.fixture
def workdir(base):
    """A provider's private work directory with an unpacked rootfs, as node-init.sh leaves it."""
    path = base / "tmp" / "hpcop-op.abc123"
    (path / "rootfs" / "volumes").mkdir(parents=True)
    path.chmod(0o700)
    (path / "rootfs.complete").touch()
    return path


# -- the argument ------------------------------------------------------------------------------------------------------


def test_o56_an_import_argument_is_a_space_and_the_path_of_its_tree():
    assert imp.parse_arg("--import=alpha=/srv/shared/alpha") == ("alpha", "/srv/shared/alpha")


@pytest.mark.parametrize(
    "arg",
    [
        "--import=alpha",
        "--import==/srv/x",
        "--import=Alpha=/srv/x",
        "--import=-alpha=/srv/x",
        "--import=alpha=",
        "--import=" + "a" * 64 + "=/srv/x",
        "--import=alpha=/srv/x y",
        "--import=alpha=/srv/x;rm",
        "--import=alpha=/srv/$HOME",
        "--import=alpha=/srv/x:/etc",
        "--import=alpha=/srv/x,ro",
        "--import=alpha=/srv/\u00e9",
        "--import=alpha=/srv/x\n/etc",
        "--import=alpha=/" + "x" * 4096,
        "--other=alpha=/srv/x",
    ],
)
def test_o56_an_argument_outside_the_narrow_form_is_refused_without_echoing_the_path(arg):
    """The path reaches a bind option and a file: a character that could end the option or add one never gets there."""
    with pytest.raises(imp.ImportRefused) as caught:
        imp.parse_arg(arg)
    assert "/srv" not in str(caught.value)


# -- the tree ----------------------------------------------------------------------------------------------------------


def refused(path, workdir, mounts=NO_MOUNTS, space="alpha"):
    with pytest.raises(imp.ImportRefused) as caught:
        imp.check_source(space, str(path), workdir, mounts)
    assert str(path) not in str(caught.value)  # never the value itself
    return str(caught.value)


def test_o56_a_tree_that_is_a_plain_directory_is_taken(tree, workdir):
    imp.check_source("alpha", str(tree), workdir, NO_MOUNTS)


def test_o56_a_path_that_is_not_in_its_plain_absolute_form_is_refused(tree, workdir):
    assert "plain" in refused("relative/alpha", workdir)
    assert "plain" in refused(f"{tree}/../alpha", workdir)
    assert "plain" in refused(f"{tree}/", workdir)
    assert "plain" in refused(f"{tree}//x", workdir)
    with pytest.raises(imp.ImportRefused, match="plain"):  # the filesystem's root, whose message names the character
        imp.check_source("alpha", "/", workdir, NO_MOUNTS)


def test_o56_a_link_on_the_way_to_the_tree_is_refused(tree, workdir, base):
    """A link can be re-pointed between the check and the mount; the real path is what is named."""
    (base / "link").symlink_to(tree)
    assert "symbolic link" in refused(base / "link", workdir)
    (base / "dirlink").symlink_to(tree.parent)
    assert "symbolic link" in refused(base / "dirlink" / "alpha", workdir)


def test_o56_a_tree_that_is_missing_or_not_a_directory_is_refused(tree, workdir, base):
    assert "does not exist" in refused(base / "absent", workdir)
    assert "not a directory" in refused(tree / "day-01.csv", workdir)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_o56_a_tree_this_user_cannot_read_is_refused(tree, workdir):
    tree.chmod(0)
    try:
        assert "readable" in refused(tree, workdir)
    finally:
        tree.chmod(0o755)


MOUNTINFO = (
    "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "30 22 0:25 / {tmp} rw,nosuid,nodev shared:2 - tmpfs tmpfs rw,size=1024k\n"
    "31 22 0:40 / /scratch rw,relatime shared:3 - lustre 10.0.0.1@o2ib:/fs rw\n"
)


def test_o56_a_tree_on_node_local_memory_is_refused_since_the_other_nodes_do_not_see_it(base, workdir):
    """A tree on tmpfs belongs to one node: another federation's provider on another node would import something
    else, or nothing."""
    local = base / "ram"
    (local / "alpha").mkdir(parents=True)
    table = MOUNTINFO.format(tmp=local)
    assert "node-local memory" in refused(local / "alpha", workdir, table)
    elsewhere = base / "disk"
    elsewhere.mkdir()
    imp.check_source("alpha", str(elsewhere), workdir, table)


def test_o56_the_mount_table_is_read_for_the_longest_mount_that_holds_the_path(base, workdir):
    inner = base / "ram" / "inner"
    inner.mkdir(parents=True)
    table = f"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n30 22 0:25 / {base / 'ram'} rw - tmpfs tmpfs rw\n"
    table += f"31 30 0:26 / {inner} rw - lustre fs rw\n"
    imp.check_source("alpha", str(inner), workdir, table)  # a shared mount inside the tmpfs: the inner one counts


def test_o56_a_mount_table_that_cannot_be_read_refuses_the_tree(tree, workdir, monkeypatch):
    """Fail closed: not knowing that a tree is shared is not knowing that it is."""
    monkeypatch.setattr(imp, "MOUNTINFO", "/nonexistent/mountinfo")
    with pytest.raises(imp.ImportRefused, match="mount table cannot be read"):
        imp.prepare(workdir, [("alpha", str(tree))])


def test_o56_a_tree_inside_or_holding_the_work_directory_is_refused(workdir, base):
    inside = workdir / "rootfs" / "volumes" / "x"
    inside.mkdir()
    assert "work directory" in refused(inside, workdir)
    assert "work directory" in refused(base, workdir)


# -- prepare and binds -------------------------------------------------------------------------------------------------


def test_o56_prepare_makes_the_mountpoint_and_the_list_and_binds_name_the_tree_read_only(tree, workdir):
    imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    mountpoint = workdir / "rootfs" / "volumes" / "import" / "alpha"
    assert mountpoint.is_dir()
    assert not any(mountpoint.iterdir())
    assert stat.S_IMODE(mountpoint.stat().st_mode) == 0o755
    listed = workdir / "imports"
    assert stat.S_IMODE(listed.stat().st_mode) == 0o600
    assert listed.read_text(encoding="utf-8") == f"alpha\t{tree}\n"
    assert imp.binds(workdir) == ["--bind", f"{tree}:/volumes/import/alpha:ro"]
    assert (tree / "day-01.csv").read_text(encoding="utf-8") == "01,ok\n"  # nothing was written to the tree
    assert sorted(p.name for p in tree.iterdir()) == ["day-01.csv"]


def test_o56_every_bind_is_read_only_and_one_per_space_in_the_listed_order(base, workdir):
    trees = []
    for name in ("beta", "alpha"):
        trees.append((name, base / "shared" / name))
        trees[-1][1].mkdir(parents=True)
    imp.prepare(workdir, [(name, str(path)) for name, path in trees], mountinfo=NO_MOUNTS)
    args = imp.binds(workdir)
    assert args[0::2] == ["--bind", "--bind"]
    assert all(value.endswith(":ro") for value in args[1::2])
    assert [value.split(":")[1] for value in args[1::2]] == ["/volumes/import/alpha", "/volumes/import/beta"]


def test_o56_a_service_with_no_imports_binds_nothing(workdir):
    assert imp.binds(workdir) == []


def test_o56_a_second_tree_that_is_refused_leaves_nothing_made_for_the_first(tree, workdir, base):
    with pytest.raises(imp.ImportRefused):
        imp.prepare(workdir, [("alpha", str(tree)), ("beta", str(base / "absent"))], mountinfo=NO_MOUNTS)
    assert not (workdir / "imports").exists()
    assert not (workdir / "rootfs" / "volumes" / "import").exists()


def test_o56_one_space_twice_or_one_tree_twice_is_refused(tree, workdir):
    with pytest.raises(imp.ImportRefused, match="twice"):
        imp.prepare(workdir, [("alpha", str(tree)), ("alpha", str(tree))], mountinfo=NO_MOUNTS)
    with pytest.raises(imp.ImportRefused, match="twice"):
        imp.prepare(workdir, [("alpha", str(tree)), ("beta", str(tree))], mountinfo=NO_MOUNTS)


def test_o56_a_mountpoint_that_holds_something_or_is_a_link_is_refused(tree, workdir, base):
    """A bind over a directory hides what it holds; a link would carry the bind outside the rootfs."""
    mountpoint = workdir / "rootfs" / "volumes" / "import" / "alpha"
    mountpoint.mkdir(parents=True)
    (mountpoint / "stray").touch()
    with pytest.raises(imp.ImportRefused, match="mountpoint"):
        imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    (mountpoint / "stray").unlink()
    mountpoint.rmdir()
    mountpoint.symlink_to(base)
    with pytest.raises(imp.ImportRefused, match="mountpoint"):
        imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    assert not (workdir / "imports").exists()


def test_o56_a_work_directory_that_is_not_a_providers_private_one_is_refused(tree, base):
    zone = base / "tmp" / "hpcop-oz.abc123"
    (zone / "rootfs").mkdir(parents=True)
    zone.chmod(0o700)
    (zone / "rootfs.complete").touch()
    with pytest.raises(imp.ImportRefused, match="provider's private work directory"):
        imp.prepare(zone, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    open_dir = base / "tmp" / "hpcop-op.zzz999"
    (open_dir / "rootfs").mkdir(parents=True)
    open_dir.chmod(0o755)
    with pytest.raises(imp.ImportRefused, match="provider's private work directory"):
        imp.prepare(open_dir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)


def test_o56_a_work_directory_without_its_unpacked_rootfs_is_refused(tree, workdir):
    (workdir / "rootfs.complete").unlink()
    with pytest.raises(imp.ImportRefused, match="no complete rootfs"):
        imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)


def test_o56_a_tree_that_became_a_link_after_prepare_is_refused_when_the_service_starts(tree, workdir, base):
    """`binds` checks again: the start, or a restart, may come long after prepare."""
    imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    moved = base / "elsewhere"
    tree.rename(moved)
    tree.symlink_to(moved)
    with pytest.raises(imp.ImportRefused, match="symbolic link"):
        imp.binds(workdir)


def test_o56_a_list_that_was_edited_into_something_else_is_refused_at_the_start(tree, workdir):
    imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    (workdir / "imports").write_text("alpha\t/srv/x:/etc\n", encoding="utf-8")
    with pytest.raises(imp.ImportRefused):
        imp.binds(workdir)


def test_o56_the_command_line_refuses_what_the_functions_refuse_and_says_so_on_stderr(workdir, base):
    done = cli("prepare", "--workdir", str(workdir), f"--import=alpha={base}/absent")
    assert done.returncode == 1
    assert "importstorage: ERROR" in done.stderr
    assert str(base) not in done.stderr + done.stdout
    assert cli("binds", "--workdir", str(workdir)).stdout == ""
    assert cli("nothing").returncode == 1
    assert cli("prepare", "--workdir", str(workdir), "stray").returncode == 1


# -- the read-only proof from the running service's mounts ------------------------------------------------------------


def mountinfo(options="ro,relatime", space="alpha", extra=""):
    return (
        "100 99 0:50 / / rw,relatime - overlay overlay rw\n"
        f"101 100 0:60 /shared/{space} /volumes/import/{space} {options} master:3 - lustre fs rw,flock\n" + extra
    )


@pytest.fixture
def proc(base):
    """A fake /proc: the service's processes' mount tables, by pid."""
    root = base / "proc"
    root.mkdir()

    def add(pid, text, uid=None):
        (root / str(pid)).mkdir()
        (root / str(pid) / "mountinfo").write_text(text, encoding="utf-8")

    add.root = root
    return add


def listed(workdir, tree, *more):
    imp.prepare(workdir, [("alpha", str(tree)), *more], mountinfo=NO_MOUNTS)


def test_o56_a_read_only_mount_in_every_process_that_holds_it_passes(tree, workdir, proc):
    listed(workdir, tree)
    proc(11, mountinfo())
    proc(12, mountinfo())
    facts, ok = imp.check_mounts(workdir, proc.root)
    assert ok is True
    assert facts == [{"space": "alpha", "processes": 2, "read_only": 2, "writable": 0}]


def test_o56_a_writable_mount_in_one_process_fails_whatever_the_others_hold(tree, workdir, proc):
    listed(workdir, tree)
    proc(11, mountinfo())
    proc(12, mountinfo(options="rw,relatime"))
    facts, ok = imp.check_mounts(workdir, proc.root)
    assert ok is False
    assert facts == [{"space": "alpha", "processes": 2, "read_only": 1, "writable": 1}]


def test_o56_the_flag_of_the_mount_counts_never_the_flag_of_the_filesystem(tree, workdir, proc):
    """A read-only bind of a writable filesystem is `ro` in the mount's options and `rw` after the hyphen; the reverse
    is a writable mount of a read-only filesystem, which this check also reads as writable."""
    listed(workdir, tree)
    proc(11, mountinfo(options="ro,relatime"))  # superblock rw: the shape of a read-only bind
    assert imp.check_mounts(workdir, proc.root)[1] is True
    (proc.root / "11" / "mountinfo").write_text(mountinfo(options="rw,relatime").replace("rw,flock", "ro"))
    assert imp.check_mounts(workdir, proc.root)[1] is False


def test_o56_a_tree_no_process_holds_fails_since_nothing_is_proven(tree, workdir, proc):
    listed(workdir, tree)
    proc(11, "100 99 0:50 / / rw,relatime - overlay overlay rw\n")
    facts, ok = imp.check_mounts(workdir, proc.root)
    assert ok is False
    assert facts == [{"space": "alpha", "processes": 0, "read_only": 0, "writable": 0}]


def test_o56_every_space_the_service_lists_is_checked_and_another_mountpoint_is_ignored(tree, workdir, proc, base):
    second = base / "shared" / "beta"
    second.mkdir(parents=True)
    listed(workdir, tree, ("beta", str(second)))
    proc(11, mountinfo() + mountinfo(space="beta", options="rw")[len(mountinfo("rw", "beta").split("\n")[0]) + 1 :])
    proc(12, mountinfo(space="gamma", options="rw"))  # a space this service does not list
    facts, ok = imp.check_mounts(workdir, proc.root)
    assert ok is False
    assert [f["space"] for f in facts] == ["alpha", "beta"]
    assert [f["writable"] for f in facts] == [0, 1]


def test_o56_a_process_that_is_gone_or_not_a_process_is_skipped(tree, workdir, proc):
    listed(workdir, tree)
    proc(11, mountinfo())
    (proc.root / "self").mkdir()
    (proc.root / "13").mkdir()  # a process that ended between the listing and the read: no mountinfo
    assert imp.check_mounts(workdir, proc.root)[1] is True


def test_o56_the_processes_of_another_user_are_not_counted(tree, workdir, proc):
    listed(workdir, tree)
    proc(11, mountinfo())
    facts, ok = imp.check_mounts(workdir, proc.root, uid=os.getuid() + 1)
    assert (ok, facts[0]["processes"]) == (False, 0)


def test_o56_the_mount_table_decodes_the_escapes_the_kernel_writes(tree, workdir, proc):
    listed(workdir, tree)
    proc(11, mountinfo().replace("/volumes/import/alpha", "/volumes/import/alpha", 1))
    assert imp.parse_mountinfo("1 0 0:1 / /a\\040b ro - ext4 x rw\n") == [("/a b", {"ro"}, "ext4")]


def test_o56_the_mounts_command_prints_counts_and_never_a_path(tree, workdir, proc):
    listed(workdir, tree)
    proc(11, mountinfo())
    done = cli("mounts", "--workdir", str(workdir), "--proc", str(proc.root))
    assert done.returncode == 0
    assert json.loads(done.stdout) == [{"space": "alpha", "processes": 1, "read_only": 1, "writable": 0}]
    assert str(tree) not in done.stdout + done.stderr
    proc(12, mountinfo(options="rw"))
    failed = cli("mounts", "--workdir", str(workdir), "--proc", str(proc.root))
    assert failed.returncode == 1
    assert "FAIL" in failed.stderr


# -- the shell wiring --------------------------------------------------------------------------------------------------


def lib(command: str, *args: str):
    """A bundle function in a fresh bash that sources lib.sh, its Python helpers run by this interpreter."""
    script = f'source "{BUNDLE}/lib.sh"; {command} "$@"'
    env = {**os.environ, "HPCOP_PY": sys.executable}
    return subprocess.run(
        ["bash", "-c", script, "lib", *args], capture_output=True, text=True, timeout=30, check=False, env=env
    )


def test_o56_the_arguments_are_split_into_the_imports_and_the_peers():
    script = (
        f'source "{BUNDLE}/lib.sh"; split_imports IMPORTS PEERS "$@"; '
        'printf "I=%s\\n" "${IMPORTS[@]}"; printf "P=%s\\n" "${PEERS[@]}"'
    )
    done = subprocess.run(
        ["bash", "-c", script, "x", "10.0.0.1", "--import=a=/x", "10.0.0.2", "--import=b=/y"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.stdout.split() == ["I=--import=a=/x", "I=--import=b=/y", "P=10.0.0.1", "P=10.0.0.2"]


def test_o56_the_service_binds_what_the_list_names_and_a_service_with_none_binds_nothing(tree, workdir):
    assert lib("import_binds", str(workdir)).stdout == ""
    imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    done = lib("import_binds", str(workdir))
    assert done.returncode == 0
    assert done.stdout.splitlines() == ["--bind", f"{tree}:/volumes/import/alpha:ro"]


def test_o56_a_list_that_cannot_be_bound_stops_the_start(tree, workdir):
    imp.prepare(workdir, [("alpha", str(tree))], mountinfo=NO_MOUNTS)
    tree.rename(tree.parent / "moved")
    done = lib("import_binds", str(workdir))
    assert done.returncode != 0
    assert str(tree) not in done.stdout + done.stderr


def read(name: str) -> str:
    return (BUNDLE / name).read_text(encoding="utf-8")


def test_o56_the_run_binds_the_trees_read_only_before_the_image_and_through_the_function_alone():
    """The bind arguments come from `import_binds` and nowhere else, are placed before the rootfs argument, and the
    exec keeps its flags."""
    run = read("lib.sh")
    body = run[run.index("run_service() {") :]
    body = body[: body.index("\n}\n")]
    assert 'import_binds "$W"' in body
    exec_line = body[body.index("exec ") :]
    assert exec_line.index('"${binds[@]}"') < exec_line.index('"$R"')
    assert "--writable --containall --no-home" in exec_line
    assert not re.search(r"--bind[ =]", exec_line)  # no bind of its own: every one is read from the list


def test_o56_the_provider_job_and_its_configuration_pass_the_imports_through_and_the_zone_takes_none():
    batch, configure = read("provider.sbatch"), read("configure.sh")
    assert 'configure.sh" op --workdir "$W" "$NAME" "$ZONE" "$MAIL" "$@"' in batch
    assert "split_imports" in configure
    assert re.search(r'importstorage\.py" prepare --workdir "\$W"', configure)
    assert "the zone mounts no import" in configure


def test_o56_the_script_never_removes_renames_links_or_truncates_and_writes_only_in_the_work_directory():
    """The shared tree is read, and named as a read-only bind source: nothing in the script can remove, rename, link or
    truncate a path, and the calls that make or write one are in the two functions that build the work directory's
    mountpoints and list from `workdir` alone."""
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    forbidden = {"rmtree", "remove", "unlink", "rmdir", "rename", "renames", "symlink", "link", "truncate", "utime"}
    writers = {"mkdir", "makedirs", "chmod", "replace", "fdopen"}
    where = {}
    for function in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                where.setdefault(name, set()).add(function.name)
                if name == "open" and any(isinstance(a, ast.Constant) and a.value in ("w", "a") for a in node.args):
                    where.setdefault("open-for-write", set()).add(function.name)
    assert not forbidden & set(where)
    assert {f for name in writers for f in where.get(name, ())} <= {"make_mountpoint", "prepare"}
    assert where.get("open-for-write", set()) == set()
    assert "O_WRONLY" in SCRIPT.read_text(encoding="utf-8")  # the list is made with os.open, in prepare


# -- what `up` takes from the layout ----------------------------------------------------------------------------------

LAYOUT = """\
project = "expcore-test"
domain = "expcore-test.test"
study_space = "alpha-space"

[zone]
port = 29643

[providers.oak]
port = 29644

[providers.elm]
port = 29645

[providers.ash]
port = 29646

[spaces.alpha-space]
owner = "oak"
mirrors = ["elm", "ash"]
baseline = false
import = { readonly = true, shared = true, direct_io = true, source_env = "EXPCORE_FED_IMPORT_ALPHA", manifest_env = "EXPCORE_FED_IMPORT_ALPHA_MANIFEST" }

[spaces.beta-space]
owner = "elm"
mirrors = ["oak"]
baseline = false
import = { readonly = true, shared = true, source_env = "EXPCORE_FED_IMPORT_BETA", manifest_env = "EXPCORE_FED_IMPORT_BETA_MANIFEST" }

[spaces.gamma-space]
owner = "oak"
mirrors = ["elm"]

[principals.reader]
privileges = ["space_view", "space_read_data"]
"""  # noqa: E501


def load_hpcimport():
    spec = importlib.util.spec_from_file_location("hpcop_hpcimport", HPC / "hpcimport.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpcop_hpcimport"] = module
    spec.loader.exec_module(module)
    return module


hpcimport = load_hpcimport()


@pytest.fixture
def federation(fed, base):
    from federation_support import import_layout

    layout, data = import_layout(fed, base, LAYOUT)
    trees = {}
    for name in ("alpha", "beta"):
        trees[name] = data / name
        trees[name].mkdir()
    environ = {"EXPCORE_FED_IMPORT_ALPHA": str(trees["alpha"]), "EXPCORE_FED_IMPORT_BETA": str(trees["beta"])}
    return layout, environ, trees


def test_o56_a_provider_is_given_the_trees_it_owns_and_a_mirror_none(federation):
    layout, environ, trees = federation
    assert hpcimport.provider_import_args(layout, "oak", environ) == [f"--import=alpha-space={trees['alpha']}"]
    assert hpcimport.provider_import_args(layout, "elm", environ) == [f"--import=beta-space={trees['beta']}"]
    assert hpcimport.provider_import_args(layout, "ash", environ) == []  # mirrors both and owns nothing


def test_o56_an_argument_made_for_a_provider_is_one_the_node_accepts(federation):
    layout, environ, _ = federation
    for arg in hpcimport.provider_import_args(layout, "oak", environ):
        assert imp.parse_arg(arg)


def test_o56_a_tree_variable_that_is_not_set_is_refused_naming_the_variable_and_not_a_value(federation):
    layout, environ, _ = federation
    del environ["EXPCORE_FED_IMPORT_ALPHA"]
    with pytest.raises(ValueError, match="EXPCORE_FED_IMPORT_ALPHA is not set"):
        hpcimport.provider_import_args(layout, "oak", environ)


def test_o56_an_import_that_is_not_shared_is_refused_on_hpc(fed, base):
    from federation_support import import_layout

    text = LAYOUT.replace("shared = true, ", "", 1).replace("direct_io = true, ", "", 1)
    layout, data = import_layout(fed, base, text)
    with pytest.raises(hpcimport.HpcImportRefused, match=r"spaces\.alpha-space\.import must be shared = true"):
        hpcimport.provider_import_args(layout, "oak", {"EXPCORE_FED_IMPORT_ALPHA": str(data)})


def test_o56_the_creation_of_an_imported_storage_for_a_provider_that_runs_as_the_job_user():
    assert hpcimport.import_storage_extra(1234, 4321) == {
        "rootUid": 1234,
        "rootGid": 4321,
        "lumaFeed": "local",
        "storagePathType": "canonical",
    }
    for bad in (-1, True, "1", None):
        with pytest.raises(hpcimport.HpcImportRefused):
            hpcimport.import_storage_extra(bad, 1)
    # what it adds is never what the layout declares, so the bootstrap's own refusal of an override cannot be met
    declared = {"type", "mountPoint", "importedStorage", "readonly"}
    assert declared.isdisjoint(hpcimport.import_storage_extra(1, 1))


def test_o56_the_user_mappings_are_those_the_onboarding_step_sends_for_the_regular_storage():
    """The same three calls as bundle/onboard.py: the space's default credentials, then each member, to the job user."""
    calls = hpcimport.luma_requests("st1", "sp1", ["u-a", "u-b"], 1234, 4321)
    base = "/provider/storages/st1/luma/local_feed/storage_access"
    assert calls[0] == ("PUT", f"{base}/posix_compatible/default_credentials/sp1", {"uid": 1234, "gid": 4321})
    assert [c[:2] for c in calls[1:]] == [("POST", f"{base}/all/onedata_user_to_credentials")] * 2
    assert calls[1][2] == {
        "onedataUser": {"mappingScheme": "onedataUser", "onedataUserId": "u-a"},
        "storageUser": {"storageCredentials": {"type": "posix", "uid": "1234"}, "displayUid": 1234},
    }
    onboard = (BUNDLE / "onboard.py").read_text(encoding="utf-8")
    for piece in (
        "luma/local_feed/storage_access/posix_compatible/default_credentials/",
        "onedata_user_to_credentials",
    ):
        assert piece in onboard
        assert piece in "".join(c[1] for c in calls)


def test_o56_the_mount_check_is_a_control_step_on_the_owners_node(base):
    argv = hpcimport.mount_check_argv(str(base / "bundle"), str(base / "hpcop-op.abc123"))
    assert argv == [
        "python3.11",
        "-I",
        str(base / "bundle" / "importstorage.py"),
        "mounts",
        "--workdir",
        str(base / "hpcop-op.abc123"),
    ]


def test_o56_a_direct_mount_reads_the_tree_of_each_space_that_declares_it_and_no_other(federation):
    layout, environ, trees = federation
    ids = {("oak", "alpha-space"): "st-alpha", ("elm", "beta-space"): "st-beta"}
    assert hpcimport.direct_storages(layout, ids, environ) == {"st-alpha": str(trees["alpha"])}
    with pytest.raises(hpcimport.HpcImportRefused, match="alpha-space"):
        hpcimport.direct_storages(layout, {}, environ)


def test_o56_the_storage_of_a_provider_that_is_not_the_owner_is_its_own_regular_one(federation, fed):
    """The mirrors mount nothing and keep node-local storage: the storage the bootstrap picks for them is not the
    imported one, which only the owner's support names."""
    layout, _, _ = federation
    space = layout.spec.space("alpha-space")
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout, boot.spec = layout, layout.spec
    boot.storages = lambda service: {"posix": ("regular-id", {}), "import-alpha-space": ("import-id", {})}
    assert boot.storage_for(layout.spec.provider("oak"), space) == "import-id"
    assert boot.storage_for(layout.spec.provider("elm"), space) == "regular-id"
    assert boot.storage_for(layout.spec.provider("ash"), space) == "regular-id"


# -- the example layout and its platform template ---------------------------------------------------------------------

LAYOUT_FILE = HPC / "layouts" / "shared-import.toml"
TEMPLATE = HPC / "layouts" / "shared-import.platform.toml.template"


def rendered(fed, tmp_path) -> str:
    """The template filled from facts, as up fills it, over made-up ids."""
    import fedboot

    spec = fed.spec.load_spec(LAYOUT_FILE)
    layout = fed.layout.Layout(tmp_path, spec=spec)
    boot = fedboot.Bootstrap.__new__(fedboot.Bootstrap)
    boot.layout = layout
    boot.ids = fedboot.Ids(
        users={p.name: f"user-{p.name}" for p in spec.principals},
        providers={p.name: f"provider-{p.name}" for p in spec.providers},
        spaces={s.name: f"space-{s.name}" for s in spec.spaces},
    )
    facts = fed.facts.facts_of(boot)
    return fed.facts.render(TEMPLATE.read_text(encoding="utf-8"), fed.facts.placeholders(facts))


def test_o56_the_example_layout_declares_two_shared_read_only_imports_with_a_manifest_each(fed):
    spec = fed.spec.load_spec(LAYOUT_FILE)
    declared = {s.name: s.imported for s in spec.spaces if s.imported}
    assert set(declared) == {"alpha-data", "beta-data"}
    assert all(d.shared and d.readonly and d.manifest_env for d in declared.values())
    assert {name: d.direct_io for name, d in declared.items()} == {"alpha-data": False, "beta-data": True}
    assert spec.space("alpha-work").imported is None
    assert spec.space("alpha-data").owner != spec.space("beta-data").owner


def test_o56_the_example_layout_and_its_template_hold_no_path_and_no_site_name():
    for path in (LAYOUT_FILE, TEMPLATE):
        text = path.read_text(encoding="utf-8")
        for host_path in ("/srv", "/mnt", "/home", "/scratch", "/net", "/lustre"):
            assert host_path not in text
        assert re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text) is None


def test_o56_the_template_repeats_each_shared_declaration_so_the_core_refuses_a_writable_one(fed, tmp_path):
    """The platform file is what the core reads: a template that left `shared` out of an import the layout shares would
    leave the core unable to refuse a storage that is not read-only, so every declaration is repeated."""
    import tomllib

    spec = fed.spec.load_spec(LAYOUT_FILE)
    entries = {e["name"]: e for e in tomllib.loads(rendered(fed, tmp_path))["spaces"]}
    for space in spec.spaces:
        if space.imported is None:
            assert "import" not in entries[space.name]
        else:
            assert entries[space.name]["import"] == {
                "readonly": space.imported.readonly,
                "shared": space.imported.shared,
            }
        assert entries[space.name]["holder"] == space.owner


def test_o56_the_rendered_template_is_a_platform_file_the_core_loads_with_both_imports_shared(fed, tmp_path):
    from expcore_onedata.config import load_platform
    from expcore_onedata.spaces import ImportedStorage

    platform = tmp_path / "platform.toml"
    platform.write_text(rendered(fed, tmp_path), encoding="utf-8")
    (tmp_path / "ca").mkdir()
    fed.pki.make_ca(tmp_path / "ca" / "federation-ca.pem", tmp_path / "ca.key", "test CA")
    config = load_platform(platform, require_secrets=False)
    shared = ImportedStorage(readonly=True, shared=True)
    assert [config.space_named(label).imported for label in ("alpha-data", "beta-data")] == [shared, shared]
    assert config.space_named("alpha-work").imported is None


def test_o56_only_the_owner_of_each_import_binds_its_tree_and_only_read_only_when_composed(fed):
    """The same layout under Compose binds each tree into its owner's service alone, read-only."""
    model = fed.compose.model(fed.spec.load_spec(LAYOUT_FILE))["services"]
    binds = {name: [v for v in s["volumes"] if v["target"].startswith("/volumes/import/")] for name, s in model.items()}
    assert [(v["target"], v["read_only"]) for v in binds["pine"]] == [("/volumes/import/alpha-data", True)]
    assert [(v["target"], v["read_only"]) for v in binds["birch"]] == [("/volumes/import/beta-data", True)]
    assert not [name for name, found in binds.items() if found and name not in ("pine", "birch")]

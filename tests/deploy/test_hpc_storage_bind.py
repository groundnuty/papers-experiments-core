"""The HPC federation's provider storage bound onto scratch (deploy/hpc-federation, O57; DESIGN.md section 11): with
`HPC_STORAGE_ROOT` set, `up` gives each provider's step its own recorded directory on scratch (`--storage=PATH`), which
the bundle binds onto `/volumes/posix`; without it the steps are what they were and the providers store on their nodes'
tmpfs. The inode guard is mandatory for a federation bound onto scratch, and `HPC_QUOTA_CHECK=off` is refused with it.
The cell sees its storage at the same path either way, so the rendered platform file and its fingerprint are the same,
and the data check reads each space through its owner, wherever that provider stores it. Over the fake Slurm of
hpc_fed_support and temporary directories for scratch; nothing reaches a cluster."""

import dataclasses
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeProcess, FakeSlurm, agent_site, data_stand_in, hpc_on_path
from hpc_rehearsal_support import BootStandIn, link_check_stand_in

from expcore_onedata.config import load_platform

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]
PATTERN = r"inodes used (?P<used>[0-9,]+) of (?P<quota>[0-9,]+)"  # an invented format, as invented as its tool
TOOL = "fake-quota"  # an invented command name
MARKER = ".hpcop-storage-owner"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedcontent
        import fedlinks
        import hpcfed_control

        modules.fedlinks, modules.content, modules.control = fedlinks, fedcontent, hpcfed_control
        yield modules


@pytest.fixture(scope="module")
def ca_pem(tmp_path_factory) -> str:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_text(encoding="utf-8")


def make_site(base: Path, *, scratch: bool) -> dict:
    """A site of its own below `base`: images, logs, the agent's files, and with `scratch` a scratch root and the
    invented quota command."""
    images, logs = base / "images", base / "logs"
    for directory in (images, logs, base / "fed"):
        directory.mkdir(parents=True)
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    site = {**SITE, **agent_site(base / "agent"), "SH": str(images), "L": str(logs)}
    if scratch:
        (base / "scratch").mkdir()
        (base / "scratch").chmod(0o755)  # this user's alone to write, whatever the umask
        site.update(HPC_STORAGE_ROOT=str(base / "scratch"), HPC_QUOTA_CMD=f"{TOOL} --inodes", HPC_QUOTA_PATTERN=PATTERN)
    return site


class Quota:
    """The invented quota command: well under any line, every call recorded."""

    def __init__(self, hpc):
        self.completed, self.calls = hpc.slurm.Completed, []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        return self.completed(0, "inodes used 1,000 of 1,000,000\n", "")


class Rig:
    """A driver over the fake Slurm below `base`, the core's phase recorded (or the real one, given), the quota
    command answered."""

    def __init__(self, hpc, base, site, *, core=None, ca_pem=None):
        self.hpc, self.site, self.quota = hpc, site, Quota(hpc)
        self.fake = FakeSlurm(tools={TOOL: self.quota}, **({"ca_pem": ca_pem} if ca_pem else {}))
        self.root = base / "fed" / "root"
        self.driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake, environ={})
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.core_calls = []
        self.driver.core = core or self.record_core
        self.driver.relay_available = lambda: False
        self.said = []
        self.driver.say = self.said.append

    def record_core(self, layout, owner, values, environ=None):
        self.core_calls.append(values)
        return "core: the link check passed"

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def state(self):
        return self.driver.store.read()

    def bound(self) -> dict:
        """Each provider's bound directory, as up made it: its recorded directory's `posix`."""
        return {name: Path(path) / "posix" for name, path in self.state()["storage"]["providers"].items()}

    def provider_steps(self) -> dict:
        return {step.command[4]: step.command for step in self.fake.spawned[1:]}


def listing(directory: Path) -> set:
    return {entry.name for entry in directory.iterdir()}


# -- the provider's step -------------------------------------------------------------------------------------------


def test_o57_each_provider_step_is_given_its_own_recorded_storage_directory_and_the_zone_none(hpc, tmp_path):
    """O57: with a scratch root, each provider's step ends with `--storage=` naming the `posix` directory in its own
    recorded directory, and no other's; the zone's step takes none."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path, scratch=True))
    rig.up()
    record = rig.state()["storage"]
    zone = rig.fake.spawned[0]
    assert not any(word.startswith("--storage") for word in zone.command)
    steps = rig.provider_steps()
    assert set(steps) == set(PROVIDERS)
    for name, command in steps.items():
        assert [word for word in command if word.startswith("--storage")] == [
            f"--storage={record['providers'][name]}/posix"
        ]
        assert command[-1] == f"--storage={record['providers'][name]}/posix"


def test_o57_without_a_scratch_root_each_provider_step_is_word_for_word_as_before(hpc, tmp_path):
    """O57: no HPC_STORAGE_ROOT, no storage argument: each provider's step is the bundle's batch script, the tarball,
    its name, the zone's name, the admin's mail, the zone's address and every other provider's, exactly."""
    site = make_site(tmp_path, scratch=False)
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    bundle = hpc.driver.BUNDLE
    for index, name in enumerate(PROVIDERS):
        peers = [f"10.0.0.{i}" for i in range(2, 7) if i != index + 2]
        assert rig.provider_steps()[name] == [
            "bash", str(bundle / "provider.sbatch"), str(bundle), f"{site['SH']}/oneprovider.tar", name,
            "node01.cluster.example.org", "admin@example.org", "10.0.0.1", *peers,
        ]  # fmt: skip
    assert rig.state()["storage"] is None


def test_o57_a_storage_argument_is_given_only_for_a_record_of_the_form_up_writes(hpc, tmp_path):
    """O57: the argument is read from the state's record checked whole (`hpcstorage.checked`): an edited record is
    refused, as is a state that names a scratch root and no record (a provider never falls back to tmpfs for want
    of one); a state with neither gives no argument."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path, scratch=True))
    rig.up()
    state = rig.state()
    args = hpc.storage.provider_args
    assert args(state, "larch") == [f"--storage={state['storage']['providers']['larch']}/posix"]
    edited = json.loads(json.dumps(state))
    edited["storage"]["providers"]["larch"] = state["storage"]["providers"]["aspen"]
    with pytest.raises(hpc.storage.StorageRefused, match="not what up records"):
        args(edited, "larch")
    lost = {**state, "storage": None}
    with pytest.raises(hpc.storage.StorageRefused, match="scratch root and no provider storage"):
        args(lost, "larch")
    plain = {**state, "storage": None, "site": {**state["site"], "storage_root": ""}}
    assert args(plain, "larch") == []


def node_script(hpc):
    """The bundle's node-side script (`posixstorage.py`), loaded by its path as the node runs it."""
    spec = importlib.util.spec_from_file_location("hpcop_posixstorage_of_driver", hpc.driver.BUNDLE / "posixstorage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def provider_workdir(base: Path) -> Path:
    """A provider's private work directory with an unpacked rootfs, as node-init.sh leaves it on its node."""
    path = base / "hpcop-op.abc123"
    (path / "rootfs" / "volumes" / "posix").mkdir(parents=True)
    path.chmod(0o700)
    (path / "rootfs.complete").touch()
    return path


@pytest.mark.parametrize("setgid", [False, True], ids=["plain", "setgid-scratch"])
def test_o57_the_directory_up_names_in_a_providers_step_is_the_one_its_node_takes_and_binds(hpc, tmp_path, setgid):
    """O57: end to end, the driver and the node agree: each provider's `--storage=` argument, as up made and named the
    directory, passes the node's checks (posixstorage.py prepare) and is bound onto /volumes/posix, also on a scratch
    root whose setgid bit every directory made in it inherits; another provider's argument is refused on that node."""
    site = make_site(tmp_path, scratch=True)
    if setgid:
        Path(site["HPC_STORAGE_ROOT"]).chmod(0o2755)
        if not Path(site["HPC_STORAGE_ROOT"]).stat().st_mode & stat.S_ISGID:
            pytest.skip("this filesystem keeps no setgid bit on a directory")
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    script, steps = node_script(hpc), rig.provider_steps()
    for name, command in steps.items():
        if setgid:
            assert rig.bound()[name].stat().st_mode & stat.S_ISGID
        work = provider_workdir(tmp_path / "nodes" / name)
        script.prepare(str(work), name, command[-1], mountinfo="")
        assert script.binds(str(work), command[-1]) == ["--bind", f"{rig.bound()[name]}:/volumes/posix:rw"]
    elsewhere = provider_workdir(tmp_path / "nodes" / "elsewhere")
    with pytest.raises(script.StorageRefused, match="not of the form up makes"):
        script.prepare(str(elsewhere), "aspen", steps["larch"][-1], mountinfo="")


def test_o57_a_scratch_root_given_through_a_link_is_refused_before_anything_is_submitted(hpc, tmp_path):
    """O57: each node binds its storage only through a path with no link on the way, so up refuses a scratch root that
    is not its own real path, naming the variable, before the root, the storage or a job exists."""
    site = make_site(tmp_path, scratch=True)
    (tmp_path / "scratch-link").symlink_to(site["HPC_STORAGE_ROOT"])
    rig = Rig(hpc, tmp_path, {**site, "HPC_STORAGE_ROOT": str(tmp_path / "scratch-link")})
    with pytest.raises(RuntimeError, match=r"HPC_STORAGE_ROOT must be its real path") as refused:
        rig.up()
    assert str(tmp_path) not in str(refused.value)
    assert not any(call[0] == "sbatch" for call in rig.fake.calls)
    assert listing(Path(site["HPC_STORAGE_ROOT"])) == set()
    assert not rig.root.exists()


class Verbs:
    """`hpcfed.py stop | start <service>` over a rig's federation, and their node side: status.sh reads a service alive
    while its step is listed, teardown.sh --keep ends its step."""

    def __init__(self, hpc, rig, monkeypatch):
        self.hpc, self.rig = hpc, rig
        rig.fake.on_control = self.node
        monkeypatch.setattr(hpc.control, "_SLEEP", lambda s: None)

    def node(self, srun):
        script = Path(srun.command[1]).name if len(srun.command) > 1 else ""
        if script not in ("status.sh", "teardown.sh"):
            return None
        workdir = srun.command[srun.command.index("--workdir") + 1]
        services = self.rig.state()["services"]
        (step,) = [r["step"]["name"] for r in services.values() if r.get("workdir") == workdir]
        if script == "teardown.sh":
            self.rig.fake.steps.pop(step, None)
            said = 'kept stop ended\nhpcop-stop {"ended": "exit", "seconds": 3.5}\n'
            return FakeProcess(self.hpc.slurm.Completed(0, said, ""))
        alive = step in self.rig.fake.steps
        reading = {"alive": alive, "pid": 4321 if alive else None, "start": "88" if alive else None, "ready": alive}
        return FakeProcess(self.hpc.slurm.Completed(0, json.dumps(reading) + "\n", ""))

    def __call__(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sys, "stdout", out)
            patch.setattr(sys, "stderr", err)
            status = self.hpc.cli.main(["--layout", str(LAYOUT), "--root", str(self.rig.root), *words], {},
                                       runner=self.rig.fake)  # fmt: skip
        return status, err.getvalue()

    def restarts(self):
        return [s.command for s in self.rig.fake.spawned if Path(s.command[1]).name == "restart.sh"]


def test_o57_a_provider_started_again_is_given_its_own_storage_from_the_state_and_the_zone_none(
    hpc, tmp_path, monkeypatch
):
    """O57: `hpcfed.py start` gives restart.sh the provider's own directory on scratch, read from the state's record
    checked whole, so its node binds exactly what the state names; a tmpfs federation's start is as it was."""
    for scratch in (True, False):
        base = tmp_path / ("scratch" if scratch else "tmpfs")
        rig = Rig(hpc, base, make_site(base, scratch=scratch))
        rig.up()
        verb = Verbs(hpc, rig, monkeypatch)
        assert verb("stop", "aspen")[0] == 0
        status, err = verb("start", "aspen")
        assert status == 0, err
        record = rig.state()["services"]["aspen"]
        named = [f"--storage={rig.bound()['aspen']}"] if scratch else []
        script = str(hpc.control.BUNDLE / "restart.sh")
        assert verb.restarts() == [["bash", script, "op", "--workdir", record["workdir"], *named]]


def test_o57_a_provider_whose_state_lost_its_storage_record_is_not_started_again(hpc, tmp_path, monkeypatch):
    """O57: a state that names a scratch root and no storage record (or an edited one) starts no provider: refused
    before anything is recorded or launched, so it is never started on its node's tmpfs instead."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path, scratch=True))
    rig.up()
    verb = Verbs(hpc, rig, monkeypatch)
    assert verb("stop", "aspen")[0] == 0
    before = rig.state()["services"]["aspen"]
    path = rig.root / "hpc-federation.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for broken in (None, {**state["storage"], "providers": {**state["storage"]["providers"], "aspen": "/elsewhere"}}):
        path.write_text(json.dumps({**state, "storage": broken}), encoding="utf-8")
        status, err = verb("start", "aspen")
        assert status == 1
        assert "storage" in err
        assert verb.restarts() == []
        assert rig.state()["services"]["aspen"] == before


# -- what up makes, and down removes ---------------------------------------------------------------------------------


def test_o57_up_makes_in_each_provider_directory_the_empty_directory_its_service_binds_beside_its_marker(hpc, tmp_path):
    """O57: each provider's recorded directory holds its owner marker and the empty directory the service binds
    (0700, this user's, no link): the marker is beside the bound directory, so the provider never sees it."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path, scratch=True))
    rig.up()
    state = rig.state()
    for name, bound in rig.bound().items():
        assert listing(bound.parent) == {MARKER, "posix"}
        assert bound.is_dir()
        assert not bound.is_symlink()
        assert listing(bound) == set()
        info = bound.lstat()
        assert (stat.S_IMODE(info.st_mode), info.st_uid) == (0o700, os.getuid())
        held = json.loads((bound.parent / MARKER).read_text(encoding="utf-8"))
        assert held == {"allocation": state["allocation"]["name"], "token": state["storage"]["token"], "provider": name}


def test_o57_down_removes_what_each_provider_stored_in_its_bound_directory_and_nothing_else(hpc, tmp_path):
    """O57: what a provider stored through its bind (nested space directories, a read-only one, a link out) is removed
    with its directory once the job is gone, proved by its marker as before; another federation's storage beside it
    and a link's target are untouched, and nothing of this federation's remains."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path, scratch=True))
    scratch = Path(rig.site["HPC_STORAGE_ROOT"])
    (scratch / "another-federations-storage").mkdir()
    (scratch / "another-federations-storage" / "data.bin").write_bytes(b"y")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the federation's\n", encoding="utf-8")
    rig.up()
    for bound in rig.bound().values():
        day = bound / "space-id" / "records" / "day"
        day.mkdir(parents=True)
        (day / "file.bin").write_bytes(b"x" * 64)
        (bound / "space-id" / "records").chmod(0o500)
        (bound / "link-out").symlink_to(outside)
    out = rig.driver.down()
    assert "provider storage: 5 directories removed, 0 entries remain" in out
    assert listing(scratch) == {"another-federations-storage"}
    assert (scratch / "another-federations-storage" / "data.bin").read_bytes() == b"y"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "not the federation's\n"


# -- the inode guard is mandatory on scratch -----------------------------------------------------------------------


def test_o57_a_federation_bound_onto_scratch_refuses_the_quota_override_at_load_naming_it(hpc, tmp_path):
    """O57: HPC_QUOTA_CHECK=off does not apply to a federation whose storage is bound onto scratch: the site is refused
    as it is loaded, naming the combination, before anything is checked, made or submitted."""
    site = {**make_site(tmp_path, scratch=True), "HPC_QUOTA_CHECK": "off"}
    with pytest.raises(hpc.settings.SettingsError, match=r"HPC_QUOTA_CHECK=off.*HPC_STORAGE_ROOT"):
        hpc.settings.site_from_environment(site)
    without = {k: v for k, v in site.items() if k not in ("HPC_QUOTA_CMD", "HPC_QUOTA_PATTERN")}
    with pytest.raises(hpc.settings.SettingsError, match=r"HPC_QUOTA_CHECK=off.*HPC_STORAGE_ROOT"):
        hpc.settings.site_from_environment(without)


def test_o57_a_federation_bound_onto_scratch_always_reads_the_quota_before_it_submits(hpc, tmp_path):
    """O57: with a scratch root the quota command runs on every up, before the first sbatch, and up says the figures."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path, scratch=True))
    rig.up()
    assert rig.quota.calls == [[TOOL, "--inodes"]]
    first = [call[0] for call in rig.fake.calls].index("sbatch")
    assert rig.fake.calls.index([TOOL, "--inodes"]) < first
    assert any(line.startswith("inode check: used 1000") for line in rig.said)


def test_o57_the_guard_itself_never_skips_a_federation_bound_onto_scratch(hpc, tmp_path):
    """O57: structural, not only at load: a site that holds a scratch root and the override however it was built is
    refused by the guard, and the quota command is never taken as skipped."""
    import fedlayout
    import fedspec

    site = hpc.settings.site_from_environment(make_site(tmp_path, scratch=True))
    off = dataclasses.replace(site, quota_check="off")
    layout = fedlayout.Layout(tmp_path / "root", tmp_path, fedspec.load_spec(LAYOUT))
    quota = Quota(hpc)
    with pytest.raises(hpc.quota.QuotaRefused, match="cannot be turned off"):
        hpc.quota.guard(FakeSlurm(tools={TOOL: quota}), off, layout, {})
    assert quota.calls == []


def test_o57_a_tmpfs_federation_takes_the_override_and_runs_no_quota_check_as_before(hpc, tmp_path):
    """O57: without a scratch root the providers store on their nodes' tmpfs and there is no quota to guard: the
    override is accepted and changes nothing, and no quota command is run."""
    site = {**make_site(tmp_path, scratch=False), "HPC_QUOTA_CHECK": "off"}
    rig = Rig(hpc, tmp_path, site)
    assert rig.up().startswith("core: the link check passed")
    assert rig.quota.calls == []
    assert rig.state()["storage"] is None
    assert not any("inode check" in line for line in rig.said)


# -- the platform and the data, tmpfs or scratch -------------------------------------------------------------------


def real_core(hpc, label, data=None, content=None):
    """The driver's core phase, run for real over the stand-ins of a live federation (its bootstrap's ids are the
    label's, every link passes)."""

    def core(layout, owner, values, environ=None):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
            return hpc.boot.core_phase(
                layout, owner, values, environ=environ or {},
                rest=lambda ca_file: SimpleNamespace(close=lambda: None), bootstrap=BootStandIn(label),
                steps=lambda boot: None,
                content=content or (lambda boot: SimpleNamespace(reset=lambda timeout: None)),
                data=data or data_stand_in(),
            )  # fmt: skip

    return core


def test_o57_a_tmpfs_and_a_scratch_federation_render_two_fingerprints_that_differ_in_the_storage_kind_alone(
    hpc, tmp_path, ca_pem
):
    """O57: the cell sees its posix storage at /volumes/posix whether it is bound from scratch or not, but where a
    provider stores its data shapes a run's I/O timing, so the rendered file declares the kind (`provider_storage`) and
    the two federations' shards can never merge: their files differ in that one line (the root's path aside), their
    fingerprints in that one entry, and the scratch root's path is in neither."""
    tmpfs = Rig(hpc, tmp_path / "t", make_site(tmp_path / "t", scratch=False), core=real_core(hpc, "f"), ca_pem=ca_pem)
    bound = Rig(hpc, tmp_path / "s", make_site(tmp_path / "s", scratch=True), core=real_core(hpc, "f"), ca_pem=ca_pem)
    for rig in (tmpfs, bound):
        rig.up()
    assert tmpfs.state()["storage"] is None
    assert bound.state()["storage"] is not None
    texts = [
        (rig.root / "platform.toml").read_text(encoding="utf-8").replace(str(base), "<base>").splitlines()
        for rig, base in ((tmpfs, tmp_path / "t"), (bound, tmp_path / "s"))
    ]
    assert len(texts[0]) == len(texts[1])
    changed = [(a.split("#")[0].strip(), b.split("#")[0].strip()) for a, b in zip(*texts, strict=True) if a != b]
    assert changed == [('provider_storage = "tmpfs"', 'provider_storage = "shared_filesystem"')]
    one, two = (load_platform(rig.root / "platform.toml") for rig in (tmpfs, bound))
    assert one.fingerprint() != two.fingerprint()
    assert one.fingerprint()["sandbox_backend"]["provider_storage"] == "tmpfs"
    assert two.fingerprint()["sandbox_backend"]["provider_storage"] == "shared_filesystem"

    def without_kind(fingerprint):
        backend = {k: v for k, v in fingerprint["sandbox_backend"].items() if k != "provider_storage"}
        return {**fingerprint, "sandbox_backend": backend}

    assert without_kind(one.fingerprint()) == without_kind(two.fingerprint())  # nothing else tells them apart
    assert len(one.fingerprint()["spaces"]) == 4  # each space's data identity: the equality is not of empty records
    assert str(tmp_path / "s" / "scratch") not in json.dumps(two.fingerprint())


def test_o57_up_hands_the_core_the_storage_kind_of_its_site_and_never_the_root(hpc, tmp_path):
    """O57: the core's phase is given `hpc_provider_storage`: the word `tmpfs` or `shared_filesystem`, by whether the
    site has a scratch root, and the root's path is in no value of that key."""
    kinds = {}
    for label, scratch in (("t", False), ("s", True)):
        rig = Rig(hpc, tmp_path / label, make_site(tmp_path / label, scratch=scratch))
        rig.up()
        (values,) = rig.core_calls
        kinds[label] = values["hpc_provider_storage"]
        assert str(tmp_path) not in kinds[label]
    assert kinds == {"t": "tmpfs", "s": "shared_filesystem"}


class BoundProviders:
    """Each provider serving its spaces from its bound directory on scratch, as Onedata's canonical posix storage lays
    them out (`<storage>/<space id>/<path>`): the baseline's write lands in the owner's bound directory, and a read
    back reads that directory. Stands in for the REST calls `fedcontent` makes, since the check reads each space
    through its owner and never a path of its own."""

    def __init__(self, hpc, rig):
        self.hpc, self.rig = hpc, rig

    def where(self, boot, space):
        owner = self.hpc.content.writer_of(boot.layout.spec, space).name
        return self.rig.bound()[owner] / boot.ids.spaces[space]

    def content(self, boot):
        from fedlayout import baseline_plan

        def reset(timeout):
            for space, files in baseline_plan(boot.layout.spec).items():
                for entry in files:
                    path = self.where(boot, space) / entry.path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(entry.content())

        return SimpleNamespace(reset=reset)

    def data(self, boot):
        def read(space):
            top = self.where(boot, space)
            files, dirs = {}, set()
            for path in sorted(top.rglob("*")) if top.exists() else []:
                name = str(PurePosixPath(path.relative_to(top)))
                if path.is_dir():
                    dirs.add(name)
                else:
                    files[name] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
            return files, dirs

        return read


def test_o57_the_data_check_reads_each_space_from_the_storage_its_owner_keeps_on_scratch(hpc, tmp_path, ca_pem):
    """O57, O63: with the storage bound, a space's data is in its owner's directory on scratch, and the check reads it
    there through its owner: the baseline up wrote is found to be the manifest, and up is ready."""
    site = make_site(tmp_path, scratch=True)
    rig = Rig(hpc, tmp_path, site, ca_pem=ca_pem)
    providers = BoundProviders(hpc, rig)
    rig.driver.core = real_core(hpc, "f", data=providers.data, content=providers.content)
    assert "data: every space that is not ingested holds the data its manifest declares" in rig.up()
    stored = [path for bound in rig.bound().values() for path in bound.rglob("*") if path.is_file()]
    assert stored  # the baseline is on scratch, in the owners' bound directories


def test_o57_a_space_whose_data_on_scratch_differs_from_its_manifest_fails_up_naming_the_space(hpc, tmp_path, ca_pem):
    """O57, O63: one byte other in a file of the study space's owner's storage on scratch, and up fails, naming the
    space and never a path."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path, scratch=True), ca_pem=ca_pem)
    providers = BoundProviders(hpc, rig)

    def content(boot):
        written = providers.content(boot)

        def reset(timeout):
            written.reset(timeout)
            first = sorted(p for p in providers.where(boot, "alpha-space").rglob("*") if p.is_file())[0]
            first.write_bytes(first.read_bytes()[:-1] + b"!")

        return SimpleNamespace(reset=reset)

    rig.driver.core = real_core(hpc, "f", data=providers.data, content=content)
    with pytest.raises(RuntimeError, match=r"alpha-space does not hold the data its manifest declares") as failed:
        rig.up()
    assert str(tmp_path) not in str(failed.value)

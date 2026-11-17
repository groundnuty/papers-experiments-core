"""O83: the HPC federation provisions and renders the layout's slots as Compose does.

The slot users are ordinary principals of the layout, so `hpcfed.py up` makes them with the Compose bootstrap's own
steps, and `platform.toml` carries the template's `[slots]` and `[toolserver]`, filled and checked against the layout
by the function the Compose renderer calls too. The zone and the providers are fakes, the core's phase is stood in as
the tests of the declared names stand it in, and nothing starts a job. That an existing slot user is never changed is
tested in `test_hpc_fed_slots_provision.py`.
"""

import re
import stat
import tomllib
from types import SimpleNamespace

import pytest
from federation_support import MINIMAL
from hpc_fed_support import (
    HPC,
    SITE,
    FakeSlurm,
    agent_site,
    data_stand_in,
    hpc_on_path,
    pin_values,
)
from slots_boot import boots, modules, provision
from slots_support import (
    SLOT_USERS,
    TEMPLATE,
    WITH_TOOLSERVER,
    artifacts,
    ids_of,
    layout_with_template,
    make_root,
    pilot_artifacts,
    principal_of,
    secret_of,
)

from expcore.config.loading import ConfigError
from expcore_onedata.addresses import AddressMap
from expcore_onedata.config import load_platform
from expcore_onedata.toolserver import server_argv
from expcore_onedata.users import load_slots

LAYOUT = HPC / "layouts" / "pilot-shape-slots.toml"
DOMAIN = "expcore-hpc-slots.test"
PROVIDERS = ("aspen", "larch", "rowan", "alder", "hazel")
DECLARED = {"zone": f"zone.{DOMAIN}", **{p: f"{p}.{DOMAIN}" for p in PROVIDERS}}


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as found:
        yield modules(found)


def test_o83_every_slot_user_is_mapped_to_the_jobs_uid_on_a_providers_storage_like_a_fixed_user(
    hpc, tmp_path, monkeypatch
):
    """O83: the providers run as the job's user, so a user the storage does not map reaches none of its files: the HPC
    bootstrap maps every principal of the layout, the slots' users with the others, on the storage a support names."""
    (_, _, boot, _) = next(b for b in boots(hpc, tmp_path) if b[0] == "hpc")
    boot.ids.admin = "uid-admin"
    boot.ids.users = {p.name: f"uid-{p.name}" for p in boot.spec.principals}
    sent = []
    boot.luma = lambda service, method, path, body: sent.append(body)
    boot.storage_for = lambda service, space: "posix-local"
    monkeypatch.setattr(hpc.fedboot.Bootstrap, "support", lambda self, service, space, sid: None)  # not under test
    boot.support(boot.spec.provider("oak"), boot.spec.space("alpha-space"), "sid-alpha-space")
    mapped = {body["onedataUser"]["onedataUserId"] for body in sent if "onedataUser" in body}
    assert {f"uid-{user}" for user in SLOT_USERS} <= mapped
    assert {f"uid-{p.name}" for p in boot.spec.principals} | {"uid-admin"} == mapped


def test_o83_up_audits_the_slot_users_after_the_members_and_before_it_waits_for_the_import(hpc):
    """O83: the HPC `bring_up` is the Compose bootstrap's steps, so a slot user, an ordinary principal, is made before
    any membership, made a member of each space the layout gives it, and then audited (verified, never changed, when it
    existed), all before the wait that ends `up`."""
    spec = hpc.fedspec.load_spec(LAYOUT)
    taken = []

    class Recorder:
        def __init__(self):
            self.spec = spec

        def __getattr__(self, name):
            return lambda *args, **kwargs: taken.append((name, args))

    hpc.boot.bring_up(Recorder())
    names = [name for name, _ in taken]
    assert names.index("ensure_users") < names.index("ensure_members") < names.index("audit_slot_users")
    assert names.index("audit_slot_users") < names.index("wait_import")
    member_spaces = {args[0] for name, args in taken if name == "ensure_members"}
    assert {s for user in SLOT_USERS for s in principal_of(spec, user).spaces} <= member_spaces


def test_o83_each_slot_user_has_a_password_file_of_mode_0600_and_no_other_file_of_the_root_holds_a_password(
    hpc, tmp_path
):
    """O83: `up` writes a slot user's password as it writes a fixed user's, a 0600 file in a 0700 directory, never
    replaced by a later up; and neither the facts, the platform file nor a record holds one."""
    (_, zone, boot, layout) = next(b for b in boots(hpc, tmp_path) if b[0] == "hpc")
    spec = boot.spec
    provision(boot, spec)
    before = {user: layout.password(user).read_text(encoding="utf-8") for user in SLOT_USERS}
    study = artifacts(tmp_path / "artifacts")
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, layout.root),
        "study_artifacts": str(study),
    }
    hpc.boot.write_facts(layout, boot, values)
    secrets = {user: password for user, (_, password) in zone.users.items()}
    for user in SLOT_USERS:
        assert stat.S_IMODE(layout.password(user).stat().st_mode) == 0o600, user
        assert before[user] == secrets[user]
    assert stat.S_IMODE(layout.users.stat().st_mode) == 0o700
    outside = [p for p in layout.root.rglob("*") if p.is_file() and layout.secrets not in p.parents]
    assert {p.name for p in outside} == {"facts.json", "platform.toml"}
    for path in outside:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not [user for user, password in secrets.items() if password in text], path.name
        assert "an-admin-token-for-the-scan" not in text
    provision(boot, spec)  # a later up reads each password and makes no other
    assert before == {user: layout.password(user).read_text(encoding="utf-8") for user in SLOT_USERS}


# -- the platform file the HPC render writes ------------------------------------------------------------------------


def test_o83_the_same_layout_and_template_render_the_same_slots_and_tool_server_on_both_platforms(hpc, tmp_path):
    """O83: Compose's renderer and the HPC one, over one layout and one template, write the same `[slots]` and the same
    `[toolserver]`: the same users with the same ids and rights, the same paths, commit and conditions, and the slot
    users each provisioned the same."""
    (_, c_zone, c_boot, c_layout), (_, h_zone, h_boot, h_layout) = boots(hpc, tmp_path, WITH_TOOLSERVER)
    spec = c_boot.spec
    study = artifacts(tmp_path / "artifacts")
    for boot in (c_boot, h_boot):
        provision(boot, spec)
    assert c_zone.members == h_zone.members
    hpc.fedfacts.write_facts(c_layout, c_boot, str(study))
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, h_layout.root),
        "study_artifacts": str(study),
    }
    hpc.boot.write_facts(h_layout, h_boot, values)
    c_doc = tomllib.loads(c_layout.platform.read_text(encoding="utf-8"))
    h_doc = tomllib.loads(h_layout.platform.read_text(encoding="utf-8"))
    assert c_doc["slots"] == h_doc["slots"]
    assert c_doc["toolserver"] == h_doc["toolserver"]
    assert hpc.fedfacts.facts_of(c_boot)["slot_users"] == hpc.fedfacts.facts_of(h_boot)["slot_users"]


def test_o83_a_layout_without_slots_renders_the_hpc_platform_file_it_always_did(hpc, tmp_path):
    """O83: the pilot's own layout declares no slot, and its platform file has neither table."""
    assert hpc.fedtools.slot_users(hpc.fedspec.load_spec(HPC / "layouts" / "pilot-shape.toml")) == {}
    spec = hpc.fedspec.load_spec(layout_with_template(TEMPLATE, tmp_path, MINIMAL))
    layout = hpc.fedlayout.Layout(tmp_path / "root", spec=spec)
    make_root(layout)
    boot = SimpleNamespace(layout=layout, ids=hpc.fedboot.Ids(admin="uid-admin", **ids_of(spec)))
    values = hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, layout.root)
    hpc.boot.write_facts(layout, boot, values)
    document = tomllib.loads(layout.platform.read_text(encoding="utf-8"))
    assert "slots" not in document
    assert "toolserver" not in document


# -- the pilot's template, through the core's phase --------------------------------------------------------------------


def pilot_layout(tmp_path, command_form="python"):
    """A copy of the slot layout and its template in the pilot's shape (the tool server's paths under `toolserver/`),
    with the command as the pilot first wrote it (`env`: `/usr/bin/env` and the console script) or as the launcher needs
    it."""
    from slots_support import pilot_form

    folder = tmp_path / "pilot-layout"
    folder.mkdir(exist_ok=True)
    (folder / LAYOUT.name).write_text(LAYOUT.read_text(encoding="utf-8"), encoding="utf-8")
    template = pilot_form((HPC / "layouts" / "pilot-shape-slots.platform.toml.template").read_text(encoding="utf-8"))
    if command_form == "env":
        old = 'command = "@study_artifacts@/toolserver/venv/bin/python"\nargs = ["-m", "onedata_mcp"]'
        script = "@study_artifacts@/toolserver/venv/bin/onedata-mcp"
        new = f'command = "/usr/bin/env"\nargs = ["PYTHONDONTWRITEBYTECODE=1", "{script}"]'
        assert old in template
        template = template.replace(old, new)
    (folder / "pilot-shape-slots.platform.toml.template").write_text(template, encoding="utf-8")
    return folder / LAYOUT.name


@pytest.fixture
def phase(hpc, tmp_path):
    """The core's phase over the pilot-shape slot layout: a root with a map and every principal's credential file, the
    study's artifacts, and the bootstrap and the REST client stood in."""
    return build_phase(hpc, tmp_path, LAYOUT, artifacts(tmp_path / "artifacts"))


def build_phase(hpc, tmp_path, layout_path, study):
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    spec = hpc.fedspec.load_spec(layout_path)
    root = tmp_path / "root"
    layout = hpc.fedlayout.Layout(
        root, tmp_path, hpc.boot.hpc_spec(spec, {s.name: DECLARED[s.name] for s in spec.services})
    )
    secrets = make_root(layout)
    layout.passphrase(spec.zone.name).write_text("A1b2C3d4E5f6G7h8", encoding="utf-8")
    layout.passphrase(spec.zone.name).chmod(0o600)
    layout.ca_pem.parent.mkdir()
    layout.ca_pem.write_text(tls.make_federation_ca(tmp_path / "pki").ca.read_text("ascii"), encoding="utf-8")
    layout.journal.mkdir()
    hpc.names.install_toolshim(layout.toolshim)
    nodes = {role: (f"10.0.0.{i + 1}", [f"node{i + 1:02d}.cluster.example.org"]) for i, role in enumerate(DECLARED)}
    AddressMap.from_document(
        {
            "format": "expcore-address-map/1",
            "hosts": {DECLARED[r]: {"address": a, "nodes": n} for r, (a, n) in nodes.items()},
        }
    ).write(layout.address_map)
    return SimpleNamespace(root=root, layout=layout, spec=spec, tmp=tmp_path, secrets=secrets, study=study)


def run_core(hpc, phase):
    ids = SimpleNamespace(
        admin="uid-admin",
        providers={p.name: f"pid-{p.name}" for p in phase.spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in phase.spec.spaces},
        users={p.name: f"uid-{p.name}" for p in phase.spec.principals},
        scans={},
    )
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, phase.root),
        **pin_values(phase.tmp / "agent"),
        "study_artifacts": str(phase.study),
    }
    return hpc.boot.core_phase(
        phase.layout,
        (5001, 5001),
        values,
        rest=lambda ca, **kwargs: SimpleNamespace(close=lambda: None),
        bootstrap=lambda client, layout, owner: SimpleNamespace(
            layout=layout, ids=ids, spec=layout.spec, registered=dict(DECLARED), rest=client
        ),
        steps=lambda b: None,
        content=lambda b: SimpleNamespace(reset=lambda t: None),
        data=data_stand_in(),
        links=lambda layout, spaces: "link check: stood in",
        verify=lambda *a, **k: None,
    )


def test_o83_the_pilots_platform_file_has_the_slots_and_a_tool_server_the_launcher_starts_and_the_core_loads(
    hpc, phase
):
    """O83: the rendered file loads with its map and resolver, its slots as the lease pool reads them, and the tool
    server is started through the resolve launcher of O73: the study's interpreter, `-I -B`, the root's launcher and
    map, `--`, and `-m <module>`, nothing in its environment; every path is the root's or the study's, and none a
    node's."""
    run_core(hpc, phase)
    path = phase.root / "platform.toml"
    config = load_platform(path)
    slots = load_slots(path, config)
    assert [slot.name for slot in slots.slots] == ["slot-1", "slot-2"]
    assert [(s.data_path.username, s.tool_server.username) for s in slots.slots] == [
        ("slot1-data", "slot1-tools"),
        ("slot2-data", "slot2-tools"),
    ]
    assert [s.tool_server.user_id for s in slots.slots] == ["uid-slot1-tools", "uid-slot2-tools"]
    text = path.read_text(encoding="utf-8")
    pin = tomllib.loads(text)["toolserver"]["commit"]
    assert (
        config.toolserver.commit
        == pin
        == re.search(
            r"[0-9a-f]{40}", HPC.joinpath("layouts", LAYOUT.stem + ".platform.toml.template").read_text()
        ).group(0)
    )
    command, args = server_argv(config)
    assert command == f"{phase.study}/venv/bin/python"
    assert args == ["-I", "-B", str(phase.root / "toolshim" / "resolve_launch.py"), str(phase.layout.address_map), "--",
                    "-m", "onedata_mcp"]  # fmt: skip
    manifests = {name: c.manifest for name, c in config.toolserver.conditions.items()}
    assert manifests == {n: (phase.study / "manifests" / f"{n}.json").resolve() for n in ("knowledge", "writing")}
    assert not re.search(r"cluster\.example\.org|10\.0\.0\.", text)
    assert not [v for v in phase.secrets.values() if v in text]


def test_o83_the_pilots_platform_file_holds_the_rights_the_layout_declares_for_each_slot(hpc, phase):
    """O83: a slot's tool-server rights in the file are its principal's privileges in the layout, and each condition's
    rights are those of the slot that serves it, so the lease pool finds a slot for each condition."""
    run_core(hpc, phase)
    config = load_platform(phase.root / "platform.toml")
    slots = load_slots(phase.root / "platform.toml", config)
    for slot, user in zip(slots.slots, ("slot1-tools", "slot2-tools"), strict=True):
        assert slot.rights == tuple(sorted(principal_of(phase.spec, user).privileges))
    for name in ("knowledge", "writing"):
        group = slots.group_for(config.condition(name).rights)
        assert slots.keys_of(group)


def test_o83_a_tool_server_the_launcher_cannot_start_is_refused_when_the_platform_file_is_loaded(hpc, phase):
    """O83: the core refuses a `[toolserver]` whose command is no Python interpreter or whose arguments are no `-m
    <module>` at load, naming the table, when the platform's names resolve through the launcher: the layout is
    checked for the same when it is read (`fedslots`), and the file as it would be loaded here."""
    run_core(hpc, phase)
    path = phase.root / "platform.toml"
    original = path.read_text(encoding="utf-8")
    for old, new in (
        (f'command = "{phase.study}/venv/bin/python"', f'command = "{phase.study}/bin/server"'),
        ('args = ["-m", "onedata_mcp"]', 'args = ["-c", "import onedata_mcp"]'),
    ):
        assert old in original
        path.write_text(original.replace(old, new), encoding="utf-8")
        with pytest.raises(ConfigError, match=r"\[toolserver\]"):
            load_platform(path)
    path.write_text(original, encoding="utf-8")


def test_o83_the_core_phase_refuses_a_root_whose_slot_has_no_credential_before_it_calls_the_federation_ready(
    hpc, phase
):
    """O83: the rendered file is loaded as a campaign loads it, and its slots as the lease pool reads them, before `up`
    reports the federation ready: a slot user whose password file is gone is refused there, naming no user."""
    phase.layout.password("slot2-tools").unlink()
    with pytest.raises(RuntimeError, match="refused") as refused:
        run_core(hpc, phase)
    assert "slot2" not in str(refused.value)


def test_o83_no_secret_of_the_root_reaches_a_file_a_cell_or_a_record_could_hold(hpc, phase):
    """O83: after the core's phase, no file of the root outside `secrets/` (the facts, the platform file, the
    manifests, the map, the launcher) holds the admin token, the passphrase or any user's password, and every file of
    `secrets/` is private."""
    out = run_core(hpc, phase)
    secrets = {**phase.secrets, "passphrase": "A1b2C3d4E5f6G7h8"}
    assert not [v for v in secrets.values() if v in out]
    for path in phase.root.rglob("*"):
        if phase.layout.secrets in path.parents or path == phase.layout.secrets or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not [name for name, value in secrets.items() if value in text], path.name
    for path in phase.layout.secrets.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path.name
    assert {secret_of(u) for u in SLOT_USERS} <= set(phase.secrets.values())


def test_o83_without_the_study_artifacts_the_core_phase_refuses_naming_the_placeholder(hpc, phase):
    """O83: a `[toolserver]` that names `@study_artifacts@` with no directory given is refused when the file is rendered
    (the driver refuses it earlier, in its preflight)."""
    values = {**hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, phase.root)}
    with pytest.raises(RuntimeError, match="@study_artifacts@"):
        hpc.boot.core_phase(
            phase.layout,
            (5001, 5001),
            {**values, **pin_values(phase.tmp / "agent")},
            rest=lambda ca, **kwargs: SimpleNamespace(close=lambda: None),
            bootstrap=lambda client, layout, owner: SimpleNamespace(
                layout=layout,
                ids=SimpleNamespace(
                    admin="uid-admin",
                    providers={p.name: f"pid-{p.name}" for p in phase.spec.providers},
                    spaces={s.name: f"sid-{s.name}" for s in phase.spec.spaces},
                    users={p.name: f"uid-{p.name}" for p in phase.spec.principals},
                    scans={},
                ),
                spec=layout.spec,
                registered=dict(DECLARED),
                rest=client,
            ),
            steps=lambda b: None,
            content=lambda b: SimpleNamespace(reset=lambda t: None),
            data=data_stand_in(),
            links=lambda layout, spaces: "link check: stood in",
            verify=lambda *a, **k: None,
        )


# -- up: the site's directory and the preflight ------------------------------------------------------------------------


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


class Rig:
    """A driver over the fake Slurm and the slot layout, the core's phase recorded with the values it is handed."""

    def __init__(self, hpc, tmp_path, layout=LAYOUT):
        self.hpc, self.fake, self.root = hpc, FakeSlurm(), tmp_path / "fed" / "root"
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake)
        self.driver.sleep = self.driver.slurm.sleep = lambda s: None
        self.driver.poll_s = self.driver.slurm.poll_s = 0.0
        self.driver.relay_available = lambda: False
        self.values = []
        self.driver.core = lambda layout, owner, values, environ=None: self.values.append(values) or "core: linked"

    def up(self, site):
        return self.driver.up(self.hpc.settings.site_from_environment(site))

    def submitted(self):
        return [c for c in self.fake.calls if c[0] == "sbatch"]


def test_o83_up_without_hpc_study_artifacts_is_refused_naming_the_variable_before_any_job_is_held(hpc, tmp_path, site):
    """O83: the layout's tool server names `@study_artifacts@`, which only `site.env` can fill."""
    rig = Rig(hpc, tmp_path)
    with pytest.raises(RuntimeError, match="HPC_STUDY_ARTIFACTS"):
        rig.up(site)
    assert not rig.submitted()


def test_o83_up_refuses_artifacts_that_cannot_run_the_tool_server_before_any_job_is_held(hpc, tmp_path, site):
    """O83: an interpreter that is not an executable file, a clone that is not at the pinned commit (here no git
    checkout at all), and a manifest that is not the condition's are each refused by the preflight, naming no path."""
    study = artifacts(tmp_path / "study")
    site = {**site, "HPC_STUDY_ARTIFACTS": str(study)}
    (study / "venv" / "bin" / "python").chmod(0o644)
    rig = Rig(hpc, tmp_path)
    with pytest.raises(RuntimeError, match="command") as refused:
        rig.up(site)
    assert str(tmp_path) not in str(refused.value)
    (study / "venv" / "bin" / "python").chmod(0o755)
    with pytest.raises(RuntimeError, match="checkout") as refused:  # a directory that is no git checkout
        Rig(hpc, tmp_path).up(site)
    assert str(tmp_path) not in str(refused.value)
    assert not rig.submitted()


def test_o83_up_hands_the_core_the_sites_study_artifacts_and_records_them_in_the_state(
    hpc, tmp_path, site, monkeypatch
):
    """O83: the core's phase is given `study_artifacts` among its values, as the site declared it, and the state file's
    site holds it (a hand-run `core` step reads it from there), and holds no secret."""
    study = artifacts(tmp_path / "study")
    monkeypatch.setattr("expcore_onedata.pin.verify_pin", lambda clone, commit: commit)
    rig = Rig(hpc, tmp_path)
    rig.up({**site, "HPC_STUDY_ARTIFACTS": str(study)})
    (values,) = rig.values
    assert values["study_artifacts"] == str(study)
    state = rig.driver.store.read()
    assert state["site"]["study_artifacts"] == str(study)
    assert not [u for u in SLOT_USERS if u in str(state)]


def test_o83_a_site_that_gives_a_relative_or_stepping_study_directory_is_refused(hpc, site):
    """O83: `HPC_STUDY_ARTIFACTS` is an absolute path of plain characters with no `..`, as every site path is."""
    for bad in ("study/artifacts", "/srv/../etc", "/srv/a b"):
        with pytest.raises(hpc.settings.SettingsError, match="HPC_STUDY_ARTIFACTS"):
            hpc.settings.site_from_environment({**site, "HPC_STUDY_ARTIFACTS": bad})


def test_o83_a_site_with_no_study_directory_fills_no_placeholder_for_it(hpc, site):
    """O83: an unset key is no value, so the renderer cannot write an empty path for `@study_artifacts@`."""
    import hpcpins

    assert "study_artifacts" not in hpcpins.values(hpc.settings.site_from_environment(site))
    filled = hpcpins.values(hpc.settings.site_from_environment({**site, "HPC_STUDY_ARTIFACTS": "/srv/study"}))
    assert filled["study_artifacts"] == "/srv/study"


def test_o83_the_slot_layouts_template_is_the_pilots_template_with_the_slots_and_the_tool_server_after_it():
    """O83: the example's template adds the tables and changes nothing of the pilot's, so a change to the pilot's
    template (a pin, a table) cannot leave the example behind."""
    pilot = (HPC / "layouts" / "pilot-shape.platform.toml.template").read_text(encoding="utf-8")
    slots = (HPC / "layouts" / "pilot-shape-slots.platform.toml.template").read_text(encoding="utf-8")

    def body(text):  # the header comment is the one line that differs
        return text[text.index("zone = {") :]

    assert body(slots).startswith(body(pilot).rstrip("\n"))
    assert "[slots.slot-1.data_path]" in body(slots)[len(body(pilot).rstrip("\n")) :]


def test_o83_the_pilots_form_of_the_hpc_tool_server_loads_and_is_started_through_the_launcher(hpc, tmp_path):
    """O83: the pilot's tool server (its interpreter `toolserver/venv/bin/python`, its clone `toolserver/clone`, its
    frozen manifests beside them) as `command = <venv>/bin/python` and `args = ["-m", <module>]`, the launcher's form,
    loads with its slots and is started as `<interpreter> -I -B <launcher> <map> -- -m <module>`."""
    study = pilot_artifacts(tmp_path / "pilot-artifacts")
    phase = build_phase(hpc, tmp_path, pilot_layout(tmp_path), study)
    run_core(hpc, phase)
    config = load_platform(phase.root / "platform.toml")
    slots = load_slots(phase.root / "platform.toml", config)
    assert [slot.name for slot in slots.slots] == ["slot-1", "slot-2"]
    command, args = server_argv(config)
    assert command == f"{study}/toolserver/venv/bin/python"
    assert args[-3:] == ["--", "-m", "onedata_mcp"]
    assert config.toolserver.clone == (study / "toolserver" / "clone").resolve()
    assert config.toolserver.conditions["writing"].manifest == (study / "toolserver" / "K1.json").resolve()


FORM = r"use <venv>/bin/python with args \['-m', '<module>', \.\.\.\]"


def test_o83_a_tool_server_run_through_env_is_refused_naming_the_form_to_use_at_load_and_before_a_job_is_held(
    hpc, tmp_path, site
):
    """O83: the launcher starts a Python server by `-m <module>` or a script, so `/usr/bin/env` with a console script is
    refused when the platform file is loaded, and by `up`'s preflight before any job is held, each saying to use
    `<venv>/bin/python` with `args = ['-m', '<module>', ...]`."""
    study = pilot_artifacts(tmp_path / "pilot-artifacts")
    phase = build_phase(hpc, tmp_path, pilot_layout(tmp_path, "python"), study)
    run_core(hpc, phase)
    path = phase.root / "platform.toml"
    text = path.read_text(encoding="utf-8")
    old = f'command = "{study}/toolserver/venv/bin/python"\nargs = ["-m", "onedata_mcp"]'
    assert old in text
    path.write_text(
        text.replace(
            old,
            f'command = "/usr/bin/env"\nargs = ["PYTHONDONTWRITEBYTECODE=1", "{study}/toolserver/venv/bin/mcp"]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=FORM):
        load_platform(path)
    rig = Rig(hpc, tmp_path / "rig", pilot_layout(tmp_path / "env", "env"))
    with pytest.raises(RuntimeError, match=FORM):
        rig.up({**site, "HPC_STUDY_ARTIFACTS": str(study)})
    assert not rig.submitted()

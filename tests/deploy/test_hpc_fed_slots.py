"""O83: the HPC federation provisions the layout's slots as Compose does, and renders them into its platform file.

The layout's slots pair principals, so `hpcfed.py up` makes their users, their 0600 password files and their
memberships with the Compose bootstrap's own calls, and `platform.toml` gets `[slots]` and `[toolserver]` from the
same function Compose's renderer calls. The zone and the providers are fakes, the core's phase is stood in as the
tests of the declared names stand it in, and nothing starts a job.
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
from slots_support import (
    BASE_SET,
    COMMIT,
    READ_SET,
    WITH_TOOLSERVER,
    WRITE_SET,
    artifacts,
    ids_of,
    layout_with_template,
    make_root,
    secret_of,
)

from expcore_onedata.addresses import AddressMap
from expcore_onedata.config import load_platform
from expcore_onedata.toolserver import server_argv
from expcore_onedata.users import load_slots

LAYOUT = HPC / "layouts" / "pilot-shape-slots.toml"
DOMAIN = "expcore-hpc-slots.test"
PROVIDERS = ("aspen", "larch", "rowan", "alder", "hazel")
DECLARED = {"zone": f"zone.{DOMAIN}", **{p: f"{p}.{DOMAIN}" for p in PROVIDERS}}
SLOT_USERS = ("slot1-data", "slot1-tools", "slot2-data", "slot2-tools")


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedboot
        import fedfacts
        import fedlayout
        import fedpki
        import fedrest
        import fedspec
        import fedtools
        import hpcnames

        modules.fedboot, modules.fedfacts, modules.fedlayout = fedboot, fedfacts, fedlayout
        modules.fedpki, modules.fedrest, modules.fedspec, modules.fedtools = fedpki, fedrest, fedspec, fedtools
        modules.names = hpcnames
        yield modules


class FakeZone:
    """Onezone's users and space members as the bootstrap's calls see them, and the panel that makes a user: the answers
    of the 25.0 interfaces the calls use. Every user's password is the one the call carried."""

    JOINED = frozenset({"space_view"})  # what a user who joined a space holds until its privileges are set

    def __init__(self, hpc, spec):
        self.rest = hpc.fedrest
        self.users: dict[str, tuple[str, str]] = {}  # login -> (id, password)
        self.members: dict[str, dict[str, set[str]]] = {f"sid-{s.name}": {} for s in spec.spaces}
        self.invites: dict[str, str] = {}

    def login_of(self, headers):
        import base64

        user, password = base64.b64decode(headers["Authorization"].split()[1]).decode().split(":", 1)
        return user, password

    def json(self, method, url, *, label, headers=None, json=None, **options):
        assert (method, url.rsplit("/", 1)[-1]) == ("POST", "users"), (method, url)
        self.users[json["username"]] = (f"uid-{json['username']}", json["password"])

    def zone(self, method, path, label, *, auth=None, json=None, **options):
        if (method, path) == ("GET", "/user"):
            user, password = self.login_of(auth)
            if user not in self.users or self.users[user][1] != password:
                raise self.rest.RestError("no such user", 401)
            return {"userId": self.users[user][0]}
        if (method, path) == ("POST", "/user/tokens/temporary"):
            token = f"invite-{len(self.invites)}"
            self.invites[token] = json["type"]["inviteToken"]["spaceId"]
            return {"token": token}
        if (method, path) == ("POST", "/user/spaces/join"):
            user, _ = self.login_of(auth)
            self.members[self.invites[json["token"]]][self.users[user][0]] = set(self.JOINED)
            return {}
        found = re.fullmatch(r"/spaces/([^/]+)/users", path)
        if found and method == "GET":
            return {"users": sorted(self.members[found.group(1)])}
        found = re.fullmatch(r"/spaces/([^/]+)/(users|effective_users)/([^/]+)/privileges", path)
        assert found, (method, path)
        sid, _, uid = found.groups()
        if method == "PATCH":
            held = self.members[sid][uid]
            held |= set(json["grant"])
            held -= set(json["revoke"])
            return {}
        return {"privileges": sorted(self.members[sid][uid])}


def provision(boot, spec):
    """The bootstrap's user and member steps, as both platforms' `bring_up` take them."""
    boot.ensure_users()
    for space in spec.spaces:
        if spec.members(space.name):
            boot.ensure_members(space.name)


def boots(hpc, tmp_path, text=WITH_TOOLSERVER):
    """The Compose bootstrap and the HPC bootstrap over the same layout file, each in a root of its own and over a fake
    zone of its own: `(compose, hpc)`, each `(zone, boot, layout)`."""
    spec = hpc.fedspec.load_spec(layout_with_template(text, tmp_path))
    found = []
    for name, make in (
        ("compose", lambda zone, layout: hpc.fedboot.Bootstrap(zone, layout)),
        ("hpc", lambda zone, layout: hpc.boot.HpcBootstrap(zone, layout, (5001, 5001))),
    ):
        layout = hpc.fedlayout.Layout(tmp_path / name, spec=spec)
        hpc.fedpki.private_directory(layout.secrets)
        hpc.fedpki.private_directory(layout.users)
        layout.admin_token.write_text("an-admin-token-for-the-scan", encoding="utf-8")
        layout.admin_token.chmod(0o600)
        zone = FakeZone(hpc, spec)
        boot = make(zone, layout)
        boot.zone = zone.zone
        boot.ids.spaces = {s.name: f"sid-{s.name}" for s in spec.spaces}
        found.append((zone, boot, layout))
    return found


def test_o83_the_same_layout_provisions_the_same_users_with_the_same_rights_on_both_platforms(hpc, tmp_path):
    """O83: the Compose bootstrap and the HPC one, over one layout and a fake zone each, make the same users (the fixed
    one and the slots' four) and make each a member of the same spaces holding exactly the privileges the layout
    declares for it, directly and effectively."""
    compose, hpc_side = boots(hpc, tmp_path)
    spec = compose[1].spec
    for _, boot, _ in (compose, hpc_side):
        provision(boot, spec)
    assert set(compose[0].users) == set(hpc_side[0].users) == {p.name for p in spec.principals}
    assert set(SLOT_USERS) <= set(compose[0].users)
    assert compose[0].members == hpc_side[0].members
    for zone, _, _ in (compose, hpc_side):
        for principal in spec.principals:
            uid = zone.users[principal.name][0]
            for space in spec.spaces:
                held = zone.members[f"sid-{space.name}"].get(uid)
                assert held == (set(principal.privileges) if space.name in principal.spaces else None), principal.name


def test_o83_a_slots_users_hold_exactly_the_baseline_and_the_conditions_rights_and_only_in_the_declared_spaces(
    hpc, tmp_path
):
    """O83: no slot user holds a right the layout did not declare, in any space, on either platform: a data-path user
    exactly the baseline, a tool-server user exactly its condition's rights."""
    for zone, boot, _ in boots(hpc, tmp_path):
        spec = boot.spec
        provision(boot, spec)
        held = {name: {space: p for space, members in zone.members.items() if (p := members.get(uid[0])) is not None}
                for name, uid in zone.users.items()}  # fmt: skip
        for slot in spec.slots:
            assert held[slot.data_path] == {"sid-alpha-space": BASE_SET}
        assert held["slot1-tools"] == {"sid-alpha-space": READ_SET}
        assert held["slot2-tools"] == {"sid-alpha-space": WRITE_SET}
        assert set(held) == set(zone.users)


def test_o83_each_slot_user_has_a_password_file_of_mode_0600_in_the_root_and_no_other_file_holds_a_password(
    hpc, tmp_path
):
    """O83: `up` writes a slot user's password as it writes a fixed user's, a 0600 file in a 0700 directory, never
    replaced; and neither the facts, the platform file, a manifest nor a record holds one."""
    _, (zone, boot, layout) = boots(hpc, tmp_path)
    spec = boot.spec
    provision(boot, spec)
    study = artifacts(tmp_path / "artifacts")
    boot.ids.providers = ids_of(spec)["providers"]
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, layout.root),
        "study_artifacts": str(study),
    }
    hpc.boot.write_facts(layout, boot, values)
    secrets = {user: password for user, (_, password) in zone.users.items()}
    assert set(SLOT_USERS) <= set(secrets)
    for user in SLOT_USERS:
        mode = stat.S_IMODE(layout.password(user).stat().st_mode)
        assert mode == 0o600, user
        assert layout.password(user).read_text(encoding="utf-8") == secrets[user]
    assert stat.S_IMODE(layout.users.stat().st_mode) == 0o700
    outside = [p for p in layout.root.rglob("*") if p.is_file() and layout.secrets not in p.parents]
    assert {p.name for p in outside} >= {"facts.json", "platform.toml", "knowledge.json", "writing.json"}
    for path in outside:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not [user for user, password in secrets.items() if password in text], path.name
        assert "an-admin-token-for-the-scan" not in text


def test_o83_a_password_the_root_already_holds_is_never_replaced_by_a_later_up(hpc, tmp_path):
    """O83: a rerun of up reads the password of each user, a slot's too, and does not make another."""
    _, (zone, boot, layout) = boots(hpc, tmp_path)
    provision(boot, boot.spec)
    before = {user: layout.password(user).read_text(encoding="utf-8") for user in SLOT_USERS}
    provision(boot, boot.spec)
    assert before == {user: layout.password(user).read_text(encoding="utf-8") for user in SLOT_USERS}
    assert {user: zone.users[user][1] for user in SLOT_USERS} == before


def test_o83_every_slot_user_is_mapped_to_the_jobs_uid_on_a_providers_storage_like_a_fixed_user(
    hpc, tmp_path, monkeypatch
):
    """O83: the providers run as the job's user, so a user the storage does not map reaches none of its files: the HPC
    bootstrap maps every principal of the layout, the slots' users with the others, on the storage a support names."""
    _, (_, boot, _) = boots(hpc, tmp_path)
    boot.ids.admin = "uid-admin"
    boot.ids.users = {p.name: f"uid-{p.name}" for p in boot.spec.principals}
    sent = []
    boot.luma = lambda service, method, path, body: sent.append(body)
    boot.storage_for = lambda service, space: "posix-local"
    monkeypatch.setattr(hpc.fedboot.Bootstrap, "support", lambda self, service, space, sid: None)  # not under test
    boot.support(boot.spec.provider("aspen"), boot.spec.space("alpha-space"), "sid-alpha-space")
    mapped = {body["onedataUser"]["onedataUserId"] for body in sent if "onedataUser" in body}
    assert {f"uid-{user}" for user in SLOT_USERS} <= mapped
    assert {f"uid-{p.name}" for p in boot.spec.principals} | {"uid-admin"} == mapped


def test_o83_up_takes_the_user_and_member_steps_for_every_space_a_slot_user_is_a_member_of(hpc):
    """O83: the HPC `bring_up` is the Compose bootstrap's steps, so a slot user, an ordinary principal, is made before
    any membership and made a member of each space the layout gives it."""
    spec = hpc.fedspec.load_spec(LAYOUT)
    taken = []

    class Recorder:
        def __init__(self):
            self.spec = spec

        def __getattr__(self, name):
            return lambda *args, **kwargs: taken.append((name, args))

    hpc.boot.bring_up(Recorder())
    names = [name for name, _ in taken]
    assert names.index("ensure_users") < names.index("ensure_members")
    member_spaces = {args[0] for name, args in taken if name == "ensure_members"}
    assert {s for user in SLOT_USERS for s in spec.principal(user).spaces} <= member_spaces


# -- the platform file the HPC render writes ------------------------------------------------------------------------


def tables(text):
    document = tomllib.loads(text)
    return document["slots"], document["toolserver"]


def test_o83_the_same_layout_renders_the_same_slots_and_tool_server_on_both_platforms(hpc, tmp_path):
    """O83: Compose's renderer and the HPC one, over one layout file, write the same `[slots]` and the same
    `[toolserver]`: the same users with the same ids and rights, the same command, clone, commit and conditions. They
    differ only where the platforms do: the root's own manifests are named relative to the Compose file and by the
    root's absolute path in the HPC file, which is copied beside a study."""
    (_, c_boot, c_layout), (_, h_boot, h_layout) = boots(hpc, tmp_path)
    spec = c_boot.spec
    study = artifacts(tmp_path / "artifacts")
    for boot in (c_boot, h_boot):
        provision(boot, spec)
        boot.ids.providers = ids_of(spec)["providers"]
    hpc.fedfacts.write_facts(c_layout, c_boot, str(study))
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, h_layout.root),
        "study_artifacts": str(study),
    }
    hpc.boot.write_facts(h_layout, h_boot, values)
    c_slots, c_tool = tables(c_layout.platform.read_text(encoding="utf-8"))
    h_slots, h_tool = tables(h_layout.platform.read_text(encoding="utf-8"))
    assert c_slots == h_slots
    for name, condition in c_tool["conditions"].items():
        assert condition["manifest"] == f"toolserver/manifests/{name}.json"
        assert h_tool["conditions"][name]["manifest"] == f"{h_layout.root}/toolserver/manifests/{name}.json"
        condition["manifest"] = h_tool["conditions"][name]["manifest"] = None
    assert c_tool == h_tool
    for name in ("knowledge", "writing"):  # and the copies are the same bytes
        copies = [
            (layout.root / "toolserver" / "manifests" / f"{name}.json").read_bytes() for layout in (c_layout, h_layout)
        ]
        assert copies[0] == copies[1] == (study / "manifests" / f"{name}.json").read_bytes()


def test_o83_a_layout_without_slots_renders_the_hpc_platform_file_it_always_did(hpc, tmp_path):
    """O83: the pilot's own layout declares no slot, and its platform file has neither table and no tool-server
    directory in the root."""
    spec = hpc.fedspec.load_spec(HPC / "layouts" / "pilot-shape.toml")
    assert (spec.slots, spec.toolserver) == ((), None)
    spec = hpc.fedspec.load_spec(layout_with_template(MINIMAL, tmp_path))
    layout = hpc.fedlayout.Layout(tmp_path / "root", spec=spec)
    make_root(layout)
    boot = SimpleNamespace(layout=layout, ids=hpc.fedboot.Ids(admin="uid-admin", **ids_of(spec)))
    values = hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, layout.root)
    hpc.boot.write_facts(layout, boot, values)
    document = tomllib.loads(layout.platform.read_text(encoding="utf-8"))
    assert "slots" not in document
    assert "toolserver" not in document
    assert not layout.toolserver.exists()


# -- the pilot's template, through the core's phase --------------------------------------------------------------------


@pytest.fixture
def phase(hpc, tmp_path):
    """The core's phase over the pilot-shape slot layout: a root with a map and every principal's credential file, the
    study's artifacts, and the bootstrap and the REST client stood in."""
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    spec = hpc.fedspec.load_spec(LAYOUT)
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
    return SimpleNamespace(
        root=root, layout=layout, spec=spec, tmp=tmp_path, secrets=secrets, study=artifacts(tmp_path / "artifacts")
    )


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
    assert config.toolserver.commit == COMMIT
    command, args = server_argv(config)
    assert command == f"{phase.study}/venv/bin/python"
    assert args == ["-I", "-B", str(phase.root / "toolshim" / "resolve_launch.py"), str(phase.layout.address_map), "--",
                    "-m", "onedata_mcp"]  # fmt: skip
    manifests = {name: c.manifest for name, c in config.toolserver.conditions.items()}
    assert manifests == {n: phase.root / "toolserver" / "manifests" / f"{n}.json" for n in ("knowledge", "writing")}
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"cluster\.example\.org|10\.0\.0\.", text)
    assert not [v for v in phase.secrets.values() if v in text]


def test_o83_the_pilots_platform_file_holds_the_rights_the_layout_declares_for_each_slot(hpc, phase):
    """O83: a slot's tool-server rights in the file are its principal's privileges in the layout, and each condition's
    rights are those of the slot that serves it, so the lease pool finds a slot for each condition."""
    run_core(hpc, phase)
    config = load_platform(phase.root / "platform.toml")
    slots = load_slots(phase.root / "platform.toml", config)
    for slot, user in zip(slots.slots, ("slot1-tools", "slot2-tools"), strict=True):
        assert slot.rights == tuple(sorted(phase.spec.principal(user).privileges))
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
        ('args = ["-m", "onedata_mcp"]', 'args = ["onedata_mcp"]'),
    ):
        assert old in original
        path.write_text(original.replace(old, new), encoding="utf-8")
        from expcore.config.loading import ConfigError

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

    def __init__(self, hpc, tmp_path):
        self.hpc, self.fake, self.root = hpc, FakeSlurm(), tmp_path / "fed" / "root"
        self.driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
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
    with pytest.raises(RuntimeError, match="interpreter") as refused:
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

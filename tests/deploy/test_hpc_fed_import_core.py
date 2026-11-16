"""A shared imported tree in the HPC federation's bootstrap and core phase (O59, deploy/hpc-federation/hpcboot.py,
DESIGN.md section 8): the imported storage made with the job user's ids, the LUMA mappings `hpcimport.luma_requests`
gives, the scan awaited, the import verified over the rendered platform file before the link check, the direct mount's
storages, and the `pilot-shape-import` layout and template. Nothing here reaches a cluster or a panel."""

import os
import re
import tomllib
from types import SimpleNamespace

import pytest
from hpc_fed_support import data_stand_in, hpc_on_path, manifest_values, pin_values
from hpc_import_support import (
    IMPORT_LAYOUT,
    IMPORT_TEMPLATE,
    INGEST_LAYOUT,
    PROVIDERS,
    SPACES,
    edited_layout,
    make_environ,
    tree_of,
)

from expcore_onedata.linksettings import link_pairs


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def environ(tmp_path):
    return make_environ(tmp_path)


# -- the bootstrap ----------------------------------------------------------------------------------------------------


class Steps:
    """A bootstrap that only records the steps the core takes, in order."""

    def __init__(self, spec):
        self.spec, self.steps = spec, []

    def __getattr__(self, name):
        def step(*args):
            self.steps.append((name, args))

        return step


def test_o59_the_hpc_bootstrap_makes_the_import_storage_before_the_support_and_waits_for_the_scan_last(hpc):
    """O59: as the Compose federation's up does: the owner's imported storage before any support, the scan awaited
    after the members, whatever the layout (both are the bootstrap's own no-ops for a layout with no import)."""
    import fedspec

    for path in (IMPORT_LAYOUT, INGEST_LAYOUT):
        steps = Steps(fedspec.load_spec(path))
        hpc.boot.bring_up(steps)
        names = [name for name, _ in steps.steps]
        assert names.index("ensure_spaces") < names.index("ensure_import_storages") < names.index("ensure_support")
        assert names.index("ensure_support") < names.index("ensure_members") < names.index("wait_import")
        assert names[-1] == "wait_import"
        assert dict(steps.steps)["wait_import"] == (hpc.boot.IMPORT_TIMEOUT_S,)
    assert hpc.boot.IMPORT_TIMEOUT_S == 1800.0


def test_o59_the_hpc_bootstrap_makes_the_imported_storage_with_the_job_users_ids(hpc):
    """O59: the providers run as the job's user: the imported storage is created with its ids, the local LUMA feed and
    the canonical path type (`hpcimport.import_storage_extra`)."""
    import fedspec

    boot = hpc.boot.HpcBootstrap.__new__(hpc.boot.HpcBootstrap)
    boot.owner, boot.spec = (5001, 5002), fedspec.load_spec(IMPORT_LAYOUT)
    assert boot.import_storage_extra(boot.spec.provider("larch")) == {
        "rootUid": 5001,
        "rootGid": 5002,
        "lumaFeed": "local",
        "storagePathType": "canonical",
    }


def luma_boot(hpc, monkeypatch):
    import fedboot
    import fedspec

    spec = fedspec.load_spec(IMPORT_LAYOUT)
    boot = hpc.boot.HpcBootstrap.__new__(hpc.boot.HpcBootstrap)
    boot.spec, boot.owner, boot._mapped = spec, (5001, 5002), set()
    boot.ids = SimpleNamespace(admin="uid-admin", users={p.name: f"uid-{p.name}" for p in spec.principals})
    boot.storages = lambda service: {"posix-local": ("st-posix", {}), "import-alpha-space": ("st-import", {})}
    sent = []
    boot.panel = lambda service, method, path, label, **options: sent.append((service.name, method, path, options))
    monkeypatch.setattr(fedboot.Bootstrap, "support", lambda self, service, space, sid: None)
    return boot, spec, sent


def test_o59_each_storage_a_support_names_gets_the_mappings_luma_requests_gives(hpc, monkeypatch):
    """O59: the LUMA mappings are `hpcimport.luma_requests`, sent on the storage the support names: the owner's imported
    storage for its imported space, finish.sh's posix storage for its other support; the users are mapped once per
    (provider, storage), the space's default credentials for every support."""
    import hpcimport

    boot, spec, sent = luma_boot(hpc, monkeypatch)
    users = ["uid-admin", *(f"uid-{p.name}" for p in spec.principals)]
    larch = spec.provider("larch")
    boot.support(larch, spec.space("alpha-space"), "sid-alpha")  # larch's own import
    boot.support(larch, spec.space("alpha-work"), "sid-work")  # a mirror of aspen's study space: its posix storage
    boot.support(larch, spec.space("link-probes"), "sid-probes")  # the same storage again: no user mapped twice
    expected = [
        *hpcimport.luma_requests("st-import", "sid-alpha", users, 5001, 5002),
        *hpcimport.luma_requests("st-posix", "sid-work", users, 5001, 5002),
        *hpcimport.luma_requests("st-posix", "sid-probes", [], 5001, 5002),
    ]
    assert [(m, p, o["json"]) for _, m, p, o in sent] == expected
    assert {name for name, *_ in sent} == {"larch"}
    assert sum("/st-import/" in path for _, _, path, _ in sent) == 1 + len(users)
    assert sum(method == "POST" for _, method, _, _ in sent) == 2 * len(users)


def test_o59_a_mapping_already_there_is_read_back_and_must_name_the_job_users_uid(hpc, monkeypatch):
    """O59: a refused mapping (a rerun) is read back and kept when it names the job's uid, and fails up when it names
    another."""
    from fedrest import RestError

    boot, spec, sent = luma_boot(hpc, monkeypatch)
    held = {"uid": "5001"}

    def panel(service, method, path, label, **options):
        sent.append((method, path))
        if method == "POST":
            raise RestError("conflict", status=409)
        return {"storageCredentials": held}

    boot.panel = panel
    boot.support(spec.provider("larch"), spec.space("alpha-space"), "sid-alpha")
    assert [m for m, _ in sent].count("GET") == 1 + len(spec.principals)  # the admin and each principal
    held["uid"] = "9999"
    boot._mapped = set()
    with pytest.raises(RuntimeError, match="mapped to another uid on larch"):
        boot.support(spec.provider("larch"), spec.space("alpha-space"), "sid-alpha")


# -- the core's phase -------------------------------------------------------------------------------------------------


@pytest.fixture
def make_ca():
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca


def core_root(hpc, tmp_path, make_ca, layout_file):
    """A root holding what the core's phase needs of the services' bring-up, and the layout over it."""
    import fedlayout
    import fedspec

    root = tmp_path / "root"
    (root / "secrets" / "users").mkdir(parents=True)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(make_ca(tmp_path / "ca").ca.read_bytes())
    spec = fedspec.load_spec(layout_file)
    files = {"secrets/zone.passphrase": "A1b2C3d4E5f6G7h8", "secrets/admin.token": "an-admin-token"}
    files.update({f"secrets/users/{p.name}.pass": "a-password" for p in spec.principals})
    for name, value in files.items():
        (root / name).write_text(value, encoding="utf-8")
        os.chmod(root / name, 0o600)
    hosts = {s.name: f"{s.name}.cluster.example.org" for s in spec.services}
    return root, fedlayout.Layout(root, tmp_path, hpc.boot.hpc_spec(spec, hosts))


def boot_of(layout, storages=None):
    ids = SimpleNamespace(
        admin="uid-admin",
        providers={p.name: f"pid-{p.name}" for p in layout.spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in layout.spec.spaces},
        users={p.name: f"uid-{p.name}" for p in layout.spec.principals},
        scans={},
        storages=storages or {},
    )
    return SimpleNamespace(layout=layout, ids=ids, spec=layout.spec)


def run_core(hpc, layout, environ, events, storages=None, **replaced):
    options = {
        "environ": environ,
        "rest": lambda ca: SimpleNamespace(close=lambda: None),
        "bootstrap": lambda rest, lay, owner: boot_of(lay, storages),
        "steps": lambda boot: events.append("steps"),
        "content": lambda boot: SimpleNamespace(reset=lambda timeout: None),
        "links": lambda lay, spaces: events.append("links") or "link check passed",
        "data": data_stand_in(),
    }
    options.update(replaced)
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, layout.root),
        **pin_values(layout.root / "agent"),
    }
    return hpc.boot.core_phase(layout, (5001, 5002), values, **options)


def test_o59_the_import_is_verified_over_the_rendered_platform_file_before_the_link_check(
    hpc, tmp_path, make_ca, environ
):
    """O59: `fedimport.verify_imports` runs once platform.toml is rendered and before the link check, over the
    environment the driver gave the core and within the import's own time."""
    root, layout = core_root(hpc, tmp_path, make_ca, IMPORT_LAYOUT)
    events, seen = [], []

    def verify(lay, env, *, timeout_s):
        events.append("verify")
        seen.append((lay, env, timeout_s, (root / "platform.toml").is_file()))

    out = run_core(hpc, layout, environ, events, verify=verify)
    assert events == ["steps", "verify", "links"]
    assert seen == [(layout, environ, 1800.0, True)]
    assert out.endswith("link check passed")


def test_o59_a_platform_file_that_leaves_the_sharing_out_is_refused_before_the_link_check(
    hpc, tmp_path, make_ca, environ
):
    """O59: the core reads the platform file, not the layout: a template that drops `shared = true` from one import
    would leave the core unable to refuse a storage that is not read-only, so the real `verify_imports` refuses it, and
    the link check never runs."""
    template = IMPORT_TEMPLATE
    text = template.read_text(encoding="utf-8")
    line = "import = { readonly = true, shared = true }\n"
    folder = tmp_path / "layout"
    layout_file = edited_layout(folder, 'core = "login"', 'core = "login"')
    (folder / template.name).write_text(text.replace(line, "import = { readonly = true }\n", 1), encoding="utf-8")
    _, layout = core_root(hpc, tmp_path, make_ca, layout_file)
    events = []
    with pytest.raises(RuntimeError, match=r"does not declare alpha-space as a shared, read-only import") as caught:
        run_core(hpc, layout, environ, events)
    assert str(tmp_path) not in str(caught.value)
    assert "links" not in events


def test_o59_a_template_that_drops_an_import_declaration_altogether_is_refused_too(hpc, tmp_path, make_ca, environ):
    """O59: no `import` table at all is no declaration either."""
    template = IMPORT_TEMPLATE
    text = template.read_text(encoding="utf-8")
    folder = tmp_path / "layout"
    layout_file = edited_layout(folder, 'core = "login"', 'core = "login"')
    (folder / template.name).write_text(text.replace("import = { readonly = true, shared = true }\n", ""), "utf-8")
    _, layout = core_root(hpc, tmp_path, make_ca, layout_file)
    events = []
    with pytest.raises(RuntimeError, match="does not declare"):
        run_core(hpc, layout, environ, events)
    assert "links" not in events


def test_o59_a_layout_with_no_import_is_verified_over_nothing(hpc, tmp_path, make_ca):
    """O59: the pilot-shape layout, whose data is ingested, reads no variable and no platform file for an import."""
    _, layout = core_root(hpc, tmp_path, make_ca, INGEST_LAYOUT)
    events = []
    out = run_core(hpc, layout, {}, events)
    assert events == ["steps", "links"]
    assert out.endswith("link check passed")


def written_values(hpc, monkeypatch):
    """The values `write_platform` is given, which `core_phase` fills, recorded as it writes."""
    seen = []
    original = hpc.boot.write_platform

    def record(path, template, facts, values):
        seen.append(dict(values))
        return original(path, template, facts, values)

    monkeypatch.setattr(hpc.boot, "write_platform", record)
    return seen


def test_o59_a_direct_mount_gets_the_tree_of_each_storage_that_declares_it_as_a_toml_table(
    hpc, tmp_path, make_ca, environ, monkeypatch
):
    """O59: `hpcimport.direct_storages` fills the placeholder `@hpc_direct_storages@` with an inline TOML table of the
    storage id the bootstrap made to the tree's directory, for a template's `[sandbox.mount]` (M5): only a storage
    whose import declares `direct_io`, and a storage the bootstrap never made for one is refused by its space's name."""
    layout_file = edited_layout(
        tmp_path / "layout",
        'import = { readonly = true, shared = true, source_env = "EXPCORE_FED_IMPORT_ALPHA", ',
        'import = { readonly = true, shared = true, direct_io = true, source_env = "EXPCORE_FED_IMPORT_ALPHA", ',
    )
    _, layout = core_root(hpc, tmp_path, make_ca, layout_file)
    seen = written_values(hpc, monkeypatch)
    storages = {(owner, space): f"st-{space}" for space, (owner, _) in SPACES.items()}
    run_core(hpc, layout, environ, [], storages=storages, verify=lambda *a, **k: None)
    table = tomllib.loads(f"storages = {seen[0]['hpc_direct_storages']}")["storages"]
    assert table == {"st-alpha-space": tree_of(environ, "alpha-space")}
    with pytest.raises(ValueError, match="alpha-space"):
        run_core(hpc, layout, environ, [], storages={("larch", "beta-space"): "x"}, verify=lambda *a, **k: None)


def test_o59_the_direct_storages_are_an_empty_toml_table_when_no_import_declares_one(
    hpc, tmp_path, make_ca, environ, monkeypatch
):
    """O59: the placeholder is `{}`, valid TOML, for a layout whose imports declare no direct mount, and for one that
    imports nothing (whose bootstrap holds no storage ids to read)."""
    seen = written_values(hpc, monkeypatch)
    _, layout = core_root(hpc, tmp_path, make_ca, IMPORT_LAYOUT)
    run_core(hpc, layout, environ, [], verify=lambda *a, **k: None)
    (tmp_path / "plain").mkdir()
    _, plain = core_root(hpc, tmp_path / "plain", make_ca, INGEST_LAYOUT)
    run_core(hpc, plain, {}, [])
    assert [v["hpc_direct_storages"] for v in seen] == ["{}", "{}"]
    assert tomllib.loads(f"storages = {seen[0]['hpc_direct_storages']}") == {"storages": {}}


# -- the layouts ------------------------------------------------------------------------------------------------------


def test_o59_the_import_layout_has_the_pilots_shape_with_its_four_data_spaces_imported_and_shared(hpc):
    """O59: five providers, the four data spaces each owned by one provider and mirrored by aspen, every one a shared
    read-only import with a tree and a manifest of its own, and the study's own space an ordinary one; no direct mount,
    which aspen could read only on a space it owns."""
    import fedspec

    spec = fedspec.load_spec(IMPORT_LAYOUT)
    assert [p.name for p in spec.providers] == PROVIDERS
    assert {name: spec.space(name).owner for name in SPACES} == {name: owner for name, (owner, _) in SPACES.items()}
    for name, (_, stem) in SPACES.items():
        space = spec.space(name)
        assert (space.imported.shared, space.imported.readonly, space.imported.direct_io) == (True, True, False)
        assert (space.imported.source_env, space.imported.manifest_env) == (
            f"EXPCORE_FED_IMPORT_{stem}",
            f"EXPCORE_FED_IMPORT_{stem}_MANIFEST",
        )
        assert "aspen" in space.mirrors
        assert space.owner != "aspen"
    assert spec.space(spec.study_space).imported is None
    assert spec.space(spec.study_space).baseline
    assert set(spec.space(spec.probe_space).supporters) == set(PROVIDERS)
    assert hpc.settings.load_settings(spec.hpc).core == "login"


def test_o59_the_ingest_layout_stays_ingest_based(hpc):
    """O59: layouts/pilot-shape.toml declares no import, so the ingest path stays the primary one until a live slot
    shows the two equivalent."""
    import fedspec

    assert not [s.name for s in fedspec.load_spec(INGEST_LAYOUT).spaces if s.imported]
    assert "import" not in (INGEST_LAYOUT.parent / "pilot-shape.platform.toml.template").read_text(encoding="utf-8")


def rendered_import(hpc, root, make_ca):
    """The import layout's platform.toml as up writes it, over invented ids, in a root holding what it names."""
    import fedspec

    spec = fedspec.load_spec(IMPORT_LAYOUT)
    facts = {
        "zone": {"url": "https://zone.example.org:8443"},
        "study_space": spec.study_space,
        "providers": {
            p.name: {"url": f"https://{p.name}.example.org:8443", "id": f"pid-{p.name}"} for p in spec.providers
        },
        "spaces": {s.name: {"id": f"sid-{s.name}"} for s in spec.spaces},
        "principals": {p.name: {"user_id": f"uid-{p.name}"} for p in spec.principals},
    }
    (root / "secrets" / "users").mkdir(parents=True)
    for name, value in [("secrets/admin.token", "an-admin-token")] + [
        (f"secrets/users/{p.name}.pass", "a-password") for p in spec.principals
    ]:
        (root / name).write_text(value, encoding="utf-8")
        os.chmod(root / name, 0o600)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(make_ca(root / "made").ca.read_bytes())
    (root / "fault-journal").mkdir()
    settings = hpc.settings.load_settings(spec.hpc)
    values = {
        **hpc.boot.hpc_values(
            ["/venv/python", "/repo/hpcfed.py"], settings.stop_bound_s, settings.settle_timeout_s, root
        ),
        **pin_values(root / "agent"),
        **manifest_values(root, IMPORT_LAYOUT),
    }
    template = spec.platform_template.read_text(encoding="utf-8")
    hpc.boot.write_platform(root / "platform.toml", template, facts, {**values, "hpc_direct_storages": "{}"})
    return root / "platform.toml"


def test_o59_the_import_templates_platform_file_loads_with_each_data_space_shared_and_ten_pairs(hpc, tmp_path, make_ca):
    """O59: the rendered template is a platform file the core loads with the four data spaces shared and read-only,
    and the link check still covers the pilot's ten ordered pairs; the template repeats each layout declaration."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.spaces import ImportedStorage

    path = rendered_import(hpc, tmp_path, make_ca)
    config = load_platform(path)
    shared = ImportedStorage(readonly=True, shared=True)
    labels = {"alpha": "alpha-space", "beta": "beta-space", "gamma": "gamma-space", "delta": "delta-space"}
    assert [config.space_named(label).imported for label in labels] == [shared] * 4
    assert len(link_pairs(config.spaces, list(config.providers))) == 10
    entries = {e["name"]: e for e in tomllib.loads(path.read_text(encoding="utf-8"))["spaces"]}
    for name, (owner, _) in SPACES.items():
        assert entries[name]["import"] == {"readonly": True, "shared": True}
        assert entries[name]["holder"] == owner


def test_o59_the_import_layout_and_its_template_hold_no_path_and_no_site_name():
    """O59: the layout and its template hold no path and no address."""
    for path in (IMPORT_LAYOUT, IMPORT_TEMPLATE):
        text = path.read_text(encoding="utf-8")
        for host_path in ("/srv", "/mnt", "/home", "/scratch", "/net", "/lustre"):
            assert host_path not in text
        assert re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text) is None

"""O98: `up` admits a member only when a mirror is asked before the owner of every space an owner outage reaches
(deploy/hpc-federation/hpcfailover.py, hpcboot.py, hpcsettings.py), and records the order, by labels, where it holds.

The layout is the pilot-shape one, every name invented: alpha-space is held by `larch` and mirrored by `aspen` and
`rowan`, beta-space by `rowan` and `aspen`. The providers' ids are the test's own, so the order is too. Nothing here
reaches a cluster or a panel."""

import json
import os
import stat
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, PILOT_TEMPLATE, data_stand_in, hpc_on_path, pin_values, write_address_map

from expcore_onedata import failover

FILE = "failover-order.json"
DECLARED = 'outage_targets = ["larch"]'
# larch holds alpha-space (mirrors aspen, rowan); every other provider's id is below or above it as the case says
LARCH_FIRST = {"larch": "id-01", "aspen": "id-02", "rowan": "id-03", "alder": "id-04", "hazel": "id-05"}
MIRROR_FIRST = {"larch": "id-03", "aspen": "id-02", "rowan": "id-01", "alder": "id-04", "hazel": "id-05"}
ONE_MIRROR_BELOW = {"larch": "id-02", "aspen": "id-03", "rowan": "id-01", "alder": "id-04", "hazel": "id-05"}


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture(scope="module")
def make_ca():
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca


def layout_with(folder, line):
    """The pilot-shape layout with `line` added to its `[hpc]` table (none: as shipped), beside its template."""
    folder.mkdir(parents=True, exist_ok=True)
    text = LAYOUT.read_text(encoding="utf-8")
    if line is not None:
        assert text.count("\n[hpc]\n") == 1
        text = text.replace("\n[hpc]\n", f"\n[hpc]\n{line}\n", 1)
    (folder / LAYOUT.name).write_text(text, encoding="utf-8")
    (folder / PILOT_TEMPLATE.name).write_text(PILOT_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return folder / LAYOUT.name


def core_root(hpc, tmp_path, make_ca, line):
    """A root holding what the core's phase needs of the services' bring-up, and the layout over it."""
    import fedlayout
    import fedspec

    root = tmp_path / "root"
    (root / "secrets" / "users").mkdir(parents=True)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(make_ca(tmp_path / "ca").ca.read_bytes())
    spec = fedspec.load_spec(layout_with(tmp_path / "layout", line))
    files = {"secrets/zone.passphrase": "A1b2C3d4E5f6G7h8", "secrets/admin.token": "an-admin-token"}
    files.update({f"secrets/users/{p.name}.pass": "a-password" for p in spec.principals})
    for name, value in files.items():
        (root / name).write_text(value, encoding="utf-8")
        os.chmod(root / name, 0o600)
    hosts = {s.name: f"{s.name}.cluster.example.org" for s in spec.services}
    layout = fedlayout.Layout(root, tmp_path, hpc.boot.hpc_spec(spec, hosts))
    write_address_map(layout)
    return root, layout


def boot_with(layout, provider_ids):
    ids = SimpleNamespace(
        admin="uid-admin",
        providers={p.name: provider_ids[p.name] for p in layout.spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in layout.spec.spaces},
        users={p.name: f"uid-{p.name}" for p in layout.spec.principals},
        scans={},
        storages={},
    )
    return SimpleNamespace(layout=layout, ids=ids, spec=layout.spec)


def run_core(hpc, layout, provider_ids, events):
    options = {
        "environ": {},
        "rest": lambda ca, **kwargs: SimpleNamespace(close=lambda: None),
        "bootstrap": lambda rest, lay, owner: boot_with(lay, provider_ids),
        "steps": lambda boot: events.append("steps"),
        "content": lambda boot: SimpleNamespace(reset=lambda timeout: None),
        "links": lambda lay, spaces: events.append("links") or "link check passed",
        "data": data_stand_in(),
    }
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, layout.root),
        **pin_values(layout.root / "agent"),
    }
    return hpc.boot.core_phase(layout, (5001, 5002), values, **options)


# -- the declaration --------------------------------------------------------------------------------------------------


def test_o98_a_layout_that_declares_no_outage_target_declares_none_and_its_settings_are_as_they_were(hpc):
    """O98: with no `outage_targets` key, or no `[hpc]` table, nothing is declared; the shipped layouts declare none."""
    assert hpc.settings.outage_targets(None) == ()
    assert hpc.settings.outage_targets({"time": "02:00:00"}) == ()
    assert hpc.settings.load_settings({}).time_s == hpc.settings.load_settings(None).time_s
    import fedspec

    for shipped in LAYOUT.parent.glob("*.toml"):
        assert "outage_targets" not in shipped.read_text(encoding="utf-8"), shipped.name
        assert hpc.settings.outage_targets(fedspec.load_spec(shipped).hpc) == ()


def test_o98_a_declared_outage_target_is_a_list_of_distinct_provider_names_of_the_layout(hpc, tmp_path):
    """O98: the key is a non-empty list of distinct names, each a provider the layout declares; anything else is
    refused naming the key and no name."""
    import fedspec

    spec = fedspec.load_spec(layout_with(tmp_path, DECLARED))
    assert hpc.settings.load_settings(spec.hpc).time_s > 0
    assert tuple(spec.hpc["outage_targets"]) == ("larch",)
    import hpcfailover

    assert hpcfailover.declared(spec) == ("larch",)
    for number, bad in enumerate(('"larch"', "[]", '["larch", "larch"]', "[1]", '["no-such-provider"]')):
        folder = tmp_path / f"bad-{number}"
        broken = fedspec.load_spec(layout_with(folder, f"outage_targets = {bad}"))
        with pytest.raises(hpc.settings.SettingsError) as refused:
            hpcfailover.declared(broken)
        assert str(refused.value).startswith("hpc.outage_targets: ")
        assert "no-such-provider" not in str(refused.value)


# -- the admission in the core's phase --------------------------------------------------------------------------------


def test_o98_up_fails_before_the_link_check_when_the_owner_sorts_before_every_mirror(hpc, tmp_path, make_ca):
    """O98: the rendered platform names larch's id below aspen's and rowan's: the core's phase raises, naming the
    space and the providers by label and no id or name, before the link check, and leaves no record of a failed order.
    """
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    events = []
    with pytest.raises(RuntimeError) as refused:
        run_core(hpc, layout, LARCH_FIRST, events)
    text = str(refused.value)
    assert text.startswith("failover order: ")
    assert "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first" in text
    assert text.endswith("refuse this member")
    assert "spaces[0]" in text
    assert "owner providers[1]" in text  # larch is the second provider of the layout
    for hidden in ("larch", "aspen", "rowan", "alpha-space", *LARCH_FIRST.values()):
        assert hidden not in text
    assert "links" not in events
    assert not (root / FILE).exists()
    assert (root / "platform.toml").is_file()  # it was written first: the admission reads the rendered ids


@pytest.mark.parametrize("ids", [MIRROR_FIRST, ONE_MIRROR_BELOW])
def test_o98_up_records_the_order_by_labels_when_a_mirror_is_asked_first_and_goes_on_to_the_link_check(
    hpc, tmp_path, make_ca, ids
):
    """O98: with any one mirror below the owner the core's phase passes, writes `failover-order.json` (labels only,
    0600) before the link check, says so in what up prints, and the link check runs."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    events = []
    said = run_core(hpc, layout, ids, events)
    assert events == ["steps", "links"]
    assert "failover-order.json written" in said
    text = (root / FILE).read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(root / FILE).st_mode) == 0o600
    (space,) = json.loads(text)["spaces"]
    assert space["space"] == "spaces[0]"
    assert (space["owner"], space["mirrors"]) == ("providers[1]", ["providers[0]", "providers[2]"])
    assert space["mirror_first"] is True
    assert space["order"][0] != "providers[1]"
    assert sorted(space["order"]) == ["providers[0]", "providers[1]", "providers[2]"]
    for hidden in ("larch", "aspen", "rowan", "alpha", "id-0", "sid-"):
        assert hidden not in text
    assert failover.read_record(text)["spaces"] == [space]


def test_o98_the_order_by_labels_is_the_order_of_the_ids(hpc, tmp_path, make_ca):
    """O98: the labels come out in the order of the ids: rowan (providers[2]) below aspen (providers[0]) below larch."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    run_core(hpc, layout, MIRROR_FIRST, [])
    (space,) = json.loads((root / FILE).read_text(encoding="utf-8"))["spaces"]
    assert space["order"] == ["providers[2]", "providers[0]", "providers[1]"]


def test_o98_a_layout_that_declares_no_outage_target_is_up_as_it_was_whatever_the_ids(hpc, tmp_path, make_ca):
    """O98: no declared owner outage, so nothing is in scope: with the owner's id below every mirror's there is no
    error, no record, no extra line in what up prints, and the same three lines as before."""
    root, layout = core_root(hpc, tmp_path, make_ca, None)
    events = []
    said = run_core(hpc, layout, LARCH_FIRST, events)
    assert events == ["steps", "links"]
    assert not (root / FILE).exists()
    assert "failover" not in said
    assert len(said.splitlines()) == 3  # the facts line, the data line, the link check


def test_o98_a_declared_provider_that_holds_no_space_with_a_mirror_leaves_up_as_it_was(hpc, tmp_path, make_ca):
    """O98: `aspen` holds no space of the platform file (it only mirrors, and owns the link check's probe space, which
    is no `[[spaces]]` entry): declared, it puts nothing in scope, so the worst ids refuse nothing and write nothing."""
    root, layout = core_root(hpc, tmp_path, make_ca, 'outage_targets = ["aspen"]')
    events = []
    said = run_core(hpc, layout, LARCH_FIRST, events)
    assert events == ["steps", "links"]
    assert not (root / FILE).exists()
    assert len(said.splitlines()) == 3


def test_o98_a_record_of_an_earlier_run_does_not_outlive_a_refusal(hpc, tmp_path, make_ca):
    """O98: the core's phase may run again on a root; a record a passing run wrote is removed when a later run fails,
    so no record says a mirror is first of a platform whose owner is."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    run_core(hpc, layout, MIRROR_FIRST, [])
    assert (root / FILE).is_file()
    with pytest.raises(RuntimeError, match="refuse this member"):
        run_core(hpc, layout, LARCH_FIRST, [])
    assert not (root / FILE).exists()


def test_o98_the_record_is_evidence_up_writes_like_the_render_identity(hpc):
    """O98: `failover-order.json` is in the allow-list of what `down` keeps, as a file `up` writes (never what a run
    left), and a root that holds none is as complete as before."""
    import hpcevidence as evidence

    assert FILE in evidence.UP_FILES
    assert FILE in evidence.EVIDENCE_FILES
    assert evidence.kind_of(FILE) == evidence.UP
    assert FILE not in evidence.REQUIRED


# -- the file's data provider is the layout's owner --------------------------------------------------------------------


def with_alpha_held_by(hpc, tmp_path, make_ca, line, holder):
    """A root whose platform template names `holder` as the data provider of the first space, which the layout gives
    to larch: the one thing the layout and the rendered file can disagree on."""
    root, layout = core_root(hpc, tmp_path, make_ca, line)
    template = layout.spec.platform_template
    text = template.read_text(encoding="utf-8")
    assert text.count('holder = "larch"') == 1
    template.write_text(text.replace('holder = "larch"', f'holder = "{holder}"', 1), encoding="utf-8")
    return root, layout


@pytest.mark.parametrize("line", [DECLARED, 'outage_targets = ["aspen"]'])
def test_o98_a_space_whose_data_provider_in_the_file_is_not_the_layouts_owner_is_refused_not_left_out(
    hpc, tmp_path, make_ca, line
):
    """O98: the scope is read from the file's `holder`, so a template that gives alpha-space to aspen while the layout
    gives it to larch must not drop the space from scope in silence: with larch declared the space is the declared
    owner's, with aspen declared it is the file's, and either way `up` is refused naming the space by label, with no
    record, whatever the ids are (the mirrors here sort first)."""
    root, layout = with_alpha_held_by(hpc, tmp_path, make_ca, line, "aspen")
    events = []
    with pytest.raises(RuntimeError) as refused:
        run_core(hpc, layout, MIRROR_FIRST, events)
    text = str(refused.value)
    assert text.startswith("failover order: spaces[0]: the platform file's data provider is not the layout's owner")
    assert text.endswith("refuse this member")
    for hidden in ("larch", "aspen", "rowan", "alpha-space"):
        assert hidden not in text
    assert "links" not in events
    assert not (root / FILE).exists()


def test_o98_a_disagreement_over_a_space_no_declared_provider_owns_or_holds_refuses_nothing(hpc, tmp_path, make_ca):
    """O98: with hazel declared, the spaces of the others are none of the declaration's: alpha-space, whose file says
    aspen and layout says larch, is neither hazel's nor held by it, so nothing is refused for it; the one space hazel
    owns (delta-space, spaces[3]) is checked and recorded alone."""
    root, layout = with_alpha_held_by(hpc, tmp_path, make_ca, 'outage_targets = ["hazel"]', "aspen")
    events = []
    run_core(hpc, layout, LARCH_FIRST, events)
    assert events == ["steps", "links"]
    (space,) = json.loads((root / FILE).read_text(encoding="utf-8"))["spaces"]
    assert (space["space"], space["owner"], space["mirrors"]) == ("spaces[3]", "providers[4]", ["providers[0]"])
    assert space["order"] == ["providers[0]", "providers[4]"]


# -- no fingerprint holds the declaration or the record ---------------------------------------------------------------


def test_o98_declaring_outage_targets_changes_neither_the_rendered_platform_nor_the_render_identity(
    hpc, tmp_path, make_ca
):
    """O98: the render identity is the template's hash, the rendered platform's fingerprint and the pins, which a shard
    compares with the one its plan expects. The layout's `outage_targets` and the record the admission writes are in
    none of them: two roots of one layout and one set of ids, one declaring the outage and one not, render the same
    platform file (but for the root's own path) and have the same render identity byte for byte."""
    texts = {}
    one_ca = make_ca(tmp_path / "shared-ca")  # the CA bundle's hash is in the fingerprint: one bundle for both roots
    for name, line in (("declared", DECLARED), ("plain", None)):
        root, layout = core_root(hpc, tmp_path / name, lambda directory: one_ca, line)
        run_core(hpc, layout, MIRROR_FIRST, [])
        platform = (root / "platform.toml").read_text(encoding="utf-8").replace(str(root), "<root>")
        texts[name] = (platform, (root / "render-identity.json").read_bytes(), (root / FILE).exists())
    assert texts["declared"][2] is True
    assert texts["plain"][2] is False
    assert texts["declared"][0] == texts["plain"][0]
    assert texts["declared"][1] == texts["plain"][1]
    assert b"failover" not in texts["declared"][1]

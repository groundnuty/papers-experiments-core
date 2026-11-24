"""O98: `up` admits a member only when a mirror is asked before the owner of every space an owner outage reaches
(deploy/hpc-federation/hpcfailover.py, hpcboot.py, hpcsettings.py), and records the order, by labels, where it holds.

The layout is the pilot-shape one, every name invented: alpha-space is held by `larch` and mirrored by `aspen` and
`rowan`, beta-space by `rowan` and `aspen`. The providers' ids are the test's own, so the order is too. Nothing here
reaches a cluster or a panel."""

import json
import os
import stat

import pytest
from hpc_failover_support import (
    ALPHA_MIRRORS,
    DECLARED,
    FILE,
    LARCH_FIRST,
    LOCAL_FIRST,
    MIRROR_FIRST,
    ONE_MIRROR_BELOW,
    ROWAN_ONLY,
    core_root,
    layout_with,
    run_core,
)
from hpc_fed_support import LAYOUT, hpc_on_path

from expcore_onedata import failover


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


# -- only the layout's declared mirrors are in the order ---------------------------------------------------------------


def test_o98_a_supporter_that_is_no_declared_mirror_does_not_make_an_owner_stop_pass(hpc, tmp_path, make_ca):
    """O98, the second code review: the layout designates rowan alone as alpha-space's mirror, and the rendered file
    still lists aspen, the agent's local provider, as a supporter of it (it supports the space and holds none of the
    replicated file's blocks). With the ids aspen < larch (the owner) < rowan, counting aspen made the order pass, and
    op-worker, choosing between the owner and rowan, the only holders, would ask the stopped owner first. The order is
    over the owner and the declared mirrors, so `up` refuses the member, naming labels only, with no record."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED, ROWAN_ONLY)
    events = []
    with pytest.raises(RuntimeError) as refused:
        run_core(hpc, layout, LOCAL_FIRST, events)
    text = str(refused.value)
    assert text.startswith("failover order: ")
    assert "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first" in text
    assert "(owner providers[1]; mirrors providers[2])" in text  # larch, and rowan alone: aspen (providers[0]) is out
    assert text.endswith("refuse this member")
    for hidden in ("larch", "aspen", "rowan", "alpha-space", *LOCAL_FIRST.values()):
        assert hidden not in text
    assert "links" not in events
    assert not (root / FILE).exists()


def test_o98_with_every_supporter_declared_a_mirror_the_same_ids_pass_as_they_did(hpc, tmp_path, make_ca):
    """O98: the shipped layout designates aspen and rowan both, so the same ids pass, and the record names both."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    run_core(hpc, layout, LOCAL_FIRST, [])
    (space,) = json.loads((root / FILE).read_text(encoding="utf-8"))["spaces"]
    assert space["mirrors"] == ["providers[0]", "providers[2]"]
    assert space["order"] == ["providers[0]", "providers[1]", "providers[2]"]


def test_o98_the_record_lists_the_declared_mirrors_alone_and_the_order_is_over_them_and_the_owner(
    hpc, tmp_path, make_ca
):
    """O98: rowan, the one declared mirror, sorts below the owner while aspen sorts above it: the order passes, and the
    record holds rowan as the mirror and no label of aspen anywhere in the space's order."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED, ROWAN_ONLY)
    run_core(
        hpc, layout, {"larch": "id-02", "aspen": "id-04", "rowan": "id-01", "alder": "id-05", "hazel": "id-06"}, []
    )
    (space,) = json.loads((root / FILE).read_text(encoding="utf-8"))["spaces"]
    assert (space["owner"], space["mirrors"]) == ("providers[1]", ["providers[2]"])
    assert space["order"] == ["providers[2]", "providers[1]"]
    assert failover.read_record((root / FILE).read_text(encoding="utf-8"))["spaces"] == [space]


def test_o98_a_space_the_layout_gives_no_mirror_is_out_of_scope_whatever_else_supports_it(hpc, tmp_path, make_ca):
    """O98: nothing is designated to serve alpha-space when the layout declares no mirror for it, so its owner's outage
    is no failover to check, however the file lists its supporters: nothing is refused, written or said."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED, ((ALPHA_MIRRORS, "mirrors = []"),))
    events = []
    said = run_core(hpc, layout, LARCH_FIRST, events)
    assert events == ["steps", "links"]
    assert not (root / FILE).exists()
    assert len(said.splitlines()) == 3


def test_o98_a_file_space_a_declared_owner_holds_that_the_layout_gives_no_mirror_is_not_ordered_over_its_supporters(
    hpc, tmp_path, make_ca
):
    """O98: the file gives gamma-space to alder, the declared owner, while the layout gives it to hazel with no mirror,
    which `holders_differ` skips. It is no failover to check (nothing is designated), and must not be ordered over every
    supporter either: aspen, listed in the file, sorts below alder, and counting it would write a record for the space.
    Nothing is refused, written or said."""
    replace = (('owner = "alder"\nmirrors = ["aspen"]', 'owner = "hazel"\nmirrors = []'),)
    root, layout = core_root(hpc, tmp_path, make_ca, 'outage_targets = ["alder"]', replace)
    events = []
    said = run_core(hpc, layout, {**LOCAL_FIRST, "alder": "id-06"}, events)
    assert events == ["steps", "links"]
    assert not (root / FILE).exists()
    assert len(said.splitlines()) == 3


def test_o98_a_declared_mirror_the_file_does_not_list_as_a_supporter_is_refused_not_ignored(hpc, tmp_path, make_ca):
    """O98: the layout designates alder too, which the rendered file does not list as a supporter of alpha-space: no
    order is told over a mirror the file does not hold, so the member is refused (labels only, no record) even though
    the order over the others would pass."""
    root, layout = core_root(
        hpc, tmp_path, make_ca, DECLARED, ((ALPHA_MIRRORS, 'mirrors = ["aspen", "rowan", "alder"]'),)
    )
    events = []
    with pytest.raises(RuntimeError) as refused:
        run_core(hpc, layout, MIRROR_FIRST, events)
    text = str(refused.value)
    assert text.startswith("failover order: spaces[0]: a declared mirror is no other supporter of the space")
    assert text.endswith("refuse this member")
    for hidden in ("larch", "aspen", "rowan", "alder", "alpha-space"):
        assert hidden not in text
    assert "links" not in events
    assert not (root / FILE).exists()


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

"""The support sets up measures (O35, deploy/compose-federation/fedsupport.py, fedboot.py): each space's providers as
Onezone lists them, compared with the layout's declaration before the federation is marked ready, on Compose and on
HPC alike. The zone is a fake that lists what a test says it lists; nothing starts a container or reaches a panel."""

import json
import re
from types import SimpleNamespace

import pytest
from federation_support import MINIMAL, WITH_IMPORT, import_layout
from hpc_fed_support import hpc_on_path
from hpc_import_support import IMPORT_LAYOUT
from slots_boot import modules
from slots_support import ids_of, layout_with_template, make_root

NOW = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
STAMP = "2023-11-14T22:13:20Z"
# three providers: beta-space is oak's, mirrored by ash, and elm supports only alpha-space: elm listed for beta-space
# is an extra supporter, and ash listed for alpha-space is one too
THREE = (
    MINIMAL
    + """
[providers.ash]
port = 29646

[spaces.beta-space]
owner = "oak"
mirrors = ["ash"]
"""
)


class SupportZone:
    """Onezone's list of the providers that support each space, by space id and in the order a test gives; a space not
    given is listed as supported by exactly its declared providers. Every call is recorded."""

    def __init__(self, spec, ids, **listed):
        self.calls = []
        self.listed = {f"sid-{s.name}": [ids[name] for name in s.supporters] for s in spec.spaces}
        self.listed.update({f"sid-{space}": found for space, found in listed.items()})

    def __call__(self, method, path, label, **options):
        self.calls.append((method, path))
        parts = path.split("/")
        if method == "GET" and len(parts) == 4 and parts[1] == "spaces" and parts[3] == "providers":
            return {"providers": self.listed[parts[2]]}  # as given: a test may give what is no list
        raise AssertionError(f"unexpected zone call: {method} {path}")


def provider_ids(spec):
    return {p.name: f"pid-{p.name}" for p in spec.providers}


def compose_boot(fed, tmp_path, text=THREE, **listed):
    """The Compose bootstrap over a layout, holding the ids it holds once the providers are online, and a zone that
    lists the declared supporters except where `listed` says otherwise (a space's name, with the ids it lists)."""
    layout, _ = import_layout(fed, tmp_path, text)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout, boot.spec = layout, layout.spec
    ids = provider_ids(layout.spec)
    boot.ids = fed.boot.Ids(
        users={p.name: f"uid-{p.name}" for p in layout.spec.principals},
        providers=ids,
        spaces={s.name: f"sid-{s.name}" for s in layout.spec.spaces},
    )
    boot.zone = SupportZone(layout.spec, ids, **listed)
    boot._clock = lambda: NOW
    return boot


def refusal(boot) -> str:
    with pytest.raises(RuntimeError) as refused:
        boot.verify_support()
    return str(refused.value)


# -- the comparison ---------------------------------------------------------------------------------------------------


def test_o35_support_sets_that_match_the_declaration_pass_and_are_recorded_by_label_with_the_time_read(fed, tmp_path):
    """O35: every space's listed providers equal its owner and mirrors: nothing is refused, and the measured sets are
    kept by provider label, sorted, with the time each was read."""
    boot = compose_boot(fed, tmp_path)
    boot.verify_support()
    assert boot.ids.support["alpha-space"].providers == ("elm", "oak")
    assert boot.ids.support["beta-space"].providers == ("ash", "oak")
    assert {read.read_at for read in boot.ids.support.values()} == {STAMP}
    assert {path for _, path in boot.zone.calls} == {f"/spaces/sid-{s.name}/providers" for s in boot.spec.spaces}


def test_o35_an_extra_supporter_is_refused_naming_the_space_and_the_provider_by_label(fed, tmp_path):
    """O35: a provider the zone lists for a space the layout does not declare it for would hold bytes no check reads."""
    boot = compose_boot(fed, tmp_path, **{"beta-space": ["pid-oak", "pid-ash", "pid-elm"]})
    message = refusal(boot)
    assert "space beta-space: supported by elm but the platform doesn't declare it" in message
    assert "alpha-space" not in message
    assert boot.ids.support == {}  # nothing measured is kept for a federation that was refused


def test_o35_a_missing_supporter_is_refused_naming_the_space_and_the_provider_by_label(fed, tmp_path):
    """O35: a declared supporter the zone does not list would break the pairs through it without a word."""
    boot = compose_boot(fed, tmp_path, **{"alpha-space": ["pid-oak"]})
    assert "space alpha-space: declares elm but it doesn't support the space" in refusal(boot)


def test_o35_a_provider_id_that_maps_to_no_label_is_an_undeclared_provider_and_only_counted(fed, tmp_path):
    """O35: an id the bootstrap did not give out is named as an undeclared provider, by count: never the id."""
    boot = compose_boot(fed, tmp_path, **{"alpha-space": ["pid-oak", "pid-elm", "secret-id-1", "secret-id-2"]})
    message = refusal(boot)
    assert "space alpha-space: supported by 2 undeclared providers but the platform doesn't declare them" in message
    assert "secret-id" not in message
    (tmp_path / "one").mkdir()
    boot = compose_boot(fed, tmp_path / "one", **{"alpha-space": ["pid-oak", "pid-elm", "secret-id-1"]})
    assert "space alpha-space: supported by an undeclared provider but the platform doesn't declare it" in refusal(boot)


def test_o35_every_difference_of_every_space_is_in_one_refusal_and_no_id_is(fed, tmp_path):
    """O35: the operator sees all the differences at once, in the layout's order of spaces, and the refusal holds no id,
    host or address."""
    boot = compose_boot(
        fed,
        tmp_path,
        **{"alpha-space": ["pid-oak", "pid-ash"], "beta-space": ["pid-ash", "pid-oak", "pid-elm", "unknown-id"]},
    )
    message = refusal(boot)
    expected = [
        "space alpha-space: supported by ash but the platform doesn't declare it",
        "space alpha-space: declares elm but it doesn't support the space",
        "space beta-space: supported by elm but the platform doesn't declare it",
        "space beta-space: supported by an undeclared provider but the platform doesn't declare it",
    ]
    assert [message.index(item) for item in expected] == sorted(message.index(item) for item in expected)
    for leaked in ("pid-", "unknown-id", "sid-", "127.0.0.1", "http"):
        assert leaked not in message


@pytest.mark.parametrize("answer", ["pid-oak", None, [1, 2], {"oak": "pid-oak"}], ids=["str", "none", "ints", "dict"])
def test_o35_a_zone_answer_that_is_no_list_of_ids_is_refused_naming_the_space_and_never_read_as_a_set(
    fed, tmp_path, answer
):
    """O35: a bare string handed to `set()` would be a set of characters, each an undeclared provider."""
    boot = compose_boot(fed, tmp_path, **{"alpha-space": answer})
    message = refusal(boot)
    assert "space alpha-space" in message
    assert "no list of provider ids" in message


def test_o35_an_imported_space_follows_the_same_rule(fed, tmp_path):
    """O35: fern-space is held by its owner on an imported storage; its mirror's support is checked like any other."""
    boot = compose_boot(fed, tmp_path, WITH_IMPORT, **{"fern-space": ["pid-elm"]})
    assert "space fern-space: declares oak but it doesn't support the space" in refusal(boot)


def test_o35_a_second_read_that_finds_a_supporter_gone_since_the_first_is_refused(fed, tmp_path):
    """O35: the measurement is its own read, taken after the support, not a copy of what `ensure_support` read."""
    boot = compose_boot(fed, tmp_path)
    boot.provider = lambda service, method, path, label, **options: {"fileId": "root"}
    boot.ensure_support(5)
    boot.zone.listed["sid-beta-space"] = ["pid-oak"]  # the zone drops ash after the first read
    assert "space beta-space: declares ash but it doesn't support the space" in refusal(boot)


# -- where it is taken ------------------------------------------------------------------------------------------------


def test_o35_a_support_call_the_zone_never_shows_is_refused_though_ensure_support_made_it(fed, tmp_path):
    """O35: the panel accepted the support and the zone does not list the provider: `ensure_support` cannot tell (it
    made the call), the measurement after it can."""
    boot = compose_boot(fed, tmp_path, **{"alpha-space": []})
    boot.provider = lambda service, method, path, label, **options: {"fileId": "root"}
    made = []
    boot.support = lambda service, space, sid: made.append((service.name, space.name))
    boot.ensure_support(5)
    assert ("elm", "alpha-space") in made
    message = refusal(boot)
    assert "space alpha-space: declares oak but it doesn't support the space" in message
    assert "space alpha-space: declares elm but it doesn't support the space" in message


def test_o35_an_extra_supporter_is_refused_by_ensure_support_before_any_support_is_made(fed, tmp_path):
    """O35: the early refusal names the provider by label too, and changes nothing."""
    boot = compose_boot(fed, tmp_path, **{"beta-space": ["pid-elm"]})
    boot.provider = lambda service, method, path, label, **options: {"fileId": "root"}
    made = []
    boot.support = lambda service, space, sid: made.append((service.name, space.name))
    with pytest.raises(RuntimeError, match="space beta-space: supported by elm but the platform doesn't declare it"):
        boot.ensure_support(5)
    assert made == []


def test_o35_ensure_support_makes_no_support_call_for_an_earlier_space_when_a_later_space_is_refused(fed, tmp_path):
    """O35: alpha-space lacks elm, which up would add, and beta-space lists a provider it does not declare: the refusal
    comes first and nothing was changed, since a support is never removed again."""
    boot = compose_boot(fed, tmp_path, **{"alpha-space": ["pid-oak"], "beta-space": ["pid-oak", "pid-ash", "pid-elm"]})
    boot.provider = lambda service, method, path, label, **options: {"fileId": "root"}
    made = []
    boot.support = lambda service, space, sid: made.append((service.name, space.name))
    with pytest.raises(RuntimeError, match="space beta-space: supported by elm but the platform doesn't declare it"):
        boot.ensure_support(5)
    assert made == []


def test_o35_ensure_support_names_every_refused_space_in_one_refusal(fed, tmp_path):
    """O35: an extra supporter on the first space does not hide the one on the second."""
    boot = compose_boot(
        fed,
        tmp_path,
        **{"alpha-space": ["pid-oak", "pid-elm", "pid-ash"], "beta-space": ["pid-oak", "pid-ash", "pid-elm"]},
    )
    with pytest.raises(RuntimeError) as refused:
        boot.ensure_support(5)
    message = str(refused.value)
    assert "space alpha-space: supported by ash but the platform doesn't declare it" in message
    assert "space beta-space: supported by elm but the platform doesn't declare it" in message
    assert message.index("space alpha-space") < message.index("space beta-space")


class Steps:
    """A bootstrap that only records the steps the core takes, in order."""

    def __init__(self, spec=None):
        self.spec, self.steps = spec, []

    def __getattr__(self, name):
        return lambda *args: self.steps.append((name, args))


def test_o35_compose_up_and_both_resets_measure_the_support_set_right_after_the_support_and_before_the_members(
    fed, tmp_path, monkeypatch
):
    """O35: `bring_up` is the one sequence up, `reset --clean` and `reset --replicas` take, so none of them goes on
    with a federation whose support sets differ."""
    layout, _ = import_layout(fed, tmp_path, WITH_IMPORT)
    monkeypatch.setattr(fed.cli, "compose", lambda *args, **options: "")
    steps = Steps()
    fed.cli.bring_up(layout, steps, fed.cli.Timer())
    names = [name for name, _ in steps.steps]
    assert names.index("ensure_support") + 1 == names.index("verify_support") < names.index("ensure_members")
    assert names.count("verify_support") == 1


def test_o35_the_facts_keep_the_measured_sets_beside_the_declared_ones_and_leave_them_out_when_none_was_read(
    fed, tmp_path
):
    """O35: `supported_by` stays the declaration; `supported_by_measured` and `support_read_at` say what was read."""
    boot = compose_boot(fed, tmp_path)
    assert "supported_by_measured" not in fed.facts.facts_of(boot)["spaces"]["alpha-space"]  # facts as they were
    boot.verify_support()
    boot.layout.root.mkdir()
    fed.facts.write_facts(boot.layout, boot)
    written = json.loads(boot.layout.facts.read_text(encoding="utf-8"))["spaces"]
    assert written["alpha-space"]["supported_by"] == ["oak", "elm"]
    assert written["alpha-space"]["supported_by_measured"] == ["elm", "oak"]
    assert written["alpha-space"]["support_read_at"] == STAMP
    assert written["beta-space"]["supported_by_measured"] == ["ash", "oak"]


# -- HPC --------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as found:
        yield modules(found)


def hpc_boot(hpc, tmp_path, **listed):
    """The HPC bootstrap over the import layout (five providers, imported spaces and the probe space), whose zone lists
    the declared supporters except where `listed` says otherwise."""
    spec = hpc.fedspec.load_spec(IMPORT_LAYOUT)
    layout = hpc.fedlayout.Layout(tmp_path / "root", spec=spec)
    boot = hpc.boot.HpcBootstrap(None, layout, (5001, 5001))
    ids = provider_ids(spec)
    boot.ids.providers = ids
    boot.ids.spaces = {s.name: f"sid-{s.name}" for s in spec.spaces}
    boot.ids.users = {p.name: f"uid-{p.name}" for p in spec.principals}
    boot.zone = SupportZone(spec, ids, **listed)
    boot._clock = lambda: NOW
    return boot


def test_o35_the_hpc_bootstrap_measures_and_refuses_as_the_compose_one_does(hpc, tmp_path):
    """O35: `HpcBootstrap` is the Compose bootstrap over the bundle's providers: the same step, the same refusal."""
    boot = hpc_boot(hpc, tmp_path)
    boot.verify_support()
    assert boot.ids.support["alpha-space"].providers == ("aspen", "larch", "rowan")
    declared = boot.spec.space("link-probes").supporters
    short = [boot.ids.providers[name] for name in declared if name != "hazel"]
    with pytest.raises(RuntimeError, match="space link-probes: declares hazel but it doesn't support the space"):
        hpc_boot(hpc, tmp_path / "short", **{"link-probes": short}).verify_support()


def test_o35_an_extra_supporter_of_an_hpc_probe_space_or_imported_space_is_refused_by_label(hpc, tmp_path):
    """O35: the probe space and the imported spaces follow the same rule on HPC."""
    mirror = [hpc_boot(hpc, tmp_path / "a").ids.providers[n] for n in ("larch", "aspen", "rowan", "hazel")]
    with pytest.raises(RuntimeError, match="space alpha-space: supported by hazel but the platform doesn't declare it"):
        hpc_boot(hpc, tmp_path / "b", **{"alpha-space": mirror}).verify_support()
    probes = hpc_boot(hpc, tmp_path / "c").ids.providers
    with pytest.raises(RuntimeError, match="space link-probes: supported by an undeclared provider"):
        hpc_boot(hpc, tmp_path / "d", **{"link-probes": [*probes.values(), "unknown-id"]}).verify_support()


def test_o35_the_hpc_core_phase_steps_measure_after_the_support_and_the_facts_it_writes_hold_the_sets(hpc, tmp_path):
    """O35: `hpcboot.bring_up` takes the step right after `ensure_support`; the facts HPC writes are the Compose
    facts, so they hold the measured sets."""
    steps = Steps(hpc.fedspec.load_spec(IMPORT_LAYOUT))
    hpc.boot.bring_up(steps)
    names = [name for name, _ in steps.steps]
    assert names.index("ensure_support") + 1 == names.index("verify_support") < names.index("ensure_members")
    boot = hpc_boot(hpc, tmp_path)
    boot.verify_support()
    facts = hpc.fedfacts.facts_of(SimpleNamespace(layout=boot.layout, ids=boot.ids))
    assert facts["spaces"]["link-probes"]["supported_by_measured"] == sorted(boot.spec.space("link-probes").supporters)
    assert facts["spaces"]["link-probes"]["support_read_at"] == STAMP


# -- the rendered platform file ---------------------------------------------------------------------------------------

# a study's platform file is rendered from a template that declares its own support sets; the link check, the import
# verification and a campaign read those, so they are held to what the zone listed
WIDE = THREE.replace('mirrors = ["elm"]', 'mirrors = ["elm", "ash"]')  # alpha-space: oak, elm and ash
NARROW = THREE.replace('[spaces.beta-space]\nowner = "oak"\nmirrors = ["ash"]', '[spaces.beta-space]\nowner = "ash"')


def template(*support: str, extra: str = "") -> str:
    """A platform template over the three providers whose study space declares `support` as its supporters."""
    providers = "\n".join(
        f'providers.{p} = {{ url = "@provider_url.{p}@", id = "@provider_id.{p}@" }}' for p in ("oak", "elm", "ash")
    )
    return f"""\
zone = {{ name = "zone", url = "@zone_url@" }}
{providers}
{extra}
space = {{ name = "@space_name@", id = "@space_id.alpha-space@", prefixes = ["alpha-"] }}
support = {{ providers = {json.dumps(list(support))}, local = "oak" }}
credentials.admin = {{ file = "secrets/admin.token" }}
credentials.users = {{ directory = "secrets/users", pattern = "{{user}}.pass" }}
principals.data_path = {{ username = "alpha-reader", user_id = "@user_id.alpha-reader@" }}
placement = {{ holder = "oak" }}
"""


def staged(tmp_path, layout_text, platform_template):
    """The layout with its template beside it, a root holding the credentials, and the zone listing what the layout
    declares: the spec, the layout and the zone's listing, for a bootstrap to measure."""
    import fedlayout
    import fedspec

    spec = fedspec.load_spec(layout_with_template(platform_template, tmp_path, layout_text))
    layout = fedlayout.Layout(tmp_path / "root", spec=spec)
    make_root(layout)
    return SimpleNamespace(spec=spec, layout=layout, zone=SupportZone(spec, provider_ids(spec)))


def compose_staged(fed, tmp_path, layout_text, platform_template):
    stage = staged(tmp_path, layout_text, platform_template)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout, boot.spec, boot.zone, boot._clock = stage.layout, stage.spec, stage.zone, lambda: NOW
    boot.ids = fed.boot.Ids(admin="uid-admin", **ids_of(stage.spec))
    boot.verify_support()  # the zone bears out the layout: what is under test is the file rendered from the template
    return boot


def test_o35_a_template_that_declares_fewer_supporters_than_the_zone_lists_is_refused_when_the_file_is_written(
    fed, tmp_path
):
    """O35: the link check's pairs and a campaign's support set come from the rendered file. The layout and the zone
    both have oak, elm and ash for alpha-space, and the template declares only oak and elm: the ash pairs would never
    be tested, so up is refused naming the space and the provider by label."""
    boot = compose_staged(fed, tmp_path, WIDE, template("oak", "elm"))
    with pytest.raises(RuntimeError) as refused:
        fed.facts.write_facts(boot.layout, boot)
    assert "space alpha-space: supported by ash but platform.toml doesn't declare it" in str(refused.value)
    for leaked in ("pid-", "sid-", "127.0.0.1", "http"):
        assert leaked not in str(refused.value)


def test_o35_a_template_that_declares_a_provider_the_space_does_not_have_is_refused_by_label(fed, tmp_path):
    """O35: the file claims oak, elm and ash for alpha-space; the zone lists oak and elm only."""
    boot = compose_staged(fed, tmp_path, NARROW, template("oak", "elm", "ash"))
    with pytest.raises(
        RuntimeError, match=re.escape("space alpha-space: platform.toml declares ash but it doesn't support the")
    ):
        fed.facts.write_facts(boot.layout, boot)


def test_o35_a_template_provider_with_an_id_that_is_no_provider_of_the_federation_is_an_undeclared_provider(
    fed, tmp_path
):
    """O35: a hand-typed id maps to no label: counted, and the id is never in the message."""
    extra = 'providers.stray = { url = "https://stray.test", id = "typed-by-hand-9" }'
    boot = compose_staged(fed, tmp_path, NARROW, template("oak", "elm", "stray", extra=extra))
    with pytest.raises(RuntimeError) as refused:
        fed.facts.write_facts(boot.layout, boot)
    assert "space alpha-space: platform.toml declares an undeclared provider that doesn't support the space" in str(
        refused.value
    )
    assert "typed-by-hand" not in str(refused.value)


def test_o35_a_template_that_declares_what_the_zone_lists_is_written(fed, tmp_path):
    """O35: the same sets, in the template's own order and names: nothing is refused, on three providers."""
    boot = compose_staged(fed, tmp_path, WIDE, template("ash", "oak", "elm"))
    assert fed.facts.write_facts(boot.layout, boot) == "facts.json and platform.toml written"
    assert boot.layout.platform.is_file()


def test_o35_the_hpc_render_refuses_a_template_that_declares_fewer_supporters_than_the_zone_lists(hpc, tmp_path):
    """O35: the HPC `write_facts` renders the file and checks it as the Compose one does."""
    stage = staged(tmp_path, WIDE, template("oak", "elm"))
    boot = hpc.boot.HpcBootstrap(None, stage.layout, (5001, 5001))
    boot.spec, boot.zone, boot._clock = stage.spec, stage.zone, lambda: NOW
    boot.ids = hpc.fedboot.Ids(admin="uid-admin", **ids_of(stage.spec))
    boot.verify_support()
    values = hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, stage.layout.root)
    with pytest.raises(
        RuntimeError, match=re.escape("space alpha-space: supported by ash but platform.toml doesn't declare it")
    ):
        hpc.boot.write_facts(stage.layout, boot, values)

"""The key that names the study's own files on the nodes, and the data identity of a space the study ingests into.

`@study_artifacts@` (O63): a platform template, or a space's `data_manifest`, may name absolute paths of the study's own
artifacts on the nodes; `up` fills it from `HPC_STUDY_ARTIFACTS` in `site.env` and refuses, naming the variable, a
template that uses it without the key.

An ingested space (C06) is a space that is not a baseline space and not a shared import, and whose
`@data_manifest.<space>@` the template names: its data comes from the study's own ingest, after `up`. It declares its
manifest (`data_manifest` on the space, under `@study_artifacts@` or absolute, in the core's import-manifest format),
`up` renders that path, and `PlatformConfig.fingerprint()` takes its content hash: two federations that ingested
different bytes are two platforms. Its data is read back after the ingest (test_hpc_fed_verify_data.py). Slurm is the
fake of hpc_fed_support, the bootstrap and the space reads are stand-ins, and nothing reaches a cluster."""

import json
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, data_stand_in, hpc_on_path
from hpc_ingested_support import (
    DECLARED,
    KEY,
    ONE,
    OTHER,
    artifacts,
    driver_over,
    real_core,
    site_env,
    study_layout,
    up,
)
from hpc_rehearsal_support import link_check_stand_in

from expcore_onedata.config import load_platform
from expcore_onedata.importmanifest import write_manifest


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedlinks
        import fedspec
        import hpcdata
        import hpcpins

        modules.fedlinks, modules.fedspec, modules.data, modules.pins = fedlinks, fedspec, hpcdata, hpcpins
        yield modules


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


# -- O63: @study_artifacts@, from HPC_STUDY_ARTIFACTS in site.env ------------------------------------------------------


def test_o63_a_template_may_name_study_artifacts_and_up_renders_the_sites_path(hpc, tmp_path):
    """O63: `@study_artifacts@` in a platform template is the absolute directory `HPC_STUDY_ARTIFACTS` names in
    site.env, as up renders it: the study's artifacts (its tool server's venv, clone and manifests) on the nodes."""
    base = tmp_path / "site"
    env = site_env(base, base / "artifacts" / "study")
    values = hpc.pins.values(hpc.settings.site_from_environment(env))
    facts = {"zone": {"url": "u"}, "study_space": "s", "providers": {}, "spaces": {}, "principals": {}}
    template = (
        'tool_server = "@study_artifacts@/venv/bin/python"\nclone = "@study_artifacts@/clone"\n'
        'providers.aspen = { url = "u", id = "i" }\nplacement = { holder = "aspen" }\n'
    )  # a render always carries a complete [placement] (O79), whose holder is a provider of the file
    text = hpc.boot.write_platform(tmp_path / "platform.toml", template, facts, values).splitlines()
    assert text[:2] == [
        f'tool_server = "{base}/artifacts/study/venv/bin/python"',
        f'clone = "{base}/artifacts/study/clone"',
    ]


def test_o63_up_hands_the_core_the_key_as_study_artifacts_and_records_it_in_the_state(hpc, tmp_path):
    """O63: the core's phase is given `study_artifacts` among its values, and the state file's site holds it, so a
    core step on a node (and `verify-data`) fills the same path."""
    fed = driver_over(hpc, tmp_path, study_layout(tmp_path / "layout", template_extra="# @study_artifacts@/venv\n"))
    art = artifacts(tmp_path / "art")
    fed.driver.up(hpc.settings.site_from_environment(site_env(tmp_path, art)))
    (values,) = fed.given
    assert values["study_artifacts"] == str(art)
    assert fed.driver.store.read()["site"]["study_artifacts"] == str(art)


def test_o63_a_template_that_uses_the_placeholder_without_the_key_is_refused_naming_it(hpc, tmp_path):
    """O63: no key, no path to render: up is refused before any job is held, naming HPC_STUDY_ARTIFACTS (the renderer
    alone would only count an unknown placeholder, and an empty value would write `/venv`)."""
    layout = study_layout(tmp_path / "layout", declared=None, template_extra='# tool: "@study_artifacts@/venv"\n')
    fed = driver_over(hpc, tmp_path, layout)
    with pytest.raises(RuntimeError, match=f"^{KEY} is not set") as refused:
        fed.driver.up(hpc.settings.site_from_environment(site_env(tmp_path, None)))
    assert "@study_artifacts@" in str(refused.value)
    assert not [c for c in fed.fake.calls if c[0] == "sbatch"]
    assert not (fed.root / "hpc-federation.json").exists()


def test_o63_a_spaces_data_manifest_under_the_placeholder_needs_the_key_too(hpc, tmp_path):
    """O63: the layout's own `data_manifest = "@study_artifacts@/..."` is rendered from the same key, and is refused
    without it by the same message, though the template names nothing."""
    fed = driver_over(hpc, tmp_path, study_layout(tmp_path / "layout"))
    with pytest.raises(RuntimeError, match=f"^{KEY} is not set"):
        fed.driver.up(hpc.settings.site_from_environment(site_env(tmp_path, None)))
    assert not [c for c in fed.fake.calls if c[0] == "sbatch"]


def test_o63_an_unset_key_gives_the_renderer_no_value_so_it_refuses_rather_than_write_an_empty_path(hpc, tmp_path):
    """O63: a hand-run `core` over a state recorded without the key must not render `@study_artifacts@/venv` as
    `/venv`: the key is no value, and the renderer refuses an unfilled placeholder."""
    values = hpc.pins.values(hpc.settings.site_from_environment(site_env(tmp_path, None)))
    assert "study_artifacts" not in values
    facts = {"zone": {"url": "u"}, "study_space": "s", "providers": {}, "spaces": {}, "principals": {}}
    with pytest.raises(RuntimeError, match="placeholder"):
        hpc.boot.write_platform(tmp_path / "platform.toml", 'x = "@study_artifacts@/venv"\n', facts, values)


@pytest.mark.parametrize(
    "value", ["relative/path", "/a/../b", "/a b", '/a"b', "/a\\b", "/a;b", "/a\nb"], ids=lambda v: repr(v)[:12]
)
def test_o63_the_key_is_an_absolute_path_of_plain_characters_refused_naming_the_variable_never_the_value(
    hpc, tmp_path, value
):
    """O63: it is written into a quoted TOML string and passed to steps: absolute, letters, digits, `_ . / -`, no
    `..`; the refusal names the variable and never the value."""
    env = {**site_env(tmp_path, None), KEY: value}
    with pytest.raises(hpc.settings.SettingsError, match=f"^{KEY}: must be an absolute path") as refused:
        hpc.settings.site_from_environment(env)
    assert value not in str(refused.value)


def test_o63_a_site_without_the_key_is_unchanged_and_records_it_empty(hpc, tmp_path):
    """O63: the key is optional: a site that sets none is the site it was, and the state records an empty value."""
    site = hpc.settings.site_from_environment(site_env(tmp_path, None))
    assert site.study_artifacts == ""
    assert hpc.settings.Site.from_json(site.to_json()) == site


# -- C06: an ingested space declares its data manifest -----------------------------------------------------------------


def test_c06_the_layout_may_declare_a_data_manifest_on_a_space_that_is_neither_baseline_nor_imported(hpc, tmp_path):
    """C06: `data_manifest` on a space, under `@study_artifacts@` or absolute, is read as declared."""
    for number, declared in enumerate((DECLARED, "/shared/study/manifests/delta.json")):
        spec = hpc.fedspec.load_spec(study_layout(tmp_path / str(number), declared))
        assert spec.space("delta-space").data_manifest == declared
        assert spec.space("alpha-space").data_manifest is None


@pytest.mark.parametrize(
    "declared",
    ["relative/x.json", "/a/../x.json", "/a/./x.json", "@study_artifacts@", "@study_artifacts@/../x.json",
     "@study_artifacts@/a b", "/a b/x.json", "@other@/x.json", "/", "", 3],
    ids=lambda v: repr(v)[:28],
)  # fmt: skip
def test_c06_a_data_manifest_that_is_not_an_absolute_path_or_under_the_placeholder_is_refused_naming_the_key(
    hpc, tmp_path, declared
):
    """C06: the key's shape is checked when the layout is read, naming the file and the key."""
    layout = study_layout(tmp_path, "x")
    text = layout.read_text(encoding="utf-8").replace('data_manifest = "x"', f"data_manifest = {json.dumps(declared)}")
    layout.write_text(text, encoding="utf-8")
    with pytest.raises(hpc.fedspec.LayoutError, match=r"spaces\.delta-space\.data_manifest"):
        hpc.fedspec.load_spec(layout)


def test_c06_a_data_manifest_on_a_baseline_space_is_refused_since_its_manifest_is_its_baseline(hpc, tmp_path):
    """C06: a baseline space holds what up writes, so its manifest is made from that, never declared."""
    layout = study_layout(tmp_path, None)
    text = layout.read_text(encoding="utf-8")
    text = text.replace("[spaces.beta-space]", f'[spaces.beta-space]\ndata_manifest = "{DECLARED}"')
    layout.write_text(text, encoding="utf-8")
    with pytest.raises(hpc.fedspec.LayoutError, match=r"spaces\.beta-space\.data_manifest.*baseline = false"):
        hpc.fedspec.load_spec(layout)


def test_c06_a_data_manifest_on_an_imported_space_is_refused_since_its_manifest_is_the_imports(hpc, tmp_path):
    """C06: an import names its manifest by `manifest_env`: a second declaration is two sources of one identity."""
    source = LAYOUT.parent
    for name in ("pilot-shape-import.toml", "pilot-shape-import.platform.toml.template"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    path = tmp_path / "pilot-shape-import.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("[spaces.alpha-space]", f'[spaces.alpha-space]\ndata_manifest = "{DECLARED}"'))
    with pytest.raises(hpc.fedspec.LayoutError, match=r"spaces\.alpha-space\.data_manifest.*import"):
        hpc.fedspec.load_spec(path)


def test_c06_the_spaces_that_are_ingested_are_those_the_template_names_that_are_neither_baseline_nor_imported(hpc):
    """C06: delta-space of the pilot's layout (baseline = false, named by the template) is ingested and declares
    nothing; link-probes (baseline = false, never named) is not; no baseline space and no shared import is."""
    import fedlayout
    import fedspec

    layout = fedlayout.Layout(Path("/nonexistent-root"), Path("/"), fedspec.load_spec(LAYOUT))
    assert hpc.data.ingested(layout) == {"delta-space": None}
    imports = fedspec.load_spec(LAYOUT.parent / "pilot-shape-import.toml")
    assert hpc.data.ingested(fedlayout.Layout(Path("/nonexistent-root"), Path("/"), imports)) == {}


def test_c06_up_renders_the_declared_manifests_path_for_the_space_and_every_other_spaces_as_before(hpc, tmp_path):
    """C06: `@data_manifest.delta-space@` is the declared path with the site's artifacts directory in it; every other
    space's is as it was (a baseline space's, written into the root)."""
    import fedlayout
    import fedspec

    (tmp_path / "root").mkdir()
    layout = fedlayout.Layout(tmp_path / "root", tmp_path, fedspec.load_spec(study_layout(tmp_path / "l")))
    found = hpc.data.data_manifests(layout, {}, {"study_artifacts": "/shared/study"})
    assert found["delta-space"] == Path("/shared/study/manifests/delta.json")
    for name in ("alpha-space", "beta-space", "gamma-space"):
        assert found[name] == tmp_path / "root" / "data-manifests" / f"{name}.json"
    assert not (tmp_path / "root" / "data-manifests" / "delta-space.json").exists()  # the study's file, never copied
    assert hpc.data.values(found)["data_manifest.delta-space"] == "/shared/study/manifests/delta.json"


def test_c06_a_declared_manifest_that_is_missing_or_no_manifest_refuses_up_naming_the_space_never_the_path(
    hpc, tmp_path
):
    """C06: loaded in the preflight, so a typo fails before a job is held: the message names the space and the key,
    never the file's path."""
    art = artifacts(tmp_path / "art", None)
    fed = driver_over(hpc, tmp_path, study_layout(tmp_path / "layout"))
    env = site_env(tmp_path, art)
    with pytest.raises(RuntimeError, match=r"^delta-space: data_manifest is no manifest") as missing:
        fed.driver.up(hpc.settings.site_from_environment(env))
    (art / "manifests" / "delta.json").write_text('{"version": 2}', encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"^delta-space: data_manifest is no manifest") as bad:
        fed.driver.up(hpc.settings.site_from_environment(env))
    for refused in (missing, bad):
        assert str(tmp_path) not in str(refused.value)
    assert not [c for c in fed.fake.calls if c[0] == "sbatch"]


def test_c06_two_federations_that_ingested_different_bytes_fingerprint_differently_and_equal_bytes_alike(
    hpc, tmp_path, ca
):
    """C06: the data identity of an ingested space is its declared manifest's content hash, whatever the federation's
    ids: one file different is another platform fingerprint; the same manifest, another federation, the same one."""
    layout = study_layout(tmp_path / "layout")
    kinds = (("one", ONE), ("same", ONE), ("other", OTHER))
    arts = {name: artifacts(tmp_path / f"art-{name}", manifest) for name, manifest in kinds}
    loaded = {}
    for name, art in arts.items():
        fed = up(hpc, tmp_path / f"fed-{name}", layout, ca, artifacts_dir=art, label=f"fed-{name}")
        loaded[name] = load_platform(fed.root / "platform.toml")
    entry = {name: next(e for e in cfg.spaces if e.space.name == "delta-space") for name, cfg in loaded.items()}
    assert entry["one"].space.id != entry["same"].space.id  # each federation's own ids
    assert entry["one"].data == ONE.digest()
    assert entry["other"].data == OTHER.digest() != ONE.digest()
    assert loaded["one"].fingerprint() == loaded["same"].fingerprint()
    assert loaded["one"].fingerprint() != loaded["other"].fingerprint()
    text = (tmp_path / "fed-one" / "fed" / "root" / "platform.toml").read_text(encoding="utf-8")
    assert f'data_manifest = "{arts["one"]}/manifests/delta.json"' in text  # absolute, as the dispatcher reads it


def test_c06_an_edited_manifest_is_another_fingerprint_with_the_same_federation_otherwise(hpc, tmp_path, ca):
    """C06: the hash is the file's content (`Manifest.digest()`): the same path with one file's hash edited is another
    identity, which is why a verified marker is bound to the digest and not to the path."""
    art = artifacts(tmp_path / "art")
    fed = up(hpc, tmp_path / "fed", study_layout(tmp_path / "layout"), ca, artifacts_dir=art)
    before = load_platform(fed.root / "platform.toml").fingerprint()
    write_manifest(OTHER, art / "manifests" / "delta.json")
    assert load_platform(fed.root / "platform.toml").fingerprint() != before


def test_c06_an_ingested_space_is_not_checked_at_up_since_it_holds_nothing_yet_and_up_says_so(hpc, tmp_path, ca):
    """C06: at `up` the space is empty whatever its manifest declares: the check of a baseline space would fail every
    declared one. Up reports which spaces await `verify-data`, and counts the others it checked (the three baseline
    spaces and the probe space, which holds nothing)."""
    fed = up(hpc, tmp_path, study_layout(tmp_path / "layout"), ca, artifacts_dir=artifacts(tmp_path / "art"))
    assert "data: every space that is not ingested holds the data its manifest declares (4 space(s))" in fed.printed
    assert "1 ingested space(s) await(s) `hpcfed.py verify-data` after the ingest: delta-space" in fed.printed


def test_c06_a_template_that_writes_a_data_manifest_path_of_its_own_is_refused_since_nothing_would_check_it(
    hpc, tmp_path, ca
):
    """C06: only the manifests `up` renders are read back or verified, so a template that hard-codes a `data_manifest`
    for a space would fingerprint data nothing checks: `up` refuses the rendered file, naming the space."""
    layout = study_layout(tmp_path / "layout")
    template = layout.parent / "pilot-shape.platform.toml.template"
    text = template.read_text(encoding="utf-8")
    assert text.count("@data_manifest.delta-space@") == 1
    elsewhere = artifacts(tmp_path / "elsewhere")  # a valid manifest, so the file loads and only the rule can refuse it
    own = str(elsewhere / "manifests" / "delta.json")
    template.write_text(text.replace("@data_manifest.delta-space@", own), encoding="utf-8")
    fed = driver_over(hpc, tmp_path, layout, "fed-a", ca)
    real_core(hpc, fed, data_stand_in())
    env = site_env(tmp_path, artifacts(tmp_path / "art"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        with pytest.raises(RuntimeError, match=r"^delta-space: the platform template names a data_manifest of its own"):
            fed.driver.up(hpc.settings.site_from_environment(env))
    assert not (fed.root / "link-check.json").exists()


def test_c06_a_template_that_points_a_non_baseline_space_at_the_roots_own_manifest_is_refused_too(hpc, tmp_path, ca):
    """C06: written as `@hpc_root@/data-manifests/<space>.json`, the path is the one up would render for the space's
    empty baseline, which the first rule accepts: a space the study fills would fingerprint empty and never be
    verified. `up` refuses it, naming the space."""
    layout = study_layout(tmp_path / "layout", declared=None)
    template = layout.parent / "pilot-shape.platform.toml.template"
    text = template.read_text(encoding="utf-8")
    template.write_text(
        text.replace("@data_manifest.delta-space@", "@hpc_root@/data-manifests/delta-space.json"), encoding="utf-8"
    )
    fed = driver_over(hpc, tmp_path, layout, "fed-a", ca)
    real_core(hpc, fed, data_stand_in())
    env = site_env(tmp_path, None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        with pytest.raises(RuntimeError, match=r"^delta-space: its platform entry declares a data_manifest"):
            fed.driver.up(hpc.settings.site_from_environment(env))
    assert not (fed.root / "link-check.json").exists()


def test_c06_a_baseline_space_keeps_its_check_at_up(hpc, tmp_path, ca):
    """C06: a baseline space differing from its manifest still fails `up`, naming the space and never a path."""
    fed = driver_over(hpc, tmp_path, study_layout(tmp_path / "layout"), "fed-a", ca)
    real_core(hpc, fed, data_stand_in(differ="alpha-space"))
    env = site_env(tmp_path, artifacts(tmp_path / "art"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        with pytest.raises(RuntimeError, match=r"^alpha-space does not hold the data its manifest declares"):
            fed.driver.up(hpc.settings.site_from_environment(env))

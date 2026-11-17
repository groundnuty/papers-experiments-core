"""O63: the HPC render always writes `[placement]` (deploy/hpc-federation/hpcboot.py, `write_platform`), so every reader
of the rendered platform.toml (a shard's step, an ingest tool, the platform layer) reads one file and none supplies a
default table of its own. A template's own `[placement]` passes through as declared; without one the render writes
every setting at the core's default, `PlacementSettings`' fields being the one source of the numbers, and the holder
(the one setting the core has no default for) as the first provider the file declares. Nothing here reaches a
cluster."""

import argparse
import dataclasses
import importlib.util
import math
import sys
import tomllib
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, PEM, PILOT_TEMPLATE, hpc_on_path, manifest_values, pin_values

from expcore_onedata.config import ConfigError, load_platform
from expcore_onedata.rangemap import PlacementSettings, load_placement

SHARDS = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
TEMPLATE = PILOT_TEMPLATE.read_text(encoding="utf-8")
# What the core's own dataclass declares, field by field: the render takes its numbers from the same place, and a
# setting added to the dataclass later is expected in the table with no change in either.
CORE_DEFAULTS = {item.name: item.default for item in dataclasses.fields(PlacementSettings) if item.name != "holder"}


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_placement", SHARDS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_placement"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_placement", None)


def pilot_spec():
    import fedspec

    return fedspec.load_spec(LAYOUT)


def first_provider() -> str:
    return pilot_spec().providers[0].name


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    """A real CA bundle, for a render the platform layer loads (it refuses a file whose `ca_file` is no certificate)."""
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


def render(hpc, root: Path, template: str = TEMPLATE, ca: bytes = PEM.encode()) -> tuple[Path, str]:
    """The pilot-shape platform.toml as `up` writes it from `template` (`hpcboot.write_platform`), over invented ids, in
    a root holding the files it names; the file's path and the text `write_platform` returned. Given a real `ca`, the
    file loads as a platform."""
    spec = pilot_spec()
    settings = hpc.settings.load_settings(spec.hpc)
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
    for name in ["secrets/admin.token", *(f"secrets/users/{p.name}.pass" for p in spec.principals)]:
        (root / name).write_text("an-invented-secret", encoding="utf-8")
        (root / name).chmod(0o600)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(ca)
    (root / "fault-journal").mkdir()
    values = {
        **hpc.boot.hpc_values(
            ["/venv/python", "/repo/hpcfed.py"], settings.stop_bound_s, settings.settle_timeout_s, root
        ),
        **pin_values(root / "agent"),
        **manifest_values(root),
    }
    text = hpc.boot.write_platform(root / "platform.toml", template, facts, values)
    return root / "platform.toml", text


# -- what the render writes ------------------------------------------------------------------------------------------


def test_o63_the_render_of_a_template_that_declares_no_placement_carries_one(hpc, tmp_path):
    """O63: the pilot's template declares no `[placement]` (its spaces name their own data providers) and
    `load_placement` requires the table, so the file `up` rendered was refused by it and every tool that read the
    render patched a default table in on its own. The file on disk, the last of the two passes that fill the token
    margin, and the text `write_platform` returns both carry it."""
    assert "placement" not in TEMPLATE
    assert "@hpc_token_margin_s@" in TEMPLATE  # two passes: the file left is the second's
    path, text = render(hpc, tmp_path / "root")
    assert path.read_text(encoding="utf-8") == text
    assert "placement" in tomllib.loads(text)


def test_o63_the_table_a_render_writes_is_the_cores_defaults_in_full_and_the_first_provider_as_holder(hpc, tmp_path):
    """O63: every setting of `PlacementSettings` is written out at its default, with its own type (a float stays a
    float, `reset_shortcut` a boolean), in the dataclass's order, the holder first; nothing else is in the table."""
    _, text = render(hpc, tmp_path / "root")
    written = tomllib.loads(text)["placement"]
    expected = {"holder": first_provider(), **CORE_DEFAULTS}
    assert written == expected
    assert list(written) == list(expected)
    assert {key: type(value) for key, value in written.items()} == {key: type(v) for key, v in expected.items()}


def test_o63_the_loader_reads_the_written_table_as_the_defaults_of_a_file_that_names_only_its_holder(hpc, tmp_path, ca):
    """O63: defaults written out and defaults implied are one setting. The loader keeps its own literal copy of every
    default, so the render's table (from the dataclass), a table of the holder alone (the loader's copy) and the
    dataclass's own defaults must all read as one `PlacementSettings`, and as one record at a resume
    (`reset_settings`)."""
    holder = first_provider()
    written_file, _ = render(hpc, tmp_path / "written", ca=ca)
    implied_file, _ = render(hpc, tmp_path / "implied", f'placement = {{ holder = "{holder}" }}\n' + TEMPLATE, ca)
    written = load_placement(written_file, load_platform(written_file))
    implied = load_placement(implied_file, load_platform(implied_file))
    assert written.settings == implied.settings == PlacementSettings(holder=holder)
    assert written.reset_settings() == implied.reset_settings()


def test_o63_the_platform_fingerprint_is_the_same_with_the_defaults_written_out_or_implied(hpc, tmp_path, ca):
    """O63: a render that writes the defaults out and one whose template implies them (a table naming the holder
    alone) are one platform: `PlatformConfig.fingerprint()` is equal, so shards of two federations rendered either
    way still merge as one campaign."""
    holder = first_provider()
    written_file, _ = render(hpc, tmp_path / "written", ca=ca)
    implied_file, _ = render(hpc, tmp_path / "implied", f'placement = {{ holder = "{holder}" }}\n' + TEMPLATE, ca)
    assert load_platform(written_file).fingerprint() == load_platform(implied_file).fingerprint()


@pytest.mark.parametrize("form", ["inline", "header"])
def test_o63_a_template_that_declares_placement_keeps_its_values_and_gains_no_others(hpc, tmp_path, form):
    """O63: the table a template declares, as an inline key or as a header, is rendered as it is: its values, and no
    default of the core's added beside them (the loader supplies those, as it does for every other file)."""
    declared = {"holder": "larch", "poll_s": 5.0, "concurrency": 4}
    if form == "inline":
        template = 'placement = { holder = "larch", poll_s = 5.0, concurrency = 4 }\n' + TEMPLATE
    else:
        template = TEMPLATE + '\n[placement]\nholder = "larch"\npoll_s = 5.0\nconcurrency = 4\n'
    path, text = render(hpc, tmp_path / "root", template)
    assert tomllib.loads(text)["placement"] == declared
    assert text.count("[placement]") == (1 if form == "header" else 0)
    assert path.read_text(encoding="utf-8") == text


def test_o63_the_loader_reads_a_declared_table_as_declared_and_every_other_setting_at_its_default(hpc, tmp_path, ca):
    """O63: what a template declares is what a reader gets: the declared settings, and the core's default for the rest
    (the loader's own, as for every file that declares a table in part)."""
    template = 'placement = { holder = "larch", poll_s = 5.0, concurrency = 4 }\n' + TEMPLATE
    path, _ = render(hpc, tmp_path / "root", template, ca)
    settings = load_placement(path, load_platform(path)).settings
    assert settings == PlacementSettings(holder="larch", poll_s=5.0, concurrency=4)


def test_o63_a_template_with_no_provider_is_refused_naming_what_is_missing_and_writes_nothing(hpc, tmp_path):
    """O63: the holder is a provider the file declares, so a file that declares none has no table to write: the render
    fails, naming it, and leaves no platform.toml."""
    path = tmp_path / "platform.toml"
    template = 'zone = { name = "zone", url = "https://zone.example.org:8443" }\n'
    facts = {"zone": {"url": "https://zone.example.org:8443"}, "study_space": "s", "providers": {}, "spaces": {},
             "principals": {}}  # fmt: skip
    with pytest.raises(RuntimeError, match=r"declares no provider.*\[placement\] has no holder"):
        hpc.boot.write_platform(path, template, facts, {})
    assert not path.exists()


@pytest.mark.parametrize(
    "value", [True, False, 0, 3, 16 << 20, 2.0, 0.001, 1e-07, 1e16, 3600.0, "listing", 'a "quoted" \\ word'],
    ids=repr,
)  # fmt: skip
def test_o63_a_written_setting_reads_back_as_the_same_value_of_the_same_type(hpc, value):
    """O63: a boolean is `true`/`false` (not `0`), an integer stays one, a float stays one, and a string is a basic
    string of TOML."""
    read = tomllib.loads(f"x = {hpc.boot.toml_scalar(value)}")["x"]
    assert read == value
    assert type(read) is type(value)


@pytest.mark.parametrize("value", [math.inf, math.nan, None, [1], {"a": 1}], ids=repr)
def test_o63_a_setting_of_a_type_the_writer_does_not_know_is_refused_never_written_as_text(hpc, value):
    """O63: a field that a later core adds with a type the writer cannot spell fails the render, instead of writing a
    value the loader would read differently."""
    with pytest.raises((TypeError, ValueError)):
        hpc.boot.toml_scalar(value)


# -- what the readers see ----------------------------------------------------------------------------------------------


def step_args(tmp_path):
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=1, count=1, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False,
                              archive_left=False, snapshot_every=3600.0)  # fmt: skip


def test_o63_a_shard_step_places_the_platform_file_as_it_is_and_the_loader_refuses_a_file_without_the_table(
    shards, tmp_path, monkeypatch
):
    """O63: the step adds no `[placement]` to the federation's platform.toml it places beside a study, whatever the
    file holds: a file without the table is copied as it is, and `load_placement` refuses that copy, naming the key.
    A federation rendered by `up` always has the table; a file that has none is not made loadable on the way."""
    from expcore_onedata.testing import write_platform

    root = tmp_path / "fed"
    root.mkdir()
    source = write_platform(root)  # a platform file with no [placement]
    config = load_platform(source, require_secrets=False)
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(root))
    seen = {}

    def run(argv, env, cwd):
        placed = Path(argv[argv.index("run") + 1]) / "platform.toml"
        seen["bytes"] = placed.read_bytes()
        try:
            load_placement(placed, config)
        except ConfigError as error:
            seen["refused"] = str(error)
        return 0

    shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True)
    assert seen["bytes"] == source.read_bytes()
    assert "placement" not in tomllib.loads(seen["bytes"].decode("utf-8"))
    assert "key 'placement' is required" in seen["refused"]

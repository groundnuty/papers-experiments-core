"""O79: the HPC render always carries `[placement]`, complete (deploy/hpc-federation/hpcplacement.py, `hpcboot.
write_platform`), so every reader of the rendered platform.toml (a shard's step, an ingest tool, the platform layer)
reads one file and none supplies a default table of its own. The core has no default for the data provider, so a
template declares `holder` (and `up` refuses one that does not, before anything is submitted); the render keeps what the
template declares and writes every other setting at the core's default, read from `PlacementSettings`' fields, which
are the one source of the numbers. Nothing here reaches a cluster."""

import argparse
import dataclasses
import importlib.util
import json
import math
import re
import sys
import tomllib
from pathlib import Path

import pytest
from hpc_fed_support import (
    LAYOUT,
    PILOT_TEMPLATE,
    SITE,
    FakeSlurm,
    agent_site,
    hpc_on_path,
    manifest_values,
    pin_values,
)

from expcore_onedata.config import ConfigError, load_platform
from expcore_onedata.rangemap import PlacementSettings, load_placement

SHARDS = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
TEMPLATE = PILOT_TEMPLATE.read_text(encoding="utf-8")
TEMPLATES = sorted(PILOT_TEMPLATE.parent.glob("*.platform.toml.template"))
DECLARED = 'placement = { holder = "aspen" }'  # the pilot template's own line, its comment after it
# What the core's own dataclass declares, field by field: the render takes its numbers from the same place, and a
# setting added to the dataclass later is expected in the table with no change in either.
CORE_DEFAULTS = {item.name: item.default for item in dataclasses.fields(PlacementSettings) if item.name != "holder"}
BASE = 'providers.pine = { url = "u" }\nproviders.birch = { url = "v" }\n'  # a document of two providers


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture(scope="module")
def pl(hpc):
    import hpcplacement  # on the path while the driver's modules are

    return hpcplacement


@pytest.fixture(scope="module")
def pins(hpc):
    import hpcpins

    return hpcpins


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


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    """A real CA bundle: the render reads its own file back through the platform layer (to size the token margin), which
    refuses one whose `ca_file` is no certificate."""
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


def pilot_facts() -> dict:
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    return {
        "zone": {"url": "https://zone.example.org:8443"},
        "study_space": spec.study_space,
        "providers": {
            p.name: {"url": f"https://{p.name}.example.org:8443", "id": f"pid-{p.name}"} for p in spec.providers
        },
        "spaces": {s.name: {"id": f"sid-{s.name}"} for s in spec.spaces},
        "principals": {p.name: {"user_id": f"uid-{p.name}"} for p in spec.principals},
    }


def render(hpc, root: Path, template: str, ca: bytes) -> tuple[Path, str]:
    """The pilot-shape platform.toml as `up` writes it from `template` (`hpcboot.write_platform`), over invented ids, in
    a root holding the files it names; the file's path and the text `write_platform` returned."""
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    settings = hpc.settings.load_settings(spec.hpc)
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
    text = hpc.boot.write_platform(root / "platform.toml", template, pilot_facts(), values)
    return root / "platform.toml", text


def other_document(text: str) -> dict:
    """A document without its `[placement]`: what completing the table must leave as it was."""
    return {key: value for key, value in tomllib.loads(text).items() if key != "placement"}


def implied(written: Path, into: Path) -> Path:
    """The same file with its `[placement]` as the template declared it (the holder alone), the rest implied: a copy of
    `written` beside it in the root, so the files it names are read from where they are."""
    pattern = r"(?m)^placement = \{.*\}$"
    text = re.sub(pattern, DECLARED, written.read_text(encoding="utf-8"))
    into.write_text(text, encoding="utf-8")
    assert tomllib.loads(text)["placement"] == {"holder": "aspen"}
    return into


# -- the templates declare the holder, the one setting the core has no default for ------------------------------------


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_o79_every_shipped_hpc_template_declares_the_holder_of_its_placement(pl, pins, template):
    """O79: the core has no default data provider, so each template this repository ships for the HPC federation
    declares `[placement] holder`, a provider it declares: the preflight would refuse it otherwise."""
    document = tomllib.loads(pins.filled_in(template.read_text(encoding="utf-8"), "tmpfs"))
    declared = pl.declared_placement(document)
    assert declared["holder"] in document["providers"]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_o79_completing_a_shipped_template_adds_the_cores_defaults_beside_what_it_declares_and_nothing_else(
    pl, pins, template
):
    """O79: for each shipped template, filled in as the preflight does, the table is what the template declares and
    every other setting at its default, in the dataclass's order after the declared keys; the rest of the document is as
    it was."""
    text = pins.filled_in(template.read_text(encoding="utf-8"), "tmpfs")
    declared = tomllib.loads(text)["placement"]
    completed = pl.complete(text)
    table = tomllib.loads(completed)["placement"]
    assert table == {**CORE_DEFAULTS, **declared}
    assert list(table)[: len(declared)] == list(declared)
    assert other_document(completed) == other_document(text)
    assert pl.complete(completed) == completed


def test_o79_the_pilot_template_says_why_it_declares_the_holder():
    """O79: the line is in the template with a comment, so the next reader of the template sees why it is there."""
    (line,) = [line for line in TEMPLATE.splitlines() if line.startswith(DECLARED)]
    assert "#" in line
    assert "holder" in line.split("#", 1)[1]


# -- what the render writes ------------------------------------------------------------------------------------------


def test_o79_the_pilot_render_carries_the_declared_holder_and_every_default_of_the_core(hpc, tmp_path, ca):
    """O79: the render of the pilot's template (its two passes, the last left on disk) has `[placement]` with the
    holder the template declared and every setting of `PlacementSettings` at its default, each with its own type (a
    float stays a float, `reset_shortcut` a boolean), the holder first; nothing else is in the table."""
    assert "@hpc_token_margin_s@" in TEMPLATE  # two passes: the file left is the second's
    path, text = render(hpc, tmp_path / "root", TEMPLATE, ca)
    assert path.read_text(encoding="utf-8") == text
    written = tomllib.loads(text)["placement"]
    expected = {"holder": "aspen", **CORE_DEFAULTS}
    assert written == expected
    assert list(written) == list(expected)
    assert {key: type(value) for key, value in written.items()} == {key: type(v) for key, v in expected.items()}


def test_o79_the_numbers_are_read_from_the_dataclass_never_copied(pl, monkeypatch):
    """O79: a core whose `PlacementSettings` changed a default, or gained a setting, is in the next render with no
    change in the driver: the writer reads the fields, it holds no number."""

    @dataclasses.dataclass(frozen=True)
    class Changed:
        holder: str
        poll_s: float = 9.0
        added_later: int = 7
        mode: str = "quiet"

    monkeypatch.setattr(pl, "PlacementSettings", Changed)
    table = tomllib.loads(pl.complete(BASE + 'placement = { holder = "pine" }\n'))["placement"]
    assert table == {"holder": "pine", "poll_s": 9.0, "added_later": 7, "mode": "quiet"}


def test_o79_a_setting_the_core_gives_no_default_cannot_be_written(pl, monkeypatch):
    """O79: a field of `PlacementSettings` besides the holder that has no default is refused, naming it, rather than
    written as a number of the driver's own."""

    @dataclasses.dataclass(frozen=True)
    class NoDefault:
        holder: str
        needed: int

    monkeypatch.setattr(pl, "PlacementSettings", NoDefault)
    with pytest.raises(RuntimeError, match="needed"):
        pl.complete(BASE + 'placement = { holder = "pine" }\n')


def test_o79_the_loader_reads_the_written_table_as_the_defaults_of_a_file_that_names_only_its_holder(hpc, tmp_path, ca):
    """O79: defaults written out and defaults implied are one setting. The loader keeps its own literal copy of every
    default, so the render's table (from the dataclass), a table of the holder alone (the loader's copy) and the
    dataclass's own defaults must all read as one `PlacementSettings`, and as one record at a resume
    (`reset_settings`)."""
    written_file, _ = render(hpc, tmp_path / "written", TEMPLATE, ca)
    implied_file = implied(written_file, written_file.parent / "implied.toml")
    written = load_placement(written_file, load_platform(written_file))
    holder_only = load_placement(implied_file, load_platform(implied_file))
    assert written.settings == holder_only.settings == PlacementSettings(holder="aspen")
    assert written.reset_settings() == holder_only.reset_settings()


def test_o79_the_platform_fingerprint_is_the_same_with_the_defaults_written_out_or_implied(hpc, tmp_path, ca):
    """O79: a render that writes the defaults out and one that implies them (a table naming the holder alone) are one
    platform: `PlatformConfig.fingerprint()` is equal, so shards of two federations rendered either way still merge as
    one campaign. The fingerprint holds no `[placement]` setting today, so this holds trivially; it pins the property
    for the day it holds the placement bounds a run reads, which it must take after their defaults (as the two files'
    `reset_settings` are, above), never from the text of the table."""
    written_file, _ = render(hpc, tmp_path / "written", TEMPLATE, ca)
    implied_file = implied(written_file, written_file.parent / "implied.toml")
    assert load_platform(written_file).fingerprint() == load_platform(implied_file).fingerprint()


def test_o79_a_template_that_declares_more_than_its_holder_keeps_it_and_gets_the_defaults_for_the_rest(
    hpc, tmp_path, ca
):
    """O79: what a template declares is what a reader gets (here a holder of another provider and two numbers), the
    core's default for every setting it leaves out, written out, and no setting twice."""
    assert DECLARED in TEMPLATE
    template = TEMPLATE.replace(DECLARED, 'placement = { holder = "larch", poll_s = 5.0, concurrency = 4 }')
    path, text = render(hpc, tmp_path / "root", template, ca)
    table = tomllib.loads(text)["placement"]
    assert table == {**CORE_DEFAULTS, "holder": "larch", "poll_s": 5.0, "concurrency": 4}
    assert list(table)[:3] == ["holder", "poll_s", "concurrency"]
    assert load_placement(path, load_platform(path)).settings == PlacementSettings(
        holder="larch", poll_s=5.0, concurrency=4
    )


def test_o79_a_template_that_declares_its_placement_as_a_table_gets_the_same_render(hpc, tmp_path, ca):
    """O79: the spelling does not matter: a `[placement]` table at the end of the template is completed as the inline
    one is, and the rest of the file is the render of the template without it."""
    inline, _ = render(hpc, tmp_path / "inline", TEMPLATE, ca)
    as_table = TEMPLATE.replace(DECLARED, "") + '\n[placement]\nholder = "aspen"\n'
    table, _ = render(hpc, tmp_path / "table", as_table, ca)
    assert tomllib.loads(table.read_text(encoding="utf-8")) == tomllib.loads(inline.read_text(encoding="utf-8"))


# -- what a template may declare, and what completing does to it -------------------------------------------------------

SPELLINGS = {
    "inline": BASE + 'placement = { holder = "pine" }\n',
    "inline, before a table": BASE + 'placement = { holder = "pine" }\n\n[tokens]\nmargin_s = 5\n',
    "inline, with a comment that has a brace": BASE + 'placement = { holder = "pine" }  # why } here\n',
    "inline, a number declared": BASE + 'placement = { holder = "birch", poll_s = 5.0, concurrency = 4 }\n',
    "header, then a table": BASE + '\n[placement]\nholder = "pine"\n\n[tokens]\nmargin_s = 5\n',
    "header, with a comment": BASE + '\n[placement]  # why\nholder = "pine"\n',
    "header, spaced": BASE + '\n[ placement ]\nholder = "pine"\n',
    "header, with its map rows": BASE
    + '\n[placement]\nholder = "pine"\n\n[[placement.map]]\nfile = "a"\nprovider = "birch"\nranges = [[0, 4]]\n',
    "header, no newline at the end": BASE + '\n[placement]\nholder = "pine"',
}


@pytest.mark.parametrize("text", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_o79_completing_keeps_what_is_declared_adds_the_missing_defaults_and_changes_nothing_else(pl, text):
    """O79: each spelling the render can add to (one `placement = { ... }` line of the root, or one `[placement]` table)
    comes out as the declared settings and the core's default for every other, every other table of the file as it was,
    and a second completion changes nothing."""
    declared = tomllib.loads(text)["placement"]
    completed = pl.complete(text)
    assert tomllib.loads(completed)["placement"] == {**CORE_DEFAULTS, **declared}
    assert other_document(completed) == other_document(text)
    assert json.dumps(tomllib.loads(completed)["placement"], sort_keys=True).count('"poll_s"') == 1
    assert pl.complete(completed) == completed


def test_o79_a_table_that_declares_every_setting_is_left_byte_for_byte(pl):
    """O79: nothing to add, nothing touched: the text is returned as it came."""
    full = ", ".join(f"{key} = {pl.toml_scalar(value)}" for key, value in CORE_DEFAULTS.items())
    text = BASE + f'placement = {{ holder = "pine", {full} }}  # all of it\n'
    assert pl.complete(text) is text


REFUSALS = [
    pytest.param(BASE, r"declares no \[placement\]", id="none"),
    pytest.param(BASE + "placement = {}\n", r"declares no 'holder'", id="empty"),
    pytest.param(BASE + "placement = { poll_s = 5.0 }\n", r"declares no 'holder'", id="inline, no holder"),
    pytest.param(BASE + "[placement]\npoll_s = 5.0\n", r"declares no 'holder'", id="header, no holder"),
    pytest.param(BASE + "placement = { holder = 3 }\n", r"'holder' must be the name", id="a number"),
    pytest.param(BASE + 'placement = { holder = "" }\n', r"'holder' must be the name", id="empty name"),
    pytest.param(BASE + 'placement = { holder = "nowhere" }\n', r"'holder' must be a provider", id="not a provider"),
    pytest.param(BASE + "placement = 3\n", r"\[placement\] must be a table", id="not a table"),
    pytest.param(BASE + 'placement.holder = "pine"\n', r"cannot add the core's defaults", id="dotted keys"),
    pytest.param(
        BASE + 'placement = { holder = "pine" }\n[x]\nplacement = { y = 1 }\n',
        r"cannot add the core's defaults",
        id="two lines of the same spelling",
    ),
]


@pytest.mark.parametrize(("text", "message"), REFUSALS)
def test_o79_a_placement_the_render_cannot_complete_is_refused_naming_the_key_and_never_a_value(pl, text, message):
    """O79: no holder, a holder that is no provider of the file, a table in a spelling the defaults cannot be added to
    and no table at all each fail, naming the key (`holder`, `[placement]`); a name the file holds is never in the
    message."""
    with pytest.raises(RuntimeError, match=message) as refused:
        pl.complete(text)
    assert "nowhere" not in str(refused.value)


def test_o79_a_template_without_a_holder_writes_no_platform_file(hpc, tmp_path):
    """O79: the render refuses at once, and leaves no platform.toml for a campaign to read."""
    path = tmp_path / "platform.toml"
    template = 'providers.pine = { url = "@provider_url.pine@", id = "@provider_id.pine@" }\n'
    facts = {
        **pilot_facts(),
        "providers": {"pine": {"url": "https://pine.example.org:8443", "id": "pid"}},
        "spaces": {},
        "principals": {},
    }
    with pytest.raises(RuntimeError, match=r"declares no \[placement\]"):
        hpc.boot.write_platform(path, template, facts, {})
    assert not path.exists()


@pytest.mark.parametrize(
    "value", [True, False, 0, 3, 16 << 20, 2.0, 0.001, 1e-07, 1e16, 3600.0, "listing", 'a "quoted" \\ word'], ids=repr
)  # fmt: skip
def test_o79_a_written_setting_reads_back_as_the_same_value_of_the_same_type(pl, value):
    """O79: a boolean is `true`/`false` (not `0`), an integer stays one, a float stays one, and a string is a basic
    string of TOML."""
    read = tomllib.loads(f"x = {pl.toml_scalar(value)}")["x"]
    assert read == value
    assert type(read) is type(value)


@pytest.mark.parametrize("value", [math.inf, math.nan, None, [1], {"a": 1}], ids=repr)
def test_o79_a_setting_of_a_type_the_writer_does_not_know_is_refused_never_written_as_text(pl, value):
    """O79: a field that a later core adds with a type the writer cannot spell fails the render, instead of writing a
    value the loader would read differently."""
    with pytest.raises((TypeError, ValueError)):
        pl.toml_scalar(value)


# -- the preflight: before the cluster is touched --------------------------------------------------------------------


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


def rig(hpc, tmp_path, edit=lambda text: text):
    """A driver over the fake Slurm for the pilot's layout whose template is `edit` of the shipped one."""
    folder = tmp_path / "layout"
    folder.mkdir()
    (folder / LAYOUT.name).write_text(LAYOUT.read_text(encoding="utf-8"), encoding="utf-8")
    (folder / PILOT_TEMPLATE.name).write_text(edit(TEMPLATE), encoding="utf-8")
    fake = FakeSlurm()
    root = tmp_path / "fed" / "root"
    return hpc.cli.make_driver(folder / LAYOUT.name, str(root), runner=fake), fake, root


EDITS = {
    "no table": lambda text: text.replace(DECLARED, ""),
    "no holder": lambda text: text.replace(DECLARED, "placement = { poll_s = 5.0 }"),
    "a holder that is no provider": lambda text: text.replace(DECLARED, 'placement = { holder = "nowhere" }'),
}


def test_o79_the_preflight_takes_the_shipped_template(hpc, tmp_path, site):
    """O79: the control of the refusals below: the pilot's template, as shipped, passes the preflight."""
    driver, _, _ = rig(hpc, tmp_path)
    driver.preflight(hpc.settings.site_from_environment(site))


@pytest.mark.parametrize("edit", EDITS.values(), ids=EDITS.keys())
def test_o79_up_refuses_a_template_that_declares_no_holder_before_anything_is_submitted(hpc, tmp_path, site, edit):
    """O79: the render would refuse it after the federation is up and billing; `up` reads the template first (as it
    reads the sandbox's pins), names the key and never a name the template holds, and submits and writes nothing."""
    assert DECLARED in TEMPLATE
    driver, fake, root = rig(hpc, tmp_path, edit)
    with pytest.raises(RuntimeError, match=r"\[placement\]|'holder'") as refused:
        driver.up(hpc.settings.site_from_environment(site))
    assert "nowhere" not in str(refused.value)
    assert not [call for call in fake.calls if call[0] == "sbatch"]
    assert not (root / "hpc-federation.json").exists()


# -- what the readers see ----------------------------------------------------------------------------------------------


def step_args(tmp_path):
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False,
                              archive_left=False, snapshot_every=3600.0)  # fmt: skip


def test_o79_a_shard_step_places_the_platform_file_as_it_is_and_the_loader_refuses_a_file_without_the_table(
    shards, tmp_path, monkeypatch
):
    """O79: the step adds no `[placement]` to the federation's platform.toml it places beside a study, whatever the
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

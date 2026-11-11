"""The layout a federation study declares (deploy/compose-federation/fedspec.py): the default is the deployment the
directory always had, the example holds five providers and four spaces with a mirror of a second owner's space, and
every refusal names the file and the key."""

from pathlib import Path

import pytest
from federation_support import DEPLOY, EXAMPLE, MINIMAL


def test_the_default_layout_is_the_deployment_this_directory_always_had(fed):
    """The committed day-0 facts and platform.toml.template were measured on these values: one zone and three
    providers on 29443-29446, the study space supported by all three with pine its data provider, 10 GiB support, 6g
    per service, and the three fixed users with view and read only."""
    spec = fed.spec.load_spec()
    assert spec.source == "federation.toml"
    assert (spec.project, spec.domain) == ("expcore-fed", "expcore-fed.test")
    assert [(s.name, s.port, s.memory_limit) for s in spec.services] == [
        ("zone", 29443, "6g"),
        ("pine", 29444, "6g"),
        ("birch", 29445, "6g"),
        ("cedar", 29446, "6g"),
    ]
    assert spec.zone.domain == "zone.expcore-fed.test"
    assert spec.zone.url == "https://127.0.0.1:29443"
    assert [(s.name, s.supporters, s.support_bytes) for s in spec.spaces] == [
        ("alpha-space", ("pine", "birch", "cedar"), 10 << 30),
        ("beta-space", ("birch", "cedar"), 10 << 30),
    ]
    assert (spec.study_space, spec.holder) == ("alpha-space", "pine")
    assert [(p.name, p.privileges, p.spaces) for p in spec.principals] == [
        (user, ("space_view", "space_read_data"), ("alpha-space",))
        for user in ("alpha-reader", "alpha-tools", "alpha-health")
    ]
    assert spec.platform_template == DEPLOY / "platform.toml.template"
    assert spec.memory_bytes == 24 << 30


def test_the_example_has_five_providers_four_spaces_and_one_provider_mirroring_a_second_owners_space(fed):
    spec = fed.spec.load_spec(EXAMPLE)
    assert len(spec.providers) == 5
    assert len(spec.spaces) == 4
    owners = {s.name: s.owner for s in spec.spaces}
    mirroring_owners = {m for s in spec.spaces for m in s.mirrors if m in owners.values() and m != s.owner}
    assert mirroring_owners, "a provider that owns a space also mirrors another owner's space"
    default = fed.spec.load_spec()
    assert spec.project != default.project
    assert not {s.port for s in spec.services} & {s.port for s in default.services}
    assert spec.space("delta-space").support_bytes == 5 << 30
    assert spec.members("delta-space")[0].name == "delta-reader"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "federation.toml"
    path.write_text(text, encoding="utf-8")
    return path


REFUSED = [
    ("compose project name", ('project = "expcore-test"', 'project = "Expcore Test"'), "project"),
    ("domain outside .test", ('domain = "expcore-test.test"', 'domain = "expcore-test.example"'), "domain"),
    (
        "address other than the loopback",
        ('study_space = "alpha-space"', 'study_space = "alpha-space"\naddress = "0.0.0.0"'),
        "address",
    ),
    ("port clash", ("port = 29645", "port = 29643"), "providers.elm.port: 29643 is also the port of zone"),
    ("privileged port", ("port = 29644", "port = 80"), "providers.oak.port"),
    ("port with an address", ("port = 29644", 'port = "0.0.0.0:29644"'), "providers.oak.port"),
    ("provider named like the janitor", ("[providers.elm]", "[providers.janitor]"), "providers.janitor"),
    ("provider named like the zone", ("[providers.elm]", "[providers.zone]"), "providers.zone"),
    ("provider name not a label", ("[providers.elm]", "[providers.Elm]"), "providers.Elm"),
    ("unknown key", ("port = 29644", 'port = 29644\nhost = "somewhere"'), "providers.oak.host"),
    ("owner not declared", ('owner = "oak"', 'owner = "ash"'), "spaces.alpha-space.owner"),
    ("mirror not declared", ('mirrors = ["elm"]', 'mirrors = ["ash"]'), "spaces.alpha-space.mirrors"),
    ("owner among its mirrors", ('mirrors = ["elm"]', 'mirrors = ["elm", "oak"]'), "spaces.alpha-space.mirrors"),
    ("mirror twice", ('mirrors = ["elm"]', 'mirrors = ["elm", "elm"]'), "spaces.alpha-space.mirrors"),
    ("study space not declared", ('study_space = "alpha-space"', 'study_space = "gamma-space"'), "study_space"),
    ("the zone's admin as a principal", ("[principals.alpha-reader]", "[principals.admin]"), "principals.admin"),
    ("privilege not a space privilege", ('"space_read_data"]', '"zone_admin"]'), "principals.alpha-reader.privileges"),
    (
        "member of an undeclared space",
        ('"space_read_data"]', '"space_read_data"]\nspaces = ["gamma-space"]'),
        "principals.alpha-reader.spaces",
    ),
    (
        "memory limit",
        ('study_space = "alpha-space"', 'study_space = "alpha-space"\nmemory_limit = "6 GB"'),
        "memory_limit",
    ),
    ("zone missing", ("[zone]\nport = 29643\n", ""), "zone: is required"),
    (
        "template missing",
        ('study_space = "alpha-space"', 'study_space = "alpha-space"\nplatform_template = "none.t"'),
        "platform_template",
    ),
    ("provider declared twice", ("[providers.elm]\nport = 29645", "[providers.oak]\nport = 29645"), "not a TOML file"),
]


@pytest.mark.parametrize(("change", "words"), [case[1:] for case in REFUSED], ids=[case[0] for case in REFUSED])
def test_a_layout_that_cannot_be_used_is_refused_naming_the_file_and_the_key(fed, tmp_path, change, words):
    old, new = change
    assert MINIMAL.count(old) >= 1
    path = write(tmp_path, MINIMAL.replace(old, new, 1))
    with pytest.raises(fed.spec.LayoutError) as refused:
        fed.spec.load_spec(path)
    message = str(refused.value)
    assert message.startswith(f"{path}: ")
    assert words in message


def test_the_minimal_layout_the_refusals_start_from_is_itself_usable(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, MINIMAL))
    assert spec.holder == "oak"
    assert spec.platform_template is None


def test_a_bad_layout_refuses_every_command_before_anything_is_touched(fed, tmp_path, monkeypatch, capsys):
    path = write(tmp_path, MINIMAL.replace("port = 29645", "port = 29643"))
    touched = []
    for name in ("up", "reset_clean", "reset_replicas", "down", "status"):
        monkeypatch.setattr(fed.cli, name, lambda layout, name=name: touched.append(name))
    assert fed.cli.main(["--layout", str(path), "up"]) == 2
    assert touched == []
    assert "providers.elm.port" in capsys.readouterr().err


def test_compose_prints_the_file_the_layout_generates(fed, capsys):
    assert fed.cli.main(["--layout", str(EXAMPLE), "compose"]) == 0
    assert capsys.readouterr().out == EXAMPLE.with_suffix(".compose.yaml").read_text(encoding="utf-8")


def test_describe_records_the_layout_the_root_parent_and_the_memory_limits(fed, tmp_path):
    layout = fed.layout.Layout(tmp_path / "fed", spec=fed.spec.load_spec(EXAMPLE))
    table = fed.facts.describe(layout)
    assert "| Layout | `five-providers.toml`, SHA-256 " in table
    assert "| Root parent | `/mnt/volume2/expcore-federation` (default) |" in table
    assert "| Space `delta-space` | owner `hazel`, mirrors `alder`, support 5.00 GiB each |" in table
    assert "| Memory limits together | 36.00 GiB for 6 services |" in table


def test_the_day0_template_has_every_row_describe_prints_and_a_peak_per_service(fed, tmp_path):
    """The dated facts file starts from the template, so the deployment rows describe prints and the per-service
    peak have a place in it."""
    template = (DEPLOY / "day0" / "federation-facts.template.md").read_text(encoding="utf-8")
    rows = [line.split("|")[1].strip() for line in fed.facts.describe(fed.layout.Layout(tmp_path)).splitlines()[2:]]
    kinds = {row.split("`")[0] for row in rows}  # one per kind of row: a provider, a space, a user stand for all
    assert all(f"| {kind}" in template for kind in kinds)
    assert "| Layout | Service | Declared limit | Cgroup limit | Peak after up |" in template
    for command in ("reset --replicas", "reset --clean", "up from clean", "down"):
        assert f"| {command} |" in template

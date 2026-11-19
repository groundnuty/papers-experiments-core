"""C06: the platform's fingerprint holds every declared setting that shapes a run, after its default and in one
canonical form, and nothing that is one federation's or one host's own. Two renders of one template on two federations
(their own addresses, ids, ports, names and roots) give one fingerprint; changing any one setting that shapes a run
gives another; and every key the platform's loaders accept is classified in or out, so a new one cannot stay out
unseen. The templates and the classification are `runsettings_support`'s."""

import ast
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from runsettings_support import (
    APPTAINER,
    CHECK_FILE_LINE,
    CHECK_PROVIDER_LINE,
    DOCKER,
    FEDERATIONS,
    HAND_READ,
    IN,
    OUT,
    SHA,
    SWAP_CA,
    normalised,
    platform,
    render,
)

from expcore.campaign.manifest import compare_fingerprints
from expcore.config import loading
from expcore.config.loading import ConfigError
from expcore_onedata import config as config_module
from expcore_onedata import fault_settings, linksettings, rangemap, runsettings, spaces, tls, users
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox import settings as sandbox_settings
from expcore_onedata.sandbox.apptainer import settings as apptainer_settings
from expcore_onedata.users import load_slots

TEMPLATES = {"docker": DOCKER, "apptainer": APPTAINER}
HASH = re.compile(r"(sha256:)?[0-9a-f]{64}")
LOADERS = (config_module, spaces, tls, sandbox_settings, apptainer_settings, fault_settings, rangemap, users,
           linksettings)  # fmt: skip


@pytest.fixture(scope="module")
def cas(tmp_path_factory) -> tuple[bytes, bytes]:
    """Two CA bundles: the one every federation of the image shares, and another."""
    support = pytest.importorskip("tls_support")
    if not support.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    one = support.make_federation_ca(tmp_path_factory.mktemp("ca-one")).ca.read_bytes()
    two = support.make_federation_ca(tmp_path_factory.mktemp("ca-two"), name="another-test-ca").ca.read_bytes()
    return one, two


def edited(template: dict, edits) -> dict:
    """The template with each (file, text, replacement) made, each text found exactly once."""
    files = dict(template)
    for name, old, new in edits:
        assert files[name].count(old) == 1, f"{name}: {old!r} is not found exactly once"
        files[name] = files[name].replace(old, new)
    return files


def load(template: dict, root: Path, ca: bytes, values: dict = FEDERATIONS[0]):
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return load_platform(render(template, root, values, ca), require_secrets=False)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_c06_two_renders_of_one_template_on_two_federations_give_one_fingerprint(tmp_path, cas, name):
    """C06: one template rendered for two federations, with their own addresses, ids, ports, host and network names,
    registry, address pool, Compose project and root, gives one fingerprint, and none of those values is in it (O12).
    Both roots hold the same CA bundle, as an HPC federation's `up` exports the image's own test CA (`hpcca.py`); a
    Compose federation's `up` makes a CA of its own, so two of those differ in `tls`, as `tls.ca_file` below shows."""
    one, two = (load(TEMPLATES[name], tmp_path / f"fed-{i}" / "root", cas[0], values) for i, values in
                enumerate(FEDERATIONS))  # fmt: skip
    assert one.file.read_text() != two.file.read_text()
    assert one.fingerprint() == two.fingerprint()
    leaves = [str(leaf) for leaf in _leaves(one.fingerprint()) if not HASH.fullmatch(str(leaf))]  # a hash shows none
    for values in FEDERATIONS:  # a short number (a prefix, a count) is no name: it shows in other settings' values
        shown = {key: f":{value}" if key == "port" else value for key, value in values.items()}
        leaked = sorted(key for key, value in shown.items() if len(value) > 4 and any(value in leaf for leaf in leaves))
        assert not leaked, f"per-federation values in the fingerprint: {leaked}"
    assert not any(str(tmp_path) in leaf for leaf in leaves)


def _leaves(value):
    """Every leaf of a fingerprint: each value of its mappings and lists, keys included."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


@pytest.mark.parametrize("key", sorted(key for key, change in IN.items() if change is not None))
def test_c06_changing_one_setting_that_shapes_a_run_changes_the_fingerprint(tmp_path, cas, key):
    """C06: every setting classified in, changed alone (with what the loader requires beside it), gives another
    fingerprint, so a resume under it is refused and its shards never merge with the base's."""
    change = IN[key]
    base = load(TEMPLATES[change[0]], tmp_path / "root", cas[0]).fingerprint()
    if change == SWAP_CA:
        other = load(TEMPLATES[change[0]], tmp_path / "root", cas[1]).fingerprint()
    else:
        other = load(edited(TEMPLATES[change[0]], change[1]), tmp_path / "root", cas[0]).fingerprint()
    assert other != base
    assert not compare_fingerprints({"platform": base}, {"platform": other}).same


def test_c06_the_backends_of_the_sandbox_and_the_fault_injector_are_in_the_fingerprint(tmp_path, cas):
    """C06: a Docker and an Apptainer sandbox, and a Compose and an HPC fault injector, take keys of their own, so no
    one key switches them; each fingerprint names its backend."""
    docker = load(DOCKER, tmp_path / "docker", cas[0]).fingerprint()
    hpc = load(APPTAINER, tmp_path / "apptainer", cas[0]).fingerprint()
    assert (docker["sandbox"]["backend"], hpc["sandbox"]["backend"]) == ("docker", "apptainer")
    assert (docker["faults"]["settings"]["backend"], hpc["faults"]["settings"]["backend"]) == ("compose", "hpc")


def test_o87_the_check_provider_is_in_the_fingerprint_by_position_only_when_it_is_not_the_local_one(tmp_path, cas):
    """O87: no key, or the local provider named, leaves the fingerprint as it was before the key (a store started
    before it resumes); another provider adds `content_check_provider` to `sandbox` as its `providers[i]`, a position,
    never its name, so two federations rendered from one template still give one fingerprint."""
    edits = [platform(CHECK_FILE_LINE, CHECK_FILE_LINE + CHECK_PROVIDER_LINE)]
    base = load(DOCKER, tmp_path / "base", cas[0]).fingerprint()
    assert "content_check_provider" not in base["sandbox"]
    prints = [load(edited(DOCKER, edits), tmp_path / f"fed-{i}", cas[0], values).fingerprint()
              for i, values in enumerate(FEDERATIONS)]  # fmt: skip
    assert prints[0]["sandbox"]["content_check_provider"] == "providers[1]"
    assert prints[0] == prints[1]
    assert "birch" not in json.dumps(prints[0])


def test_o87_an_apptainer_platform_with_spaces_takes_the_check_provider_of_the_first_data_path_space(tmp_path, cas):
    """O87: the Apptainer backend's `[sandbox]` with `[[spaces]]` is checked through `load_platform`: a provider of the
    first space is read through and in the fingerprint by position; one that supports another space alone is refused,
    naming the key."""
    anchor = 'content_check_file = "dir/check-apptainer.bin"'  # the template carries the check file (O69)

    def with_provider(name):
        return edited(APPTAINER, [platform(anchor, anchor + f'\ncontent_check_provider = "{name}"')])

    loaded = load(with_provider("larch"), tmp_path / "ok", cas[0])
    assert loaded.content_check_reader.name == "larch"
    assert loaded.fingerprint()["sandbox"]["content_check_provider"] == "providers[1]"
    with pytest.raises(ConfigError, match="content_check_provider") as raised:
        load(with_provider("rowan"), tmp_path / "other", cas[0])  # supports the second space alone
    assert "rowan" not in str(raised.value).replace(str(tmp_path), "")


@pytest.mark.parametrize(("template", "declared"), [("docker", 3600), ("apptainer", 7200)])
def test_o85_the_gates_upstream_read_timeout_is_in_the_fingerprint_only_when_it_is_not_the_default(
    tmp_path, cas, template, declared
):
    """C06, O85: `[sandbox.gate] upstream_read_timeout_s` decides when an agent's REST read fails in an outage, so a
    value other than the default is in `sandbox.gate` (as a float, so `3600` and `3600.0` are one value); the default
    written either way, and no table, leave the fingerprint as it was before the key (a store started before it
    resumes). It is the same on both backends."""
    table = f"[sandbox.gate]\nupstream_read_timeout_s = {declared}\n\n"
    base = load(TEMPLATES[template], tmp_path / "declared", cas[0]).fingerprint()
    left_out = load(edited(TEMPLATES[template], [platform(table, "")]), tmp_path / "left-out", cas[0]).fingerprint()
    assert base["sandbox"]["gate"] == {"upstream_read_timeout_s": float(declared)}
    assert "gate" not in left_out["sandbox"]
    assert base != left_out
    for written in ("14400", "14400.0"):
        same = [platform(table, f"[sandbox.gate]\nupstream_read_timeout_s = {written}\n\n")]
        at_default = load(edited(TEMPLATES[template], same), tmp_path / f"default-{written}", cas[0]).fingerprint()
        assert at_default == left_out
        assert json.dumps(at_default, sort_keys=True) == json.dumps(left_out, sort_keys=True)
    spelled = [platform(f"upstream_read_timeout_s = {declared}\n", f"upstream_read_timeout_s = {declared}.0\n")]
    float_spelled = load(edited(TEMPLATES[template], spelled), tmp_path / "float", cas[0]).fingerprint()
    assert json.dumps(float_spelled, sort_keys=True) == json.dumps(base, sort_keys=True)


@pytest.mark.parametrize(
    ("template", "edits"),
    [
        ("docker", [platform("max_excess_s = 600\n", ""), platform("revocation_settle_s = 330\n", "")]),
        ("docker", [platform("poll_s = 2\nsettle", "poll_s = 2.0\nsettle"), platform("clear_attempts = 3\n", "")]),
        ("docker", [platform('heal = "off"', 'heal = "off"\nheal_timeout_s = 0'),
                    platform("heal_settle_s = 180\n", "")]),
        ("docker", [platform("cpus = 2.0", "cpus = 2"), platform("gate_memory_bytes = 1073741824\n", "")]),
        ("docker", [platform("kill_grace_s = 5\n", ""), platform('placeholder_token = "placeholder"\n', "")]),
        ("docker", [platform("agent_uid = 10001\n", ""), platform('python = "python"\n', "")]),
        ("docker", [platform('["/etc/ssl/certs/ca-certificates.crt", "/opt/certs/extra.pem"]',
                             '["/opt/certs/extra.pem", "/etc/ssl/certs/ca-certificates.crt"]')]),
        ("docker", [platform('allow = ["list_files", "read_file"]', 'allow = ["read_file", "list_files"]'),
                    platform("start_timeout_s = 120\n", "")]),
        ("docker", [platform('providers = ["pine", "birch", "cedar"]\nlocal', 'providers = ["cedar", "pine", "birch"]\n'
                                                                              'local')]),
        ("docker", [platform('served = { kind = "rest", listing = "paged" }', 'served = "rest"')]),
        ("docker", [platform(CHECK_FILE_LINE, CHECK_FILE_LINE + '\ncontent_check_provider = "pine"')]),
        ("apptainer", [platform("hidden_memory_margin_bytes = 134217728\n", ""),
                       platform("memory_poll_s = 0.1\n", "")]),
    ],
    ids=["tokens", "fault-numbers", "fault-heal", "limits", "grace-placeholder", "agent", "trust-order",
         "allow-order", "support-order", "listing", "check-provider-local", "apptainer"],
)  # fmt: skip
def test_c06_a_setting_at_its_default_and_one_left_out_or_written_otherwise_give_one_fingerprint(
    tmp_path, cas, template, edits
):
    """C06: the fingerprint holds each setting's effective value, so one declared at its default and one left out are
    one experiment, `2` and `2.0` are one number, and the order of a set (an allow-list, the trust paths, a support
    set) is no setting."""
    base = load(TEMPLATES[template], tmp_path / "root", cas[0]).fingerprint()
    other = load(edited(TEMPLATES[template], edits), tmp_path / "root", cas[0]).fingerprint()
    assert other == base
    assert json.dumps(other, sort_keys=True) == json.dumps(base, sort_keys=True)  # one canonical JSON: one digest


def test_c06_the_egress_and_mount_pins_stay_in_the_fingerprint(tmp_path, cas):
    """C06, O56, O65: the Apptainer backend's pins keep their record (`sandbox_backend`, as before: the image, the
    egress shim and collector, the oneclient bundle), and the mount keeps its I/O mode per level and its sidecar's
    image id on Docker."""
    hpc = load(APPTAINER, tmp_path / "apptainer", cas[0])
    printed = hpc.fingerprint()
    assert printed["sandbox_backend"] == hpc.sandbox.apptainer.record()
    pins = printed["sandbox_backend"]
    assert (pins["image_sha256"], pins["egress_shim_sha256"], pins["egress_collector_sha256"]) == (
        SHA["sif"], SHA["shim"], SHA["collector"]
    )  # fmt: skip
    assert pins["oneclient_bundle_sha256"] == SHA["bundle"]
    assert printed["data_paths"]["mounted"] == {"kind": "mount", "io_mode": "proxy"}
    docker = load(DOCKER, tmp_path / "docker", cas[0]).fingerprint()
    assert docker["sandbox"]["images"]["oneclient"] == f"sha256:{SHA['oneclient']}"
    assert docker["sandbox"]["mount"]["io_mode"] == "proxy"
    assert docker["data_paths"]["mounted"] == {"kind": "mount", "io_mode": "proxy"}


def test_o91_a_platform_that_declares_no_read_log_pin_keeps_the_fingerprint_it_had(tmp_path, cas):
    """O91: the read-log collector's pin is in the backend's record only when it is declared (another collector is
    another experiment), so a platform without the channel has no key of it and fingerprints as before the key
    existed; one that declares it differs in that key alone."""
    key = "sandbox.apptainer.readlog_collector_sha256"
    base = load(APPTAINER, tmp_path / "root", cas[0]).fingerprint()
    pinned = load(edited(APPTAINER, IN[key][1]), tmp_path / "root", cas[0]).fingerprint()
    assert "readlog_collector_sha256" not in base["sandbox_backend"]
    assert pinned["sandbox_backend"]["readlog_collector_sha256"] == SHA["gate"]
    del pinned["sandbox_backend"]["readlog_collector_sha256"]
    assert pinned == base


def test_c06_a_store_recorded_before_the_settings_were_in_the_fingerprint_is_refused_naming_them(tmp_path, cas):
    """C06: the fingerprint's earlier form (TLS, spaces, data paths, served caps, Apptainer pins and the fault
    injector's sanction and targets) is not this one: a store that recorded it is refused at its resume, each new
    setting named as absent when the campaign started; there is no legacy form for it."""
    now = load(DOCKER, tmp_path / "root", cas[0]).fingerprint()
    earlier = {key: now[key] for key in ("tls", "storage_import", "data_paths", "served_bytes")}
    earlier["faults"] = {"sanctioned": now["faults"]["sanctioned"], "targets": now["faults"]["targets"]}
    compared = compare_fingerprints({"platform": earlier}, {"platform": now})
    assert not compared.same
    absent = "absent when the campaign started"
    assert any(line.startswith(f"platform.tokens.margin_s: {absent}") for line in compared.refused)
    assert any(line.startswith("platform.sandbox.limits.cpus: absent") for line in compared.refused)


def test_c06_a_command_line_is_held_with_its_federations_values_by_role_and_its_options_as_written(tmp_path, cas):
    """C06: the tool server's command line and oneclient's extra arguments hold the federation's own values (its
    zone's URL, a provider's id, a space's id, the platform file's directory) by their role, so one template's line is
    one line on every federation, while an option is kept as written; a value of the federation no role names stays
    as it is, so such a line keeps two federations apart rather than joining them."""
    one, two = (load(DOCKER, tmp_path / f"fed-{i}" / "root", cas[0], values) for i, values in enumerate(FEDERATIONS))
    lines = [
        runsettings.neutral_words([c.toolserver.command, *c.toolserver.args, *c.sandbox.mount.extra_args],
                                  runsettings.federation_roles(c))
        for c in (one, two)
    ]  # fmt: skip
    assert lines[0] == lines[1]
    assert lines[0] == [
        "<platform-dir>/venv/bin/server", "--relay", "<zone>", "--provider", "<providers[0]-id>", "--config",
        "<platform-dir>/server.toml", "--max-rows", "50", "--provider-timeout=60", "--space-id=<spaces[0]-id>",
    ]  # fmt: skip
    foreign = [platform('"--max-rows", "50"', '"--max-rows", "50", "--mirror", "https://mirror-@space_suffix@.org"')]
    apart = [
        load(edited(DOCKER, foreign), tmp_path / f"apart-{i}" / "root", cas[0], values)
        for i, values in enumerate(FEDERATIONS)
    ]
    assert apart[0].fingerprint()["toolserver"] != apart[1].fingerprint()["toolserver"]


# The tool server's interpreter and config in the study's own directory, which the platform declares (#451): a path
# each site has its own of, outside the federation's root.
ARTIFACTS_EDITS = [
    platform('command = "@root@/venv/bin/server"', 'command = "@artifacts@/toolserver/venv/bin/server"'),
    platform('"--config", "@root@/server.toml"', '"--config", "@artifacts@/toolserver/server.toml"'),
    platform('clone = "@root@/clone"', 'clone = "@root@/clone"\nartifacts = "@artifacts@"'),
]
SITES = ("site-a/artifacts", "cluster-b/studies/s/artifacts-v1")  # two lengths: a size can never match by chance


def _at_site(tmp_path, cas, index: int, edits=ARTIFACTS_EDITS):
    values = {**FEDERATIONS[index], "artifacts": str(tmp_path / SITES[index])}
    return load(edited(DOCKER, edits), tmp_path / f"fed-{index}" / "root", cas[0], values)


def test_o94_one_template_with_its_artifacts_at_two_sites_paths_gives_one_fingerprint(tmp_path, cas):
    """O94 (#451): the tool server's command and its files lie in the study's own directory, whose path is each
    site's own (`@study_artifacts@`). Declared as `[toolserver] artifacts`, it is a role of the command line, so two
    sites' renders give one fingerprint; undeclared, its path is in the command's hash and the two sites' shards never
    join one campaign."""
    one, two = (_at_site(tmp_path, cas, index) for index in range(2))
    assert one.fingerprint() == two.fingerprint()
    words = runsettings.neutral_words([one.toolserver.command], runsettings.federation_roles(one))
    assert words == ["<artifacts>/toolserver/venv/bin/server"]
    undeclared = [edit for edit in ARTIFACTS_EDITS if "artifacts =" not in edit[2]]
    apart = [_at_site(tmp_path, cas, index, undeclared).fingerprint()["toolserver"] for index in range(2)]
    assert apart[0] != apart[1]


def test_o94_a_command_that_differs_inside_the_declared_artifacts_directory_is_refused(tmp_path, cas):
    """O94: only the directory's path is a role: another interpreter inside it is another command line, so a shard
    under it is refused (the fingerprints differ)."""
    one = _at_site(tmp_path, cas, 0).fingerprint()
    other = [platform('"@artifacts@/toolserver/venv/bin/server"', '"@artifacts@/toolserver/venv2/bin/server"')]
    two = _at_site(tmp_path, cas, 1, [*ARTIFACTS_EDITS, *other]).fingerprint()
    assert not compare_fingerprints({"platform": one}, {"platform": two}).same


def test_o94_a_command_inside_the_clone_is_one_line_when_a_site_reaches_its_artifacts_through_a_link(tmp_path, cas):
    """O94 (Codex round 1): the clone's role is its resolved path, the artifacts directory's its path as written. With
    the command inside the clone and one site's directory declared through a parent that is a link, the clone's role
    matches the command at one site only, so the declared directory is replaced first: one line at both sites."""
    inside = [
        platform('command = "@root@/venv/bin/server"', 'command = "@artifacts@/clone/venv/bin/server"'),
        platform('clone = "@root@/clone"', 'clone = "@artifacts@/clone"\nartifacts = "@artifacts@"'),
    ]
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    configs = []
    for index, artifacts in enumerate((tmp_path / "site-a" / "artifacts", tmp_path / "linked" / "studies" / "art")):
        values = {**FEDERATIONS[index], "artifacts": str(artifacts)}
        configs.append(load(edited(DOCKER, inside), tmp_path / f"fed-{index}" / "root", cas[0], values))
    assert str(configs[1].toolserver.clone) != f"{tmp_path}/linked/studies/art/clone"  # resolved through the link
    assert configs[0].fingerprint() == configs[1].fingerprint()


@pytest.mark.parametrize("value", ["relative/artifacts", "/", "//"])
def test_o94_an_artifacts_directory_that_is_not_absolute_or_is_the_root_is_refused(tmp_path, cas, value):
    """O94: the declared directory is an absolute path other than the root, as the command that lies in it is."""
    declared = [platform('clone = "@root@/clone"', f'clone = "@root@/clone"\nartifacts = "{value}"')]
    with pytest.raises(ConfigError, match="'artifacts' must be an absolute path other than the root"):
        load(edited(DOCKER, declared), tmp_path / "root", cas[0])


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        (("read", "--read-buffer-max-size=1048576"), ("write", "--write-buffer-max-size=1048576")),
        (("cache", "--cache-size=64"), ("meta", "--meta-size=64")),
        (("pine", "--host=pine.example.org"), ("birch", "--host=birch.example.org")),
        (("/srv/root", "/srv/root2/server.toml"), ("/srv/other", "/srv/other2/server.toml")),
    ],
)
def test_c06_a_federations_value_inside_a_longer_name_is_never_replaced_so_two_options_never_become_one(ours, theirs):
    """C06 (Codex round 2): a value is replaced only where it stands whole. A zone whose host is `read` leaves
    `--read-buffer-max-size` as written, so it never becomes the same line as `--write-buffer-max-size` on a
    federation whose zone is `write`, which would merge shards under other settings."""
    lines = [runsettings.neutral_words([word], [(value, "zone-host")]) for value, word in (ours, theirs)]
    assert lines[0] != lines[1]
    assert lines[0] == [ours[1]]
    whole = runsettings.neutral_words(["--relay=read", "read", "x:read:1"], [("read", "zone-host")])
    assert whole == ["--relay=<zone-host>", "<zone-host>", "x:<zone-host>:1"]


def test_c06_a_single_label_host_is_no_role_so_an_options_value_equal_to_it_stays_as_written(tmp_path, cas):
    """C06: a zone host of one label (`read`) could be an option's own value, so it is no role: `--mode read` on a
    federation whose zone is `read` and `--mode write` on one whose zone is `write` stay two lines, never one."""
    lines = []
    for host, mode in (("read", "read"), ("write", "write")):
        values = {**FEDERATIONS[0], "zone_host": host}
        mode_args = [platform('"--max-rows", "50"', f'"--max-rows", "50", "--mode", "{mode}"')]
        config = load(edited(DOCKER, mode_args), tmp_path / host / "root", cas[0], values)
        roles = runsettings.federation_roles(config)
        assert host not in dict(roles)
        lines.append(runsettings.neutral_words(config.toolserver.args, roles))
    assert lines[0] != lines[1]


def test_c06_a_new_tool_server_condition_is_refused_at_a_resume_and_is_no_growth(tmp_path, cas):
    """C06, a known limit: a condition added to `[toolserver.conditions]` is a new leaf of the platform's half, which
    the core never counts as growth (only the schema's sections grow), so a campaign that adds a condition needing a
    tool-server entry of its own is refused a resume and starts a new store."""
    base = load(DOCKER, tmp_path / "root", cas[0]).fingerprint()
    entry = '[toolserver.conditions.lookup]\nallow = ["list_files"]\nrights = ["space_view", "space_read_data"]\n'
    entry += 'manifest = "@root@/manifests/other.json"\n\n[slots.one]'
    grown = load(edited(DOCKER, [platform("[slots.one]", entry)]), tmp_path / "root", cas[0]).fingerprint()
    compared = compare_fingerprints({"platform": base}, {"platform": grown})
    assert compared.refused
    assert not compared.grown
    assert all(line.startswith("platform.toolserver.conditions.") for line in compared.refused)


def test_c06_a_tool_servers_manifest_that_cannot_be_read_is_refused_by_its_position_never_its_path(tmp_path, cas):
    """C06, O12: the fingerprint reads each condition's manifest for its SHA-256; one gone since the load is refused
    naming the condition's position and the key, never the path or the condition's name."""
    config = load(DOCKER, tmp_path / "root", cas[0])
    (tmp_path / "root" / "manifests" / "reader.json").unlink()
    with pytest.raises(ConfigError, match=r"conditions\[0\]: key 'manifest' cannot be read") as refused:
        config.fingerprint()
    assert str(tmp_path) not in str(refused.value).split(": ", 1)[1]
    assert "reader" not in str(refused.value).split(": ", 1)[1]


def _reject_unknown_sites() -> set[tuple[str, int]]:
    """Every call of `reject_unknown` in the platform's loaders, by file and line."""
    sites = set()
    for module in LOADERS:
        path = Path(module.__file__).resolve()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "reject_unknown":
                sites.add((str(path), node.lineno))
    return sites


def test_c06_every_key_the_platforms_loaders_accept_is_classified_in_or_out_of_the_fingerprint(
    tmp_path, cas, monkeypatch
):
    """C06: every key a loader of platform.toml accepts, read from the loaders themselves (each `reject_unknown` call,
    every one of which the two templates reach) and the keys they read by hand, is in the fingerprint or out of it
    with a reason. A key added to a loader and to neither list fails here, so no setting stays out unseen; a listed
    key no loader accepts fails too. A table is no setting: its keys are classified."""
    recorded: list[tuple[str, tuple[str, ...], tuple[str, int]]] = []
    original = loading.Section.reject_unknown

    def recording(section, known):
        caller = sys._getframe(1)
        site = (str(Path(caller.f_code.co_filename).resolve()), caller.f_lineno)
        recorded.append((section.where, tuple(known), site))
        return original(section, known)

    monkeypatch.setattr(loading.Section, "reject_unknown", recording)
    for name, template in TEMPLATES.items():
        config = load(template, tmp_path / name, cas[0])
        config.fingerprint()  # reads [placement] and [faults]
        if "[slots." in template["platform.toml"]:
            load_slots(config.file, config, require_secrets=False)
    assert _reject_unknown_sites() <= {site for _, _, site in recorded}, "a loader's check no template reaches"
    sections = {normalised(where) for where, _, _ in recorded}
    accepted = {f"{normalised(where)}.{key}".lstrip(".") for where, known, _ in recorded for key in known}
    tables = {key for key in accepted if any(s == key or s.startswith(f"{key}.") for s in sections)}
    settings = (accepted - tables) | HAND_READ
    assert not set(IN) & set(OUT)
    unclassified = sorted(settings - set(IN) - set(OUT))
    assert not unclassified, f"keys neither in nor out of the fingerprint: {unclassified}"
    stale = sorted((set(IN) | set(OUT)) - accepted - HAND_READ)
    assert not stale, f"classified keys no loader accepts: {stale}"
    assert all(reason.strip() for reason in OUT.values())

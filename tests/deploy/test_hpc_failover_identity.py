"""O98: the member's identity read reports the failover order of its root, by labels, only when the root holds the
record `up` writes for a layout that declares an owner outage (deploy/hpc-federation/fleetidentity.py), and is the line
it always was otherwise. The order is in no value the join compares across members and not in the render identity. The
interpreter and the transport are stand-ins; nothing reaches a cluster."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from hpc_fleet_support import fleet_modules

from expcore_onedata import failover

FILE = "failover-order.json"
FINGERPRINT = "f" * 64
IDENTITY = {"layout": "a" * 64, "platform": "b" * 64}
PLATFORM = "c" * 64
PASSED = {
    "version": "expcore-failover-order/1",
    "spaces": [
        {
            "space": "spaces[0]",
            "owner": "providers[1]",
            "mirrors": ["providers[0]", "providers[2]"],
            "order": ["providers[2]", "providers[0]", "providers[1]"],
            "mirror_first": True,
        }
    ],
}


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetidentity
        import fleetrecord

        yield SimpleNamespace(found=found, fleetidentity=fleetidentity, fleetrecord=fleetrecord)


def member_root(deploy, tmp_path):
    """A fleet whose one member `fed-1` is up, with its root; the words `fleetidentity.py read` is run with."""
    fleet = tmp_path / "fleet"
    fleet.mkdir(mode=0o700)
    study = tmp_path / "study"
    study.mkdir()
    (study / "study.toml").write_text("[study]\n")
    settings = tmp_path / "shard-settings.toml"
    settings.write_text(
        f'study = "{study}"\npython = "/venv/bin/python"\ncore_src = "/c"\nresults = "/r"\ntmpfs = "/t"\n'
        'step_path = "/usr/bin:/bin"\ncpus = 2\nmemory = "4g"\n'
    )
    record = deploy.fleetrecord.new_record(tmp_path / "layout.toml", "digest", 2, 5, 1)
    member = deploy.fleetrecord.new_member(fleet, "fed-1")
    member["status"] = "up"
    root = deploy.fleetrecord.root_of(fleet, "fed-1")
    root.mkdir(parents=True)
    (root / "platform.toml").write_text('[space]\nname = "s"\n')
    member["made"] = [root.stat().st_dev, root.stat().st_ino]
    record["members"].append(member)
    deploy.fleetrecord.Record(fleet).save(record)
    return root, ["read", "--fleet", str(fleet), "--member", "fed-1", "--settings", str(settings)]


def runner(argv, **kwargs):
    out = json.dumps({"fingerprint": FINGERPRINT, "environment": IDENTITY, "platform": PLATFORM})
    return subprocess.CompletedProcess(argv, 0, out, "")


def printed_line(deploy, words, capsys):
    assert deploy.fleetidentity.main(words, runner) == 0
    (line,) = capsys.readouterr().out.splitlines()
    return line


def test_o98_a_root_with_no_record_prints_the_line_it_always_did(deploy, tmp_path, capsys):
    """O98: no layout declared an owner outage, so there is no record and the line is byte for byte what it was."""
    _, words = member_root(deploy, tmp_path)
    expected = json.dumps({"fingerprint": FINGERPRINT, "environment": IDENTITY, "platform": PLATFORM}, sort_keys=True)
    assert printed_line(deploy, words, capsys) == expected


def test_o98_a_root_with_the_record_adds_the_admission_and_the_label_order_to_the_line_only(deploy, tmp_path, capsys):
    """O98: the line carries `failover`: the admission and each space's labels and order. The three values the join
    compares are the ones they were, and a controller that reads the line by name takes the same triple back."""
    root, words = member_root(deploy, tmp_path)
    (root / FILE).write_text(json.dumps(PASSED), encoding="utf-8")
    line = printed_line(deploy, words, capsys)
    said = json.loads(line)
    assert said["failover"] == {"admission": "passed", "spaces": PASSED["spaces"]}
    assert (said["fingerprint"], said["environment"], said["platform"]) == (FINGERPRINT, IDENTITY, PLATFORM)
    assert set(said) == {"fingerprint", "environment", "platform", "failover"}
    assert deploy.fleetidentity.said_identity(said) == (FINGERPRINT, IDENTITY, PLATFORM)


def test_o98_the_record_the_core_writes_is_the_one_the_identity_read_takes(deploy, tmp_path):
    """O98: `failover.record_text` is read back as `passed`, with the version and the file's name the two modules share
    (this module runs where the core may not import, so it keeps its own copy of both)."""
    assert deploy.fleetidentity.FAILOVER_FILE == failover.FILE
    assert deploy.fleetidentity.FAILOVER_VERSION == failover.VERSION
    root, _ = member_root(deploy, tmp_path)
    found = failover.SpaceOrder(
        "spaces[3]", "providers[2]", ("providers[0]", "providers[4]"), ("providers[4]", "providers[2]", "providers[0]")
    )
    (root / FILE).write_text(failover.record_text([found]), encoding="utf-8")
    reading = deploy.fleetidentity.failover_reading(root)
    assert reading == {"admission": "passed", "spaces": [found.record()]}


@pytest.mark.parametrize(
    "changed",
    [
        {"owner": "larch"},  # a name where a label is
        {"order": ["id-0001", "providers[0]", "providers[1]"]},  # an id
        {"mirror_first": False},
        {"order": ["providers[1]", "providers[0]", "providers[2]"]},  # the owner first
        {"order": ["providers[2]", "providers[0]"]},  # not every supporter
        {"extra": "invented-0-space"},
    ],
)
def test_o98_a_record_that_holds_more_than_labels_or_a_failed_order_is_reported_unreadable_and_never_echoed(
    deploy, tmp_path, capsys, changed
):
    """O98: an edited or damaged record is never printed: the read says `unreadable`, and no name, id or other value of
    the file reaches the line."""
    root, words = member_root(deploy, tmp_path)
    document = json.loads(json.dumps(PASSED))
    document["spaces"][0].update(changed)
    (root / FILE).write_text(json.dumps(document), encoding="utf-8")
    line = printed_line(deploy, words, capsys)
    assert json.loads(line)["failover"] == {"admission": "unreadable"}
    for value in ("larch", "id-0001", "invented-0-space"):
        assert value not in line


@pytest.mark.parametrize(
    "text", ["not json", "[]", '{"version": "another/9", "spaces": []}', '{"version": "expcore-failover-order/1"}']
)
def test_o98_a_record_that_is_no_record_of_this_version_is_reported_unreadable(deploy, tmp_path, text):
    """O98: no JSON, no object, another version and no spaces are unreadable, never passed and never an error."""
    root, _ = member_root(deploy, tmp_path)
    (root / FILE).write_text(text, encoding="utf-8")
    assert deploy.fleetidentity.failover_reading(root) == {"admission": "unreadable"}


def test_o98_the_order_is_in_no_value_the_join_compares_and_not_in_the_render_identity(deploy):
    """O98: the order differs from one member to the next by chance. The render identity (`hpcidentity.KEYS`, which a
    shard's step compares key by key) holds none of it, and the identity tuple the join compares is three values."""
    import hpcidentity

    assert not any("failover" in key for key in hpcidentity.KEYS)
    assert "failover" not in deploy.fleetidentity.PROGRAM  # what the manifest's Python prints is the three values

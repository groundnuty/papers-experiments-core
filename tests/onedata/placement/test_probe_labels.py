"""The platform probes' snapshot names every provider by its position, never by its configured name (O12)."""

import json

import pytest
from placement_support import DATA, with_placement
from platform_support import write_platform

from expcore_onedata.probes import VERSION, PlatformProbes
from expcore_onedata.rangemap import RangeRow
from expcore_onedata.ranges import place_ranges

PATH = "study/data.bin"
NAMES = ("quokkasite", "wombatsite")  # distinctive configured names, so a leak is found wherever it lands


def renamed(data):
    """The test platform with its two providers under distinctive names, the first holding the data."""
    with_placement(data, holder=NAMES[0])
    providers = data["providers"]
    data["providers"] = {NAMES[0]: providers["alpha"], NAMES[1]: providers["beta"]}
    data["support"] = {"providers": [NAMES[0]], "local": NAMES[0]}


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, renamed)


def test_o12_no_configured_provider_name_reaches_the_snapshot_a_record_publishes(client, provisioner, seeded, ticker):
    """O12: with a replication the harness scheduled, a range read and the space's transfer listing behind them, the
    snapshot names its providers, each file's readings, the pairs and their sources, the on-demand split and every
    transfer record by position (`providers[i]`), and its JSON holds neither configured name anywhere."""
    probes = PlatformProbes(client, provisioner, {"data": PATH}, list(NAMES), sleep=ticker.sleep, now=ticker.clock)
    probes.start({})
    provisioner.replicate(PATH, NAMES[1])
    place_ranges(provisioner, [RangeRow(PATH, NAMES[1], ((0, len(DATA)),))])
    ticker.sleep(10)
    snapshot = probes.finish()
    text = json.dumps(snapshot)
    assert [name for name in NAMES if name in text] == []
    labels = ["providers[0]", "providers[1]"]
    assert snapshot["version"] == VERSION == 3
    assert snapshot["providers"] == labels
    assert sorted(snapshot["files"]["data"]["providers"]) == labels
    assert sorted(snapshot["pairs"]) == labels
    assert sorted(snapshot["on_demand"]) == labels
    assert [record["provider"] for record in snapshot["transfers"]] == ["providers[1]"]

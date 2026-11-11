"""The one-space day-0 report is kept byte for byte: the golden file was written by the day-0 code before it learned
several spaces (8be2cdd), and every check of a one-space platform must still report exactly what it reported then."""

import json
from pathlib import Path

from placement_support import BETA_ID, with_placement
from platform_support import write_platform

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.day0.runner import run

GOLDEN = Path(__file__).resolve().parents[2] / "data" / "day0-one-space-report.json"
MACHINE_BOUND = ("platform_file_sha256",)  # the platform file names this interpreter's path, so its hash is the host's


def one_space(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


def test_the_one_space_report_is_byte_identical_to_the_one_written_before_several_spaces(tmp_path, fake, ticker):
    fake.supports[BETA_ID] = {"readonly": False}
    fake.add_file("item.bin", b"x")  # a space that holds something, so the listing measures and passes
    target = tmp_path / "target"
    target.mkdir()
    platform = write_platform(target, one_space)
    client = OnezoneClient(load_platform(platform), http=fake.client_http())
    _, json_path, _ = run(
        platform,
        tmp_path / "reports",
        client=client,
        tcp=lambda *a: 2.0,
        samples=3,
        now=lambda: 1_800_000_000.0,
        sleep=ticker.sleep,
        clock=ticker.clock,
        allow_mutation=True,
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    for key in MACHINE_BOUND:
        report.pop(key)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    assert rendered == GOLDEN.read_text(encoding="utf-8")

"""C50: a campaign whose members run on several platforms declares them in campaign.toml (`platform_fingerprints`).
Its fingerprint then holds the sorted set in the platform's place, so a member on any declared platform has one
campaign fingerprint, and a start on another is refused before any run. A campaign that declares none is as it was.
Each copy of the tiny study here is one site: the same study, a platform of its own (a file its plugin reads)."""

import json
import shutil
from pathlib import Path

import pytest
from study_support import HEAD

from expcore.campaign.manifest import fingerprint_sha256
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.declared_platforms import DECLARED
from expcore.study.pipeline import campaign_member_identity, fingerprint_of, run_study
from expcore.study.plugin import StudyContext, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
APPTAINER = {"backend": "apptainer", "images": "a SIF"}
DOCKER = {"backend": "docker", "images": {"agent": "an image id"}}
BUILT = "        lease_keys=lease_keys,\n    )"


def site(tmp_path: Path, name: str, platform: dict, declared: list[str] | None) -> Path:
    """A copy of the study whose plugin's platform is `platform` (read from its own file, as the Onedata layer reads
    platform.toml) and whose campaign.toml declares `declared` (None: no key), stamped."""
    copy = tmp_path / name
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    plugin = copy / "plugin.py"
    text = plugin.read_text(encoding="utf-8")
    assert text.count(BUILT) == 1
    platform_line = '        platform=__import__("json").loads((HERE / "platform.json").read_text()),\n'
    reads = "        lease_keys=lease_keys,\n" + platform_line
    plugin.write_text(text.replace(BUILT, reads + "    )"), encoding="utf-8")
    (copy / "platform.json").write_text(json.dumps(platform), encoding="utf-8")
    if declared is not None:
        campaign = copy / "campaign.toml"
        text = campaign.read_text(encoding="utf-8")
        line = f"platform_fingerprints = {json.dumps(declared)}\n"
        campaign.write_text(text.replace("seeds = [1]\n", f"seeds = [1]\n{line}", 1), encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


def fingerprint(study_dir: Path) -> dict:
    study = load_study(study_dir)
    return fingerprint_of(study, load_plugin(StudyContext(study, {"commit": HEAD})))


BOTH = [fingerprint_sha256(APPTAINER), fingerprint_sha256(DOCKER)]


def test_c50_members_on_two_declared_platforms_have_one_campaign_fingerprint_holding_the_set(tmp_path):
    """C50: the HPC site's member and the VM site's differ in their platform's fingerprint; with both declared, the
    campaign fingerprint is one (one campaign.toml), its platform section the sorted set, and each member's own
    platform is its own."""
    hpc, vm = site(tmp_path, "hpc", APPTAINER, BOTH[::-1]), site(tmp_path, "vm", DOCKER, BOTH[::-1])
    assert fingerprint(hpc) == fingerprint(vm)
    assert fingerprint(hpc)["platform"] == {DECLARED: sorted(BOTH)}
    assert [campaign_member_identity(path)[2] for path in (hpc, vm)] == BOTH
    assert campaign_member_identity(hpc)[0] == fingerprint(hpc)


def test_c50_a_campaign_that_declares_none_holds_its_platforms_own_fingerprint_as_before(tmp_path):
    """C50: no key, no change: the platform section is the plugin's own, so the two sites are two campaigns."""
    hpc, vm = site(tmp_path, "hpc", APPTAINER, None), site(tmp_path, "vm", DOCKER, None)
    assert fingerprint(hpc)["platform"] == APPTAINER
    assert fingerprint(hpc) != fingerprint(vm)


def test_c50_a_start_on_a_platform_the_campaign_does_not_declare_is_refused_before_any_run(tmp_path):
    """C50: the start holds the member to the set, naming campaign.toml's key; nothing is run."""
    other = site(tmp_path, "other", {"backend": "something else"}, BOTH)
    with pytest.raises(ConfigError, match="platform_fingerprints") as raised:
        run_study(other)
    assert "campaign.toml" in str(raised.value)
    runs = other / "out" / "runs"
    assert not runs.exists() or not any(runs.iterdir())


@pytest.mark.parametrize(
    ("declared", "says"),
    [([], "at least one"), (["not a digest"], "64 lower-case hex"), ([BOTH[0], BOTH[0]], "more than once")],
)
def test_c50_a_set_that_is_empty_malformed_or_repeats_a_platform_is_refused_at_load(tmp_path, declared, says):
    with pytest.raises(ConfigError, match=says):
        site(tmp_path, "bad", APPTAINER, declared)

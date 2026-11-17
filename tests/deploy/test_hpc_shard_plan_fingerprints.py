"""O63: `shards.py plan` and `lines` take each shard's campaign fingerprint beside its own federation's `platform.toml`
and refuse before any line is printed when they are not all one (a shard on a federation that renders another setting
that shapes a run would never merge, and `merge` would say so only after the money was spent). The fingerprint is taken
as `resume-line` takes it (`shardresume.study_fingerprint`), by the manifest's Python on a copy of the study.

The example study's plugin has no platform half, so most tests name a stand-in interpreter (`fingerprint_support`)
that fingerprints the copy's `platform.toml` tables; one test runs the real interpreter on the real study. Every
path, name and job here is invented, and nothing runs srun."""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from fingerprint_support import seen_platforms, standin_python
from shard_support import make_study

import expcore

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
LABELS = ("fed-a", "fed-b", "fed-c")
FAULTS = '[faults]\nsanctioned = true\ntargets = [{ label = "alpha" }]\nbounds = { heal_s = 60 }\n'
PLACEMENT = "[placement]\nbound_s = 30\n"
SECRETS = ("sanctioned", "alpha", "heal_s", "bound_s")  # values of the settings: never in a message


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_plan_fingerprints", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def platform_text(n: int, body: str = FAULTS + PLACEMENT) -> str:
    """One federation's platform file: its own space (never fingerprinted here) and the settings that shape a run."""
    return f'[space]\nname = "space-{n}"\ndata_manifest = "m{n}.json"\n{body}'


def manifest(tmp_path: Path, platforms: dict | None = None, *, python: Path | None = None, same_root: bool = False,
             study: Path | None = None, core_src: Path | None = None) -> Path:  # fmt: skip
    """A shard plan of three federations, each linked, with its platform file (`platforms` by label, else the same
    settings); `same_root` puts every shard on the first federation."""
    study = study or tmp_path / "study"
    study.mkdir(exist_ok=True)
    python = python or standin_python(tmp_path / "venv")
    lines = [f'study = "{study}"', f'python = "{python}"', f'core_src = "{core_src or tmp_path / "core" / "src"}"',
             f'results = "{tmp_path / "results"}"', f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin:/bin"',
             "cpus = 6", 'memory = "18g"']  # fmt: skip
    for n, label in enumerate(LABELS, 1):
        root = tmp_path / "fed" / ("a" if same_root else label)
        root.mkdir(parents=True, exist_ok=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked",
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        if not same_root or n == 1:
            (root / "platform.toml").write_text((platforms or {}).get(label) or platform_text(n))
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{root}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def run(shards, path: Path, capsys, command: str = "lines") -> tuple[int, str, str]:
    code = shards.main([command, "--manifest", str(path)])
    seen = capsys.readouterr()
    return code, seen.out, seen.err


@pytest.mark.parametrize("command", ["plan", "lines"])
def test_o63_three_federations_rendering_one_campaign_print_every_line(shards, tmp_path, capsys, command):
    """O63: each federation renders its own space and the same settings that shape a run: all three lines are printed,
    and each fingerprint was taken beside its own federation's platform file."""
    code, out, err = run(shards, manifest(tmp_path), capsys, command)
    assert (code, err) == (0, "")
    assert len(out.splitlines()) == 3
    files = [tmp_path / "fed" / label / "platform.toml" for label in LABELS]
    assert seen_platforms(tmp_path / "venv") == [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]


CHANGES = {
    "faults": (2, platform_text(3, FAULTS.replace("60", "61") + PLACEMENT), ("fed-c",), ("fed-a", "fed-b")),
    "placement": (2, platform_text(3, FAULTS + PLACEMENT.replace("30", "31")), ("fed-c",), ("fed-a", "fed-b")),
    "no faults": (1, platform_text(2, PLACEMENT), ("fed-b",), ("fed-a", "fed-c")),
    "the first": (0, platform_text(1, FAULTS.replace("60", "61") + PLACEMENT), ("fed-a",), ("fed-b", "fed-c")),
}


@pytest.mark.parametrize("command", ["plan", "lines"])
@pytest.mark.parametrize("change", sorted(CHANGES))
def test_o63_a_federation_rendering_another_setting_refuses_before_any_line(shards, tmp_path, capsys, command, change):
    """O63: one federation renders another [faults] or [placement] (or none): `plan` and `lines` print no line and end
    with status 2, naming the shard that differs from the others by label and the first differing top-level key of
    the fingerprint, and nothing of a host, a path or a value."""
    n, text, odd, others = CHANGES[change]
    path = manifest(tmp_path, {LABELS[n]: text})
    code, out, err = run(shards, path, capsys, command)
    assert (code, out) == (2, "")
    assert err.startswith("shards: ERROR: ")
    assert f"{', '.join(odd)} differs from {', '.join(others)}" in err
    assert "(first differing key: platform)" in err
    assert str(tmp_path) not in err
    assert not [word for word in SECRETS if word in err]


def test_o63_a_key_only_one_federation_has_is_named_by_its_top_level_key(shards, tmp_path, capsys):
    """O63: a key of the fingerprint one federation has and the others lack is the first differing one; the first in
    the key order, when more than one differs."""
    marked = platform_text(3) + "# study-setting: another\n"
    code, out, err = run(shards, manifest(tmp_path, {"fed-c": marked}), capsys)
    assert (code, out) == (2, "")
    assert "fed-c differs from fed-a, fed-b (first differing key: study_setting)" in err
    both = platform_text(3, FAULTS.replace("60", "61") + PLACEMENT) + "# study-setting: another\n"
    (tmp_path / "both").mkdir()
    code, _, err = run(shards, manifest(tmp_path / "both", {"fed-c": both}), capsys)
    assert code == 2
    assert "(first differing key: platform)" in err


@pytest.mark.parametrize("remove", ["gone", "directory"])
def test_o63_a_platform_file_that_cannot_be_read_refuses_naming_the_shard(shards, tmp_path, capsys, remove):
    """O63: a federation whose platform.toml is missing, or not a file, is refused by label before any line is printed
    (none of the others' fingerprints is taken for a campaign that cannot be one)."""
    path = manifest(tmp_path)
    platform = tmp_path / "fed" / "fed-b" / "platform.toml"
    platform.unlink()
    if remove == "directory":
        platform.mkdir()
    with pytest.raises(shards.ShardsError, match=r"shard fed-b: its federation's platform.toml could not be read") as e:
        shards.check_one_campaign(shards.load_plan(path))
    assert str(tmp_path) not in str(e.value)
    assert seen_platforms(tmp_path / "venv") == []


def test_o63_lines_refuse_a_federation_whose_platform_file_is_gone_on_a_manifest_of_one_federation(
    shards, tmp_path, capsys
):
    """O63: every shard on one federation, whose platform.toml is gone: the data check (which needs two federations to
    compare) asks nothing of it, so the fingerprint check is the one that refuses, and prints nothing."""
    path = manifest(tmp_path, same_root=True)
    (tmp_path / "fed" / "a" / "platform.toml").unlink()
    code, out, err = run(shards, path, capsys)
    assert (code, out) == (2, "")
    assert "shard fed-a: its federation's platform.toml could not be read" in err


@pytest.mark.parametrize("failure", ["exits", "is missing"])
def test_o63_a_fingerprint_that_cannot_be_taken_refuses_naming_the_shard_and_never_the_programs_words(
    shards, tmp_path, capsys, failure
):
    """O63: the manifest's Python that ends non-zero, or is not there, refuses the plan naming the first shard it
    failed for; what it wrote to stderr (a path, here) is never repeated."""
    python = tmp_path / "venv" / "absent" / "python"
    if failure == "exits":
        python = standin_python(tmp_path / "venv", fail=3)
    code, out, err = run(shards, manifest(tmp_path, python=python), capsys)
    assert (code, out) == (2, "")
    assert "shard fed-a: the study's fingerprint could not be computed" in err
    assert ("(status 3)" if failure == "exits" else "(FileNotFoundError)") in err
    assert "private" not in err
    assert str(tmp_path) not in err


def test_o63_a_study_that_cannot_be_copied_refuses_naming_the_shard(shards, tmp_path, capsys):
    """O63: the study's directory is gone: refused by label, by the kind of the error alone, never a traceback."""
    path = manifest(tmp_path)
    shutil.rmtree(tmp_path / "study")
    code, out, err = run(shards, path, capsys)
    assert (code, out) == (2, "")
    assert "shard fed-a: the study could not be copied beside its federation's platform.toml (FileNotFoundError)" in err
    assert str(tmp_path) not in err


def test_o63_the_real_interpreter_takes_the_same_fingerprint_of_a_real_study_beside_each_platform_file(
    shards, tmp_path, capsys
):
    """O63: `campaign_fingerprint`, by the interpreter that runs this test, on a stamped study, beside three
    federations' platform files that differ (each its own space): one campaign, all three lines printed."""
    study = make_study(tmp_path, "real-study")
    path = manifest(tmp_path, python=Path(sys.executable), study=study, core_src=Path(expcore.__file__).parents[1])
    code, out, err = run(shards, path, capsys)
    assert (code, err) == (0, "")
    assert len(out.splitlines()) == 3
    assert not (study / "platform.toml").exists()  # a copy of the study was given each platform file, never the study

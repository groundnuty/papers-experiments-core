"""The rehearsal script of the live slot (deploy/compose-federation/rehearse_shards.py), checked without a federation:
that it imports, what its help says, the parts that need no Docker (the dataset, the identity, the study copy, the
refusal of a bad out directory), and its steps in order against stand-ins for the federation and the core."""

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from study_support import clean_environment

from expcore.provenance.codestamp import STAMP
from expcore.study.config import load_study
from expcore.study.example import ExampleError

__all__ = ["clean_environment"]  # a fixture the real-core test uses, imported from the support module


@pytest.fixture(scope="module")
def script(fed):
    import rehearse_shards

    return rehearse_shards


@pytest.fixture
def layout(fed, tmp_path):
    return fed.layout.Layout(tmp_path / "root", tmp_path, fed.spec.load_spec())


# --- what it says and what it makes ----------------------------------------------------------------------------------


def test_help_describes_every_step_and_what_the_script_never_does(script, capsys):
    with pytest.raises(SystemExit) as stop:
        script.main(["--help"])
    assert stop.value.code == 0
    shown = " ".join(capsys.readouterr().out.split())
    for words in (
        "never runs `up` or `down`",
        "Run shard 1/2",
        "reset --clean",
        "Ingest the same data again",
        "The merge must accept",
        "one file changed by one byte",
        "The merge must refuse",
        "ends at its baseline",
    ):
        assert words in shown, words


def test_the_dataset_is_the_same_every_time_and_a_planted_file_differs_in_one_byte(script):
    first = script.dataset()
    assert first == script.dataset()
    assert set(first) == set(script.DATASET_NAMES)
    planted = script.dataset(planted=script.PLANTED)
    assert [name for name in first if first[name] != planted[name]] == [script.PLANTED]
    changed = [i for i, (a, b) in enumerate(zip(first[script.PLANTED], planted[script.PLANTED], strict=True)) if a != b]
    assert changed == [0]
    assert len(first[script.PLANTED]) == len(planted[script.PLANTED])
    with pytest.raises(ValueError, match="not a file of the dataset"):
        script.dataset(planted="no-such-file")


def test_the_identity_is_names_and_digests_and_a_changed_file_changes_the_ingest_digest_alone(script, fed):
    spec = fed.spec.load_spec()
    fingerprint = {"tls": {"mode": "ca_file", "ca_file_sha256": "0" * 64}}
    first = script.environment_identity(fingerprint, spec, script.manifest_of(script.dataset()))
    again = script.environment_identity(fingerprint, spec, script.manifest_of(script.dataset()))
    planted = script.environment_identity(fingerprint, spec, script.manifest_of(script.dataset(planted=script.PLANTED)))
    assert first == again
    assert set(first) == {"platform", "layout", f"ingest-{spec.study_space}"}
    assert [name for name in first if first[name] != planted[name]] == [script.ingest_name(spec)]
    assert first["layout"] == spec.digest


def test_a_study_copy_is_stamped_loadable_and_has_caps_two_shards_can_share(script, tmp_path):
    study = script.make_study(tmp_path / "study")
    assert (study / STAMP).is_file()
    assert script.PLUGIN in tomllib.loads((study / "study.toml").read_text(encoding="utf-8"))["study"]["code"]
    caps = tomllib.loads((study / "campaign.toml").read_text(encoding="utf-8"))["scheduler"]["unit_caps"]
    assert set(caps.values()) == {script.UNIT_CAP}
    assert load_study(study).name == "tiny-study"
    with pytest.raises(ExampleError, match="not an empty directory"):
        script.make_study(study)


def test_an_out_directory_inside_the_root_or_not_empty_is_refused_before_the_federation_is_touched(
    script, fed, tmp_path, monkeypatch, capsys
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EXPCORE_FED_ROOT_PARENT", str(allowed))
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(allowed / "pilot"))
    touched = []
    monkeypatch.setattr(script.federation, "require_up", lambda layout: touched.append("require_up"))
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "file").write_text("x", encoding="utf-8")
    for out, said in (
        (allowed / "pilot" / "out", "inside the federation's root"),
        (allowed / "pilot", "inside the federation's root"),
        (busy, "new directory or an empty one"),
    ):
        assert script.main(["--out", str(out)]) == 2
        assert said in capsys.readouterr().err
    assert touched == []
    assert not (allowed / "pilot").exists()


# --- the steps, against stand-ins ----------------------------------------------------------------------------------


class Space:
    """What the federation's study space holds, as the stand-in provisioner sees it."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.resets = 0


class Provisioner:
    settings = SimpleNamespace(holder="holder")

    def __init__(self, space):
        self.space = space

    def root(self):
        return "root"

    def children(self, directory):
        return dict.fromkeys(self.space.files, object())

    def put_file(self, path, data):
        assert path not in self.space.files, "the provisioner refuses a file that exists"
        self.space.files[path] = data
        return path

    def fetch(self, provider, file_id, offset, length):
        assert provider == "holder"
        return self.space.files[file_id][offset : offset + length]


class Config:
    def fingerprint(self):
        return {"tls": {"mode": "ca_file", "ca_file_sha256": "0" * 64}}


class Core:
    """A stand-in for `python -m expcore`: a run keeps the identity it was given, a merge accepts shards that kept one
    identity and refuses the others, naming the environment."""

    def __init__(self, ingest):
        self.ingest = ingest  # the name of the study space's ingest manifest in an identity
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv, identity_file=None):
        self.calls.append(argv)
        command = argv[0]
        if command == "run":
            out = Path(argv[1]) / "out"
            (out / "runs").mkdir(parents=True)
            (out / "identity.json").write_text(identity_file.read_text(encoding="utf-8"), encoding="utf-8")
        elif command == "check":
            assert Path(argv[1]).is_dir(), "check runs on a batch that exists"
        elif command == "merge":
            kept = {Path(shard) / "identity.json": 1 for shard in argv[2:]}
            if len({path.read_text(encoding="utf-8") for path in kept}) > 1:
                said = "ran against another environment than shard 1 (shard-1); its environment identity differs in"
                return 1, "", f"merge refused: shard 2 (shard-2): {said} {self.ingest}\n"
            (Path(argv[1]) / "runs").mkdir(parents=True)
        return 0, "", ""


@pytest.fixture
def stand_in_federation(script, layout, tmp_path, monkeypatch):
    """The rehearsal with a stand-in for the federation and the platform layer, and the real `python -m expcore`."""
    space, found = Space(), SimpleNamespace(resets=0)

    def reset(argv):
        assert argv[-2:] == ["reset", "--clean"]
        found.resets += 1
        space.files.clear()
        return 0

    monkeypatch.setattr(script.federation, "require_up", lambda layout: None)
    monkeypatch.setattr(script.federation, "main", reset)
    monkeypatch.setattr(script, "CLEAN_WAIT_S", 0.0)
    monkeypatch.setattr(script, "CLEAN_POLL_S", 0.0)
    out = tmp_path / "out"
    out.mkdir()
    rehearsal = script.Rehearsal(layout, [], out)
    monkeypatch.setattr(
        rehearsal, "connect", lambda: (Config(), SimpleNamespace(close=lambda: None), Provisioner(space))
    )
    return SimpleNamespace(rehearsal=rehearsal, space=space, found=found, out=out)


@pytest.fixture
def stand_ins(stand_in_federation, monkeypatch):
    """The same, with a stand-in for the core as well."""
    core = Core(f"ingest-{stand_in_federation.rehearsal.spec.study_space}")
    monkeypatch.setattr(stand_in_federation.rehearsal, "expcore", core)
    return SimpleNamespace(**vars(stand_in_federation), core=core)


def test_the_steps_run_in_order_the_first_pair_merges_and_the_changed_one_is_refused(stand_ins, script):
    stand_ins.rehearsal.run()
    stand_ins.rehearsal.finish()
    shards = [call for call in stand_ins.core.calls if call[0] == "run"]
    assert [call[2:] for call in shards] == [
        ("--shard", "1/2", "--shard-label", "shard-1"),
        ("--shard", "2/2", "--shard-label", "shard-2"),
        ("--shard", "2/2", "--shard-label", "shard-2"),
    ]
    assert [call[0] for call in stand_ins.core.calls] == [
        *("run", "check", "run", "check", "merge", "check", "run", "check", "merge")
    ]
    assert stand_ins.found.resets == 3  # between the shards, before the planted ingest, and at the end
    assert stand_ins.space.files == {}
    first, again, changed = (
        json.loads((stand_ins.out / name / "environment-identity.json").read_text(encoding="utf-8"))
        for name in ("shard-1", "shard-2", "shard-2-changed")
    )
    assert first == again
    assert [key for key in first if first[key] != changed[key]] == [script.ingest_name(stand_ins.rehearsal.spec)]
    assert (stand_ins.out / "merged" / "study" / "out" / "runs").is_dir()
    assert not (stand_ins.out / "refused" / "study" / "out" / "runs").exists()


def test_a_merge_that_accepts_the_changed_environment_fails_the_rehearsal(stand_ins, monkeypatch):
    accepting = stand_ins.core

    def merge_accepts(*argv, identity_file=None):
        if argv[0] == "merge":
            (Path(argv[1]) / "runs").mkdir(parents=True)
            return 0, "", ""
        return accepting(*argv, identity_file=identity_file)

    monkeypatch.setattr(stand_ins.rehearsal, "expcore", merge_accepts)
    with pytest.raises(RuntimeError, match="had to refuse for the environment identity"):
        stand_ins.rehearsal.run()


def test_a_reset_that_leaves_the_dataset_fails_the_rehearsal_naming_the_files(stand_ins, script, monkeypatch):
    monkeypatch.setattr(script.federation, "main", lambda argv: 0)  # a reset that removes nothing
    with pytest.raises(RuntimeError, match="still holds 4 file") as failed:
        stand_ins.rehearsal.run()
    assert script.DATASET_NAMES[0] in str(failed.value)
    assert stand_ins.rehearsal.dirty


def test_a_merge_that_refuses_the_first_pair_fails_the_rehearsal(stand_ins, monkeypatch):
    monkeypatch.setattr(
        stand_ins.rehearsal, "expcore", lambda *argv, identity_file=None: (1, "", "merge refused: nothing\n")
    )
    with pytest.raises(RuntimeError, match="ended with status 1"):
        stand_ins.rehearsal.run()


# --- the steps, against the real core ---------------------------------------------------------------------------------


def test_the_steps_pass_against_the_real_core_with_its_flags_and_the_layout_it_writes(
    stand_in_federation, clean_environment, capsys
):
    """Only the federation is a stand-in: `run --shard i/2 --shard-label`, `check`, `merge OUT SHARD_OUT...` and the
    plugin that hands the core the identity are as this tree has them, so a flag or a file the core renamed fails here
    and not in the live slot."""
    rehearsal, out = stand_in_federation.rehearsal, stand_in_federation.out
    rehearsal.run()
    rehearsal.finish()
    merged = json.loads((out / "merged" / "study" / "out" / "merge.json").read_text(encoding="utf-8"))
    assert [(s["index"], s["label"]) for s in merged["shards"]] == [(1, "shard-1"), (2, "shard-2")]
    identity = json.loads((out / "shard-1" / "environment-identity.json").read_text(encoding="utf-8"))
    assert merged["environment_identity"] == dict(sorted(identity.items()))
    assert not (out / "refused" / "study" / "out").exists()  # the merge that must refuse wrote nothing
    assert stand_in_federation.space.files == {}
    printed = " ".join(capsys.readouterr().out.split())
    assert (
        "refused: merge refused: shard 2 (shard-2): ran against another environment than shard 1 (shard-1)" in printed
    )
    assert f"its environment identity differs in ingest-{rehearsal.spec.study_space}" in printed

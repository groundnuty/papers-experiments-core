"""O63: a shard step's declared operator variables (deploy/hpc-federation/shards.py): the manifest's `pass_env` names a
few variables the study needs in its dispatcher's environment (the operator's half of a fault sanction), and `lines`
pins each one's value from the operator's environment on the line, so the step, whose own environment is an
allowlist, passes exactly those to the dispatcher and records them. The federation, the variable and its value are
invented, and nothing runs srun; the dispatcher is a stand-in, in one test a real child process."""

import argparse
import hashlib
import importlib.util
import json
import shlex
import signal
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
NAME, VALUE = "FAULT_ACK_MODE", "go-ahead"
OTHER = "FAULT_REPORT_MODE"
HOME_NAMES = {"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPTAINER_CONFIGDIR",
              "APPTAINER_CACHEDIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "PYTHONNOUSERSITE", "GIT_CONFIG_GLOBAL",
              "GIT_CONFIG_NOSYSTEM", "TMPDIR"}  # fmt: skip
STEP_OWN = {"PYTHONDONTWRITEBYTECODE"}  # the step sets it for every child of its dispatcher (B1)
DECLARED = f'pass_env = ["{NAME}"]'


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_pass_env", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_pass_env"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_pass_env", None)


def make_study(tmp_path: Path) -> Path:
    study = tmp_path / "study"
    study.mkdir(parents=True, exist_ok=True)
    study.chmod(0o755)
    (study / "study.toml").write_text("[study]\n")
    (study / "campaign.toml").write_text("[scheduler]\nworkers = 1\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return study


def manifest_file(tmp_path: Path, extra: str = "") -> Path:
    """A shard plan of two federations; `extra` (top-level keys) goes before the `[[shards]]`."""
    make_study(tmp_path)
    lines = [f'study = "{tmp_path / "study"}"', f'python = "{tmp_path / "venv" / "python"}"',
             f'core_src = "{tmp_path / "core" / "src"}"', f'results = "{tmp_path / "results"}"',
             f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin:/bin"', "cpus = 6",
             'memory = "18g"']  # fmt: skip
    for n, name in enumerate(("a", "b"), 1):
        root = tmp_path / "fed" / name
        root.mkdir(parents=True, exist_ok=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked",
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')
    if extra:
        lines.append(extra)
    for label, name in (("fed-a", "a"), ("fed-b", "b")):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{tmp_path / "fed" / name}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def printed(shards, tmp_path, capsys, command, extra="") -> list[str]:
    assert shards.main([command, "--manifest", str(manifest_file(tmp_path, extra))]) == 0
    return capsys.readouterr().out.splitlines()


def refused_plan(shards, tmp_path, capsys, extra) -> str:
    """`plan` and `lines` of a manifest with `extra` refuse (status 2) and print nothing; the message."""
    path = manifest_file(tmp_path, extra)
    err = ""
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        err += captured.err
    return err


def node_words(line: str) -> tuple[dict[str, str], list[str]]:
    """What the node makes of a printed line: the `env -i` words (the step's whole environment) and the step's
    own arguments, as the shell would split them."""
    words = shlex.split(line)
    at = words.index("-i") + 1
    environ = {}
    while "=" in words[at] and not words[at].startswith("-"):
        key, _, value = words[at].partition("=")
        environ[key] = value
        at += 1
    return environ, words[words.index("step") :]


STUB = (
    "import os, sys\n"
    "from pathlib import Path\n"
    f"if os.environ.get({NAME!r}) != {VALUE!r}:\n"
    "    print('unsanctioned: the operator half of the sanction is not in the environment', file=sys.stderr)\n"
    "    raise SystemExit(3)\n"
    "out = Path(sys.argv[1]) / 'out'\n"
    "(out / 'runs').mkdir(parents=True, exist_ok=True)\n"
    "(out / 'runs' / 'r1.json').write_text('{}')\n"
    "(out / 'campaign.lock').write_text('')\n"
)


def dispatcher(shards=None, code=None, status=0):
    """The stand-in dispatcher: what it was given, and the step's record as it saw it. With `code`, a real child
    process of `shards.run_dispatcher` runs that source with the environment the step gave it."""
    seen = {}

    def run(argv, env, cwd):
        seen.update(argv=list(argv), env=dict(env), owner=json.loads((Path(cwd) / "step.json").read_text()))
        copy = Path(argv[argv.index("run") + 1])
        if code is not None:
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:  # `run_dispatcher` relays these two to its child and leaves its handlers set
                return shards.run_dispatcher([sys.executable, "-I", "-B", "-c", code, str(copy)], env, cwd)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
        (copy / "out" / "runs").mkdir(parents=True, exist_ok=True)
        (copy / "out" / "runs" / "r1.json").write_text("{}")
        (copy / "out" / "campaign.lock").write_text("")
        return status

    return run, seen


def run_line(shards, monkeypatch, line, runner, node=None) -> int:
    """Run a printed line's step as the node would: its `env -i` words are the environment the step reads, and its
    arguments go through `main`'s own parser; only the dispatcher and the core check are stand-ins."""
    environ, argv = node_words(line)
    for key in (NAME, OTHER, *(node or {})):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**{k: v for k, v in environ.items() if k not in HOME_NAMES}, **(node or {})}.items():
        monkeypatch.setenv(key, value)
    original = shards.step
    monkeypatch.setattr(shards, "step", lambda args: original(args, runner=runner, core_found=lambda path: True))
    return shards.main(argv)


def manifest_of(tmp_path, label="fed-a") -> dict:
    return json.loads((tmp_path / "results" / f"{label}.out.json").read_text())


def nothing_made(tmp_path: Path) -> None:
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not list((tmp_path / "results").iterdir())


# -- the printed line pins each declared variable -------------------------------------------------------------------


def test_o63_a_declared_name_is_pinned_on_every_line_with_its_value_and_nothing_else_changes(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: `lines` reads the operator's value of each declared name and the line carries it, as an `env -i` word
    after the step's own and as `--pass-env NAME` at the end of the step's arguments; `plan` shows both; the line is
    otherwise byte for byte the one a manifest without the key prints (and `plan` then has no `pass_env`)."""
    monkeypatch.delenv(NAME, raising=False)
    plain = printed(shards, tmp_path / "plain", capsys, "lines")
    plain_plans = [json.loads(line) for line in printed(shards, tmp_path / "plain", capsys, "plan")]
    assert all("pass_env" not in p for p in plain_plans)
    monkeypatch.setenv(NAME, VALUE)
    plans = [json.loads(line) for line in printed(shards, tmp_path / "pinned", capsys, "plan", DECLARED)]
    assert [p["pass_env"] for p in plans] == [{NAME: VALUE}] * 2
    lines = printed(shards, tmp_path / "pinned", capsys, "lines", DECLARED)
    for line, base in zip(lines, plain, strict=True):
        environ, argv = node_words(line)
        assert environ[NAME] == VALUE
        assert line.endswith(f" --pass-env {NAME}")
        assert f"{NAME}={VALUE} " in line
        stripped = line.replace(f"{NAME}={VALUE} ", "").removesuffix(f" --pass-env {NAME}")
        assert stripped.replace(str(tmp_path / "pinned"), "ROOT") == base.replace(str(tmp_path / "plain"), "ROOT")
        assert argv.count("--pass-env") == 1


def test_o63_the_pinned_value_is_the_one_printed_not_the_one_the_operator_has_later(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: a step can neither gain nor change a variable: the value on the node is the line's, whatever the
    operator's environment holds by then (here: another value, and none)."""
    monkeypatch.setenv(NAME, VALUE)
    line = printed(shards, tmp_path, capsys, "lines", DECLARED)[0]
    monkeypatch.setenv(NAME, "another")
    run, seen = dispatcher()
    assert run_line(shards, monkeypatch, line, run) == 0
    assert seen["env"][NAME] == VALUE


@pytest.mark.parametrize("value", ["", VALUE, "a.b_c:d=e,f+g-h", "Qz" * 64])
def test_o63_a_value_of_the_allowed_characters_is_pinned_and_arrives_whole(
    shards, tmp_path, capsys, monkeypatch, value
):
    """O63: letters, digits and `._:=,+-` (up to 128, empty too) are a value; it survives the line's quoting and the
    split at the first `=` and reaches the dispatcher as given."""
    monkeypatch.setenv(NAME, value)
    line = printed(shards, tmp_path, capsys, "lines", DECLARED)[0]
    run, seen = dispatcher()
    assert run_line(shards, monkeypatch, line, run) == 0
    assert seen["env"][NAME] == value


# -- refused before any line is printed -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["FAULT_API_KEY", "RUN_TOKEN", "TOP_SECRET", "DB_PASSWORD", "OLD_PASSWD",
                                  "CLOUD_CREDENTIAL", "AUTH_MODE", "AUTHORIZED_FAULTS"])  # fmt: skip
def test_o63_a_name_that_looks_like_a_secret_refuses_the_plan_naming_it(shards, tmp_path, capsys, monkeypatch, name):
    """O63: a name containing KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL or AUTH never rides on a line, whatever
    the operator's environment holds: `plan` and `lines` print nothing."""
    monkeypatch.setenv(name, VALUE)
    err = refused_plan(shards, tmp_path, capsys, f'pass_env = ["{name}"]')
    assert name in err
    assert "looks like a secret" in err


@pytest.mark.parametrize("name", ["PATH", "LC_ALL", "EXPCORE_FED_ROOT", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_ANYTHING",
                                  "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", *sorted(HOME_NAMES)])  # fmt: skip
def test_o63_a_name_the_step_sets_itself_refuses_the_plan_naming_it(shards, tmp_path, capsys, name):
    """O63: the allowlist's names, anything starting EXPCORE_ or PYTHON, and every name of the step's own home and
    TMPDIR are the step's: a manifest cannot point the dispatcher back at the account's home."""
    err = refused_plan(shards, tmp_path, capsys, f'pass_env = ["{name}"]')
    assert name in err
    assert "sets itself" in err


@pytest.mark.parametrize(
    "name", ["lower_case", "1STARTS_WITH_DIGIT", "HAS-DASH", "WITH SPACE", "A" * 65, "", "NAME\\n"]
)
def test_o63_a_name_that_is_not_a_plain_upper_case_name_refuses_the_plan(shards, tmp_path, capsys, name):
    """O63: a name matches `^[A-Z][A-Z0-9_]{0,63}$` (a trailing newline included)."""
    err = refused_plan(shards, tmp_path, capsys, f'pass_env = ["{name}"]')
    assert "must match" in err


@pytest.mark.parametrize("value", ['"FAULT_ACK_MODE"', "[3]", "true", f'["{NAME}", "{NAME}"]', "{ a = 1 }"])
def test_o63_a_pass_env_that_is_not_a_list_of_distinct_names_refuses_the_plan(shards, tmp_path, capsys, value):
    """O63: `pass_env` is a list of strings, each name once."""
    err = refused_plan(shards, tmp_path, capsys, f"pass_env = {value}")
    assert "pass_env" in err


def test_o63_a_declared_variable_the_operator_has_not_set_refuses_the_plan_naming_it(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: a declared variable must be set in the operator's environment when `plan` and `lines` run (an empty
    value is set): refused, nothing printed, naming it."""
    monkeypatch.delenv(NAME, raising=False)
    err = refused_plan(shards, tmp_path, capsys, DECLARED)
    assert f"{NAME} is not set" in err


def test_o63_a_second_declared_variable_that_is_unset_refuses_both_lines_before_the_first_is_printed(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: the values are read before the first shard's line is printed: one unset name refuses the whole plan."""
    monkeypatch.setenv(NAME, VALUE)
    monkeypatch.delenv(OTHER, raising=False)
    err = refused_plan(shards, tmp_path, capsys, f'pass_env = ["{NAME}", "{OTHER}"]')
    assert f"{OTHER} is not set" in err


@pytest.mark.parametrize("value", ["go ahead", "go;ahead", "go/ahead", 'go"ahead', "$GOAHEAD", "go\nahead",
                                   "go\\ahead", "go'ahead", "goéahead", "g" * 129])  # fmt: skip
def test_o63_a_value_with_a_disallowed_character_refuses_the_plan_never_echoing_it(
    shards, tmp_path, capsys, monkeypatch, value
):
    """O63: a value over 128 characters or with one outside `A-Za-z0-9._:=,+-` refuses `plan` and `lines`, naming the
    variable and never its value."""
    monkeypatch.setenv(NAME, value)
    err = refused_plan(shards, tmp_path, capsys, DECLARED)
    assert f"{NAME}'s value does not match" in err
    assert value[:8] not in err
    assert value not in err


# -- the step: exactly what its line pins ---------------------------------------------------------------------------


def step_args(tmp_path, pass_env=None, *, index=1, label="fed-a"):
    """The step's arguments as its generated line gives them; `pass_env=None` is a line with no such option."""
    study = make_study(tmp_path)
    extra = {} if pass_env is None else {"pass_env": list(pass_env)}
    return argparse.Namespace(study=study, index=index, count=2, label=label, tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False, archive_left=False,
                              snapshot_every=3600.0, **extra)  # fmt: skip


def test_o63_a_declared_name_reaches_the_dispatcher_through_the_real_step_and_is_recorded(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: through `shards.py step` as the printed line runs it: the dispatcher's environment is the allowlist, the
    step's home and TMPDIR, and the declared name with the line's value, and nothing else; step.json holds the name
    and value, so does the archive's manifest, and the step says which name it passed."""
    monkeypatch.setenv(NAME, VALUE)
    line = printed(shards, tmp_path, capsys, "lines", DECLARED)[0]
    run, seen = dispatcher()
    assert run_line(shards, monkeypatch, line, run, {OTHER: "left-over"}) == 0
    extras = set(seen["env"]) - set(shards.ALLOWLIST) - HOME_NAMES - STEP_OWN
    assert extras == {NAME}  # a variable the node holds that the line did not declare is never passed
    assert seen["env"][NAME] == VALUE
    assert seen["owner"]["pass_env"] == {NAME: VALUE}
    assert manifest_of(tmp_path)["pass_env"] == {NAME: VALUE}
    assert f"passed {NAME}" in capsys.readouterr().err
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_a_step_without_the_option_passes_nothing_and_records_nothing(shards, tmp_path, monkeypatch):
    """O63: a line with no `--pass-env` (every line before this key): the dispatcher's environment holds no variable
    of the node's beyond the allowlist, and neither record has a `pass_env`."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    monkeypatch.setenv(NAME, VALUE)
    run, seen = dispatcher()
    assert shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True) == 0
    assert NAME not in seen["env"]
    assert "pass_env" not in seen["owner"]
    assert "pass_env" not in manifest_of(tmp_path)


@pytest.mark.parametrize(
    ("declared", "node", "says"),
    [
        ([NAME], {}, f"{NAME} is not set"),  # the line dropped the word that carries the value
        ([NAME], {NAME: "go ahead"}, f"{NAME}'s value does not match"),
        ([NAME], {NAME: "g" * 129}, f"{NAME}'s value does not match"),
        (["FAULT_API_KEY"], {"FAULT_API_KEY": VALUE}, "looks like a secret"),
        (["HOME"], {}, "sets itself"),
        (["PYTHONPATH"], {}, "sets itself"),
        (["lower_case"], {}, "must match"),
        ([NAME, NAME], {NAME: VALUE}, "twice"),
    ],
)
def test_o63_a_step_refuses_a_line_whose_declaration_is_not_met_with_nothing_made(
    shards, tmp_path, capsys, monkeypatch, declared, node, says
):
    """O63: `step` checks again what `lines` checked (the line may have been edited): a declared name that is
    unset, has a bad value (never echoed), looks like a secret, is the step's own or is repeated ends the step with
    the refusal's status before the dispatcher, the step's directory or an archive."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    monkeypatch.delenv(NAME, raising=False)
    for key, value in node.items():
        monkeypatch.setenv(key, value)
    run, seen = dispatcher()
    assert shards.step(step_args(tmp_path, declared), runner=run, core_found=lambda path: True) == (
        shards.PRE_STEP_REFUSED
    )
    err = capsys.readouterr().err
    assert says in err
    assert "go ahead" not in err
    assert "gggggggg" not in err
    assert seen == {}
    nothing_made(tmp_path)


def test_o63_a_typed_line_that_lost_the_variable_s_word_is_refused_by_the_real_step(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: the printed line, run as typed, with its `env -i` word for the declared name removed: the step is
    refused (status 73), the dispatcher never runs and nothing is made."""
    monkeypatch.setenv(NAME, VALUE)
    line = printed(shards, tmp_path, capsys, "lines", DECLARED)[0]
    run, seen = dispatcher()
    assert run_line(shards, monkeypatch, line.replace(f"{NAME}={VALUE} ", ""), run) == shards.PRE_STEP_REFUSED
    assert f"{NAME} is not set" in capsys.readouterr().err
    assert seen == {}
    nothing_made(tmp_path)


def test_o63_the_declared_name_is_passed_to_the_post_run_script_with_the_dispatchers_environment(
    shards, tmp_path, monkeypatch
):
    """O63: the post-run script runs in the dispatcher's environment (it needs the federation as the run did), so it
    sees the declared name too; the guard, which runs before anything is made, gets the allowlist alone."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    monkeypatch.setenv(NAME, VALUE)
    args = step_args(tmp_path, [NAME])
    script = f"import os\nraise SystemExit(0 if os.environ.get({NAME!r}) == {VALUE!r} else 9)\n"
    (args.study / "post_run.py").write_text(script)
    args.post_run, args.post_run_timeout = "post_run.py", 20.0
    args.post_run_sha256 = hashlib.sha256(script.encode()).hexdigest()
    run, _ = dispatcher()
    assert shards.step(args, runner=run, core_found=lambda path: True) == 0
    assert manifest_of(tmp_path)["final"] is True
    assert shards.pre_step_env().get(NAME) is None


def test_o63_an_archive_left_on_the_node_carries_the_names_its_step_was_given(shards, tmp_path, monkeypatch):
    """O63: an out/ that could not be archived stays with the step's record, which holds the names and values the
    step was given; `--archive-left` writes the same manifest the step would have."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    monkeypatch.setenv(NAME, VALUE)
    args = step_args(tmp_path, [NAME])
    run, _ = dispatcher()
    with monkeypatch.context() as patch:
        patch.setattr(shards, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
        assert shards.step(args, runner=run, core_found=lambda path: True) == shards.ARCHIVE_FAILED
    (kept,) = (tmp_path / "tmpfs").glob("expcore-shard-fed-a.*")
    owner = json.loads((kept / "step.json").read_text())
    assert owner["pass_env"] == {NAME: VALUE}
    (kept / "step.json").write_text(json.dumps({**owner, "start": "0"}))  # the step ran in this process: gone
    assert shards.archive_left(argparse.Namespace(**{**vars(args), "archive_left": True})) == 0
    assert manifest_of(tmp_path)["pass_env"] == {NAME: VALUE}


# -- collect refuses a shard whose step was not given the plan's names --------------------------------------------


def two_shards(shards, tmp_path, monkeypatch, declared=(True, True), plan_extra=DECLARED):
    """Run both shards' steps (`declared[i]` False: the line of shard i has no `--pass-env`, as one edited by hand),
    against a plan with `plan_extra`; the plan, loaded as `collect` loads it (no operator environment needed)."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    monkeypatch.setenv(NAME, VALUE)
    for index, (label, kept) in enumerate(zip(("fed-a", "fed-b"), declared, strict=True), 1):
        run, _ = dispatcher()
        args = step_args(tmp_path, [NAME] if kept else None, index=index, label=label)
        assert shards.step(args, runner=run, core_found=lambda path: True) == 0
    monkeypatch.delenv(NAME)
    return shards.load_plan(manifest_file(tmp_path, plan_extra))


def test_o63_collect_unpacks_shards_whose_steps_were_given_the_plans_names(shards, tmp_path, monkeypatch):
    """O63: every shard recorded the plan's declared name: collected as ever, with no operator variable set."""
    plan = two_shards(shards, tmp_path, monkeypatch)
    assert [o.parent.name for o in shards.collect(plan, tmp_path / "into")] == ["fed-a", "fed-b"]


def test_o63_collect_refuses_a_shard_whose_line_lacked_a_declared_name_naming_it(shards, tmp_path, monkeypatch):
    """O63: a step whose line dropped a name the manifest declares (a hand-edited line, which `step` cannot tell: it
    never reads the manifest) is refused at `collect`, naming the shard and the name, leaving nothing behind."""
    plan = two_shards(shards, tmp_path, monkeypatch, declared=(True, False))
    with pytest.raises(shards.ShardsError, match=rf"shard fed-b: its step was not given {NAME}"):
        shards.collect(plan, tmp_path / "into")
    assert not (tmp_path / "into").exists()


def test_o63_collect_refuses_a_shard_that_was_given_a_name_the_plan_does_not_declare(shards, tmp_path, monkeypatch):
    """O63: a step that gained a variable the manifest does not declare is as refused as one that lost one."""
    plan = two_shards(shards, tmp_path, monkeypatch, plan_extra="")
    with pytest.raises(shards.ShardsError, match=rf"shard fed-a: its step was given {NAME}"):
        shards.collect(plan, tmp_path / "into")
    assert not (tmp_path / "into").exists()


def test_o63_collect_refuses_a_record_whose_value_cannot_be_a_value(shards, tmp_path, monkeypatch):
    """O63: an archive whose manifest holds a value outside the allowed characters (edited by hand) is refused, the
    value never echoed."""
    plan = two_shards(shards, tmp_path, monkeypatch)
    path = tmp_path / "results" / "fed-b.out.json"
    manifest = json.loads(path.read_text())
    manifest["pass_env"] = {NAME: "go ahead"}
    path.write_text(json.dumps(manifest))
    with pytest.raises(shards.ShardsError, match=rf"shard fed-b: .*{NAME}") as error:
        shards.collect(plan, tmp_path / "into")
    assert "go ahead" not in str(error.value)


def test_o63_an_older_archive_without_the_record_is_taken_by_a_plan_that_declares_nothing(
    shards, tmp_path, monkeypatch
):
    """O63: a shard archived before this key existed (no `pass_env` in its manifest) is collected by a plan without
    one, as every archive was."""
    plan = two_shards(shards, tmp_path, monkeypatch, declared=(False, False), plan_extra="")
    assert len(shards.collect(plan, tmp_path / "into")) == 2


# -- the rehearsal: a run whose sanction needs the operator's variable --------------------------------------------


def archived_names(tmp_path, label) -> set[str]:
    with tarfile.open(tmp_path / "results" / f"{label}.out.tar") as tar:
        return set(tar.getnames())


def test_o63_a_run_that_needs_the_operators_sanction_is_refused_without_the_key_and_runs_with_it(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: the rehearsal of a fault-injecting run on HPC, through `lines`, the node's `env -i` words and the real
    `step` and `run_dispatcher`: the dispatcher is a child process that, like a study's sanction, refuses to start
    (status 3) unless the operator's variable is in its environment with its value. The old line (no key in the
    manifest, the operator's variable set where `lines` ran) is refused, as every fault-injecting run on HPC was; the
    line of a manifest declaring the name runs, archives its out/, and `collect` takes both shards."""
    monkeypatch.setenv(NAME, VALUE)
    old = printed(shards, tmp_path / "old", capsys, "lines")[0]
    run, seen = dispatcher(shards, STUB)
    assert run_line(shards, monkeypatch, old, run) == 3  # the sanction's second half never reached the dispatcher
    assert NAME not in seen["env"]
    assert not list((tmp_path / "old" / "results").glob("*.out.*"))  # no archive: the run never started
    lines = printed(shards, tmp_path / "new", capsys, "lines", DECLARED)
    for line in lines:
        run, seen = dispatcher(shards, STUB)
        assert run_line(shards, monkeypatch, line, run) == 0
        assert seen["env"][NAME] == VALUE
    assert "out/campaign.lock" in archived_names(tmp_path / "new", "fed-a")
    monkeypatch.delenv(NAME)
    plan = shards.load_plan(tmp_path / "new" / "shards.toml")
    assert [o.parent.name for o in shards.collect(plan, tmp_path / "into")] == ["fed-a", "fed-b"]


def test_o63_no_line_is_made_from_a_plan_that_has_not_pinned_its_values(shards, tmp_path):
    """O63: `step_argv` refuses a plan whose declared names were not given their values (`pin_pass_env`), as it
    refuses one whose guard was not hashed; the fleet's lines, which never pin, fail closed the same way."""
    plan = shards.load_plan(manifest_file(tmp_path, DECLARED))
    assert plan.pass_env == (NAME,)
    with pytest.raises(shards.ShardsError, match="not pinned"):
        shards.step_argv(plan, plan.shards[0], shards.federation(plan.shards[0]), SCRIPT)


def test_o63_collect_needs_no_operator_environment_and_pins_nothing(shards, tmp_path, monkeypatch):
    """O63: `collect` calls `pinned` (it hashes the post-run script), which must read nothing of the operator's
    environment: a plan that declares a name stays unpinned through it, and loads with the name unset."""
    monkeypatch.delenv(NAME, raising=False)
    plan = shards.load_plan(manifest_file(tmp_path, DECLARED))
    assert shards.pinned(plan).pass_env_pins is None

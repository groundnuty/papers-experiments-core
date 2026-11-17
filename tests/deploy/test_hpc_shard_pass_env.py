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
from fingerprint_support import standin_python

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
NAME, VALUE = "FAULT_ACK_MODE", "go-ahead"
TREE_NAME, TREE = "HPC_STUDY_ARTIFACTS", "/srv/example/artifacts/tree"  # an invented absolute path
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
    lines = [f'study = "{tmp_path / "study"}"', f'python = "{standin_python(tmp_path / "venv")}"',
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
        (root / "facts.json").write_text("{}")  # what `up` writes beside the state: a collect archives both
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
    for key in (NAME, OTHER, TREE_NAME, *(node or {})):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**{k: v for k, v in environ.items() if k not in HOME_NAMES}, **(node or {})}.items():
        monkeypatch.setenv(key, value)
    real = getattr(shards.step, "real", shards.step)  # a second line in one test replaces the first's stand-ins

    def stood_in(args):
        return real(args, runner=runner, core_found=lambda path: True)

    stood_in.real = real
    monkeypatch.setattr(shards, "step", stood_in)
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


@pytest.mark.parametrize("value", ["", VALUE, "a.b_c:d=e,f+g-h", "Qz" * 64, TREE, "/a.b/c_d-e/f.g", "/" + "p" * 1024])
def test_o63_a_value_of_the_allowed_characters_is_pinned_and_arrives_whole(
    shards, tmp_path, capsys, monkeypatch, value
):
    """O63: letters, digits and `._:=,+-` (up to 128, empty too), or an absolute path of letters, digits and `._/-`
    (up to 1024 after its first slash), are a value; it survives the line's quoting and the split at the first `=`
    and reaches the dispatcher as given."""
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


LOADER_AND_SHELL = ["LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
                    "BASH_ENV", "ENV", "IFS", "GCONV_PATH", "LOCPATH", "NLSPATH"]  # fmt: skip


@pytest.mark.parametrize("name", LOADER_AND_SHELL)
def test_o63_a_loader_or_shell_variable_refuses_the_plan_naming_only_it(shards, tmp_path, capsys, name):
    """O63: the dynamic loader's variables (a library named by `LD_PRELOAD` runs before Python starts, so before the
    environment, identity and guard checks), the shell's start-up file and field separator, and the locale and
    conversion paths change how a program is started: none can be declared, whatever the operator's environment holds
    (it is not read: the name is refused first). The message names the variable and nothing else."""
    err = refused_plan(shards, tmp_path, capsys, f'pass_env = ["{name}"]')
    assert name in err
    assert "loaded or started" in err
    assert str(tmp_path) not in err


@pytest.mark.parametrize("name", LOADER_AND_SHELL)
def test_o63_a_step_refuses_a_loader_or_shell_variable_its_line_declares_with_nothing_made(
    shards, tmp_path, capsys, name
):
    """O63: a line typed or edited by hand to pass one is refused by the step as the plan would have, before anything
    of the study runs; nothing is made."""
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, [name]))
    assert name in said
    assert "loaded or started" in said
    nothing_made(tmp_path)


@pytest.mark.parametrize("name", ["LDAP_MODE", "LDFLAGS", "ENVIRONMENT_NAME", "IFS_MODE", "BASH_ENVIRONMENT",
                                  "LOCPATHS", "DYLD", "LD"])  # fmt: skip
def test_o63_a_name_that_only_starts_like_a_loader_or_shell_variable_is_still_accepted(shards, name):
    """O63: the refusal is of the names, `LD_` and `DYLD_` as prefixes and the exact shell and locale names: no other
    variable that begins alike is refused."""
    assert shards.pass_env_name_problem(name) is None


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


@pytest.mark.parametrize("value", ["/qzdir/../etc", "/qzdir/..", "/qzdir/../", "qzdir/rel", "./qzdir", "/qzdir/./x",
                                   "/qzdir/.", "/qzdir//x", "//qzdir", "/qzdir/my dir", "/qzdir/$HOME", "/qzdir/a;b",
                                   "/qzdir/a:b", "/qzdir/\u00e9", "/qzdir/" + "p" * 1025, "/", "/\n", ".",
                                   ".."])  # fmt: skip
def test_o63_a_value_that_is_neither_a_plain_word_nor_a_plain_absolute_path_refuses_the_plan(
    shards, tmp_path, capsys, monkeypatch, value
):
    """O63: an absolute path may hold no `..` or `.` part and no `//`, no character outside `A-Za-z0-9._/-` and at most
    1024 after its first slash; a relative path, one with a space, `$`, `;` or `:` and a bare `/` refuse `plan` and
    `lines` with nothing printed, naming the variable and never the value."""
    monkeypatch.setenv(NAME, value)
    err = refused_plan(shards, tmp_path, capsys, DECLARED)
    assert f"{NAME}'s value does not match" in err
    assert "qzdir" not in err


# -- the step: exactly what its line pins ---------------------------------------------------------------------------


def step_args(tmp_path, pass_env=None, *, index=1, label="fed-a", resume=False):
    """The step's arguments as its generated line gives them; `pass_env=None` is a line with no such option."""
    study = make_study(tmp_path)
    extra = {} if pass_env is None else {"pass_env": list(pass_env)}
    return argparse.Namespace(study=study, index=index, count=2, label=label, tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume, archive_left=False,
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
        ([NAME], {NAME: "/qzdir/../etc"}, f"{NAME}'s value does not match"),
        ([NAME], {NAME: "qzdir/rel"}, f"{NAME}'s value does not match"),
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
    assert "qzdir" not in err
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


POST_ASSERTS = (
    "import os\n"
    f"want = {{{NAME!r}: {VALUE!r}, {TREE_NAME!r}: {TREE!r}}}\n"
    "raise SystemExit(0 if all(os.environ.get(k) == v for k, v in want.items()) else 9)\n"
)


def test_o63_an_absolute_path_value_reaches_the_dispatcher_and_the_post_run_script_through_the_printed_line(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: the pilot's second use: the study's artifacts directory (an absolute path; `HPC_` is not a name the step
    manages) declared beside the sanction variable. Through `lines` and the real `step`, both reach the dispatcher and
    the study's post-run script (which exits non-zero unless it sees both, so its results would be not final), and
    step.json and the archive's manifest hold both."""
    study = make_study(tmp_path)
    (study / "guards").mkdir()
    (study / "guards" / "post_run.py").write_text(POST_ASSERTS)
    monkeypatch.setenv(NAME, VALUE)
    monkeypatch.setenv(TREE_NAME, TREE)
    extra = f'pass_env = ["{NAME}", "{TREE_NAME}"]\npost_run = "guards/post_run.py"\npost_run_timeout_s = 20'
    line = printed(shards, tmp_path, capsys, "lines", extra)[0]
    assert f"{TREE_NAME}={TREE} " in line
    assert line.count("--pass-env") == 2
    run, seen = dispatcher()
    assert run_line(shards, monkeypatch, line, run) == 0
    both = {NAME: VALUE, TREE_NAME: TREE}
    assert {k: seen["env"][k] for k in both} == both
    assert seen["owner"]["pass_env"] == both
    manifest = manifest_of(tmp_path)
    assert manifest["pass_env"] == both
    assert manifest["final"] is True
    assert manifest["post_run"]["exit_status"] == 0
    assert set(seen["env"]) - set(shards.ALLOWLIST) - HOME_NAMES - STEP_OWN == set(both)


def test_o63_the_post_run_script_that_does_not_see_the_variables_leaves_the_results_not_final(
    shards, tmp_path, monkeypatch
):
    """O63: the post-run assertion above is real: the same script, in a step whose line declared neither variable,
    exits 9 and the results are archived not final."""
    study = make_study(tmp_path)
    (study / "guards").mkdir()
    (study / "guards" / "post_run.py").write_text(POST_ASSERTS)
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    args = step_args(tmp_path)
    args.post_run, args.post_run_timeout = "guards/post_run.py", 20.0
    args.post_run_sha256 = hashlib.sha256(POST_ASSERTS.encode()).hexdigest()
    run, _ = dispatcher()
    assert shards.step(args, runner=run, core_found=lambda path: True) == shards.POST_RUN_FAILED
    assert manifest_of(tmp_path)["final"] is False


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


def test_o63_the_origin_each_first_step_recorded_is_listed_as_evidence_by_collect(shards, tmp_path, monkeypatch):
    """O63: the `pass-env.json` a shard's first step writes (the values its variables had) is what `collect` lists
    beside the shard's archive for the copy back (`archives.sha256.json`, with the root evidence of the same collect):
    the writer and the evidence name the file alike, and the hash is of the bytes the step wrote."""
    plan = two_shards(shards, tmp_path, monkeypatch)
    shards.collect(plan, tmp_path / "into")
    listed = {e["name"]: e for e in json.loads((plan.results / shards.ARCHIVES_MANIFEST).read_text())["evidence"]}
    assert shards.PASS_ENV == shards.PASS_ENV_ORIGIN
    for label in ("fed-a", "fed-b"):
        written = plan.results / label / shards.PASS_ENV_ORIGIN
        assert listed[f"{label}/{shards.PASS_ENV_ORIGIN}"]["sha256"] == hashlib.sha256(written.read_bytes()).hexdigest()


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


@pytest.mark.parametrize("value", ["go ahead", "/qzdir/../x"])
def test_o63_collect_refuses_a_record_whose_value_cannot_be_a_value(shards, tmp_path, monkeypatch, value):
    """O63: an archive whose manifest holds a value outside the allowed characters, or a path that climbs (edited by
    hand), is refused, the value never echoed."""
    plan = two_shards(shards, tmp_path, monkeypatch)
    path = tmp_path / "results" / "fed-b.out.json"
    manifest = json.loads(path.read_text())
    manifest["pass_env"] = {NAME: value}
    path.write_text(json.dumps(manifest))
    with pytest.raises(shards.ShardsError, match=rf"shard fed-b: .*{NAME}") as error:
        shards.collect(plan, tmp_path / "into")
    assert "go ahead" not in str(error.value)
    assert "qzdir" not in str(error.value)


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
    monkeypatch.setenv(NAME, VALUE)  # the operator's shell, where `lines` runs again (the node's was emulated above)
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


# -- one value for a shard's whole life: a later step is refused a value its first step did not have ----------------

CHANGED = "changed-mode"  # a value the operator holds later: it must appear in no message
SPEC_A = {"index": 1, "count": 2, "label": "fed-a"}


def origin_file(tmp_path, label="fed-a") -> Path:
    """Where the shard's first step records the values it passed: its directory of the results, beside its mark."""
    return tmp_path / "results" / label / "pass-env.json"


def node_holds(monkeypatch, value=VALUE):
    """The node's environment the step reads: the federation's root and the declared variable (its line's word)."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    monkeypatch.setenv(NAME, value)


def first_step(shards, tmp_path, monkeypatch, value=VALUE):
    """The shard's first step, really run with a stand-in dispatcher: its archive, and the record of its values."""
    node_holds(monkeypatch, value)
    run, _ = dispatcher()
    assert shards.step(step_args(tmp_path, [NAME]), runner=run, core_found=lambda path: True) == 0


def never_run(argv, env, cwd):
    raise AssertionError("a refused step must never start its dispatcher")


def refused_step(shards, tmp_path, capsys, args) -> str:
    """A step with `args` that the check refuses (status 73) before its dispatcher starts: what it said."""
    capsys.readouterr()
    assert shards.step(args, runner=never_run, core_found=lambda path: True) == shards.PRE_STEP_REFUSED
    return capsys.readouterr().err


def test_o63_a_first_step_records_the_values_it_passed_in_its_shards_results_directory(shards, tmp_path, monkeypatch):
    """O63: the shard's first step writes the name and value of each variable it passed into
    `<results>/<label>/pass-env.json` (the origin a later step is compared with), whole and private to its user."""
    first_step(shards, tmp_path, monkeypatch)
    record = json.loads(origin_file(tmp_path).read_text())
    assert record["pass_env"] == {NAME: VALUE}
    assert record["shard"] == SPEC_A
    assert origin_file(tmp_path).stat().st_mode & 0o077 == 0
    assert not list(origin_file(tmp_path).parent.glob(".pass-env*"))  # no draft left behind


def test_o63_a_step_that_passes_nothing_records_no_origin(shards, tmp_path, monkeypatch):
    """O63: with no `pass_env` the results directory is as it was: no origin file."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    run, _ = dispatcher()
    assert shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True) == 0
    assert not origin_file(tmp_path).exists()


def test_o63_a_step_refused_before_it_runs_records_no_origin(shards, tmp_path, capsys, monkeypatch):
    """O63: a step the pass_env check refuses on its own line (a name with no value) made no first step: no origin."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    monkeypatch.delenv(NAME, raising=False)
    assert "is not set" in refused_step(shards, tmp_path, capsys, step_args(tmp_path, [NAME]))
    assert not origin_file(tmp_path).exists()


def test_o63_a_resume_whose_value_changed_since_the_first_step_is_refused_naming_only_the_variable(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: a `--resume` step, through the real `step`, whose line gives the declared variable another value than the
    shard's first step passed is refused (73) before anything is made or its dispatcher runs; the message names the
    variable and shows neither value; the archive and the origin are as they were."""
    first_step(shards, tmp_path, monkeypatch)
    archived = manifest_of(tmp_path)
    node_holds(monkeypatch, CHANGED)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, [NAME], resume=True))
    assert NAME in said
    assert VALUE not in said
    assert CHANGED not in said
    assert manifest_of(tmp_path) == archived
    assert json.loads(origin_file(tmp_path).read_text())["pass_env"] == {NAME: VALUE}
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not (tmp_path / "results" / "fed-a" / "running.json").exists()


def test_o63_a_resume_with_the_same_values_proceeds_and_keeps_the_origin(shards, tmp_path, monkeypatch):
    """O63: the same value again continues the shard, and the origin is the first step's, untouched."""
    first_step(shards, tmp_path, monkeypatch)
    before = origin_file(tmp_path).read_bytes()
    node_holds(monkeypatch)
    run, seen = dispatcher()
    assert shards.step(step_args(tmp_path, [NAME], resume=True), runner=run, core_found=lambda path: True) == 0
    assert seen["env"][NAME] == VALUE
    assert origin_file(tmp_path).read_bytes() == before
    assert manifest_of(tmp_path)["pass_env"] == {NAME: VALUE}


def first_step_passing_nothing(shards, tmp_path, monkeypatch):
    """The shard's first step, run with a line that declares no variable: its archive, and no origin."""
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    run, _ = dispatcher()
    assert shards.step(step_args(tmp_path), runner=run, core_found=lambda path: True) == 0
    assert not origin_file(tmp_path).exists()


def test_o63_a_resume_that_adds_a_variable_to_a_shard_whose_first_step_passed_none_is_refused(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: a shard that has results (an archive) and no origin had a first step that passed nothing: a later step
    that now passes a declared variable adds one mid-shard, a change of value, and is refused (73) naming the
    variable and never its value, before anything is made; no origin is written and the archive is as it was."""
    first_step_passing_nothing(shards, tmp_path, monkeypatch)
    archived = manifest_of(tmp_path)
    node_holds(monkeypatch)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, [NAME], resume=True))
    assert NAME in said
    assert VALUE not in said
    assert manifest_of(tmp_path) == archived
    assert not origin_file(tmp_path).exists()
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_a_snapshot_alone_is_results_too_for_a_variable_added_mid_shard(shards, tmp_path, capsys, monkeypatch):
    """O63: a shard killed before it archived anything has a snapshot and no archive: with no origin, a step that
    passes a declared variable is refused as it is with an archive."""
    (tmp_path / "results" / "fed-a" / "snapshots" / "snap-000001").mkdir(parents=True)
    node_holds(monkeypatch)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, [NAME], resume=True))
    assert NAME in said
    assert VALUE not in said
    assert not origin_file(tmp_path).exists()


def test_o63_a_resume_that_passes_nothing_after_a_first_step_that_passed_nothing_proceeds(
    shards, tmp_path, monkeypatch
):
    """O63: the strict rule is about variables: a shard with results and no origin, resumed with a line that declares
    none, is what it was, and writes no origin."""
    first_step_passing_nothing(shards, tmp_path, monkeypatch)
    run, _ = dispatcher()
    assert shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True) == 0
    assert not origin_file(tmp_path).exists()


def test_o63_a_variable_added_mid_shard_is_refused_by_the_writer_too_when_results_appeared_after_the_check(
    shards, tmp_path
):
    """O63: the first step records its values only while the shard has no results: between its first check and its
    claim of the running mark another step may have archived (passing nothing), and the writer refuses then."""
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "fed-a.out.json").write_text("{}")
    with pytest.raises(shards.ShardsError, match=NAME) as caught:
        shards.record_pass_env_origin(tmp_path / "results", SPEC_A, {NAME: VALUE})
    assert VALUE not in str(caught.value)
    assert not origin_file(tmp_path).exists()


def test_o63_a_plain_relaunch_is_compared_too_when_the_first_step_left_only_its_origin(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: a first step killed before its first snapshot left no archive and no snapshot, only its origin: a plain
    (not `--resume`) launch is refused a changed value, and proceeds with the same one."""
    shards.record_pass_env_origin(tmp_path / "results", SPEC_A, {NAME: VALUE})  # the step's own writer
    node_holds(monkeypatch, CHANGED)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, [NAME]))
    assert NAME in said
    assert VALUE not in said
    assert CHANGED not in said
    node_holds(monkeypatch)
    run, _ = dispatcher()
    assert shards.step(step_args(tmp_path, [NAME]), runner=run, core_found=lambda path: True) == 0


@pytest.mark.parametrize(
    ("origin", "line", "named", "not_named"),
    [
        ({NAME: VALUE}, [], [NAME], []),  # the manifest dropped the declaration: the shard would lose its variable
        ({OTHER: VALUE}, [NAME], [NAME, OTHER], []),  # one the first step did not pass, and one it passed and lost
        ({NAME: VALUE, OTHER: VALUE}, [NAME], [OTHER], [NAME]),  # only the one that differs is named
    ],
)
def test_o63_a_later_step_must_pass_the_names_of_the_first_one_not_more_not_fewer(
    shards, tmp_path, capsys, monkeypatch, origin, line, named, not_named
):
    """O63: the names count as the values do: a later step that passes a name the first did not, or leaves one out,
    is refused, naming those variables only."""
    shards.record_pass_env_origin(tmp_path / "results", SPEC_A, origin)
    node_holds(monkeypatch)
    monkeypatch.setenv(OTHER, VALUE)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, line))
    assert all(name in said for name in named)
    assert not any(name in said for name in not_named)
    assert VALUE not in said


@pytest.mark.parametrize(
    "garbage",
    ["", "not json", "[]", '{"format": "other/1"}', json.dumps({"format": "expcore-hpc-shard-pass-env/1", "shard":
     {"index": 2, "count": 2, "label": "fed-b"}, "pass_env": {NAME: VALUE}}),
     json.dumps({"format": "expcore-hpc-shard-pass-env/1", "shard": SPEC_A, "pass_env": {NAME: ["x"]}}),
     json.dumps({"format": "expcore-hpc-shard-pass-env/1", "shard": SPEC_A, "pass_env": {"lower": VALUE}})],
)  # fmt: skip
def test_o63_an_origin_that_cannot_be_read_as_this_shards_refuses_the_step_never_taken_for_none(
    shards, tmp_path, capsys, monkeypatch, garbage
):
    """O63: an origin file that is empty, not JSON, not a record, of another shard, or holding a value or a name a
    variable may not carry refuses the step: truncating the record must not lift the rule."""
    origin_file(tmp_path).parent.mkdir(parents=True)
    origin_file(tmp_path).write_text(garbage)
    node_holds(monkeypatch)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, [NAME]))
    assert "pass-env.json" in said
    assert VALUE not in said


def test_o63_the_origin_is_never_followed_through_a_link(shards, tmp_path, capsys, monkeypatch):
    """O63: an origin that is a link to a file elsewhere is refused unread, as any record it cannot take for the
    shard's."""
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"format": shards.PASS_ENV_ORIGIN_FORMAT, "shard": SPEC_A,
                                     "pass_env": {NAME: VALUE}}))  # fmt: skip
    origin_file(tmp_path).parent.mkdir(parents=True)
    origin_file(tmp_path).symlink_to(elsewhere)
    node_holds(monkeypatch)
    assert "pass-env.json" in refused_step(shards, tmp_path, capsys, step_args(tmp_path, [NAME]))


def test_o63_a_bad_label_is_still_the_labels_refusal_not_the_origins(shards, tmp_path, monkeypatch):
    """O63: the origin is looked for only under a label the core accepts: a path-like label reaches `step`'s own label
    check, which refuses it as it always did."""
    node_holds(monkeypatch)
    args = step_args(tmp_path, [NAME], label="../escape")
    run, _ = dispatcher()
    with pytest.raises(shards.ShardsError, match="label"):
        shards.step(args, runner=run, core_found=lambda path: True)
    assert not (tmp_path / "escape").exists()


# -- a deleted origin does not lift the rule: the shard's archive manifest still says what its step passed -----------


def resumed_without_origin(shards, tmp_path, monkeypatch, capsys, line, value=VALUE):
    """A shard whose first step passed NAME, its `pass-env.json` then deleted, and a `--resume` step whose line
    declares `line` (names, or None for no option) with the node holding `value`: what that step said when refused."""
    first_step(shards, tmp_path, monkeypatch)
    origin_file(tmp_path).unlink()
    node_holds(monkeypatch, value)
    monkeypatch.setenv(OTHER, value)
    return refused_step(shards, tmp_path, capsys, step_args(tmp_path, line, resume=True))


def test_o63_a_resume_declaring_nothing_after_the_origin_was_deleted_is_refused_naming_the_variable(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: the rule that a shard's variables keep one value for its whole life is held by its origin file, and by the
    archive manifest that records what its step passed: with the file deleted, a resume that declares nothing was
    accepted (and its archive then recorded no variable). It is compared with the manifest instead, and refused,
    naming the variable and no value; the archive is as it was and no origin was made."""
    said = resumed_without_origin(shards, tmp_path, monkeypatch, capsys, None)
    assert NAME in said
    assert VALUE not in said
    assert manifest_of(tmp_path)["pass_env"] == {NAME: VALUE}
    assert not origin_file(tmp_path).exists()
    assert not (tmp_path / "results" / "fed-a" / "running.json").exists()


def test_o63_a_resume_with_another_value_after_the_origin_was_deleted_is_refused_naming_only_the_variable(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: the manifest records the values too, so a deleted origin does not make a changed value acceptable (a
    names-only comparison would have let the resume write a new origin holding it)."""
    said = resumed_without_origin(shards, tmp_path, monkeypatch, capsys, [NAME], value=CHANGED)
    assert NAME in said
    assert VALUE not in said
    assert CHANGED not in said
    assert not origin_file(tmp_path).exists()


def test_o63_a_resume_adding_a_variable_after_the_origin_was_deleted_is_refused_naming_it(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: a name the first step did not pass is named, and the one it did is not."""
    said = resumed_without_origin(shards, tmp_path, monkeypatch, capsys, [NAME, OTHER])
    assert OTHER in said
    assert NAME not in said
    assert VALUE not in said


def test_o63_a_resume_matching_the_archive_manifest_after_the_origin_was_deleted_continues_and_restores_it(
    shards, tmp_path, monkeypatch
):
    """O63: the same names and values as the manifest records are what the first step passed: the shard continues, and
    the origin is written again with them (as a first step writes it), so the rule holds from there on."""
    first_step(shards, tmp_path, monkeypatch)
    origin_file(tmp_path).unlink()
    node_holds(monkeypatch)
    run, seen = dispatcher()
    assert shards.step(step_args(tmp_path, [NAME], resume=True), runner=run, core_found=lambda path: True) == 0
    assert seen["env"][NAME] == VALUE
    assert json.loads(origin_file(tmp_path).read_text())["pass_env"] == {NAME: VALUE}


@pytest.mark.parametrize("damage", ["not json", "no shard", "of another shard", "a value that is no value"])
def test_o63_an_archive_manifest_that_cannot_say_what_was_passed_refuses_a_step_with_no_origin(
    shards, tmp_path, capsys, monkeypatch, damage
):
    """O63: with the origin gone, the manifest is the only record: one that cannot be read as this shard's, or that
    holds a value a variable may not carry, is never taken for "nothing was passed"."""
    first_step(shards, tmp_path, monkeypatch)
    origin_file(tmp_path).unlink()
    path = tmp_path / "results" / "fed-a.out.json"
    record = json.loads(path.read_text())
    if damage == "not json":
        path.write_text("{")
    elif damage == "no shard":
        del record["shard"]
        path.write_text(json.dumps(record))
    elif damage == "of another shard":
        record["shard"] = {"index": 2, "count": 2, "label": "fed-b"}
        path.write_text(json.dumps(record))
    else:
        record["pass_env"] = {NAME: ["x"]}
        path.write_text(json.dumps(record))
    node_holds(monkeypatch)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, None, resume=True))
    assert "out.json" in said
    assert VALUE not in said
    assert not origin_file(tmp_path).exists()


def test_o63_a_shard_that_passed_nothing_resumes_with_nothing_as_before(shards, tmp_path, monkeypatch):
    """O63: no origin and an archive manifest with no `pass_env` is a first step that passed nothing: a resume that
    declares nothing continues, as it always did."""
    first_step_passing_nothing(shards, tmp_path, monkeypatch)
    run, _ = dispatcher()
    assert shards.step(step_args(tmp_path, resume=True), runner=run, core_found=lambda path: True) == 0
    assert not origin_file(tmp_path).exists()


def test_o63_a_shard_killed_after_its_first_archives_tar_and_before_its_manifest_resumes_as_before(
    shards, tmp_path, monkeypatch
):
    """O63: the archive's tar is renamed into place and hashed before its manifest is written, so a step killed
    between the two leaves a tar and no manifest (its tmpfs, and so the leftover `--archive-left` needs, is gone). A
    study that declares no variable has no origin either, and the shard resumes from its snapshots as it always did:
    no manifest is not an unreadable one. A step that does declare variables is refused for adding them, naming only
    them."""
    first_step_passing_nothing(shards, tmp_path, monkeypatch)
    (tmp_path / "results" / "fed-a.out.json").unlink()
    assert (tmp_path / "results" / "fed-a.out.tar").exists()
    results = tmp_path / "results"
    assert shards.check_pass_env_origin(results, SPEC_A, {}) is None
    with pytest.raises(shards.ShardsError, match=NAME) as refused:
        shards.check_pass_env_origin(results, SPEC_A, {NAME: VALUE})
    assert VALUE not in str(refused.value)


def test_o63_an_unreadable_manifest_still_names_the_variable_a_step_adds(shards, tmp_path):
    """O63: with no origin and a manifest that cannot be read (here an empty record), a step that declares a variable
    is refused for adding it, as it was before the manifest was read; one that declares nothing is refused naming
    the manifest."""
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "fed-a.out.json").write_text("{}")
    with pytest.raises(shards.ShardsError, match=NAME):
        shards.check_pass_env_origin(tmp_path / "results", SPEC_A, {NAME: VALUE})
    with pytest.raises(shards.ShardsError, match=r"fed-a\.out\.json"):
        shards.check_pass_env_origin(tmp_path / "results", SPEC_A, {})


# -- the origin is never longer than the reader takes --------------------------------------------------------------


def origin_size(passed) -> int:
    """The bytes `record_pass_env_origin` writes for `passed`, built as it builds them."""
    record = {"format": "expcore-hpc-shard-pass-env/1", "shard": SPEC_A, "pass_env": passed}
    return len((json.dumps(record, sort_keys=True) + "\n").encode())


def test_o63_an_origin_longer_than_the_reader_takes_is_refused_by_the_writer_before_it_writes(
    shards, tmp_path, capsys, monkeypatch
):
    """O63: 64 valid names with absolute-path values of 1,025 characters each are all accepted by the checks on a
    line, and wrote a record of 66,404 bytes that the reader (limit 65,536) refuses as a record it cannot take: the
    step passed, and every later one, however identical, was refused. The writer measures the record first and refuses
    the step, naming the limit and no variable or value; no draft, no origin and no running mark are left."""
    passed = {f"V{n}": "/" + "a" * 1024 for n in range(64)}
    assert origin_size(passed) > shards.PASS_ENV_ORIGIN_MAX
    monkeypatch.setenv("EXPCORE_FED_ROOT", "/fed/root")
    for name, value in passed.items():
        monkeypatch.setenv(name, value)
    said = refused_step(shards, tmp_path, capsys, step_args(tmp_path, list(passed)))
    refusal = next(line for line in said.splitlines() if "ERROR" in line)  # the note of what it passed names them
    assert str(shards.PASS_ENV_ORIGIN_MAX) in refusal
    assert "V0" not in refusal
    assert "aaaa" not in said
    results = tmp_path / "results"
    assert not origin_file(tmp_path).exists()
    assert not list(results.glob("**/.pass-env*"))
    assert not (results / "fed-a" / "running.json").exists()
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o63_the_writer_takes_a_record_of_exactly_the_readers_limit_and_the_reader_reads_it_back(shards, tmp_path):
    """O63: the limit is the reader's: a record of exactly 65,536 bytes is written, and read back whole (and the record
    one byte longer is refused)."""
    full = "/" + "a" * 1024
    passed = {f"V{n}": full for n in range(63)}
    room = shards.PASS_ENV_ORIGIN_MAX - origin_size({**passed, "W": "/"})
    assert 0 < room <= 1024
    passed["W"] = "/" + "a" * room
    assert origin_size(passed) == shards.PASS_ENV_ORIGIN_MAX
    results = tmp_path / "results"
    shards.record_pass_env_origin(results, SPEC_A, passed)
    assert origin_file(tmp_path).stat().st_size == shards.PASS_ENV_ORIGIN_MAX
    assert shards.read_pass_env_origin(results, SPEC_A) == passed
    other = tmp_path / "other"
    longer = {**passed, "W": passed["W"] + "a"}
    assert origin_size(longer) == shards.PASS_ENV_ORIGIN_MAX + 1
    with pytest.raises(shards.ShardsError, match=str(shards.PASS_ENV_ORIGIN_MAX)):
        shards.record_pass_env_origin(other / "results", SPEC_A, longer)
    assert not (other / "results").exists()

"""O68: the fleet's plan gives each shard of one campaign a federation of one platform fingerprint, its step lands on
that federation's own job and node, and a shard moves only to a fresh federation of the fleet's fingerprint whose first
reset is full (deploy/hpc-federation/fleetplan.py).

Every federation runs over a fake cluster of its own (`hpc_fleet_support.Bench`), its platform.toml the one its own
up rendered; the step's srun is stood in by `StepRunner`, which checks each line on its federation's cluster."""

import hashlib
import json

import pytest
from fingerprint_support import seen_environments, seen_platforms
from hpc_fed_support import data_stand_in
from hpc_fleet_support import another_ca, fleet_modules, job_ids, make_bench, make_ca
from tls_support import HAS_OPENSSL

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the zone's CA is made with openssl")


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        yield found


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return make_ca(tmp_path_factory.mktemp("ca"))


@pytest.fixture
def bench(deploy, ca, tmp_path, monkeypatch):
    return make_bench(deploy, tmp_path, ca, monkeypatch)


def record(bench) -> dict:
    return json.loads((bench.fleet / "fleet.json").read_text(encoding="utf-8"))


def plan(bench, *extra: str) -> tuple[int, str, dict]:
    """`fleet.py plan`: its exit status, what it printed, and each shard's line by its label."""
    code, printed = bench.run("plan", "--manifest", str(bench.settings()), *extra)
    lines = {}
    for text in printed.splitlines():
        if text.startswith("{"):
            entry = json.loads(text)
            lines[entry["shard"]] = entry
    return code, printed, lines


def assigned(bench) -> dict:
    return {s["label"]: s["member"] for s in record(bench)["shards"]}


GUARD, POST = "raise SystemExit(0)  # a study's guard\n", "raise SystemExit(0)  # a study's post-run script\n"
DECLARED_SCRIPTS = ('pre_step = "guards/pre_step.py"', 'post_run = "guards/post_run.py"')


def make_scripts(bench) -> None:
    """The study directory the settings name, with the two scripts they declare."""
    guards = bench.base / "study" / "guards"
    guards.mkdir(parents=True)
    (guards / "pre_step.py").write_text(GUARD)
    (guards / "post_run.py").write_text(POST)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def script_words(argv: list) -> dict:
    """The guard's and the post-run script's words on a step line: each option and its value."""
    options = ("--pre-step", "--pre-step-sha256", "--post-run", "--post-run-sha256")
    return {option: argv[argv.index(option) + 1] for option in options if option in argv}


# ------------------------------------------------------------------------------------------------ plan
def test_o68_plan_gives_each_shard_its_own_federations_job_and_node(bench):
    """O68: shard-k on fed-k: its line an exact step of fed-k's held job's component 1, on the agent's node fed-k's
    state names, labelled and counted as one shard of three; shards.toml written for collect."""
    assert bench.up(3)[0] == 0
    code, printed, lines = plan(bench)
    assert code == 0, printed
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": "fed-2", "shard-3": "fed-3"}
    for index, label in enumerate(("fed-1", "fed-2", "fed-3"), 1):
        argv = lines[f"shard-{index}"]["argv"]
        assert argv[1:3] == [f"--jobid={job_ids(label)[1]}", "--exact"]  # the agent's own job: no component
        assert not [word for word in argv if word.startswith("--het-group")]
        assert "--chdir=/" in argv[: argv.index("/usr/bin/env")]
        sandbox = json.loads((bench.root(label) / "hpc-federation.json").read_text(encoding="utf-8"))["sandbox"]
        assert argv[argv.index("-w") + 1] == sandbox["node"]
        assert argv[argv.index("--count") + 1] == "3"
        assert argv[argv.index("--label") + 1] == f"shard-{index}"
        assert f"EXPCORE_FED_ROOT={bench.root(label)}" in argv
        bench.fake(label)._step(argv)  # the fake cluster places it on that job's component 1, or fails the test
    manifest = (bench.fleet / "shards.toml").read_text(encoding="utf-8")
    assert manifest.count("[[shards]]") == 3
    reference = record(bench)["reference"]
    assert reference["fingerprint"] == bench.fingerprint("fed-1") == bench.fingerprint("fed-3")


def test_o68_plan_refuses_a_fleet_whose_federations_platform_fingerprints_differ(bench, tmp_path):
    """O68: fed-3's zone is of another image's build (another CA): the federations' platform fingerprints differ in
    their TLS record, so their shards would never be one campaign; nothing is assigned, nothing is written."""
    bench.cas["fed-3"] = another_ca(tmp_path / "other-ca")
    assert bench.up(3)[0] == 0
    code, printed, lines = plan(bench)
    assert code == 2
    assert "the federations' platform fingerprints differ" in printed
    assert "fed-1, fed-2; fed-3 (differing: tls)" in printed
    assert lines == {}
    assert all(member is None for member in assigned(bench).values())
    assert record(bench)["reference"] is None
    assert not (bench.fleet / "shards.toml").exists()


def test_o68_a_failed_federations_shard_is_left_unassigned_and_the_others_planned(bench):
    """O68: fed-2's up failed: shard-1 and shard-3 are planned on their federations, shard-2 is unassigned."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    assert bench.up(3)[0] == 1
    code, printed, lines = plan(bench)
    assert code == 1
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": None, "shard-3": "fed-3"}
    assert sorted(lines) == ["shard-1", "shard-3"]
    assert "shard-2: unassigned: no fresh federation is ready for it" in printed
    assert lines["shard-3"]["argv"][lines["shard-3"]["argv"].index("--count") + 1] == "3"


def test_o68_an_unassigned_shard_goes_to_a_fresh_federation_of_the_fleets_fingerprint(bench):
    """O68: fed-2 failed and was taken down; a fresh fed-4 (its own up, its data checked, the same fingerprint)
    takes shard-2; shard-1 and shard-3 stay where they are."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    assert bench.up(3)[0] == 1
    assert plan(bench)[0] == 1
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert bench.run("add", "1")[0] == 0
    code, printed, lines = plan(bench)
    assert code == 0, printed
    assert assigned(bench) == {"shard-1": "fed-1", "shard-2": "fed-4", "shard-3": "fed-3"}
    assert lines["shard-2"]["argv"][1] == f"--jobid={job_ids('fed-4')[1]}"


def test_o68_a_fresh_federation_of_another_fingerprint_is_never_given_a_shard(bench, tmp_path):
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    bench.cas["fed-4"] = another_ca(tmp_path / "other-ca")
    assert bench.up(3)[0] == 1
    assert plan(bench)[0] == 1
    assert bench.run("down", "--member", "fed-2")[0] == 0
    assert bench.run("add", "1")[0] == 0
    code, printed, _ = plan(bench)
    assert code == 1
    assert "not used: fed-4: its platform fingerprint differs from the fleet's (differing: tls)" in printed
    assert assigned(bench)["shard-2"] is None


def test_o68_plan_refuses_settings_other_than_the_first_plans(bench):
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    other = bench.base / "other-settings.toml"
    other.write_text(bench.settings().read_text(encoding="utf-8").replace("cpus = 2", "cpus = 3"), encoding="utf-8")
    code, printed = bench.run("plan", "--manifest", str(other))
    assert code == 2
    assert "differ from the ones the fleet's first plan read" in printed


# ------------------------------------------------------------------------------------------------ step
def test_o68_step_launches_each_shard_on_its_own_federation_once(bench):
    """O68: each assigned shard's line spawned detached, checked on its own federation's cluster; a second step
    launches nothing while the clients run."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    code, printed = bench.run("step")
    assert code == 0, printed
    assert [(s.label, s.srun.job) for s in bench.spawned] == [("fed-1", job_ids("fed-1")[1]),
                                                              ("fed-2", job_ids("fed-2")[1])]  # fmt: skip
    assert all("--resume" not in s.argv for s in bench.spawned)
    code, printed = bench.run("step")
    assert len(bench.spawned) == 2
    assert "shard-1: its step on fed-1 still runs" in printed


def test_o68_step_checks_the_federation_again_and_refuses_one_no_longer_the_fleets(bench):
    """O68: fed-1's state replaced after the plan: shard-1 is not launched there; shard-2 is, and step exits 1, since
    a shard it was asked to launch was refused."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    path = bench.root("fed-1") / "hpc-federation.json"
    replaced = json.loads(path.read_text(encoding="utf-8"))
    replaced["allocation"]["name"] = "hpcop-another-federation-0000"
    path.write_text(json.dumps(replaced), encoding="utf-8")
    code, printed = bench.run("step")
    assert code == 1
    assert "shard-1: not launched: fed-1: its state file is of another federation" in printed
    assert [s.label for s in bench.spawned] == ["fed-2"]


@pytest.mark.parametrize("now", ["another job", "gone", "pending"])
def test_o68_step_never_launches_into_a_job_slurm_no_longer_holds_for_the_federation(bench, now):
    """O68: everything on file still says fed-1, but Slurm now says otherwise of its agent's job: its id handed to
    another job (Slurm reuses ids), the job gone, or not running. shard-1 is not launched; shard-2 is; step exits 1."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    fake, agent = bench.fake("fed-1"), job_ids("fed-1")[1]
    if now == "another job":
        fake.submitted[agent][0]["--job-name"] = "someone-elses-job"
    elif now == "gone":
        fake.job_states[agent] = "GONE"
    else:
        fake.job_states[agent] = "PENDING"
    code, printed = bench.run("step")
    assert code == 1
    assert "shard-1: not launched: " in printed
    assert [s.label for s in bench.spawned] == ["fed-2"]
    assert all(s.srun.job != agent for s in bench.spawned)


def test_o68_step_launches_nothing_from_a_shard_manifest_the_record_does_not_give(bench):
    """O68: shards.toml edited after the plan to give shard-1 fed-2's root and shard-2 fed-1's (linked federations of
    the fleet's, each still one shard's own): step refuses whole and launches nothing, since a line built from it would
    land in a job the fleet never checked."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    path = bench.fleet / "shards.toml"
    text = path.read_text(encoding="utf-8")
    first, second = f'root = "{bench.root("fed-1")}"', f'root = "{bench.root("fed-2")}"'
    assert text.count(first) == 1
    assert text.count(second) == 1
    path.write_text(text.replace(first, "FIRST").replace(second, first).replace("FIRST", second), encoding="utf-8")
    code, printed = bench.run("step")
    assert code == 2
    assert "the fleet's shards.toml is not the one its record gives" in printed
    assert bench.spawned == []


def test_o68_a_moved_shard_resumes_on_a_fresh_federation_whose_first_reset_is_full(bench):
    """O68: shard-1 ran on fed-1, which is taken down: it is released, given to the fresh fed-3 (the same
    fingerprint, [placement] reset_shortcut off), and its step there resumes; shard-2 runs on, untouched."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    assert bench.run("step")[0] == 0
    bench.alive.discard(bench.spawned[0].pid)  # fed-1's job is cancelled: its step and client end
    assert bench.run("down", "--member", "fed-1")[0] == 0
    shard = {s["label"]: s for s in record(bench)["shards"]}["shard-1"]
    assert (shard["member"], shard["last"]) == (None, "fed-1")
    assert bench.run("add", "1")[0] == 0
    code, printed, _ = plan(bench)
    assert code == 0, printed
    assert assigned(bench) == {"shard-1": "fed-3", "shard-2": "fed-2"}
    assert {s["label"]: s for s in record(bench)["shards"]}["shard-1"]["moved"] is True
    code, printed = bench.run("step")
    assert code == 0, printed
    moved = bench.spawned[-1]
    assert (moved.label, moved.argv[-1]) == ("fed-3", "--resume")
    assert "moved from fed-1: fed-3's platform.toml leaves [placement] reset_shortcut off" in printed
    assert len(bench.spawned) == 3  # shard-2's step still runs on fed-2: not launched again


def test_o68_a_shard_is_never_moved_to_a_federation_whose_reset_shortcut_is_on(bench):
    """O68: a moved shard's first reset must be full; fed-3's platform.toml turns the shortcut on: refused. The
    shortcut is no part of the platform fingerprint (C35) but the data provider is (C06): every federation's render
    carries the same complete `[placement]` (O79), and fed-3's differs by the shortcut alone: the fingerprints are one,
    and it is the fleet's own check that refuses the move."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    assert bench.run("down", "--member", "fed-1")[0] == 0
    assert bench.run("add", "1")[0] == 0
    platform = bench.root("fed-3") / "platform.toml"
    text = platform.read_text(encoding="utf-8")
    assert text.count("reset_shortcut = false") == 1  # the render writes every setting at the core's default
    platform.write_text(text.replace("reset_shortcut = false", "reset_shortcut = true"), encoding="utf-8")
    code, printed, _ = plan(bench)
    assert code == 1
    assert "shard-1: not moved to fed-3: its platform.toml turns [placement] reset_shortcut on" in printed
    assert assigned(bench)["shard-1"] is None


def test_o68_step_relaunches_an_ended_shard_only_with_resume(bench):
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    assert bench.run("step")[0] == 0
    bench.alive.discard(bench.spawned[0].pid)
    code, printed = bench.run("step", "--shard", "shard-1")
    assert code == 1
    assert "has ended; `fleet.py step --shard shard-1 --resume` relaunches" in printed
    code, printed = bench.run("step", "--shard", "shard-1", "--resume")
    assert code == 0, printed
    assert bench.spawned[-1].argv[-1] == "--resume"
    assert len(bench.spawned) == 3


# ------------------------------------------------------------------------------------------------ status
def test_o68_status_says_each_federation_its_job_and_shard_and_runs_nothing(bench):
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    assert bench.up(3)[0] == 1
    assert plan(bench)[0] == 1
    assert bench.run("step")[0] == 0
    calls = {label: len(fake.calls) for label, fake in bench.fakes.items()}
    code, printed = bench.run("status")
    assert code == 0
    assert f"fed-1: up and ready (phase linked, job(s) {', '.join(job_ids('fed-1'))}, shard shard-1)" in printed
    assert "fed-2: failed, still HOLDING 2 job(s): it bills until `fleet.py down --member fed-2` (phase " in printed
    assert job_ids("fed-2")[0] in printed
    assert "shard-1: on fed-1, its step runs" in printed
    assert "shard-2: unassigned" in printed
    assert {label: len(fake.calls) for label, fake in bench.fakes.items()} == calls


# ------------------------------------------------------------------------------------------------ the study's scripts
SCRIPT_WORDS = {"--pre-step": "guards/pre_step.py", "--pre-step-sha256": sha(GUARD),
                "--post-run": "guards/post_run.py", "--post-run-sha256": sha(POST)}  # fmt: skip


def test_o63_the_fleets_line_builder_pins_the_studys_guard_and_post_run_script_as_plan_does(bench, deploy):
    """O63: `fleetplan.line_of` made its line from a plan nothing had hashed, so a manifest declaring `pre_step` or
    `post_run` could give no line at all (`step_argv`'s refusal of an unpinned plan escaped as a traceback). It pins
    them as `shards.py plan` and `lines` do: the line carries each script's path and SHA-256 as the study holds it."""
    assert bench.up(2)[0] == 0
    assert plan(bench)[0] == 0
    make_scripts(bench)
    text = (bench.fleet / "shards.toml").read_text(encoding="utf-8")
    head, divider, tail = text.partition("\n\n[[shards]]")
    declared = bench.base / "declared.toml"
    declared.write_text(head + "\n" + "\n".join(DECLARED_SCRIPTS) + divider + tail, encoding="utf-8")
    fleetplan = deploy.fleet.fleetplan
    argv = fleetplan.line_of(fleetplan.shards.load_plan(declared), "shard-1", False)
    assert script_words(argv) == SCRIPT_WORDS


def test_o63_the_shard_settings_take_the_studys_guard_and_post_run_script_and_plan_and_step_pin_them(bench):
    """O63: a fleet's campaign carries the study's own refusals like any other (the settings took none of the
    optional keys but `snapshot_every_s`, so a step it launched could never run the study's guard): `pre_step` and
    `post_run` in the settings are hashed by `plan`, which prints lines carrying them, written to shards.toml for
    `collect`, and by `step`, which launches those lines."""
    assert bench.up(2)[0] == 0
    make_scripts(bench)
    code, printed = bench.run("plan", "--manifest", str(bench.settings(*DECLARED_SCRIPTS)))
    assert code == 0, printed
    lines = {e["shard"]: e for e in map(json.loads, (t for t in printed.splitlines() if t.startswith("{")))}
    for entry in lines.values():
        assert script_words(entry["argv"]) == SCRIPT_WORDS
    manifest = (bench.fleet / "shards.toml").read_text(encoding="utf-8")
    assert 'pre_step = "guards/pre_step.py"' in manifest
    assert 'post_run = "guards/post_run.py"' in manifest
    assert bench.run("step")[0] == 0
    assert [script_words(s.argv) for s in bench.spawned] == [SCRIPT_WORDS] * 2


def test_o63_plan_refuses_a_guard_the_study_does_not_hold_before_it_assigns_anything(bench):
    """O63: a declared script that is not in the study refuses `plan` whole, as it refuses `shards.py plan`: no
    shard is assigned and nothing is written."""
    assert bench.up(2)[0] == 0
    code, printed = bench.run("plan", "--manifest", str(bench.settings('pre_step = "guards/missing.py"')))
    assert code == 2
    assert "guards/missing.py" in printed
    assert all(member is None for member in assigned(bench).values())
    assert not (bench.fleet / "shards.toml").exists()


# ------------------------------------------------------------------------------------------------ pass_env
NAME, VALUE = "FAULT_ACK_MODE", "1"  # an invented operator variable, as a study's sanction takes one
TREE_NAME, TREE = "HPC_STUDY_ARTIFACTS", "/srv/example/artifacts/tree"  # and an invented absolute path
PASS_ENV = f'pass_env = ["{NAME}", "{TREE_NAME}"]'


def operator(bench, **given) -> dict:
    """The operator's environment the fleet runs in (the bench's site) with the declared variables set."""
    return {**bench.environ, NAME: VALUE, TREE_NAME: TREE, **given}


def planned(bench, environ, *settings) -> tuple[int, str, dict]:
    """`fleet.py plan` with these settings in `environ`: its status, what it printed and each shard's entry."""
    code, printed = bench.run("plan", "--manifest", str(bench.settings(*settings)), environ=environ)
    entries = [json.loads(text) for text in printed.splitlines() if text.startswith("{")]
    return code, printed, {entry["shard"]: entry for entry in entries}


def env_words(argv: list) -> dict:
    """The `env -i` words of a step line: the step's whole environment on its node."""
    words = {}
    for word in argv[argv.index("-i") + 1 :]:
        if "=" not in word or word.startswith("-"):
            break
        key, _, value = word.partition("=")
        words[key] = value
    return words


def test_o63_the_shard_settings_take_pass_env_and_plan_pins_each_value_on_every_line(bench, deploy):
    """O63: the fleet's campaign passes the operator's variables to its dispatchers: `plan` reads each declared value
    from the operator's environment, every line carries it as an `env -i` word and `--pass-env NAME`, the entry
    printed shows the values, and shards.toml (which `collect` reads) declares the names; the fake cluster takes each
    line."""
    assert bench.up(2)[0] == 0
    code, printed, lines = planned(bench, operator(bench), PASS_ENV)
    assert code == 0, printed
    for index, label in enumerate(("fed-1", "fed-2"), 1):
        entry = lines[f"shard-{index}"]
        assert entry["pass_env"] == {NAME: VALUE, TREE_NAME: TREE}
        words = env_words(entry["argv"])
        assert (words[NAME], words[TREE_NAME]) == (VALUE, TREE)
        assert [entry["argv"][i + 1] for i, w in enumerate(entry["argv"]) if w == "--pass-env"] == [NAME, TREE_NAME]
        bench.fake(label)._step(entry["argv"])
    manifest = deploy.fleet.fleetplan.shards.load_plan(bench.fleet / "shards.toml")
    assert manifest.pass_env == (NAME, TREE_NAME)
    assert f'pass_env = ["{NAME}", "{TREE_NAME}"]' in (bench.fleet / "shards.toml").read_text(encoding="utf-8")


def test_o63_step_launches_the_lines_pinned_with_the_values_the_operator_holds_when_it_runs(bench):
    """O63: `fleet.py step` pins the declared variables as `plan` does, from its own environment: the lines it spawns
    carry what the operator holds then (here changed since the plan), and no line carries a variable that was not
    declared."""
    assert bench.up(2)[0] == 0
    assert planned(bench, operator(bench), PASS_ENV)[0] == 0
    code, printed = bench.run("step", environ=operator(bench, **{NAME: "2", "NOT_DECLARED_VAR": "x"}))
    assert code == 0, printed
    assert len(bench.spawned) == 2
    for spawned in bench.spawned:
        assert (env_words(spawned.argv)[NAME], env_words(spawned.argv)[TREE_NAME]) == ("2", TREE)
        assert spawned.argv.count("--pass-env") == 2
        assert "NOT_DECLARED_VAR" not in env_words(spawned.argv)


def test_o63_plan_refuses_a_declared_variable_the_operator_has_not_set_and_assigns_nothing(bench):
    """O63: an unset variable refuses `plan` whole, naming it: nothing is printed, assigned or written."""
    assert bench.up(2)[0] == 0
    environ = operator(bench)
    del environ[NAME]
    code, printed, lines = planned(bench, environ, PASS_ENV)
    assert code == 2
    assert f"{NAME} is not set" in printed
    assert lines == {}
    assert all(member is None for member in assigned(bench).values())
    assert record(bench)["template"] is None
    assert not (bench.fleet / "shards.toml").exists()


def test_o63_step_refuses_whole_when_a_declared_variable_is_no_longer_set_and_launches_nothing(bench):
    """O63: the variable is set at `plan` and gone at `step`: no line is made, so no shard is launched."""
    assert bench.up(2)[0] == 0
    assert planned(bench, operator(bench), PASS_ENV)[0] == 0
    environ = operator(bench)
    del environ[TREE_NAME]
    code, printed = bench.run("step", environ=environ)
    assert code == 2
    assert f"{TREE_NAME} is not set" in printed
    assert bench.spawned == []


def needing(bench):
    """The settings' study plugin refuses to be built without the declared variables; the directory in which the
    program that builds it records the environment of each of its calls."""
    bench.needs = (NAME, TREE_NAME)
    return bench.base / "venv"


def test_o63_plan_builds_a_plugin_that_needs_a_declared_variable_with_the_values_its_lines_pin(bench):
    """O63: a study's plugin may refuse to be built without a variable the settings declare in `pass_env`, so the
    campaign fingerprint `plan` takes before it assigns a shard could never be taken. The program that takes it is
    given the declared variables, valued as the lines pin them (from the environment `fleet.py` runs in), once for
    each assigned shard."""
    assert bench.up(2)[0] == 0
    venv = needing(bench)
    code, printed, lines = planned(bench, operator(bench), PASS_ENV)
    assert code == 0, printed
    assert [(env[NAME], env[TREE_NAME]) for env in seen_environments(venv)] == [(VALUE, TREE)] * 2
    assert [entry["pass_env"] for entry in lines.values()] == [{NAME: VALUE, TREE_NAME: TREE}] * 2


def test_o63_plan_gives_the_plugin_no_variable_the_settings_do_not_declare(bench):
    """O63: nothing else of the operator's environment, nor of the site's, reaches the program that builds the
    plugin."""
    assert bench.up(2)[0] == 0
    venv = needing(bench)
    code, printed, _ = planned(bench, operator(bench, NOT_DECLARED_VAR="x", FAULT_API_KEY="y"), PASS_ENV)
    assert code == 0, printed
    taken = seen_environments(venv)
    assert len(taken) == 2
    for env in taken:
        assert not [name for name in ("NOT_DECLARED_VAR", "FAULT_API_KEY", "HPC_FLEET_MAX") if name in env]


def test_o63_plan_refuses_an_unset_declared_variable_before_it_builds_the_plugin_naming_only_the_variable(bench):
    """O63: the values are read before any program is run: a declared variable the operator does not hold refuses the
    whole `plan`, naming it and showing no value, with nothing assigned and the plugin never built."""
    assert bench.up(2)[0] == 0
    venv = needing(bench)
    environ = operator(bench)
    del environ[TREE_NAME]
    code, printed, lines = planned(bench, environ, PASS_ENV)
    assert code == 2
    assert f"{TREE_NAME} is not set" in printed
    assert TREE not in printed
    assert lines == {}
    assert seen_environments(venv) == []
    assert all(member is None for member in assigned(bench).values())


@pytest.mark.parametrize(
    ("setting", "says"),
    [
        ('pass_env = ["FAULT_API_KEY"]', "looks like a secret"),
        ('pass_env = ["PATH"]', "sets itself"),
        ('pass_env = ["EXPCORE_FED_ROOT"]', "sets itself"),
        ('pass_env = ["lower_case"]', "must match"),
        ('pass_env = "FAULT_ACK_MODE"', "must be a list"),
        (f'pass_env = ["{NAME}", "{NAME}"]', "twice"),
    ],
)
def test_o63_the_settings_refuse_a_declaration_shards_py_would_refuse_and_nothing_is_made(bench, setting, says):
    """O63: the names are checked by `shards.py`'s own rule when the settings are read: a secret-looking name, one
    the step sets itself, a bad one, a string and a repeat refuse `plan` before anything is assigned or written."""
    assert bench.up(2)[0] == 0
    code, printed, _ = planned(bench, operator(bench, FAULT_API_KEY="x"), setting)
    assert code == 2
    assert says in printed
    assert record(bench)["template"] is None
    assert not (bench.fleet / "shards.toml").exists()


@pytest.mark.parametrize("value", ["go ahead", "/srv/../etc", "relative/dir", "/srv/my dir", "/srv/$HOME"])
def test_o63_plan_refuses_a_value_that_is_neither_a_plain_word_nor_a_plain_path_never_echoing_it(bench, value):
    """O63: the value rule is `shards.py`'s: a space, a climbing path, a relative path or a shell character refuses
    `plan`, naming the variable and never the value."""
    assert bench.up(2)[0] == 0
    code, printed, _ = planned(bench, operator(bench, **{TREE_NAME: value}), PASS_ENV)
    assert code == 2
    assert f"{TREE_NAME}'s value does not match" in printed
    assert value not in printed
    assert not (bench.fleet / "shards.toml").exists()


def test_o63_a_relaunch_with_resume_carries_the_pins_too(bench):
    """O63: `step --resume` on a shard whose step ended pins the declared variables like any launch."""
    assert bench.up(2)[0] == 0
    assert planned(bench, operator(bench), PASS_ENV)[0] == 0
    assert bench.run("step", environ=operator(bench))[0] == 0
    bench.alive.discard(bench.spawned[0].pid)
    code, printed = bench.run("step", "--shard", "shard-1", "--resume", environ=operator(bench))
    assert code == 0, printed
    assert (bench.spawned[-1].argv[-1], env_words(bench.spawned[-1].argv)[NAME]) == ("--resume", VALUE)
    assert bench.spawned[-1].argv.count("--pass-env") == 2


# -- a shard's variables keep their values from its first step: a later fleet step is refused a changed one ----------
FIRST, LATER = "go-ahead", "changed-mode"  # the value the first step passed, and one the operator holds later


def first_steps(bench, deploy, *values):
    """Each shard's first step's record of the values it passed (`values[i]` for shard i + 1), as the step's own
    writer makes it."""
    shards = deploy.fleet.fleetplan.shards
    for index, passed in enumerate(values, 1):
        spec = {"index": index, "count": 2, "label": f"shard-{index}"}
        shards.record_pass_env_origin(bench.base / "results", spec, passed)


def launched_and_ended(bench):
    """Both shards' first launch made, as the fleet's record has it, and both steps ended."""
    assert bench.up(2)[0] == 0
    assert planned(bench, operator(bench, **{NAME: FIRST}), PASS_ENV)[0] == 0
    assert bench.run("step", environ=operator(bench, **{NAME: FIRST}))[0] == 0
    for spawned in bench.spawned:
        bench.alive.discard(spawned.pid)


def test_o63_a_later_fleet_step_whose_value_changed_since_the_first_step_is_refused_and_launches_nothing(bench, deploy):
    """O63: `fleet.py step --resume`, the operator holding another value of a declared variable than the shard's
    first step passed: refused whole before anything is launched (status 2), naming the variable and showing neither
    value (nor the variable that did not change)."""
    launched_and_ended(bench)
    first_steps(bench, deploy, *[{NAME: FIRST, TREE_NAME: TREE}] * 2)
    spawned = len(bench.spawned)
    code, printed = bench.run("step", "--resume", environ=operator(bench, **{NAME: LATER}))
    assert code == 2
    assert NAME in printed
    assert FIRST not in printed
    assert LATER not in printed
    assert TREE_NAME not in printed
    assert len(bench.spawned) == spawned


def test_o63_a_later_fleet_step_with_the_values_of_the_first_step_launches_with_resume(bench, deploy):
    """O63: the same values again: both shards are launched with `--resume`, carrying the first step's values."""
    launched_and_ended(bench)
    first_steps(bench, deploy, *[{NAME: FIRST, TREE_NAME: TREE}] * 2)
    spawned = len(bench.spawned)
    code, printed = bench.run("step", "--resume", environ=operator(bench, **{NAME: FIRST}))
    assert code == 0, printed
    assert len(bench.spawned) == spawned + 2
    assert all(env_words(each.argv)[NAME] == FIRST for each in bench.spawned[spawned:])


def test_o63_a_first_fleet_step_has_no_origin_to_differ_from(bench):
    """O63: with no first step recorded, the first launch is not compared with anything and takes the operator's values
    as they are."""
    assert bench.up(2)[0] == 0
    assert planned(bench, operator(bench, **{NAME: FIRST}), PASS_ENV)[0] == 0
    assert bench.run("step", environ=operator(bench, **{NAME: LATER}))[0] == 0
    assert all(env_words(each.argv)[NAME] == LATER for each in bench.spawned)


def test_o63_a_variable_added_to_a_shard_that_has_results_and_no_origin_is_refused_whole(bench):
    """O63: shard 1 has a snapshot in the results (a first step that passed nothing) and no origin; the settings
    declare a variable now: `step --resume` refuses the whole command, naming the variable and showing neither value,
    and launches nothing."""
    launched_and_ended(bench)
    (bench.base / "results" / "shard-1" / "snapshots" / "snap-000001").mkdir(parents=True)
    spawned = len(bench.spawned)
    code, printed = bench.run("step", "--resume", environ=operator(bench, **{NAME: FIRST}))
    assert code == 2
    assert NAME in printed
    assert FIRST not in printed
    assert len(bench.spawned) == spawned


def test_o63_each_shard_is_compared_with_its_own_first_step(bench, deploy):
    """O63: the origin is the shard's own, in the results directory the shards share (not a federation's): shard 1's
    first step passed the value the operator holds now and shard 2's another, so `step --resume` of shard 1 launches
    and that of shard 2 is refused, naming the variable and showing neither value."""
    launched_and_ended(bench)
    first_steps(bench, deploy, {NAME: LATER, TREE_NAME: TREE}, {NAME: FIRST, TREE_NAME: TREE})
    spawned = len(bench.spawned)
    code, printed = bench.run("step", "--shard", "shard-2", "--resume", environ=operator(bench, **{NAME: LATER}))
    assert (code, len(bench.spawned)) == (2, spawned)
    assert NAME in printed
    assert LATER not in printed
    assert FIRST not in printed
    code, printed = bench.run("step", "--shard", "shard-1", "--resume", environ=operator(bench, **{NAME: LATER}))
    assert code == 0, printed
    assert len(bench.spawned) == spawned + 1


def test_o63_plan_after_a_first_step_still_prints_the_lines_it_always_printed(bench, deploy):
    """O63: `plan` launches nothing and prints first-step lines, so it is not compared with the origin: it prints the
    operator's values as it did; the refusal is at the step that would launch."""
    launched_and_ended(bench)
    first_steps(bench, deploy, *[{NAME: FIRST, TREE_NAME: TREE}] * 2)
    code, printed, lines = planned(bench, operator(bench, **{NAME: LATER}), PASS_ENV)
    assert code == 0, printed
    assert lines["shard-1"]["pass_env"][NAME] == LATER


# ------------------------------------------------------------------------------------------------ one campaign (O63)
def test_o63_plan_takes_each_assigned_shards_campaign_fingerprint_beside_its_own_federations_platform_file(bench):
    """O63: three federations of one platform fingerprint: each shard's campaign fingerprint is taken by the manifest's
    Python beside its own federation's platform.toml, all three equal, and every line printed."""
    assert bench.up(3)[0] == 0
    code, printed, lines = plan(bench)
    assert code == 0, printed
    assert sorted(lines) == ["shard-1", "shard-2", "shard-3"]
    files = [bench.root(label) / "platform.toml" for label in ("fed-1", "fed-2", "fed-3")]
    assert seen_platforms(bench.base / "venv") == [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]


def test_o63_plan_refuses_federations_whose_campaign_fingerprints_differ_though_their_platform_fingerprints_do_not(
    bench,
):
    """O63: fed-3 renders a setting only the study's plugin fingerprints (the platform's own fingerprint leaves it out,
    so O68's check passes): the campaign fingerprints differ, and the plan names the shard and the key, assigns
    nothing, writes nothing and prints no line, before any step is launched."""
    assert bench.up(3)[0] == 0
    with (bench.root("fed-3") / "platform.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n# study-setting: another\n")
    assert bench.fingerprint("fed-3") == bench.fingerprint("fed-1")  # the platform's half is one
    code, printed, lines = plan(bench)
    assert code == 2
    assert "shard-3 differs from shard-1, shard-2 (first differing key: study_setting)" in printed
    assert str(bench.base) not in printed
    assert lines == {}
    assert all(member is None for member in assigned(bench).values())
    assert record(bench)["reference"] is None
    assert not (bench.fleet / "shards.toml").exists()
    assert bench.spawned == []


def test_o63_a_shard_no_federation_holds_is_not_part_of_the_campaign_check(bench):
    """O63: fed-2's up failed and shard-2 is unassigned: its federation's file is not read, shard-1 and shard-3 are
    compared and planned as before."""
    bench.data["fed-2"] = data_stand_in(differ="alpha-space")
    assert bench.up(3)[0] == 1
    code, _, lines = plan(bench)
    assert code == 1
    assert sorted(lines) == ["shard-1", "shard-3"]
    assert len(seen_platforms(bench.base / "venv")) == 2

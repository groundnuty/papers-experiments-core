"""A study directory: the four files loaded and checked against each other, and the plugin module found and checked."""

import dataclasses
import shutil
from pathlib import Path

import pytest

from expcore.config.loading import ConfigError
from expcore.plugin import ToolSpec
from expcore.study.config import find_study_root, load_study
from expcore.study.pipeline import fingerprint_of
from expcore.study.plugin import StudyContext, load_plugin, tools_sha256

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"


@pytest.fixture
def study_dir(tmp_path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return copy


def edit(study: Path, name: str, old: str, new: str) -> None:
    path = study / name
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def refused(study: Path, *fragments: str) -> str:
    with pytest.raises(ConfigError) as caught:
        load_study(study)
    for fragment in fragments:
        assert fragment in str(caught.value)
    return str(caught.value)


def test_the_example_loads_with_its_tasks_conditions_models_and_plan(study_dir):
    study = load_study(study_dir)
    assert study.name == "tiny-study"
    assert list(study.tasks) == ["lake", "river", "hill", "dune"]
    assert dict(study.tasks["hill"].attributes) == {"region": "south"}
    assert list(study.schema.conditions) == ["baseline", "extended", "relay-down"]
    assert study.schema.treatment_kinds["relay-down"] == ("knowledge",)
    assert list(study.models) == ["alpha-stub", "beta-stub"]
    assert len(study.manifest.rows) == 22  # (4 + 3 + 4 tasks) x 2 models x 1 seed
    assert set(study.config_sha256) == {"study.toml", "models.toml", "campaign.toml", "preregistration.toml"}
    assert study.prereg.holm_family == ("extended-vs-baseline", "relay-vs-baseline")


def test_the_manifest_order_depends_on_the_order_seed_alone(study_dir):
    first = [row.key.text for row in load_study(study_dir).manifest.rows]
    assert first == [row.key.text for row in load_study(study_dir).manifest.rows]
    edit(study_dir, "campaign.toml", "order_seed = 20260101", "order_seed = 5")
    assert first != [row.key.text for row in load_study(study_dir).manifest.rows]


def test_a_study_is_found_from_a_directory_below_it_and_not_from_outside_one(study_dir, tmp_path):
    (study_dir / "out" / "runs").mkdir(parents=True)
    assert find_study_root(study_dir / "out" / "runs") == study_dir.resolve()
    with pytest.raises(ConfigError, match=r"no study\.toml"):
        find_study_root(tmp_path / "elsewhere")


def test_a_model_of_the_plan_that_the_model_table_lacks_is_refused_naming_it(study_dir):
    edit(study_dir, "campaign.toml", '"beta-stub"]', '"gamma-stub"]')
    refused(study_dir, "campaign.toml", "'models'", "gamma-stub", "models.toml")


def test_p06_a_model_of_the_plan_the_table_lacks_is_refused_listing_the_table_with_the_closest_name(study_dir):
    """P06: the plan names a model by its key in the one table; a misspelt key is refused with the keys it could be."""
    edit(study_dir, "campaign.toml", '"beta-stub"]', '"beta-stbu"]')
    text = refused(study_dir, "campaign.toml")
    assert text.endswith(
        "key 'models' names 'beta-stbu' (did you mean 'beta-stub'?), which models.toml does not define; it defines: "
        "alpha-stub, beta-stub"
    )


WINDOW = "context_tokens = 8192\n"


def test_c25_a_budgeted_study_without_a_context_window_loads_and_only_its_run_is_refused(study_dir):
    """C25: the context window bounds what a run is reserved, so loading the study for an analysis, a report or a
    package's check never needs it; the check a run makes refuses a planned model that declares none, naming
    models.toml, the key and what to add, and lets a free model through."""
    models = (study_dir / "models.toml").read_text(encoding="utf-8")
    assert models.count(WINDOW) == 2  # the example declares one for each of its models
    edit(study_dir, "models.toml", "[models.beta-stub]\nbackend = \"stub\"\nstatus = \"frozen\"\n" + WINDOW,
         "[models.beta-stub]\nbackend = \"stub\"\nstatus = \"frozen\"\n")  # fmt: skip
    study = load_study(study_dir)
    assert study.campaign.scheduler.budget is not None
    with pytest.raises(ConfigError) as caught:
        study.models.require_context_window(study.campaign.models)
    text = str(caught.value)
    assert text.startswith(f"{study_dir.resolve() / 'models.toml'}: no context window for model(s) 'beta-stub':")
    assert "max_usd" in text
    assert "context_tokens" in text
    edit(study_dir, "models.toml", "\n[models.beta-stub.price]\ninput_per_mtok = 0.5\noutput_per_mtok = 1.5\n",
         "\nfree = true\n")  # fmt: skip
    free = load_study(study_dir)
    free.models.require_context_window(free.campaign.models)  # a free model costs nothing past its cap


def test_a_cluster_that_a_task_lacks_is_refused_naming_the_contrast_and_the_task(study_dir):
    edit(study_dir, "study.toml", '[tasks.dune]\nregion = "south"', '[tasks.dune]\nzone = "south"')
    refused(study_dir, "preregistration.toml", "extended-vs-baseline", "'region'", "'dune'")


PLACES = (("lake", "north"), ("river", "north"), ("hill", "south"), ("dune", "south"))
TASKS = "".join(f'[tasks.{task}]\nregion = "{region}"\n\n' for task, region in PLACES).rstrip("\n") + "\n"


@pytest.mark.parametrize(
    ("old", "new", "fragments"),
    [
        ('plugin = "plugin.py"', 'plugin = "gone.py"', ("[study]", "plugin", "gone.py")),
        ('plugin = "plugin.py"', 'plugin = "../plugin.py"', ("[study]", "plugin", "inside the study directory")),
        ('"plugin.py", "prompt.txt"]', '"plugin.py", "nothing.txt"]', ("[study]", "code", "nothing.txt")),
        ('code = ["study.toml",', 'code = ["/etc/hosts", "study.toml",', ("[study]", "code", "relative path")),
        ('name = "tiny-study"\n', "", ("[study]", "name", "is required")),
        ('plugin = "plugin.py"', 'plugin = "plugin.py"\nrequires = ["toy-platform>=2"]', ("[study]", "requires")),
        ('plugin = "plugin.py"', 'plugin = "plugin.py"\nrequires = ["toy", "toy"]', ("[study]", "requires", "once")),
        (TASKS, "", ("tasks", "at least one task")),
        ('[tasks.lake]\nregion = "north"', "[tasks.lake]\nregion = 4", ("[tasks.lake]", "region", "non-empty string")),
    ],
)
def test_a_study_table_or_task_that_cannot_work_is_refused_naming_the_file_and_the_key(study_dir, old, new, fragments):
    edit(study_dir, "study.toml", old, new)
    refused(study_dir, "study.toml", *fragments)


# --- the plugin --------------------------------------------------------------------------------------------------


def context(study_dir: Path) -> StudyContext:
    return StudyContext(load_study(study_dir), {"commit": "a" * 40})


def test_the_plugin_module_is_loaded_from_its_file_and_covers_every_condition(study_dir):
    plugin = load_plugin(context(study_dir))
    assert [s.name for s in plugin.scorers] == ["tiny"]
    assert set(plugin.prompts) == set(plugin.tools) == {"baseline", "extended", "relay-down"}
    assert plugin.tools["baseline"] == plugin.tools["relay-down"] != plugin.tools["extended"]  # a level, a tool set
    assert len(plugin.scorer_digest()) == 64
    assert plugin.lease_group({}) == "stub-platform"
    assert list(plugin.lease_keys("stub-platform")) == ["slot-1", "slot-2"]


def test_a_module_without_build_or_with_a_wrong_return_is_refused_naming_the_file(study_dir):
    (study_dir / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"plugin\.py: must define a function build"):
        load_plugin(context(study_dir))
    (study_dir / "plugin.py").write_text("def build(context):\n    return 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="returned int, not a StudyPlugin"):
        load_plugin(context(study_dir))


def test_a_module_that_fails_on_import_is_refused_naming_the_file_and_the_error(study_dir):
    (study_dir / "plugin.py").write_text("raise ZeroDivisionError('boom')\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"plugin\.py: failed on import \(ZeroDivisionError: boom\)"):
        load_plugin(context(study_dir))


def test_r12_the_import_failure_of_a_plugin_shows_its_text_with_every_secret_redacted(study_dir, monkeypatch):
    """R12: the exception's text helps the author, but a client that failed to start can carry a credential in it."""
    monkeypatch.setenv("TOYLAB_ACCESS_TOKEN", "amber-falcon-6620")
    (study_dir / "plugin.py").write_text(
        "import os\nraise RuntimeError('login refused for ' + os.environ['TOYLAB_ACCESS_TOKEN'])\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert "failed on import (RuntimeError: login refused for [redacted])" in str(caught.value)
    assert "amber-falcon-6620" not in str(caught.value)


def test_r12_the_import_failure_redacts_the_keys_models_toml_points_at_in_a_variable_or_a_file(study_dir, monkeypatch):
    """R12: a model's key is redacted from a record whatever its variable is called, so from this message too: the
    value of its `key_env` and the contents of its `key_file`."""
    monkeypatch.setenv("TOYLAB_GAMMA", "gamma-secret-4471")  # a name no secret pattern matches: only the table knows
    key_file = study_dir.parent / "delta.key"
    key_file.write_text("delta-secret-9930\n", encoding="utf-8")
    remote = (
        '\n[models.gamma-remote]\nbackend = "openai_compat"\napi_id = "gamma-1"\n'
        'base_url = "https://llm.example.invalid/v1"\nkey_env = "TOYLAB_GAMMA"\n'
        '\n[models.delta-remote]\nbackend = "anthropic"\napi_id = "delta-1"\n'
        f'base_url = "https://api.example.invalid"\nkey_file = "{key_file}"\n'
    )
    with (study_dir / "models.toml").open("a", encoding="utf-8") as handle:
        handle.write(remote)
    (study_dir / "plugin.py").write_text(
        "import os, pathlib\n"
        f"raise RuntimeError(os.environ['TOYLAB_GAMMA'] + ' ' + pathlib.Path({str(key_file)!r}).read_text().strip())\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert "failed on import (RuntimeError: [redacted] [redacted])" in str(caught.value)
    log = (study_dir / "out" / "logs" / "harness-errors.log").read_text(encoding="utf-8")
    for value in ("gamma-secret-4471", "delta-secret-9930"):
        assert value not in str(caught.value)
        assert value not in log


def test_r12_a_short_secret_is_redacted_from_an_import_failure_where_it_stands_alone_and_nowhere_else(
    study_dir, monkeypatch
):
    """R12: a secret too short to redact everywhere is still a secret: it is redacted where it stands as a token of its
    own, in the message and in the log, while the same letters inside a longer name are left."""
    monkeypatch.setenv("TEST_TOKEN", "lib")
    (study_dir / "plugin.py").write_text("raise ValueError('lib')\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert "failed on import (ValueError: [redacted])" in str(caught.value)
    log = (study_dir / "out" / "logs" / "harness-errors.log").read_text(encoding="utf-8")
    assert "ValueError: [redacted]" in log
    assert "'lib'" not in log
    (study_dir / "plugin.py").write_text("import needed_library\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert "No module named 'needed_library'" in str(caught.value)  # inside a name, the letters are no secret


def test_l08_an_import_failure_keeps_its_diagnostic_and_logs_its_traceback_whatever_short_value_is_set(
    study_dir, monkeypatch
):
    """L08: a secret too short to redact everywhere is redacted only where it stands alone, so an unrelated one does
    not wipe the message; the traceback is logged."""
    monkeypatch.setenv("OTHER_TOKEN", "abc")
    (study_dir / "plugin.py").write_text("import needed_library\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    log = study_dir / "out" / "logs" / "harness-errors.log"
    assert "failed on import (ModuleNotFoundError: No module named 'needed_library')" in str(caught.value)
    assert str(caught.value).endswith(f"; its traceback, redacted, is in {log}")
    text = log.read_text(encoding="utf-8")
    assert "importing plugin.py: ModuleNotFoundError" in text
    assert "Traceback (most recent call last)" in text


STUB_PLUGIN = """\
from expcore.study.plugin import StudyPlugin


class Scorer:
    name = "s"
    version = "1"

    def score(self, record):
        return {}


def build(context):
    hashes = {c: "0" * 64 for c in context.study.schema.conditions}
    return StudyPlugin(executor=None, scorers=SCORERS, prompts=PROMPTS or hashes, tools=hashes)
"""


@pytest.mark.parametrize(
    ("scorers", "prompts", "fragment"),
    [
        ("[Scorer(), Scorer()]", "None", "names must differ"),
        ("[]", "None", "at least one scorer"),
        ("[Scorer()]", '{"baseline": "0" * 64}', "prompts has no entry for condition 'extended'"),
    ],
)
def test_a_plugin_that_repeats_a_scorer_or_leaves_a_condition_out_is_refused(study_dir, scorers, prompts, fragment):
    source = STUB_PLUGIN.replace("SCORERS", scorers).replace("PROMPTS", prompts)
    (study_dir / "plugin.py").write_text(source, encoding="utf-8")
    with pytest.raises(ConfigError, match=fragment):
        load_plugin(context(study_dir))


def test_c06_the_plugins_platform_settings_reach_the_campaign_fingerprint(study_dir):
    """C06: what the platform says makes two starts one experiment reaches the fingerprint through the plugin's
    `platform`; the example gives none, which leaves the fingerprint as it was, and a value that is no mapping is
    refused when the plugin is loaded, naming the file."""
    study, plugin = load_study(study_dir), load_plugin(context(study_dir))
    assert plugin.platform == {}
    assert "platform" not in fingerprint_of(study, plugin)
    given = dataclasses.replace(plugin, platform={"tls": {"mode": "insecure"}})
    assert fingerprint_of(study, given)["platform"] == {"tls": {"mode": "insecure"}}
    source = STUB_PLUGIN.replace("SCORERS", "[Scorer()]").replace("PROMPTS", "None")
    source = source.replace("tools=hashes)", 'tools=hashes, platform=["tls"])')
    (study_dir / "plugin.py").write_text(source, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"plugin\.py: the plugin's platform must be a mapping"):
        load_plugin(context(study_dir))


def test_c35_the_plugins_reset_settings_are_no_part_of_the_fingerprint_and_a_bad_value_is_refused_at_load(study_dir):
    """C35: the settings the platform keeps out of the fingerprint ride on the plugin as `reset_settings`, empty by
    default; they change no fingerprint, and a value that is no flat mapping of plain data is refused when the plugin
    is loaded, naming the file and the key."""
    study, plugin = load_study(study_dir), load_plugin(context(study_dir))
    assert plugin.reset_settings == {}
    given = dataclasses.replace(plugin, reset_settings={"placement.qos_scan": "listing"})
    assert fingerprint_of(study, given) == fingerprint_of(study, plugin)
    for bad, fragment in (
        ('["placement.qos_scan"]', r"the plugin's reset_settings must be a mapping"),
        ('{"placement.x": {"nested": 1}}', r"the plugin's reset_settings: .*placement\.x"),
    ):
        source = STUB_PLUGIN.replace("SCORERS", "[Scorer()]").replace("PROMPTS", "None")
        source = source.replace("tools=hashes)", f"tools=hashes, reset_settings={bad})")
        (study_dir / "plugin.py").write_text(source, encoding="utf-8")
        with pytest.raises(ConfigError, match=rf"plugin\.py: {fragment}"):
            load_plugin(context(study_dir))


def test_a_tool_set_hashes_by_what_the_model_sees_and_not_by_the_order_it_was_listed_in():
    a = ToolSpec("a", "first")
    b = ToolSpec("b", "second", terminal=True)
    assert tools_sha256([a, b]) == tools_sha256([b, a])
    assert tools_sha256([a, b]) != tools_sha256([a, ToolSpec("b", "second")])  # terminal is part of it

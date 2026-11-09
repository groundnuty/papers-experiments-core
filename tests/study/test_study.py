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


def test_a_tool_set_hashes_by_what_the_model_sees_and_not_by_the_order_it_was_listed_in():
    a = ToolSpec("a", "first")
    b = ToolSpec("b", "second", terminal=True)
    assert tools_sha256([a, b]) == tools_sha256([b, a])
    assert tools_sha256([a, b]) != tools_sha256([a, ToolSpec("b", "second")])  # terminal is part of it

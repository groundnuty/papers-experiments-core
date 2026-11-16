"""C11: the core asks each platform a study lists for its declared deferral, from the platform's own configuration.

`[study] platforms` names the modules the core imports before it validates anything. A module may also expose
`declared_deferral_from_config(path)`, which answers the longest the platform's health check may stay deferred
(`DeferralBound` or None) from the platform configuration the study points at (`[study] platform_config`, by default
the `platform.toml` beside study.toml). The pipeline passes the answers to the scheduler, which holds the health check
to them, so a study whose plugin wraps both its health check and its run admission is still refused.
"""

import dataclasses
import shutil
import uuid
from pathlib import Path

import pytest
from study_support import EXAMPLE, HEAD

from expcore.campaign.healthgate import DeferralBound, PlatformDeferral
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import run_study
from expcore.study.platform_deferral import PLATFORM_HOOK, platform_deferrals

PLUGIN_LINE = 'plugin = "plugin.py"\n'
HOOKED = f'''
import tomllib

from expcore.campaign.healthgate import DeferralBound


def {PLATFORM_HOOK}(path):
    """The toy platform's declaration: the figure its configuration holds, as the platform would compute it."""
    seconds = tomllib.loads(path.read_text(encoding="utf-8"))["deferral_s"]
    return DeferralBound(float(seconds), "the toy platform's heal: lower heal_timeout_s", rechecks=0, checks=0)
'''
UNHOOKED = "# a platform that registers nothing and declares nothing\n"
# What a study's plugin adds to wrap the platform's health check, and its run admission, in objects of its own.
WRAPPING = '''

import contextlib as _contextlib
import dataclasses as _dataclasses

_build_before_wrapping = build


class _Carrying:
    """A wrapper that passes `max_deferral_s` on, as the study guide asks."""

    def __init__(self, check, bound):
        self.check, self.bound = check, bound

    def __call__(self):
        return self.check()

    def max_deferral_s(self):
        return self.bound


def build(context):
    plugin = _build_before_wrapping(context)
    inner = plugin.health

    def hidden():  # a closure: nothing of the platform's health check is visible through it
        return inner()

    def admission(treatment_kinds, treatment_targets, cancel, exclusive):  # no declared_deferral_s either
        return _contextlib.nullcontext("shared")

    health = _CARRY(inner) if _CARRY is not None else hidden
    return _dataclasses.replace(plugin, health=health, admission=admission)
'''


def platform_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> str:
    """Write a platform module that exposes (or not) the hook, importable by a name no other test uses."""
    name = f"toy_deferral_platform_{uuid.uuid4().hex[:12]}"
    folder = tmp_path / "site"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(folder))
    return name


def study_with(tmp_path: Path, *modules: str, platform: str | None = "deferral_s = 100\n", key: str = "") -> Path:
    """A copy of the example listing `modules`, with the platform configuration `platform` beside study.toml."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    toml = copy / "study.toml"
    listed = ", ".join(f'"{m}"' for m in modules)
    extra = (f"platforms = [{listed}]\n" if modules else "") + key
    toml.write_text(toml.read_text(encoding="utf-8").replace(PLUGIN_LINE, PLUGIN_LINE + extra, 1), encoding="utf-8")
    if platform is not None:
        (copy / "platform.toml").write_text(platform, encoding="utf-8")
    return copy


def refused(study: Path) -> str:
    with pytest.raises(ConfigError) as caught:
        load_study(study)
    return str(caught.value)


def test_c11_a_study_points_at_its_platform_configuration_and_by_default_at_the_platform_toml_beside_it(tmp_path):
    """C11: `[study] platform_config` names a file of the study directory; without it a `platform.toml` beside
    study.toml is the one, and a study with neither points at none."""
    named = study_with(tmp_path / "named", platform=None, key='platform_config = "settings/federation.toml"\n')
    (named / "settings").mkdir()
    (named / "settings" / "federation.toml").write_text("deferral_s = 7\n", encoding="utf-8")
    default = study_with(tmp_path / "default")
    neither = study_with(tmp_path / "neither", platform=None)
    assert load_study(named).platform_config == (named / "settings" / "federation.toml").resolve()
    assert load_study(default).platform_config == (default / "platform.toml").resolve()
    assert load_study(neither).platform_config is None


def test_c11_the_key_wins_over_the_default_beside_study_toml(tmp_path):
    """C11: a named file is the platform's configuration even where a `platform.toml` lies beside study.toml."""
    study = study_with(tmp_path, key='platform_config = "other.toml"\n')
    (study / "other.toml").write_text("deferral_s = 7\n", encoding="utf-8")
    assert load_study(study).platform_config == (study / "other.toml").resolve()


@pytest.mark.parametrize(
    ("value", "words"),
    [
        ('"../outside.toml"', "must name a file inside the study directory; '../outside.toml' is not one"),
        ('"/etc/hostname"', "must name a file inside the study directory; '/etc/hostname' is not one"),
        ("3", "must be a non-empty string, got"),
    ],
    ids=["outside", "absolute", "number"],
)
def test_c11_a_platform_configuration_outside_the_study_directory_is_refused_naming_the_key(tmp_path, value, words):
    """C11: the pointer is checked when the study is loaded, naming study.toml and `platform_config`: a path outside the
    study directory, absolute or through `..`, and a value that is no text."""
    (tmp_path / "outside.toml").write_text("deferral_s = 7\n", encoding="utf-8")
    study = study_with(tmp_path / "s", key=f"platform_config = {value}\n")
    text = refused(study)
    assert "platform_config" in text, text
    assert words in text, text


def test_c11_a_platform_that_exposes_the_hook_is_asked_with_the_studys_platform_configuration(tmp_path, monkeypatch):
    """C11: the answer is the platform's own, computed from the file the study points at (no study code between), and
    named for the platform module that gave it."""
    module = platform_module(tmp_path, monkeypatch, HOOKED)
    study = load_study(study_with(tmp_path / "s", module, platform="deferral_s = 4321\n"))
    declared = platform_deferrals(study)
    assert declared == (
        PlatformDeferral(module, DeferralBound(4321.0, "the toy platform's heal: lower heal_timeout_s")),
    )


def test_c11_a_platform_without_the_hook_and_one_the_study_does_not_list_declare_nothing(tmp_path, monkeypatch):
    """C11: the hook is optional per platform, so a listed platform without it is unaffected, and a platform that has
    one but is not in `[study] platforms` is never asked."""
    plain = platform_module(tmp_path / "a", monkeypatch, UNHOOKED)
    unlisted = platform_module(tmp_path / "b", monkeypatch, HOOKED + "\nraise SystemExit('never imported')\n")
    study = load_study(study_with(tmp_path / "s", plain))
    assert platform_deferrals(study) == ()
    assert unlisted not in study.platforms
    assert platform_deferrals(load_study(study_with(tmp_path / "t"))) == ()  # no platform at all


def test_c11_a_named_platform_configuration_that_is_a_link_out_of_the_study_directory_is_refused_at_load(tmp_path):
    """C11: the file the core hands to a platform is inside the study directory whichever way it is named: a link under
    a named key that leaves the directory is refused when the study loads."""
    outside = tmp_path / "outside.toml"
    outside.write_text("deferral_s = 7\n", encoding="utf-8")
    named = study_with(tmp_path / "named", platform=None, key='platform_config = "link.toml"\n')
    (named / "link.toml").symlink_to(outside)
    text = refused(named)
    assert "platform_config" in text, text
    assert "inside the study directory" in text, text


def test_c11_an_implicit_platform_toml_that_links_out_loads_and_is_refused_only_where_a_platform_reads_it(
    tmp_path, monkeypatch
):
    """C11: nothing read the default platform.toml before, so a study that links a shared one in still loads, and
    the platform is never asked to read what lies outside the study: a listed platform with the function is refused,
    naming the key, and one without the function is not asked."""
    outside = tmp_path / "outside.toml"
    outside.write_text("deferral_s = 7\n", encoding="utf-8")
    calls = tmp_path / "calls"
    hooked = platform_module(
        tmp_path / "a",
        monkeypatch,
        f"def {PLATFORM_HOOK}(path):\n    open({str(calls)!r}, 'a').write(str(path))\n    return None\n",
    )
    plain = platform_module(tmp_path / "b", monkeypatch, UNHOOKED)
    for number, module in enumerate((hooked, plain)):
        study = study_with(tmp_path / f"s{number}", module, platform=None)
        (study / "platform.toml").symlink_to(outside)
        loaded = load_study(study)
        assert loaded.platform_config == outside.resolve()
        if module == plain:
            assert platform_deferrals(loaded) == ()
            continue
        with pytest.raises(ConfigError, match="inside the study directory") as caught:
            platform_deferrals(loaded)
        assert "[study] platform_config" in str(caught.value)
    assert not calls.exists()


def test_c11_a_study_built_in_code_with_a_platform_configuration_outside_the_directory_is_refused_unread(
    tmp_path, monkeypatch
):
    """C11: `check_study` takes a `StudyConfig` built in code as it is, so the pipeline's own read of the platform
    refuses a file outside the study directory (and a link out of it) before it calls the platform's function."""
    calls = tmp_path / "calls"
    module = platform_module(
        tmp_path,
        monkeypatch,
        f"def {PLATFORM_HOOK}(path):\n    open({str(calls)!r}, 'a').write(str(path))\n    return None\n",
    )
    study = load_study(study_with(tmp_path / "s", module))
    outside = tmp_path / "outside.toml"
    outside.write_text("deferral_s = 7\n", encoding="utf-8")
    link = study.root / "link.toml"
    link.symlink_to(outside)
    for path in (outside, link):
        with pytest.raises(ConfigError) as caught:
            platform_deferrals(dataclasses.replace(study, platform_config=path))
        for part in ("[study] platform_config", "inside the study directory", module):
            assert part in str(caught.value), caught.value
    assert not calls.exists()  # the platform was never asked to read a file outside the study


def test_c11_a_named_file_that_is_not_there_loads_and_is_refused_only_where_a_platform_reads_it(tmp_path, monkeypatch):
    """C11: a replication package ships the study without its platform.toml and still loads, so the pointer is a path
    inside the study directory and the file's being there is checked when a listed platform declares from it: the
    refusal names the key and the platform; a platform with no hook reads nothing and asks for nothing."""
    hooked = platform_module(tmp_path / "a", monkeypatch, HOOKED)
    plain = platform_module(tmp_path / "b", monkeypatch, UNHOOKED)
    key = 'platform_config = "missing.toml"\n'
    study = load_study(study_with(tmp_path / "s", hooked, platform=None, key=key))
    assert study.platform_config == (study.root / "missing.toml").resolve()
    with pytest.raises(ConfigError) as caught:
        platform_deferrals(study)
    for part in ("[study] platform_config", "'missing.toml'", "not a file", hooked):
        assert part in str(caught.value), caught.value
    assert platform_deferrals(load_study(study_with(tmp_path / "t", plain, platform=None, key=key))) == ()


def test_c11_a_platform_that_declares_none_is_kept_in_the_answer_as_none(tmp_path, monkeypatch):
    """C11: a hook that answers None (a heal off, no `[faults]`) is a platform that declares nothing: the answer holds
    it with no bound, so the scheduler asks nothing of the health check."""
    module = platform_module(tmp_path, monkeypatch, f"def {PLATFORM_HOOK}(path):\n    return None\n")
    assert platform_deferrals(load_study(study_with(tmp_path / "s", module))) == (PlatformDeferral(module, None),)


def test_c11_a_listed_platform_with_the_hook_and_no_configuration_to_read_is_refused_naming_the_key(
    tmp_path, monkeypatch
):
    """C11: the study lists a platform that declares its deferral from its configuration but points at none, so the
    guard would be skipped by leaving the pointer out: refused when the core asks, naming the platform and the key."""
    module = platform_module(tmp_path, monkeypatch, HOOKED)
    study = load_study(study_with(tmp_path / "s", module, platform=None))
    with pytest.raises(ConfigError) as caught:
        platform_deferrals(study)
    text = str(caught.value)
    for part in (module, "[study] platform_config", "platform.toml"):
        assert part in text, text


def test_c11_a_hook_that_refuses_its_configuration_refuses_the_start_with_its_message(tmp_path, monkeypatch):
    """C11: what the platform finds wrong in its own configuration (a margin too small for its restore, a key it does
    not know) reaches the study as the platform worded it."""
    module = platform_module(
        tmp_path,
        monkeypatch,
        "from expcore.config.loading import ConfigError\n"
        f"def {PLATFORM_HOOK}(path):\n    raise ConfigError(f'{{path.name}}: [tokens] margin_s is too small')\n",
    )
    with pytest.raises(ConfigError, match=r"platform.toml: \[tokens\] margin_s is too small"):
        platform_deferrals(load_study(study_with(tmp_path / "s", module)))


def test_c11_a_hook_that_fails_in_any_other_way_names_the_platform_and_the_type_and_never_the_text(
    tmp_path, monkeypatch
):
    """C11: an exception of the platform's own code may hold a credential in its text, so the refusal names the
    platform and the exception's type only, as an import that fails does."""
    module = platform_module(
        tmp_path, monkeypatch, f"def {PLATFORM_HOOK}(path):\n    raise RuntimeError('token-0123456789abcdef')\n"
    )
    with pytest.raises(ConfigError) as caught:
        platform_deferrals(load_study(study_with(tmp_path / "s", module)))
    text = str(caught.value)
    for part in (module, "RuntimeError", PLATFORM_HOOK):
        assert part in text, text
    assert "token-0123456789abcdef" not in text, text


@pytest.mark.parametrize(
    "answer",
    ["5000.0", "'an hour'", "DeferralBound(-1.0, 'a heal')", "DeferralBound(float('nan'), 'a heal')", "(1.0, 'x')"],
    ids=["number", "text", "negative", "nan", "tuple"],
)
def test_c11_an_answer_that_is_not_a_deferral_bound_is_refused_naming_the_hook(tmp_path, monkeypatch, answer):
    """C11: what a platform declares crosses into the core as what a check's bound does: anything but None or a
    `DeferralBound` of a finite, non-negative number of seconds, a detail and non-negative whole counts is refused."""
    source = f"from expcore.campaign.healthgate import DeferralBound\ndef {PLATFORM_HOOK}(path):\n    return {answer}\n"
    module = platform_module(tmp_path, monkeypatch, source)
    with pytest.raises(ConfigError, match=PLATFORM_HOOK) as caught:
        platform_deferrals(load_study(study_with(tmp_path / "s", module)))
    assert module in str(caught.value)


def test_c11_a_hook_that_is_not_callable_is_refused(tmp_path, monkeypatch):
    """C11: a platform whose `declared_deferral_from_config` is not a function is a mistake, never a platform that
    declares nothing."""
    module = platform_module(tmp_path, monkeypatch, f"{PLATFORM_HOOK} = 5\n")
    with pytest.raises(ConfigError, match=f"{PLATFORM_HOOK} that is not a function"):
        platform_deferrals(load_study(study_with(tmp_path / "s", module)))


# --- the pipeline: what a run of the study does with the declaration -------------------------------------------------


def stamped(study: Path, plugin_addition: str = "", campaign_edit: tuple[str, str] | None = None) -> Path:
    """Add to the plugin, edit campaign.toml, and stamp the copy as a snapshot made without .git is."""
    if plugin_addition:
        plugin = study / "plugin.py"
        plugin.write_text(plugin.read_text(encoding="utf-8") + plugin_addition, encoding="utf-8")
    if campaign_edit is not None:
        campaign = study / "campaign.toml"
        text = campaign.read_text(encoding="utf-8")
        assert campaign_edit[0] in text
        campaign.write_text(text.replace(*campaign_edit, 1), encoding="utf-8")
    write_stamp(study, HEAD, load_study(study).scope)
    return study


def carrying(addition: str, carry: bool) -> str:
    """The plugin's wrapping, with the health check wrapped in a closure, or in one that passes `max_deferral_s` on."""
    carry_with = "lambda inner: _Carrying(inner, _DECLARED)" if carry else "None"
    return (
        "\n_DECLARED = __import__('expcore.campaign.healthgate', fromlist=['x']).DeferralBound("
        '4000.0, "the toy platform\'s heal: lower heal_timeout_s", rechecks=0, checks=0)\n'
        f"_CARRY = {carry_with}\n" + addition
    )


def test_c11_a_study_wrapping_both_its_health_check_and_its_admission_is_refused_before_anything_runs(
    tmp_path, monkeypatch
):
    """C11: the study's plugin wraps the health check in a closure and the admission in a function of its own, so the
    platform's deferral reaches the scheduler through neither; the platform's configuration, read by the core through
    `[study] platforms`, still declares 4,000 s and the run is refused when the scheduler is built, before its store
    is opened, naming the platform, the missing method and the figure."""
    module = platform_module(tmp_path, monkeypatch, HOOKED)
    study = stamped(
        study_with(tmp_path / "s", module, platform="deferral_s = 4000\n"),
        carrying(WRAPPING, carry=False),
        ("lease_timeout_s = 30", "lease_timeout_s = 30\nmax_defer_s = 5000"),
    )
    with pytest.raises(ConfigError) as caught:
        run_study(study)
    text = str(caught.value)
    for part in (
        "the health check passed to the scheduler doesn't expose max_deferral_s",
        f"the platform {module!r} (its configuration, read through [study] platforms) declares a deferral of 4000 s",
        "pass the method on",
    ):
        assert part in text, text
    assert not (study / "out" / "state.sqlite").exists()


def test_c11_a_study_whose_health_check_passes_the_method_on_runs_and_is_held_to_max_defer_s(tmp_path, monkeypatch):
    """C11: an unwrapped study starts: the health check carries the 4,000 s the platform's configuration declares, and
    a `max_defer_s` of 3,600 s (the default) is refused naming the figure, where one of 5,000 s runs the campaign."""
    module = platform_module(tmp_path, monkeypatch, HOOKED)
    short = stamped(study_with(tmp_path / "short", module, platform="deferral_s = 4000\n"), carrying(WRAPPING, True))
    with pytest.raises(ConfigError, match="raise max_defer_s to at least 4000"):
        run_study(short)
    assert not (short / "out" / "state.sqlite").exists()
    enough = stamped(
        study_with(tmp_path / "enough", module, platform="deferral_s = 4000\n"),
        carrying(WRAPPING, True),
        ("lease_timeout_s = 30", "lease_timeout_s = 30\nmax_defer_s = 4000"),
    )
    summary = run_study(enough)
    assert set(summary.statuses) == {"done"}


def test_c11_a_study_listing_no_hooked_platform_runs_as_before(tmp_path, monkeypatch):
    """C11: a platform without the hook declares nothing: the study, with the same wrappers, starts and runs."""
    module = platform_module(tmp_path, monkeypatch, UNHOOKED)
    study = stamped(study_with(tmp_path / "s", module), carrying(WRAPPING, carry=False))
    assert set(run_study(study).statuses) == {"done"}

"""A model backend of the study's own: named in models.toml, built by the plugin's factory, with no edit to the core."""

import json
import shutil
import uuid

import pytest
from study_support import EXAMPLE, HEAD, clean_environment, cli

from expcore.campaign.resume import FingerprintMismatch
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.records.redact import REDACTED
from expcore.replay.check import CHECK_FILE, what_records_need
from expcore.study.config import load_study
from expcore.study.pipeline import HASHED_ONLY, fingerprint_of, run_study
from expcore.study.plugin import StudyContext, load_plugin

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

TOY_BACKEND = "toy_relay"

# What a study adds to its plugin to serve a backend of its own: a backend class and a factory in the plugin's build.
RELAY = '''

# --- a backend the core does not know, served by the plugin's own factory ----------------------------------------

import dataclasses as _dataclasses

from expcore.campaign.executor import default_backend_factory as _default_factory
from expcore.models.factory import build_backend as _build_backend


class RelayBackend:
    """Relays every call to a stub it holds; it is no StubAdapter, so nothing may tell the two apart by type."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


def relay_factory(spec, *, seed, limits, base_url, time_left):
    """Serve the study's own backend and leave every other model to the core's factory."""
    if spec.backend != "toy_relay":
        return _default_factory(spec, seed=seed, limits=limits, base_url=base_url, time_left=time_left)
    as_stub = _dataclasses.replace(spec, backend="stub", key_env=None)  # a stub reads no key
    return RelayBackend(_build_backend(as_stub, seed=seed, limits=limits, time_left=time_left))


relay_factory.version = "1"
_build_without_factory = build


def build(context):
    return _dataclasses.replace(_build_without_factory(context), backend_factory=relay_factory)
'''


def study_copy(tmp_path, *, factory: bool, extra_entry: str = ""):
    """The example with alpha-stub moved to the study's own backend, and the plugin's factory when asked."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    models = copy / "models.toml"
    entry = '[models.alpha-stub]\nbackend = "stub"\n'
    assert entry in models.read_text(encoding="utf-8")
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            entry, f'[models.alpha-stub]\nbackend = "{TOY_BACKEND}"\napi_id = "alpha-stub"\n{extra_entry}'
        ),
        encoding="utf-8",
    )
    if factory:
        plugin = copy / "plugin.py"
        plugin.write_text(plugin.read_text(encoding="utf-8") + RELAY, encoding="utf-8")
    return copy


def test_b16_a_backend_the_core_does_not_know_is_refused_at_load_unless_the_plugin_supplies_a_factory(tmp_path):
    """B16: a model whose backend the core cannot build is refused before anything runs, naming models.toml and the
    key, unless the plugin's factory serves it; the factory, not the core, decides which models it serves."""
    bare = study_copy(tmp_path / "bare", factory=False)
    study = load_study(bare)
    assert study.models.resolve("alpha-stub").backend == TOY_BACKEND
    with pytest.raises(ConfigError) as caught:
        load_plugin(StudyContext(study, {"commit": HEAD}))
    message = str(caught.value)
    assert message.startswith(f"{bare / 'models.toml'}: [models.alpha-stub]: key 'backend'")
    assert repr(TOY_BACKEND) in message
    assert "backend_factory" in message
    served = study_copy(tmp_path / "served", factory=True)
    plugin = load_plugin(StudyContext(load_study(served), {"commit": HEAD}))
    assert plugin.backend_factory is not None
    assert plugin.backend_factory.__name__ == "relay_factory"


def test_c14_a_toy_backend_wrapping_the_stub_runs_the_example_study_through_the_plugin_s_factory(
    tmp_path, clean_environment
):
    """C14: the scheduler builds every backend, for admission and for each run, with the factory the plugin supplies:
    the example study runs to the end on a backend the core does not know, from configuration and the factory alone.
    The example's executor checks for the method it needs, not for the stub's class."""
    copy = study_copy(tmp_path, factory=True)
    write_stamp(copy, HEAD, load_study(copy).scope)
    summary = run_study(copy)
    planned = len(load_study(copy).manifest.rows)
    assert summary.statuses == {"done": planned}, summary.statuses
    assert summary.dispatched >= planned


def edit_plugin(study, old: str, new: str) -> None:
    """Change the plugin and stamp the study again, as a commit of the change would."""
    plugin = study / "plugin.py"
    text = plugin.read_text(encoding="utf-8")
    assert old in text, old
    plugin.write_text(text.replace(old, new), encoding="utf-8")
    shutil.rmtree(study / "__pycache__", ignore_errors=True)  # a same-size edit within a second keeps the cached code
    write_stamp(study, HEAD, load_study(study).scope)


def test_c06_a_resume_under_another_backend_factory_or_another_version_of_it_is_refused(tmp_path, clean_environment):
    """C06: the backend builds every request of a run, so a campaign started with one factory never resumes under
    another: its name and its version are in the fingerprint, and the same factory at the same version resumes."""
    copy = study_copy(tmp_path, factory=True)
    write_stamp(copy, HEAD, load_study(copy).scope)
    run_study(copy)
    edit_plugin(copy, 'relay_factory.version = "1"', 'relay_factory.version = "2"')
    with pytest.raises(FingerprintMismatch, match=r"backend_factory\.version: recorded '1', now '2'"):
        run_study(copy)
    edit_plugin(copy, 'relay_factory.version = "2"', 'relay_factory.version = "1"')
    assert run_study(copy).dispatched == 0  # nothing left to run, and not refused
    edit_plugin(copy, "relay_factory", "other_factory")
    with pytest.raises(
        FingerprintMismatch, match=r"backend_factory\.name: recorded 'relay_factory', now 'other_factory'"
    ):
        run_study(copy)


def test_c06_only_a_supplied_factory_is_in_the_fingerprint_and_its_identity_holds_no_path(tmp_path):
    """C06: a study without a factory keeps the fingerprint it had; one with a factory in its plugin has the same
    identity wherever the study directory is, because the module name of a plugin holds the path."""
    plain = study_copy(tmp_path / "plain", factory=False)
    shutil.copy(EXAMPLE / "models.toml", plain / "models.toml")
    study = load_study(plain)
    assert "backend_factory" not in fingerprint_of(study, load_plugin(StudyContext(study, {"commit": HEAD})))
    identities = []
    for name in ("here", "there"):
        study = load_study(study_copy(tmp_path / name, factory=True))
        identities.append(fingerprint_of(study, load_plugin(StudyContext(study, {"commit": HEAD})))["backend_factory"])
    assert identities == [{"name": "relay_factory", "version": "1"}] * 2


@pytest.mark.parametrize("replacement", ['relay_factory.version = "  "', "relay_factory.version = 1", "pass"])
def test_c06_a_backend_factory_without_a_version_is_refused_when_the_plugin_is_loaded(tmp_path, replacement):
    """C06: a factory whose behaviour can change without a version would make the fingerprint say nothing, so a plugin
    that supplies one must version it; the refusal names the plugin file and the attribute."""
    copy = study_copy(tmp_path, factory=True)
    plugin = copy / "plugin.py"
    text = plugin.read_text(encoding="utf-8")
    plugin.write_text(text.replace('relay_factory.version = "1"', replacement), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"plugin\.py: the plugin's backend_factory needs a 'version' attribute"):
        load_plugin(StudyContext(load_study(copy), {"commit": HEAD}))


CREDENTIAL_VARIABLE = "TOY_RELAY_CREDENTIAL"  # not a name the environment's own redaction matches
CREDENTIAL = "toy-relay-credential-value-0123456789"
LEAKING_TOOL = '''

import os as _os

_lookup_without_the_key = lookup


def lookup(arguments, *, timeout_s, ctx):
    """A careless tool: it echoes the backend's key in its result."""
    plain = _lookup_without_the_key(arguments, timeout_s=timeout_s, ctx=ctx)
    return _dataclasses.replace(plain, text=f"{plain.text} {_os.environ['TOY_RELAY_CREDENTIAL']}")
'''


def test_r12_the_key_of_a_backend_without_a_spec_that_a_tool_echoes_never_reaches_a_record(
    tmp_path, clean_environment, monkeypatch
):
    """R12: the protocol does not require a backend to hold its model entry, so a run is recorded with the study's
    whole model table: a tool that echoes the key_env value of a custom backend leaves it out of every written file."""
    monkeypatch.setenv(CREDENTIAL_VARIABLE, CREDENTIAL)
    copy = study_copy(tmp_path, factory=True, extra_entry=f'key_env = "{CREDENTIAL_VARIABLE}"\n')
    plugin = copy / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + LEAKING_TOOL, encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    summary = run_study(copy)
    assert summary.statuses == {"done": len(load_study(copy).manifest.rows)}, summary.statuses
    written = [path.read_text(encoding="utf-8") for path in (copy / "out" / "runs").rglob("*.json")]
    assert any(REDACTED in text for text in written), "the tool's result was not recorded as redacted"
    assert not any(CREDENTIAL in text for text in written)


OWN_TRANSCRIPT_NAME = """

from expcore.replay.native import openai_neutral as _openai_neutral, register_native_parser as _register_native_parser

_register_native_parser("toy_relay", _openai_neutral)


def _info_under_its_own_name(self):
    return {**self.inner.info(), "backend": "toy_relay"}


RelayBackend.info = _info_under_its_own_name
"""


def test_r03_a_check_in_a_fresh_process_finds_the_native_parser_a_custom_backend_s_plugin_registers(
    tmp_path, clean_environment
):
    """R03: a custom backend names its own transcript format and its plugin module registers the parser for it, so
    `check`, which replays the records and may be the first thing a new process does, imports that module first, and
    says in one line that it is about to run the study's code and for which backend."""
    copy = study_copy(tmp_path, factory=True)
    plugin = copy / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + OWN_TRANSCRIPT_NAME, encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    assert run_study(copy).statuses == {"done": len(load_study(copy).manifest.rows)}
    code, stderr = cli(clean_environment, "check", str(copy / "out" / "runs"), process=True)
    assert code == 0, "the fresh process could not replay the records of the custom backend"
    notices = [line for line in stderr.splitlines() if IMPORT_NOTICE in line]
    assert len(notices) == 1, stderr
    assert repr(TOY_BACKEND) in notices[0]
    check = json.loads((copy / "out" / "runs" / CHECK_FILE).read_text(encoding="utf-8"))
    assert check["failures"] == []
    assert check["n_ok"] == check["n_records"] > 0


IMPORT_NOTICE = "importing the study's platforms and plugin"
REFUSE_VARIABLE = "TOY_REFUSE_IMPORT"
IMPORT_CREDENTIAL = "toy-import-credential-0123456789"
REFUSE_AT_IMPORT = f"""

import os as _os_of_the_gate

if _os_of_the_gate.environ.get("{REFUSE_VARIABLE}"):
    raise RuntimeError("login refused for {IMPORT_CREDENTIAL}")
"""


def gated_platform(tmp_path, monkeypatch) -> tuple[str, str]:
    """A platform module that imports in this process and raises at import in one that sets the variable."""
    folder = tmp_path / "site"
    folder.mkdir()
    module = f"toy_gated_platform_{uuid.uuid4().hex[:12]}"
    (folder / f"{module}.py").write_text(REFUSE_AT_IMPORT, encoding="utf-8")
    monkeypatch.syspath_prepend(str(folder))
    return module, str(folder)


def list_platform(study, module: str) -> None:
    toml = study / "study.toml"
    line = 'plugin = "plugin.py"\n'
    text = toml.read_text(encoding="utf-8")
    assert line in text
    toml.write_text(text.replace(line, f'{line}platforms = ["{module}"]\n', 1), encoding="utf-8")


def append_to_plugin(study, source: str) -> None:
    plugin = study / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + source, encoding="utf-8")


def test_r03_a_batch_of_core_backend_records_checks_clean_without_importing_anything_of_the_study(
    tmp_path, clean_environment, monkeypatch
):
    """R03: every record names a backend the core can read, so `check` runs none of the study's code: a plugin and a
    listed platform that raise at import, as a study from elsewhere or a missing dependency would, change nothing."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    module, folder = gated_platform(tmp_path, monkeypatch)
    list_platform(copy, module)
    append_to_plugin(copy, REFUSE_AT_IMPORT)
    write_stamp(copy, HEAD, load_study(copy).scope)
    assert run_study(copy).statuses == {"done": len(load_study(copy).manifest.rows)}
    env = {**clean_environment, REFUSE_VARIABLE: "1", "PYTHONPATH": folder}
    code, stderr = cli(env, "check", str(copy / "out" / "runs"), process=True)
    assert code == 0, stderr
    assert IMPORT_NOTICE not in stderr
    check = json.loads((copy / "out" / "runs" / CHECK_FILE).read_text(encoding="utf-8"))
    assert check["failures"] == []
    assert check["n_ok"] == check["n_records"] > 0


def test_r03_only_what_this_process_cannot_read_asks_for_the_study_s_code(tmp_path):
    """R03: the scan reads the backend and the infrastructure kinds each replayable record names and nothing else, so a
    file the check will report as broken, a field of the wrong type, a core name or a marker that is only hashed asks
    for nothing; a kind may be named by the condition's treatment, the outcome or the failed request."""
    batch = tmp_path / "batch"
    batch.mkdir()
    files = {
        "broken.json": "{not json",
        "array.json": "[1]",
        "no-route.json": '{"route": 3}',
        "no-backend.json": '{"route": {}}',
        "odd-backend.json": '{"route": {"backend": 3}}',
        "core.json": '{"route": {"backend": "stub"}, "treatment_kinds": ["knowledge"], "outcome": {}}',
        "unhashable.json": '{"route": {"backend": []}, "treatment_kinds": [[]], "outcome": {"infra_kind": {}}}',
        "own.json": '{"route": {"backend": "toy_unread"}}',
        "treated.json": '{"treatment_kinds": ["toy_treated", 3]}',
        "ended.json": '{"outcome": {"infra_kind": "toy_ended"}, "failed_request": {"infra_kind": "toy_failed"}}',
        "odd-kinds.json": '{"treatment_kinds": "toy_not_a_list", "outcome": 3}',
        "x-attempt-marker.json": '{"route": {"backend": "toy_marker"}, "treatment_kinds": ["toy_marker"]}',
    }
    for name, body in files.items():
        (batch / name).write_text(body, encoding="utf-8")
    assert what_records_need(batch, HASHED_ONLY) == (["toy_unread"], ["toy_ended", "toy_failed", "toy_treated"])


@pytest.mark.parametrize("gated", ["plugin", "platform"])
def test_r03_a_custom_backend_batch_whose_study_code_cannot_be_imported_is_refused_by_module_and_exception_type_only(
    tmp_path, clean_environment, monkeypatch, gated
):
    """R03: the records need the study's code and it will not import, so `check` refuses them; the text of the
    exception can hold a credential, so the refusal names only the module and the exception's type."""
    copy = study_copy(tmp_path, factory=True)
    module, folder = gated_platform(tmp_path, monkeypatch)
    append_to_plugin(copy, OWN_TRANSCRIPT_NAME)
    if gated == "plugin":
        append_to_plugin(copy, REFUSE_AT_IMPORT)
        expected = "plugin.py: failed on import (RuntimeError)"
    else:
        list_platform(copy, module)
        expected = f"key 'platforms' names module {module!r}, which cannot be imported (RuntimeError)"
    write_stamp(copy, HEAD, load_study(copy).scope)
    run_study(copy)
    env = {**clean_environment, REFUSE_VARIABLE: "1", "PYTHONPATH": folder}
    code, stderr = cli(env, "check", str(copy / "out" / "runs"), process=True)
    assert code != 0
    assert IMPORT_NOTICE in stderr  # said before the import that failed
    assert expected in stderr
    assert IMPORT_CREDENTIAL not in stderr
    assert "Traceback" not in stderr


def test_r03_the_check_imports_the_plugin_module_without_needing_a_model_s_key(
    tmp_path, clean_environment, monkeypatch
):
    """R03: importing the module registers the parser and reads no key, so a machine that replays without the study's
    credentials can still check it, even for a backend whose entry names a key_env that is not set here."""
    monkeypatch.setenv(CREDENTIAL_VARIABLE, CREDENTIAL)
    copy = study_copy(tmp_path, factory=True, extra_entry=f'key_env = "{CREDENTIAL_VARIABLE}"\n')
    plugin = copy / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + OWN_TRANSCRIPT_NAME, encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    run_study(copy)
    assert CREDENTIAL_VARIABLE not in clean_environment  # the snapshot of the environment the new process gets
    code, _ = cli(clean_environment, "check", str(copy / "out" / "runs"), process=True)
    assert code == 0, "the check needed the model's key"

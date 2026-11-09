"""A model backend of the study's own: named in models.toml, built by the plugin's factory, with no edit to the core."""

import shutil

import pytest
from study_support import EXAMPLE, HEAD, clean_environment

from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import run_study
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
    stub = _build_backend(_dataclasses.replace(spec, backend="stub"), seed=seed, limits=limits, time_left=time_left)
    return RelayBackend(stub)


_build_without_factory = build


def build(context):
    return _dataclasses.replace(_build_without_factory(context), backend_factory=relay_factory)
'''


def study_copy(tmp_path, *, factory: bool):
    """The example with alpha-stub moved to the study's own backend, and the plugin's factory when asked."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    models = copy / "models.toml"
    entry = '[models.alpha-stub]\nbackend = "stub"\n'
    assert entry in models.read_text(encoding="utf-8")
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            entry, f'[models.alpha-stub]\nbackend = "{TOY_BACKEND}"\napi_id = "alpha-stub"\n'
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

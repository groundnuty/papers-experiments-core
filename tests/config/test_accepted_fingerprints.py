"""A model's declared set of accepted serving fingerprints (B18): how `models.toml` declares it, the rules it is held
to, and how it enters the campaign fingerprint, leaving a model that declares none with the digest it had (C06)."""

import shutil
import textwrap
from pathlib import Path

import pytest
from campaign_support import NOW

from expcore.campaign.manifest import compare_fingerprints
from expcore.campaign.resume import FingerprintMismatch, open_campaign
from expcore.config.loading import ConfigError
from expcore.config.models import ModelSpec, RuleViolation, load_models
from expcore.study.config import load_study
from expcore.study.pipeline import fingerprint_of, model_digest
from expcore.study.plugin import StudyContext, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
RELAY = """
[models.relay]
backend = "openai_compat"
api_id = "vendor/relay-large"
base_url = "https://llm.example.invalid/v1"
status = "frozen"
"""


def table(tmp_path, extra: str, base: str = RELAY):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "models.toml"
    path.write_text(textwrap.dedent(base) + textwrap.dedent(extra), encoding="utf-8")
    return load_models(path)


def test_b18_a_model_declares_its_accepted_fingerprints_and_their_order_does_not_matter(tmp_path):
    """B18: `accepted_fingerprints` is read as a set: the entry keeps it sorted, so two files that list the same
    fingerprints in another order describe the same model."""
    spec = table(tmp_path, 'accepted_fingerprints = ["deploy-plover", "deploy-kestrel"]\n').resolve("relay")
    assert spec.accepted_fingerprints == ("deploy-kestrel", "deploy-plover")
    again = ModelSpec(
        key="relay",
        backend="stub",
        api_id="relay",
        status="frozen",
        accepted_fingerprints=["deploy-plover", "deploy-kestrel"],
    )
    assert again.accepted_fingerprints == ("deploy-kestrel", "deploy-plover")
    assert table(tmp_path / "none", "").resolve("relay").accepted_fingerprints == ()


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        ('accepted_fingerprints = ["deploy-kestrel", "deploy-kestrel"]\n', "lists 'deploy-kestrel' twice"),
        ('accepted_fingerprints = ["deploy-kestrel | deploy-plover"]\n', "several fingerprints joined"),
        ("accepted_fingerprints = []\n", "at least one fingerprint"),
        ('accepted_fingerprints = "deploy-kestrel"\n', "accepted_fingerprints' must be an array of non-empty strings"),
    ],
)
def test_b18_a_set_that_cannot_be_meant_is_refused_naming_the_file_and_the_key(tmp_path, extra, fragment):
    """B18: a repeated fingerprint, one that is really several, an empty list or a value that is no list is refused at
    load, naming the file and the key."""
    with pytest.raises(ConfigError, match=fragment) as caught:
        table(tmp_path, extra)
    assert str(caught.value).startswith(f"{tmp_path / 'models.toml'}: [models.relay]")


def test_b18_an_anthropic_model_cannot_declare_accepted_fingerprints(tmp_path):
    """B18: the anthropic adapter records no system fingerprint, so a set declared for it could never be checked."""
    with pytest.raises(RuleViolation, match="records no system fingerprint"):
        ModelSpec(
            key="quillmark",
            backend="anthropic",
            api_id="vendor-large",
            status="frozen",
            key_env="TEST_VENDOR_KEY",
            accepted_fingerprints=("deploy-kestrel",),
        )


def test_b18_a_model_that_declares_no_set_keeps_the_digest_it_had_in_the_fingerprint():
    """B18, C06: the digest of a model entry without `accepted_fingerprints` is byte for byte the one it had before
    the key existed, so a campaign started before it resumes; one that declares a set has another."""
    spec = ModelSpec(
        key="gamma-relay",
        backend="openai_compat",
        api_id="gamma-relay",
        status="frozen",
        base_url="https://llm.example.invalid/v1",
        free=True,
    )  # no key location: where a key lives enters as a placeholder (K13), which gave a keyed entry a new digest
    assert model_digest(spec) == "675005d213c3f888d5e23a42967d5709cf0e0f3d5845470e6264877807031aca"
    declared = ModelSpec(**{**spec.__dict__, "accepted_fingerprints": ("deploy-kestrel",)})
    assert model_digest(declared) != model_digest(spec)


def _declared(study_dir: Path, accepted: str):
    """The example study with `accepted` added to alpha-stub's entry, and its campaign fingerprint."""
    shutil.copytree(EXAMPLE, study_dir)
    models = study_dir / "models.toml"
    entry = '[models.alpha-stub]\nbackend = "stub"\n'
    models.write_text(models.read_text(encoding="utf-8").replace(entry, f"{entry}{accepted}\n"), encoding="utf-8")
    study = load_study(study_dir)
    return study, fingerprint_of(study, load_plugin(StudyContext(study, {"commit": "0" * 40})))


def test_c06_a_resume_under_another_accepted_set_is_refused(tmp_path):
    """C06, B18: the set is frozen into the campaign fingerprint through the model's digest, so a start that declares
    another set is refused as a changed model, never taken as a grown plan."""
    study, first = _declared(tmp_path / "one", 'accepted_fingerprints = ["deploy-kestrel", "deploy-plover"]')
    _, second = _declared(tmp_path / "two", 'accepted_fingerprints = ["deploy-kestrel", "deploy-osprey"]')
    assert first["models"]["beta-stub"] == second["models"]["beta-stub"]
    comparison = compare_fingerprints(first, second)
    assert any(leaf.startswith("models.alpha-stub:") for leaf in comparison.refused)
    assert not comparison.grown
    path = tmp_path / "state.sqlite"
    store, _ = open_campaign(path, study.schema, study.manifest, first, now=NOW, max_attempts=3)
    store.close()
    with pytest.raises(FingerprintMismatch, match=r"models\.alpha-stub"):
        open_campaign(path, study.schema, study.manifest, second, now=NOW, max_attempts=3)

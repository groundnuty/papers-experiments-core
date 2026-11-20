"""A model's declared set of accepted serving fingerprints (B18): how `models.toml` declares it, the rules it is held
to, and that it stays out of each model's digest in the campaign fingerprint (C48), so a model keeps the digest it had
before the key existed (C06)."""

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


def test_c48_a_models_digest_is_the_same_whatever_set_it_declares():
    """C48, C06: the digest of a model entry without `accepted_fingerprints` is byte for byte the one it had before
    the key existed, so a campaign started before it resumes, and one that declares a set has that same digest:
    deployments of one checkpoint are interchangeable, and the set lives in the elastic ledger's declaration log."""
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
    assert model_digest(declared) == model_digest(spec)


def _declared(study_dir: Path, accepted: str):
    """The example study with `accepted` added to alpha-stub's entry, and its campaign fingerprint."""
    shutil.copytree(EXAMPLE, study_dir)
    models = study_dir / "models.toml"
    entry = '[models.alpha-stub]\nbackend = "stub"\n'
    models.write_text(models.read_text(encoding="utf-8").replace(entry, f"{entry}{accepted}\n"), encoding="utf-8")
    study = load_study(study_dir)
    return study, fingerprint_of(study, load_plugin(StudyContext(study, {"commit": "0" * 40})))


def test_c48_another_accepted_set_leaves_the_models_digest_and_only_models_toml_s_hash_names_it(tmp_path):
    """C06, C48: a start whose models.toml declares another set has the same digest for every model; the file's own
    hash still differs, so a hand-run campaign resumed under an edited models.toml is refused as a changed
    configuration (an elastic campaign changes its set with `declare`, never by editing the file)."""
    study, first = _declared(tmp_path / "one", 'accepted_fingerprints = ["deploy-kestrel", "deploy-plover"]')
    _, second = _declared(tmp_path / "two", 'accepted_fingerprints = ["deploy-kestrel", "deploy-osprey"]')
    assert first["models"] == second["models"]
    comparison = compare_fingerprints(first, second)
    assert comparison.refused
    assert all(leaf.startswith("configs.models.toml") for leaf in comparison.refused)
    assert not comparison.grown
    path = tmp_path / "state.sqlite"
    store, _ = open_campaign(path, study.schema, study.manifest, first, now=NOW, max_attempts=3)
    store.close()
    with pytest.raises(FingerprintMismatch, match=r"configs\.models\.toml"):
        open_campaign(path, study.schema, study.manifest, second, now=NOW, max_attempts=3)

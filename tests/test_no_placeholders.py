"""O12: no neutral replacement of the sanitising import is left in code; each became a lookup in platform.toml.

The sanitising import replaces a study's names with neutral stand-ins (the patterns below, which are public). A stand-in
left in a module under src/ would be a federation name written in code under another spelling, so none may remain.
Fixtures under tests/ may use them.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# What the map writes in place of a name: the forms in the plan, with both separators where an identifier would spell
# the same stand-in with an underscore, and the numbered stand-ins that survived the first import.
PATTERNS = {
    "example.org": r"example\.org",
    ".invalid": r"\.invalid\b",
    "provider-x": r"\bprovider[-_][a-z]\b",
    "study-space": r"study[-_]space",
    "slot-user-": r"slot[-_]user[-_]",
    "user-N": r"\buser[-_][0-9]",
    "host-N": r"\bhost[-_][0-9]",
    "192.0.2.": r"192\.0\.2\.",
    "STUDY_VAR_": r"STUDY_VAR_",
    "federation-N": r"\bfederation[-_][0-9]",
    "site-N": r"\bsite[-_][0-9]",
    "dataset-N": r"\bdataset[-_][0-9]+",
}
COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in PATTERNS.items()}


def findings(text: str) -> list[str]:
    """Return the names of the patterns that occur in the text."""
    return sorted(name for name, pattern in COMPILED.items() if pattern.search(text))


def source_files() -> list[Path]:
    """Every file under src/ that is text, whatever its extension."""
    return sorted(p for p in SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_o12_no_file_under_src_holds_a_replacement_pattern():
    """O12: every placeholder the import left was turned into a lookup in the loaded configuration."""
    found = {}
    for path in source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if hits := findings(text):
            found[str(path.relative_to(ROOT))] = hits
    assert not found, f"replacement patterns left in code: {found}"


def test_o12_the_scan_covers_the_platform_layer_and_every_kind_of_file():
    names = {p.name for p in source_files()}
    assert {"config.py", "tokens.py", "toolserver.py", "health.py", "client.py"} <= names


@pytest.mark.parametrize(
    "planted",
    [
        'ZONE = "zone.example.org"',
        "host: node.invalid",
        'name = "provider-a"',
        "provider_e",
        "study-space-prefix",
        "slot-user-3",
        "user-12",
        "host-5.local",
        "192.0.2.14",
        'os.environ["STUDY_VAR_57"]',
        "federation-2",
        "site-9",
        "dataset_76",
        "# a comment naming Example.Org",
    ],
)
def test_o12_the_scan_notices_each_pattern_wherever_it_is_planted(planted):
    assert findings(planted)


@pytest.mark.parametrize(
    "ordinary",
    ["the provider declared", "providers[name]", "provider_id", "user_id", "hostname", "dataset of the study", "sites"],
)
def test_o12_ordinary_words_are_not_findings(ordinary):
    assert findings(ordinary) == []

"""Paths and layouts the tests of deploy/compose-federation share."""

from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "compose-federation"
EXAMPLE = DEPLOY / "layouts" / "five-providers.toml"

# A small layout of the tests' own: two providers, one space with one mirror, one member.
MINIMAL = """\
project = "expcore-test"
domain = "expcore-test.test"
study_space = "alpha-space"

[zone]
port = 29643

[providers.oak]
port = 29644

[providers.elm]
port = 29645

[spaces.alpha-space]
owner = "oak"
mirrors = ["elm"]

[principals.alpha-reader]
privileges = ["space_view", "space_read_data"]
"""

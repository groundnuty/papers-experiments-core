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

# The same with a space for the day-0 checks alone: no user of the agent is a member of it.
WITH_DAY0 = (
    MINIMAL
    + """
[spaces.day0-space]
owner = "elm"
agent_visible = false

[principals.day0-checker]
privileges = ["space_view", "space_read_data", "space_write_data"]
spaces = ["day0-space"]
agent = false
"""
)

# The same with a space declared baseline = false: owned by elm, mirrored by oak, and left empty by up and the reset.
WITH_BASELINE_OFF = (
    MINIMAL
    + """
[spaces.scratch-space]
owner = "elm"
mirrors = ["oak"]
baseline = false
"""
)

# The study space itself declared baseline = false: the study ingests everything that is ever in it.
STUDY_BASELINE_OFF = MINIMAL.replace('mirrors = ["elm"]', 'mirrors = ["elm"]\nbaseline = false', 1)

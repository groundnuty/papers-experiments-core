"""The example study with the two builders of R17 (`build` and `build_analysis`), for tests of the analysis and of a
package built from it. The plugin of a stamped copy gains both; each notes its call in a file the test names, `build`
fails when the test says the federation is gone, and `build_analysis` gives the scorers the test asks for.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from study_support import HEAD, Campaign, cli, copy_campaign, make_copy

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study

LOG = "TINY_BUILD_LOG"  # the file each builder notes its call in
GONE = "TINY_NO_FEDERATION"  # "1": `build` fails, as it does where the federation no longer exists
SCORERS = "TINY_ANALYSIS_SCORERS"  # which scorers `build_analysis` gives: same, other, extra or none

# What the copy's plugin gains: the run's `build` keeps its body under another name and wraps it.
BOTH_BUILDERS = """

# --- the test's two builders ---------------------------------------------------------------------------------------
import os

_RUN_BUILD = build


def _note(name):
    path = os.environ.get("TINY_BUILD_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(name + "\\n")


def build(context):
    _note("build")
    if os.environ.get("TINY_NO_FEDERATION") == "1":
        raise RuntimeError("the federation is gone")
    return _RUN_BUILD(context)


class OtherScorer(TinyScorer):
    version = "2"


class ExtraScorer(TinyScorer):
    name = "extra"


def build_analysis(context):
    _note("build_analysis")
    chosen = {
        "same": [TinyScorer()],
        "other": [OtherScorer()],
        "extra": [TinyScorer(), ExtraScorer()],
        "none": [],
    }[os.environ.get("TINY_ANALYSIS_SCORERS", "same")]
    return StudyPlugin(executor=None, scorers=chosen, prompts={}, tools={})
"""


def with_builders(base: Path, name: str, extra: str = BOTH_BUILDERS, *, held: bool = False) -> Path:
    """A stamped copy of the example whose plugin gains `extra`; the scorer is code, so the stamp is made after. With
    `held`, a budget too small for any run, so a start opens the store, pins its fingerprint and returns, and room for
    two shards (a model's cap is at least the number of shards)."""
    copy = make_copy(base, name)
    if held:
        text = (copy / "campaign.toml").read_text(encoding="utf-8").replace("max_usd = 5.0", "max_usd = 0.00001")
        text = text.replace("alpha-stub = 1", "alpha-stub = 2").replace("beta-stub = 1", "beta-stub = 2")
        (copy / "campaign.toml").write_text(text, encoding="utf-8")
    plugin = copy / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + extra, encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


def edit_analysis(root: Path, change: Callable[[dict[str, Any]], object]) -> None:
    """Rewrite out/analysis.json after `change(data)` in the form the core writes it (sorted keys, one-space indent,
    a final newline), so that the bytes differ from what the records give only by what the change did."""
    path = root / "out" / "analysis.json"
    held = path.read_text(encoding="utf-8")
    data = json.loads(held)
    assert json.dumps(data, indent=1, sort_keys=True) + "\n" == held  # the form is the core's own
    change(data)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def rescored_copy(source: Campaign, base: Path, name: str = "rescored") -> Campaign:
    """A copy of a finished campaign whose analysis and report were made again with `build_analysis` giving the
    corrected scorer (`other`, version 2), with `--rescore`: the batch a package of a re-scored study is built from. The
    copy's out/ then holds the `rescore` entry; the caller sets the same scorers in its own environment to build and
    check."""
    copy = copy_campaign(source, base, name)
    with pytest.MonkeyPatch.context() as env:
        env.setenv(SCORERS, "other")
        for command in ("analyze", "report"):
            status, errors = cli(dict(os.environ), command, str(copy.root), "--rescore")
            assert status == 0, errors
    assert "rescore" in json.loads((copy.root / "out" / "analysis.json").read_text(encoding="utf-8"))
    return copy

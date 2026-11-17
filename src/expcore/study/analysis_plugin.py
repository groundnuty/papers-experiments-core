# Provenance: new
"""The plugin an analysis builds, and the guard that keeps it scoring as the run did (R17).

A study's `build(context)` often needs its platform: the rendered federation, the site's variables. Neither exists
when a batch run on an HPC allocation is analysed after copy-back, and analysis needs only the scorers. A plugin module
may therefore define one more function, `build_analysis(context) -> StudyPlugin`, with the same context. The analysis
(`study.pipeline.analyse_study`, so `analyze`, `report`, `regenerate` and a package's check) builds its plugin with it
when the module defines it and with `build` otherwise, as before. Only the scorers of what it returns are read, so only
they are checked: `build_analysis` need give no prompt or tool hash, no executor and no backend factory.

It is never used for a run. `study.plugin.load_plugin`, which every start, resume and shard builds with, does not look
for it, and `load_analysis_plugin` here is called from one place, the analysis (`tests/study/test_build_analysis.py`
keeps it so).

The guard is the one way the two builders cannot score differently. The campaign fingerprint, which the store and its
snapshot record, holds the run plugin's `scorer_digest`: the SHA-256 of its scorers' names and versions
(`plugin.scorers_digest`). An analysis built by `build_analysis` is refused unless its scorers have that digest, and
the refusal names them, never a digest or a version. An analysis built by `build` is not compared: it is the run's
own builder, and re-scoring with a scorer that has changed since (R13) is what it is for. A store from before the
fingerprint held a digest has nothing to compare with: the analysis goes on and says so in `StudyAnalysis.notes`.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from expcore.records.rescore import scorer_name
from expcore.study.plugin import BUILD, StudyContext, StudyPlugin, call_builder, import_for_build, scorers_digest

BUILD_ANALYSIS = "build_analysis"
SCORER_DIGEST = "scorer_digest"  # the key of the campaign fingerprint that holds the run plugin's scorers' digest
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ScorersDiffer(Exception):
    """The analysis plugin's scorers are not the scorers the campaign ran with; the message names them."""


@dataclass(frozen=True)
class AnalysisPlugin:
    """The plugin an analysis scores with, and which builder gave it: `build_analysis` (True) or `build` (False)."""

    plugin: StudyPlugin
    from_build_analysis: bool


def load_analysis_plugin(context: StudyContext) -> AnalysisPlugin:
    """Import the study's plugin module and build the plugin an analysis scores with.

    The module's `build_analysis` where it defines one, checked for its scorers alone; its `build` otherwise, checked as
    a run's plugin is, so a module without `build_analysis` is loaded exactly as `load_plugin` loads it. A
    `build_analysis` that is not a function, or gives no `StudyPlugin`, is refused as a `build` is, naming it.
    """
    module = import_for_build(context)
    if getattr(module, BUILD_ANALYSIS, None) is None:
        return AnalysisPlugin(call_builder(context, module, BUILD), False)
    return AnalysisPlugin(call_builder(context, module, BUILD_ANALYSIS, scorers_only=True), True)


def scorer_notes(
    scorers: Sequence[Any], plugin_file: str, recorded: Sequence[Mapping[str, Any] | None]
) -> tuple[str, ...]:
    """Compare the analysis plugin's scorers with what each snapshot of the store recorded of the run's, and return the
    notes the analysis carries; raise `ScorersDiffer` when a snapshot records another digest.

    `recorded` holds the campaign fingerprint of each snapshot the analysis reads (one for a campaign, one per shard of
    a merged batch), or None for a snapshot that records none. A snapshot with no `scorer_digest`, or one that is not a
    SHA-256 digest, has nothing to compare with: the others are compared as they are, and one note says how many were
    not. Without any, the note says the scorers were not compared at all.
    """
    if not recorded:
        return (f"{BUILD_ANALYSIS}'s scorers were not compared with the run's: no snapshot of the store was read",)
    held = [fingerprint.get(SCORER_DIGEST) if isinstance(fingerprint, Mapping) else None for fingerprint in recorded]
    digests = [d for d in held if isinstance(d, str) and _DIGEST.fullmatch(d)]
    if digests and any(d != scorers_digest(scorers) for d in digests):
        names = ", ".join(repr(scorer_name(s)) for s in scorers)
        raise ScorersDiffer(
            f"{plugin_file}: {BUILD_ANALYSIS} gives the scorers {names}, but the campaign was run with other scorers "
            f"(the scorer digest its fingerprint records is not theirs): give {BUILD_ANALYSIS} the scorers `build` "
            "gives, with the same names and versions"
        )
    missing = len(held) - len(digests)
    if not missing:
        return ()
    noun, verb = ("snapshot", "records") if len(held) == 1 else ("snapshots", "record")
    return (
        f"{BUILD_ANALYSIS}'s scorers were not compared with the run's: {missing} of {len(held)} {noun} of the store "
        f"{verb} no scorer digest",
    )

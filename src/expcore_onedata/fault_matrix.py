# Provenance: new
"""The pair matrix the link heal reads (O35): the link check of every ordered pair, by label.

`LinkMatrix` is `linkcheck.check_links`'s report as the heal needs it: each pair's reader and source by label
(`providers[i]`, never a name, O12), whether it passed, its seconds, and whether its fetch failed (`stuck`: a link that
transferred nothing; a pair whose probe could not be made ready was not tested, fails too, and names no link to heal).
Its record (`to_json`) holds every pair, the count of failing pairs per source, and the source they all share when they
share one, which names a crashed provider.
"""

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

_LABEL = re.compile(r"providers\[(\d+)\]")


def label_order(label: str) -> tuple[int, str]:
    """A provider label's place in platform.toml's order, for a fixed order of the heal's actions."""
    found = _LABEL.fullmatch(label)
    return (int(found.group(1)) if found else 1 << 30, label)


@dataclass(frozen=True)
class PairState:
    """One ordered pair of the link check, by label: `reader` fetched from `source`. `stuck` is a failed fetch."""

    reader: str
    source: str
    ok: bool
    seconds: float | None = None
    stuck: bool = False

    def to_json(self) -> dict[str, Any]:
        seconds = None if self.seconds is None else round(self.seconds, 3)
        return {"reader": self.reader, "source": self.source, "ok": self.ok, "seconds": seconds, "stuck": self.stuck}


@dataclass(frozen=True)
class LinkMatrix:
    """The link check of every ordered pair, as the heal reads it."""

    ok: bool
    detail: str
    pairs: tuple[PairState, ...] = ()

    @classmethod
    def from_report(cls, report: Any) -> "LinkMatrix":
        """From a `linkcheck.LinkReport`: a pair is stuck when its fetch failed (its result names the remedy)."""
        pairs = tuple(
            PairState(p.local, p.source, p.ok, p.seconds, not p.ok and p.remedy is not None) for p in report.pairs
        )
        return cls(bool(report.ok), str(report.detail), pairs)

    def failing(self) -> list[PairState]:
        return [pair for pair in self.pairs if not pair.ok]

    def sources(self) -> set[str]:
        """The sources of every failing pair: no container restart acts on one."""
        return {pair.source for pair in self.failing()}

    def stuck_readers(self) -> list[str]:
        """The readers of the stuck pairs, in label order."""
        return sorted({pair.reader for pair in self.failing() if pair.stuck}, key=label_order)

    def shared_source(self) -> str | None:
        """The source every failing pair shares, when they share one (the crashed provider of the pattern)."""
        sources = self.sources()
        return next(iter(sources)) if len(sources) == 1 else None

    def broken_since(self, before: "LinkMatrix") -> list[tuple[str, str]]:
        """The pairs that passed in `before` and do not pass here, or are missing here."""
        now = {(pair.reader, pair.source): pair.ok for pair in self.pairs}
        return [(p.reader, p.source) for p in before.pairs if p.ok and now.get((p.reader, p.source)) is not True]

    def to_json(self) -> dict[str, Any]:
        failing = Counter(pair.source for pair in self.failing())
        return {
            "ok": self.ok,
            "pairs": [pair.to_json() for pair in self.pairs],
            "failing_sources": dict(sorted(failing.items(), key=lambda item: label_order(item[0]))),
            "shared_source": self.shared_source(),
        }


def unreadable(error: BaseException) -> LinkMatrix:
    """The matrix of a link check that could not run: failed, with no pair, so nothing passes and nothing is healed."""
    return LinkMatrix(False, f"the link check raised {type(error).__name__}")

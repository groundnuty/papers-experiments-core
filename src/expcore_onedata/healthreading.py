# Provenance: new
"""The platform check's structured reading, beside its text: which providers were offline, in which spaces, and whose
injection took the run's own targets down (O90).

`PlatformHealth` answers with `Health(ok, detail)`, and `detail` is text for a person. It names the first failing space
and no other, so a study with several spaces and an outage condition cannot tell, from the text, an outage of the one
provider the run took down from a wider one. This module is the answer that does not need the text. The text is
unchanged, byte for byte; the reading is made from the same scope answer the check already read (no further request),
and it is kept three ways: `PlatformHealth.reading` (the latest call of the calling thread), the try's note
(`prerun`, key `READING_NOTE`, JSON), and `HealthReading.from_json` / `noted_reading()` to read it back.

**What a reading holds.** One `SpaceReading` for every declared space, in the file's order: its position
(`spaces[i]`), whether it was **read completely**, and the declared providers Onezone reported offline in it, by layout
label (`providers[i]`, never a name, an id or a host). A space is read completely only when its name is the declared
one, the providers it reports are exactly its declared support set (none missing, none a stranger, none ambiguous,
none unexpected) and every online flag is a boolean; the same comparison the text is made from (`match`). Anything
else is **unread**, and an unread space has no offline set: a provider the federation stopped reporting is not an
offline provider, and a space no token of the data path reaches was not looked at. And `targets`: the labels of the
providers the run's own attempt took down, read from the fault journal's `injecting` lines owned by exactly the attempt
the check judges (`fault_owner.attempt_of`), whose stop was asked for (`stopping`, `stop_unknown` or `injected`),
cleared or still open, never a heal's restart (`heal`), never another attempt's, nor a line that names no attempt. A
check that judges no attempt (a pre-run check, the dispatcher's own) has no targets of its own: the empty set. A
platform with no `[faults]` has no injector, so the empty set. A journal that cannot be read leaves them None: unknown.

**`target_only(reading)`** is the one question a study asks, and it fails closed:

- `True` when every space was read completely and every provider offline anywhere is one of the run's targets
  (nothing offline at all is True, with or without targets);
- `None` (unknown) when the reading is absent or empty, or any space is unread, or something is offline and the run's
  targets are unknown. Unknown is never true, and the order is the brief's: an unread space makes it unknown even
  beside another provider's outage;
- `False` otherwise: a provider offline in a read space that the run did not take down.

A reading is per call: `PlatformHealth` resets it at the start of every call, so a call that never reached the space
(a deferral, a failed baseline, a content type that refused) leaves `UNREAD`, never the call before's reading.
"""

import json
import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from expcore.campaign import liveness, prerun
from expcore.errors import TARGET_LABEL
from expcore_onedata.client import PlatformError
from expcore_onedata.config import PlatformConfig, StudySpace
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.fault_owner import attempt_of
from expcore_onedata.federation import ReportedProvider, SpaceView, declared_names, parse_scope
from expcore_onedata.tokens import Plane

READING_NOTE = "health_reading"  # the key of the try's note (`expcore.campaign.prerun`) the latest reading is put under
_POSITION = re.compile(r"spaces\[(0|[1-9][0-9]*)\]")
_ASKED = ("stopping", "injected", "stop_unknown")  # a journal line that says the stop of the target was asked for
log = logging.getLogger(__name__)


class Matching(NamedTuple):
    """The providers a space reports against its declared support set: what `compare` makes its text from and the
    reading its completeness from, so the two never disagree about a space."""

    matched: dict[str, list[bool]]  # declared provider name -> the online flags reported for it
    strangers: list[ReportedProvider]  # reported, and no declaration fits
    ambiguous: set[str]  # declared names that more than one reported provider fits, or a reported one fits several
    missing: set[str]  # in the support set, not reported
    unexpected: set[str]  # reported as a declared provider, but not in the support set

    @property
    def differs(self) -> bool:
        """Whether the reported providers are not exactly the declared support set."""
        return bool(self.missing or self.strangers or self.unexpected or self.ambiguous)


def match(view: SpaceView, config: PlatformConfig, declared: Collection[str]) -> Matching:
    """Match the providers a space reports to its `declared` support set by provider id (or by host for a declaration
    with no id), never taking the first of several."""
    matched: dict[str, list[bool]] = {}
    strangers: list[ReportedProvider] = []
    ambiguous: set[str] = set()
    for reported in view.providers:
        names = declared_names(config, reported)
        if not names:
            strangers.append(reported)
        elif len(names) > 1:
            ambiguous.update(names)
        else:
            matched.setdefault(names[0], []).append(reported.online)
    ambiguous.update(name for name, online in matched.items() if len(online) > 1)
    return Matching(matched, strangers, ambiguous, set(declared) - set(matched), set(matched) - set(declared))


@dataclass(frozen=True)
class SpaceReading:
    """One space: whether it was read completely, and the declared providers reported offline in it (empty when it was
    not: an unread space claims nothing)."""

    position: str  # `spaces[i]`
    read: bool
    offline: frozenset[str] = frozenset()  # layout labels, `providers[i]`

    def __post_init__(self) -> None:
        if not self.read and self.offline:
            raise ValueError("an unread space cannot hold providers offline")


@dataclass(frozen=True)
class HealthReading:
    """What one call of the platform check read: every declared space, and the run's own injection targets (None when
    the journal could not be read). No spaces means the call never reached them (`UNREAD`)."""

    spaces: tuple[SpaceReading, ...] = ()
    targets: frozenset[str] | None = None

    @property
    def offline(self) -> frozenset[str]:
        """Every provider offline in a space that was read."""
        return frozenset().union(*(space.offline for space in self.spaces))

    def to_json(self) -> dict[str, Any]:
        """The reading as JSON-ready data, in a fixed order: labels only, no name, id or host."""
        return {
            "spaces": [
                {"position": space.position, "read": space.read, "offline": sorted(space.offline)}
                for space in self.spaces
            ],
            "targets": None if self.targets is None else sorted(self.targets),
        }

    @classmethod
    def from_json(cls, value: Any) -> "HealthReading | None":
        """The reading `to_json` made, or None for anything else: a malformed value is no reading, never a guess."""
        if not isinstance(value, Mapping) or set(value) != {"spaces", "targets"}:
            return None
        spaces, targets = value["spaces"], value["targets"]
        if not isinstance(spaces, list) or not (targets is None or _labels(targets) is not None):
            return None
        parsed: list[SpaceReading] = []
        for item in spaces:
            if not isinstance(item, Mapping) or set(item) != {"position", "read", "offline"}:
                return None
            position, read, offline = item["position"], item["read"], _labels(item["offline"])
            if not (isinstance(position, str) and _POSITION.fullmatch(position) and isinstance(read, bool)):
                return None
            if offline is None or (offline and not read):
                return None
            parsed.append(SpaceReading(position, read, offline))
        return cls(tuple(parsed), None if targets is None else _labels(targets))


def _labels(value: Any) -> frozenset[str] | None:
    """A list of target labels as a set, or None when it is anything else."""
    if not isinstance(value, list) or not all(isinstance(item, str) and TARGET_LABEL.fullmatch(item) for item in value):
        return None
    return frozenset(value)


UNREAD = HealthReading()  # a call that never reached the spaces


def target_only(reading: HealthReading | None) -> bool | None:
    """Whether an outage was the run's own: True when every space was read completely and every provider offline
    anywhere is one of the run's own targets; None (unknown) when the reading is absent, a space is unread, or something
    is offline and the targets are unknown; False when a provider the run did not take down is offline. Never True
    on anything it could not read."""
    if reading is None or not reading.spaces or any(not space.read for space in reading.spaces):
        return None
    offline = reading.offline
    if not offline:
        return True
    if reading.targets is None:
        return None
    return offline <= reading.targets


def read_space(view: SpaceView, config: PlatformConfig, entry: StudySpace) -> SpaceReading:
    """One space as the federation reports it: read completely only when `compare` would find no difference in its name
    or its providers, and then with the labels of the declared providers reported offline (as `compare` counts them:
    only a flag that is `False`)."""
    unread = SpaceReading(entry.position, False)
    if view.name != entry.space.name:
        return unread
    found = match(view, config, entry.support.providers)
    flags = {name: online[0] for name, online in found.matched.items()}
    if found.differs or not all(isinstance(flag, bool) for flag in flags.values()):
        return unread
    offline = frozenset(config.provider_label(name) for name, flag in flags.items() if flag is False)
    return SpaceReading(entry.position, True, offline)


def own_targets(journal: InjectionJournal | None, attempt: Mapping[str, Any] | None) -> frozenset[str] | None:
    """The labels of the providers the attempt `attempt` (`liveness.current()`) took down, from the append-only journal,
    whether their restore is done or not. The empty set for no attempt (nothing is read), or no journal (a platform
    with no `[faults]`); None when the journal cannot be read. Only an `injecting` line whose owner is exactly this
    attempt, which is no heal's, and whose stop was asked for, counts: no other attempt's, no line that names none."""
    judged = attempt_of(attempt)
    if journal is None or judged is None:
        return frozenset()
    try:
        text = journal.complete_text()
    except OSError:
        return None
    owned: dict[str, str] = {}  # injection id -> its target, for the judged attempt's own
    asked: set[str] = set()
    for number, text_line in enumerate(text.splitlines(), 1):
        try:
            line = json.loads(text_line)
        except ValueError:
            return None
        event, key = (line.get("event"), line.get("id")) if isinstance(line, dict) else (None, None)
        if event == "injecting" and isinstance(key, str) and TARGET_LABEL.fullmatch(str(line.get("target"))):
            if line.get("heal") is not True and attempt_of(line.get("owner")) == judged:
                owned[key] = line["target"]
        elif event in _ASKED and isinstance(key, str):
            asked.add(key)
        elif event not in ("cleared", "clear_failed"):  # a line that is no injection event: nothing can be said
            log.warning("the injection journal's line %d is no injection event: the run's targets are unknown", number)
            return None
    return frozenset(target for key, target in owned.items() if key in asked)


def make_reading(answer: Any, config: PlatformConfig, journal: InjectionJournal | None) -> HealthReading:
    """The reading of one scope answer, for the attempt this context runs; never raises. A space that no data-path token
    reaches, or whose answer cannot be parsed, is unread; any other failure of the reading itself leaves every space
    unread (its type only is logged), and never touches the check's verdict."""
    try:
        several = len(config.spaces) > 1
        spaces = []
        for entry in config.spaces:
            if several and Plane.DATA_PATH not in entry.tokens:
                spaces.append(SpaceReading(entry.position, False))
                continue
            try:
                spaces.append(read_space(parse_scope(answer, entry.space.id), config, entry))
            except PlatformError:  # the answer does not describe this space
                spaces.append(SpaceReading(entry.position, False))
        return HealthReading(tuple(spaces), own_targets(journal, liveness.current()))
    except Exception as error:
        log.warning("the health reading failed (%s): no space is read", type(error).__name__)
        return UNREAD


def noted_reading() -> HealthReading | None:
    """The latest reading in the try this context is in (`prerun`), or None outside a try or when it holds none."""
    note = prerun.current()
    return None if note is None else HealthReading.from_json(note.get(READING_NOTE))

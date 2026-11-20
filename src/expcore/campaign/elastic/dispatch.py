# Provenance: new
"""The dispatcher loop on the orchestrating host (#462 step 3, C44): one call per site per tick, the ledger decides.

Each tick, for each site, in this order:

1. **Issue.** Only to a site whose last call was answered: every federation of it that the ledger lets take a lease
   gets one now (`Ledger.issue`), written `issued` before its file goes out.
2. **One call** (`transport.exchange`, the verb `elastic.exchange`): it delivers those leases, settles every other
   `issued` lease of the site (left by a crash, or by a call whose reply was lost: a tombstone takes the name of a
   lease whose file is absent), lists each inbox, and reads each outbox. A call that fails is the site unread: the
   ledger's sweep runs (`Ledger.sweep`), so a site out still expires its leases after `site_out_s`.
3. **Sent or cancelled.** A lease is marked sent (`Ledger.mark_sent`) only when the call's own listing, after its
   writes, shows the lease's file under its name; a settled lease whose name a tombstone holds is cancelled, since its
   file never landed and never can. A drain the watchdog asked for drains the federation in the ledger.
4. **The reading.** When every outbox was read, the heartbeats, completions and returns go to the ledger in one
   reading (`Ledger.observe_site`), with what each completion or return says its run saw undeclared (C48), each
   fenced first: a file counts only for a lease of the federation whose outbox
   it is in, of the run the ledger leased, and of the format. A completion of a lease the ledger already read is not
   read again. A reading the ledger refuses changes nothing, and the site is swept as unread.
5. **The archives.** For each completion read and not yet settled, its archive is pulled (`transport.fetch`) into
   `archives/` and hashed, and the hash settles it (`Ledger.verify_archive`). An archive missing or not matching
   `FETCH_TRIES` times is given up, and the lease is `lost`; an unreachable site is tried again next tick.

No state but the ledger's carries across ticks or a restart, save which sites answered last and the pull tries: after a
restart the first tick issues nothing and settles every `issued` lease from the inbox.
"""

import contextlib
import hashlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.config import usd_of
from expcore.campaign.elastic.exchange import FORMAT
from expcore.campaign.elastic.ledger import Ledger
from expcore.campaign.elastic.records import Completion, Lease, LedgerError, Return, SiteReading
from expcore.campaign.elastic.transport import Missing, Site, Unreachable

TICK_S = 30.0
FETCH_TRIES = 3
_READ = ("reported", "counted", "superseded", "lost", "returned", "cancelled")  # a completion of these is not read
_RETURNABLE = ("issued", "sent", "expired")
_LOG = logging.getLogger(__name__)


class Transport(Protocol):
    def exchange(self, site: Site, request: dict[str, Any]) -> dict[str, Any]: ...

    def fetch(self, site: Site, remote: str, local: Path) -> None: ...


@dataclass
class SiteTick:
    """What one tick did at a site."""

    read: bool = False
    issued: list[int] = field(default_factory=list)
    sent: list[int] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    settled: dict[int, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


class Dispatcher:
    """The loop: `tick()` once per `TICK_S`, on a ledger it alone holds open."""

    def __init__(
        self, ledger: Ledger, sites: Sequence[Site], transport: Transport, archives: Path, *, seq: int = 0
    ) -> None:
        names = [site.name for site in sites]
        if len(set(names)) != len(names):
            raise ValueError("each site is given once")
        self.ledger = ledger
        self.sites = {site.name: site for site in sites}
        self.transport = transport
        self.archives = archives
        self._answered: dict[str, bool] = {}
        self._fetch_failures: dict[int, int] = {}
        self._seq = seq

    def tick(self) -> dict[str, SiteTick]:
        """One pass over every site."""
        return {name: self._tick_site(site) for name, site in self.sites.items()}

    def run(self, sleep: Callable[[float], None], stop: Callable[[], bool] = lambda: False) -> None:
        """Tick every `TICK_S` until `stop()`."""
        while not stop():
            self.tick()
            sleep(TICK_S)

    # --- one site ---------------------------------------------------------------------------------------------

    def _tick_site(self, site: Site) -> SiteTick:
        done = SiteTick()
        leases = self.ledger.leases()
        labels = self._federations(site, leases)
        fresh: dict[str, Lease] = {}
        if self._answered.get(site.name):
            for fed in self.ledger.federations():
                if fed.label in labels and fed.ended_at is None and fed.drained_at is None:
                    got = self.ledger.issue(fed.label)
                    if isinstance(got, Lease):
                        fresh[fed.label] = got
                        done.issued.append(got.lease_id)
        fresh_ids = {lease.lease_id for lease in fresh.values()}
        settle: dict[str, list[int]] = {}
        for lease in self.ledger.leases():
            if lease.site == site.name and lease.state == "issued" and lease.lease_id not in fresh_ids:
                settle.setdefault(lease.federation, []).append(lease.lease_id)
        self._seq += 1
        request = {
            "format": FORMAT,
            "site": site.name,
            "root": site.root,
            "contact": {"seq": self._seq},
            "federations": [
                {
                    "label": label,
                    "deliver": [ch.envelope(fresh[label])] if label in fresh else [],
                    "settle": sorted(settle.get(label, [])),
                }
                for label in sorted(labels | set(settle))
            ],
        }
        try:
            reply = self.transport.exchange(site, request)
            answers = _answers(reply, site, request)
        except Unreachable as error:
            _LOG.warning("site %s not read: %s", site.name, error)
            self._answered[site.name] = False
            self.ledger.sweep()
            return done
        self._answered[site.name] = True
        self._confirm(answers, fresh_ids, done)
        if any(answer.get("outbox") is None for answer in answers.values()):
            _LOG.warning("site %s not read: an outbox could not be listed", site.name)
            self.ledger.sweep()
            return done
        reading, expected = self._reading(answers, done)
        try:
            self.ledger.observe_site(site.name, reading)
        except (LedgerError, ValueError) as error:
            _LOG.error("site %s: the ledger refused its reading, so it stays unread: %s", site.name, error)
            self.ledger.sweep()
            return done
        done.read = True
        self._pull(site, expected)
        return done

    def _federations(self, site: Site, leases: Sequence[Lease]) -> set[str]:
        """The federations of the site to read: every one not proven ended, and one holding a lease that may still
        bring a completion."""
        open_leases = {lease.federation for lease in leases if lease.state in ("issued", "sent", "expired", "reported")}
        return {
            fed.label
            for fed in self.ledger.federations()
            if fed.site == site.name and (fed.ended_at is None or fed.label in open_leases)
        }

    def _confirm(self, answers: Mapping[str, Mapping[str, Any]], fresh_ids: set[int], done: SiteTick) -> None:
        for label, answer in answers.items():
            for key, state in answer["inbox"].items():
                lease_id = int(key)
                lease = self.ledger.lease(lease_id)
                if lease.federation != label or lease.state != "issued":
                    continue
                if state == "lease":
                    self.ledger.mark_sent(lease_id)
                    done.sent.append(lease_id)
                elif state == "tombstone" and lease_id not in fresh_ids:
                    try:
                        self.ledger.cancel(lease_id)
                        done.cancelled.append(lease_id)
                    except LedgerError as error:
                        _LOG.error("lease %d was not cancelled: %s", lease_id, error)
                if lease_id not in fresh_ids:
                    done.settled[lease_id] = state
            if answer["drain"]:
                self.ledger.drain(label)

    def _reading(
        self, answers: Mapping[str, Mapping[str, Any]], done: SiteTick
    ) -> tuple[SiteReading, dict[int, tuple[str, str]]]:
        """The site's reading, each file fenced to its federation and lease; and, for each completion read, the
        federation and the archive hash it names."""
        leases = {lease.lease_id: lease for lease in self.ledger.leases()}
        heartbeats: dict[int, int] = {}
        completions: list[Completion] = []
        returns: list[Return] = []
        expected: dict[int, tuple[str, str]] = {}
        bounded = self.ledger.config.max_infra_streak is not None  # C58: only then is a completion's streak read

        def streak_of(record: Mapping[str, Any]) -> dict[str, Any]:
            if not bounded:
                return {}
            return {"infra_streak": record.get("infra_streak", 0), "infra_kind": record.get("infra_kind")}

        def owned(label: str, lease_id: Any, record: Mapping[str, Any]) -> Lease | None:
            lease = leases.get(lease_id) if isinstance(lease_id, int) and not isinstance(lease_id, bool) else None
            if lease is None or lease.federation != label or record.get("federation") != label:
                done.dropped.append(f"{label}: a file of lease {lease_id!r}, not this federation's")
                return None
            return lease

        for label, answer in sorted(answers.items()):
            box = answer["outbox"]
            beat = box.get("heartbeat")
            if isinstance(beat, dict) and beat.get("format") == ch.HEARTBEAT_FORMAT:
                lease = owned(label, beat.get("lease_id"), beat)
                counter = beat.get("counter")
                if lease is not None and isinstance(counter, int) and not isinstance(counter, bool) and counter >= 0:
                    heartbeats[lease.lease_id] = counter
            for item in box.get("completions", ()):
                record = item.get("record") or {}
                if record.get("format") != ch.COMPLETION_FORMAT or record.get("lease_id") != item.get("lease_id"):
                    continue
                lease = owned(label, record.get("lease_id"), record)
                if lease is None or record.get("run_key") != lease.run_key:
                    continue
                try:
                    found = Completion(
                        lease_id=lease.lease_id,
                        state=record.get("state"),
                        charged=record.get("charged"),
                        spend_usd=_spend_usd(record),
                        archive_sha256=record.get("archive_sha256"),
                        undeclared=record.get("undeclared_fingerprints", ()),
                        **streak_of(record),
                    )
                except (TypeError, ValueError) as error:
                    done.dropped.append(f"{label}: completion of lease {lease.lease_id}: {error}")
                    continue
                if lease.state == "reported":
                    expected[lease.lease_id] = (label, found.archive_sha256)
                if lease.state not in _READ:
                    completions.append(found)
                    expected[lease.lease_id] = (label, found.archive_sha256)
            for item in box.get("returns", ()):
                record = item.get("record") or {}
                if record.get("format") != ch.RETURN_FORMAT or record.get("lease_id") != item.get("lease_id"):
                    continue
                lease = owned(label, record.get("lease_id"), record)
                if lease is None or record.get("run_key") != lease.run_key or lease.state not in _RETURNABLE:
                    continue
                try:
                    returns.append(
                        Return(
                            lease_id=lease.lease_id,
                            spend_usd=_spend_usd(record),
                            undeclared=record.get("undeclared_fingerprints", ()),
                        )
                    )
                except (TypeError, ValueError) as error:
                    done.dropped.append(f"{label}: return of lease {lease.lease_id}: {error}")
        ended = {completion.lease_id for completion in completions}
        for returned in [returned for returned in returns if returned.lease_id in ended]:
            done.dropped.append(f"the return of lease {returned.lease_id}, which has a completion")
        returns = [returned for returned in returns if returned.lease_id not in ended]  # one end per lease, or none
        for note in done.dropped:
            _LOG.warning("dropped %s", note)
        return SiteReading(heartbeats=heartbeats, completions=completions, returns=returns), expected

    # --- archives ---------------------------------------------------------------------------------------------

    def _pull(self, site: Site, expected: Mapping[int, tuple[str, str]]) -> None:
        for lease_id, (label, wanted) in sorted(expected.items()):
            if self.ledger.lease(lease_id).state != "reported":
                continue
            try:
                self._pull_one(site, lease_id, label, wanted)
            except OSError as error:  # this host's own disk: tried again next tick, never a try against the archive
                _LOG.error("lease %d's archive could not be kept here: %s", lease_id, error)

    def _pull_one(self, site: Site, lease_id: int, label: str, wanted: str) -> None:
        remote = str(ch.federation_dir(site.root, label) / ch.ARCHIVES / ch.archive_name(lease_id))
        self.archives.mkdir(parents=True, exist_ok=True)
        local = self.archives / ch.archive_name(lease_id)
        try:
            cached = local.is_file() and _sha256(local) == wanted  # pulled before a restart, not yet settled
        except OSError:
            cached = False
        if cached:
            self._fetch_failures.pop(lease_id, None)
            self.ledger.verify_archive(lease_id, wanted)
            return
        draft = local.with_name(f".{local.name}.pull")
        with contextlib.suppress(FileNotFoundError):
            draft.unlink()
        got: str | None = None
        try:
            self.transport.fetch(site, remote, draft)
            got = _sha256(draft)
        except Unreachable as error:
            _LOG.warning("lease %d's archive not pulled now: %s", lease_id, error)
            return
        except Missing as error:
            _LOG.warning("lease %d's archive: %s", lease_id, error)
        if got == wanted:
            os.replace(draft, local)
            self._fetch_failures.pop(lease_id, None)
            self.ledger.verify_archive(lease_id, got)
            return
        with contextlib.suppress(FileNotFoundError):
            draft.unlink()
        tries = self._fetch_failures.get(lease_id, 0) + 1
        self._fetch_failures[lease_id] = tries
        if tries >= FETCH_TRIES:
            self._fetch_failures.pop(lease_id, None)
            self.ledger.verify_archive(lease_id, got)


def _answers(reply: Mapping[str, Any], site: Site, request: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each federation's answer, checked whole: a reply that lacks one, or holds one of another form, is no reading."""
    federations = reply.get("federations")
    if reply.get("format") != FORMAT or reply.get("site") != site.name or not isinstance(federations, dict):
        raise Unreachable(f"site {site.name}: the reply is not an exchange reply for it")
    out: dict[str, dict[str, Any]] = {}
    for entry in request["federations"]:
        label = entry["label"]
        answer = federations.get(label)
        if (
            not isinstance(answer, dict)
            or not isinstance(answer.get("inbox"), dict)
            or not all(isinstance(key, str) and key.isdigit() for key in answer["inbox"])
            or not isinstance(answer.get("drain"), bool)
        ):
            raise Unreachable(f"site {site.name}: the reply has no whole answer for federation {label}")
        if answer.get("error") is not None:
            raise Unreachable(f"site {site.name}: federation {label}'s channel: {answer['error']}")
        box = answer.get("outbox")
        if box is not None and not (
            isinstance(box, dict)
            and {"heartbeat", "exited", "completions", "returns"} <= set(box)
            and isinstance(box.get("completions"), list)
            and isinstance(box.get("returns"), list)
            and all(isinstance(item, dict) for item in [*box["completions"], *box["returns"]])
        ):
            raise Unreachable(f"site {site.name}: federation {label}'s outbox is not of the reply's form")
        out[label] = answer
    return out


def _spend_usd(record: Mapping[str, Any]) -> float:
    """A file's spend, from its whole micro-dollars (what the ledger charges); ValueError for any other form."""
    micro = record.get("spend_micro_usd")
    if isinstance(micro, bool) or not isinstance(micro, int) or micro < 0:
        raise ValueError(f"spend_micro_usd is a whole number of micro-dollars from 0, not {micro!r}")
    return usd_of(micro)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["FETCH_TRIES", "TICK_S", "Dispatcher", "SiteTick", "Transport"]

# Provenance: new
"""Whether a restored provider answers its REST API: the probe behind `ComposeDriver.serves` (O11).

A restore whose container runs with a changed identity is not yet a provider that serves: the live run of 12 Nov 2026
found the restarted provider's REST endpoint refusing connections right after the restart. `provider_serves` builds the
probe a driver is given: for a service of `[faults.services]` it makes the one authenticated read the platform layer
already makes through a provider, the read of a space (`GET /spaces/{id}` with the admin credential, the call that
resolves the space's root, `OnezoneClient`), for the first declared space the provider supports. A provider that
supports no declared space is asked for its configuration (`GET /configuration`) instead.

A read that succeeds means the provider serves. A refused connection, a reset, a timeout, a status of 400 or more and
an answer that is not JSON all mean it does not, yet: the probe returns False, never raises for them, and its caller
polls again until its bound runs out. The read is not retried here, and every read is bounded by `PROBE_TIMEOUT_S`, so
a provider that accepts connections and never answers costs one bounded read per poll. A service that no provider is
declared for raises ConfigError, naming the file and the table, never the service.

**A restored provider's links are checked too (O35).** Once it serves, the recovery asks `ServingProbe.links` (through
`ComposeDriver.restored_links`) for the link check of every ordered pair of providers, not the provider's own alone
(an outage can leave another pair stuck), and a pair that does not pass fails the restore (`links_stuck`): the
injection stays open and the fault gate fails, as for a restore that never served, until a later clear finds every link
transferring. `ServingProbe.matrix` is the same check pair by pair, which the heal of `[faults] heal` reads, and `arm`
and `sessions` are the session gate that comes before it (`expcore_onedata.fault_sessions`).

**Every one of those checks is recorded (O35, `expcore_onedata.linkrecord`).** The restore's check and each read of
the pairs a heal makes, its re-check after the settle among them, write `link-check.json` beside platform.toml (the
federation's root, beside `facts.json`, under `deploy/compose-federation`), atomically and in the format `up` writes, so
the record on disk is the latest check's verdict and time: a passing re-check after a heal leaves a pass, a check that
finds a pair stuck leaves its failing verdict over an older pass, and a check that raises removes the older record (or
empties it in place), as does one whose record cannot be written (logged, never failing the restore or the heal). One
check at a time makes the record (`linkrecord.recording`: a leaf lock around the check and its write), so a late
finisher never replaces a later check's record. The gate's own routine link check (`PlatformHealth`) writes none, and
the record is read by no admission: it touches no federation lock and no epoch.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_matrix import LinkMatrix
from expcore_onedata.fault_sessions import SessionGate
from expcore_onedata.fault_settings import FaultSettings
from expcore_onedata.linkcheck import LinkReport, check_links
from expcore_onedata.linkrecord import RECORD, discard_record, recording, write_record
from expcore_onedata.readretry import ONE_ATTEMPT

PROBE_TIMEOUT_S = 10.0
log = logging.getLogger(__name__)


def _path(config: PlatformConfig, provider: str) -> str:
    """The read that shows a provider serving: its first declared space, or its configuration if it supports none."""
    for entry in config.spaces:
        if provider in entry.support.providers:
            return f"/spaces/{entry.space.id}"
    return "/configuration"


class ServingProbe:
    """The probe for `compose_driver`: `probe(service)` is True when the provider behind the service answers an
    authenticated REST read through the client, and False when it does not (yet); `links(service)` checks the links of
    that provider once it serves (O35)."""

    def __init__(self, config: PlatformConfig, settings: FaultSettings, client: OnezoneClient) -> None:
        self._config, self._settings, self._client = config, settings, client
        self._by_service = {service: provider for provider, service in settings.services.items()}
        self._gate = SessionGate(config, client)

    def _provider(self, service: str) -> str:
        provider = self._by_service.get(service)
        if provider is None:
            raise ConfigError(
                f"{self._settings.file}: [faults.services]: the service is not one a provider is declared for"
            )
        return provider

    def __call__(self, service: str) -> bool:
        provider = self._provider(service)
        try:
            client, path = self._client, _path(self._config, provider)
            client.get(path, provider=provider, timeout_s=PROBE_TIMEOUT_S, retry=ONE_ATTEMPT)
        except PlatformError:  # its message names a position, never the provider; here it only says "not yet"
            return False
        return True

    def links(self, service: str) -> str | None:
        """The link check (`linkcheck.check_links`) of every ordered pair, after a restore: None when each passed, else
        the failure, which names the pair and the remedy. Not the restored provider's pairs alone: a restarted
        provider's rtransfer link to a peer can come back stuck, with nothing logged, and so can the link between two
        other providers, which broke while the third was down (on the live slot of 14 Nov 2026 a mirror's link to the
        local provider stayed stuck after a third provider's outage, a pair the restored provider was in neither side
        of, while every pair the local provider reads through transferred). A read through any stuck pair would hang.
        `service` must be one a provider is declared for. Without a `[link_check]` nothing is checked:
        a federation with pairs and none is refused at its campaign's start."""
        matrix = self.matrix(service)
        return None if matrix is None or matrix.ok else matrix.detail

    def matrix(self, service: str) -> LinkMatrix | None:
        """The same check as a matrix of every pair by label, which the heal reads (`fault_heal`); None without a
        `[link_check]`. Every check made here, a restore's and each read of the pairs a heal makes, is recorded as
        `link-check.json` beside platform.toml, in the format `up` writes (`_checked`)."""
        self._provider(service)
        if self._config.link_check is None:
            return None
        return LinkMatrix.from_report(self._checked())

    def _checked(self) -> LinkReport:
        """The platform layer's all-pairs check, and its record (`expcore_onedata.linkrecord`), pass or fail: the record
        on disk is always the latest check's verdict, never an older one. The check and the writing of its record are
        one step under the record's lock, so a check that starts while another is under way waits for it and writes
        after it; one that cannot take the lock writes no record at all, and removes the older one. A check that raises
        answers nothing, so the older record is removed (or, when the directory refuses that, emptied in place) before
        the error goes on to the caller; a record that cannot be written is treated the same, and logged (its error's
        type only), and the check's answer is returned as made: the record is advisory, and neither the restore's
        verdict nor the heal's depends on it. It touches no federation lock and no epoch (`fault_epoch`)."""
        path = self._config.file.parent / RECORD
        with recording(path) as serialized:
            try:
                report = check_links(self._config, self._client)
            except Exception:
                self._discarded(path)
                raise
            if not serialized:  # a verdict that another check's may overlap is published by no one: the old one goes
                log.warning("the link check's record lock could not be taken, so no record is written")
                self._discarded(path)
                return report
            try:
                write_record(report, path)
            except OSError as error:
                log.warning("the link check's record could not be written (%s)", type(error).__name__)
                self._discarded(path)
            return report

    @staticmethod
    def _discarded(path: Path) -> None:
        """Make sure no older verdict stands: the record removed, or emptied in place; a record that can be neither is
        logged, its error's type only."""
        try:
            discard_record(path)
        except OSError as error:
            log.warning("the older link check record could be neither removed nor emptied (%s)", type(error).__name__)

    def arm(self, service: str) -> str | None:
        """Before an injection: the session gate's probe file, found or written and seen by every provider of the probe
        space (`fault_sessions.SessionGate.arm`); its id, or None without a `[link_check]`."""
        self._provider(service)
        return self._gate.arm()

    def sessions(self, service: str, gate_probe: str | None) -> str | None:
        """One reading of the session gate for the provider behind a restored service (`SessionGate.sessions`)."""
        return self._gate.sessions(self._provider(service), gate_probe)

    def transfers(self) -> int:
        """How many transfer listings of the declared spaces and the probe space show a job not yet ended
        (`SessionGate.active_transfers`), which the all-links heal waits on."""
        return self._gate.active_transfers()


def provider_serves(config: PlatformConfig, settings: FaultSettings, client: OnezoneClient) -> Callable[[str], bool]:
    """The probe for `compose_driver` (`ServingProbe`): whether the provider behind a service answers an authenticated
    REST read, and, through `ServingProbe.links`, whether its links transfer once it does."""
    return ServingProbe(config, settings, client)

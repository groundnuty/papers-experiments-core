# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/health.py, blob d841c16205d7b5b545c0186eec9788b9965880e6
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/health.py:57-64 (the scope of a token)
#   changes: see git history of this file from its import commit
"""The platform's health check, run before and after every attempt (O04).

`PlatformHealth` implements the core's `HealthCheck` protocol: it is called with no arguments and returns
`Health(ok, detail)`. It reads Onezone's view of the study space with a short-lived data-path token of the configured
service principal, on the host and outside every run, and compares the providers that support the space with the
declared set (`[support] providers`), matching them by provider id. The declared set is the only expectation: a
multi-provider federation passes when it reports exactly its declared providers, and a provider that is reported but not
declared fails the check as surely as a declared one that is missing. The check also fails when the space's name differs from the declared one or a declared
provider is reported offline. With several spaces (`[[spaces]]`) the token is a data path's, scoped to the spaces the
data path reaches (`space_ids("data_path")`) and no others, and each of those is checked against its own support set;
the first that fails is named by its position (`spaces[i]`). A space only the tool server's token reaches is not in the
token's scope, so this check cannot read it.

An answer that cannot be read is unhealthy, never a crash and never a pass: any failure, including an unexpected
exception, becomes `Health(False, reason)`. The reason names positions in the file (`providers[i]`), counts and the
ids the federation reports, never a token, URL, host or configured name.

When platform.toml has a `[faults]` table the fault gate (`expcore_onedata.fault_gate.FaultGate`) runs around the whole
check, before it asks Onezone anything or measures the content type: it recovers what a crashed run left open whenever
no run is on the federation, then holds the federation shared until the check has answered, so no injection starts in
the middle of it, and fails while an injection stays open, because a provider taken down for one condition's run, and
not verifiably back, leaves the federation out of its baseline state for every other run, including runs of conditions
that use no injector. A check whose bounded wait for the federation runs out, because an outage run holds it, reads
nothing and answers deferred, a wait that charges no run (`expcore.campaign.healthgate`). The gate is built in and
cannot be left out; `fault_driver` replaces the Compose driver that `[faults]` describes, for tests. The gate's recovery
verifies a restore only when the provider serves, as the injector's clear does (`faults.py`, "A restore is verified
only when the provider serves"): the Compose driver it builds reads through the check's own client.

The check of the earlier study also read one known file back through its provider and compared its checksum with a study manifest. That
belongs to the study (its data) and not to the platform, so a study adds it with its own check.

**The content type the data gate streams on is part of this check (O05, O11), with no opt-out.** When any level of
`[sandbox.data_paths]` is REST, the first call measures it (`precampaign.content_type_check`: the provisioned
`[sandbox] content_check_file`, read whole and by range through the local provider) before anything but the fault gate,
so it never measures through a provider an outage run holds down, and the
check keeps that verdict for the campaign, since a type once answered does not change: a refusal, or a type that could
not be measured, answers every call after it with its detail, so the gate stays closed and no run is admitted; a pass
lets every call answer as the space check does. With no REST data path nothing is read. The composition is
`precampaign.PreCampaignHealth`, built here, so a study that uses this check cannot leave the measurement out.

**So is the link check (O35, `linkcheck.py`), after the content type and the space, with no opt-out.** At the first
call every ordered (reader, source) pair a run can fetch through must fetch its probe from the `[link_check]` probe space within
the bound; a pair that cannot, or that could not be tested, fails the call, named in the detail with its remedy. Unlike
the content type its verdict is not kept: a stuck link is remedied while the campaign waits, so the check runs again at
every call after a failure, and whenever its last run is older than `[link_check] every_s` (600 s by default). A failure
at the first call holds every run until a re-check of the closed gate passes; one later, a link that stuck
mid-campaign, closes the gate, sets the run whose check found it aside as infrastructure and holds the whole campaign
the same way. A federation with no pair (every space supported by the local provider alone) has nothing to check; one
with pairs and no `[link_check]` always fails. `links` holds the last report: each pair's seconds and bytes and the
transfers the check made. `monotonic` and `sleep` are its clock and its pauses, a test's `Ticker`. The check writes
probes and evicts them, so a caller that must change nothing, day-0's read-only `health` check alone, builds this with
`links=False`; a campaign's health gate never does.

**So is every preallocated ingest's baseline (O44, `ingesthealth.py`), when the study names the ingests' journals**
(`ingest_journals`): before anything else, with no request to the federation, the check fails while a space's baseline
is marked unhealthy, or open by a run that is gone, so no run is admitted until `ProgressiveIngest.recover` verifies it
whole. A study whose conditions use a preallocated ingest passes every journal they write to.

**While `[tls] insecure_tls` is on, every reading warns** that no client of the federation checks its certificate, a
setting for disposable test federations only (`tls.py`). The warning is appended to the detail, so the campaign's
health record and day-0's report carry it; it does not change the verdict.
"""

import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from expcore.campaign.healthgate import Health, HealthCheck
from expcore.config.loading import ConfigError
from expcore_onedata.admission import fault_settings_of
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig, StudySpace
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_recovery import FaultDriver
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import compose_driver
from expcore_onedata.federation import ReportedProvider, SpaceView, declared_names, parse_scope
from expcore_onedata.ingesthealth import baseline_health
from expcore_onedata.linkcheck import LinkReport, check_links
from expcore_onedata.linksettings import LinkCheckSettings
from expcore_onedata.precampaign import ContentTypeVerdict, PreCampaignHealth, measure_content_type
from expcore_onedata.tls import WARNING
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import MintedToken, Plane, TokenIssuer, data_path_spec

TOKEN_CAP_S = 300.0  # the check's own budget; its token outlives it by the configured margin
REFRESH_BEFORE_S = 120.0  # a cached token is replaced this long before it expires
_SAFE = (PlatformError, TokenRefused)  # whose messages hold no URL, id or value, so they can be shown


_PLAIN_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")
_IDS_SHOWN = 5


def _reported_ids(reported: list[ReportedProvider]) -> str:
    """Name reported providers by the ids the federation gave them: a plain id as it is, anything else as a count."""
    shown = [r.id if _PLAIN_ID.fullmatch(r.id) else "(an id that is not plain)" for r in reported[:_IDS_SHOWN]]
    more = len(reported) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def compare(view: SpaceView, config: PlatformConfig, entry: StudySpace | None = None) -> Health:
    """Compare the providers the federation reports for a space with its declared set; any difference fails.

    `entry` is the declared space (`[[spaces]]`); without one it is the only space. Reported providers are matched to
    declared ones by provider id. A reported provider no declaration fits, and a declared provider that more than one
    reported provider fits (or a reported provider that fits several declarations), fail the check: the first match is
    never taken, so a second provider cannot hide behind the declared one.

    The detail holds no configured name, host, URL or id (O12). A declared provider is `providers[i]`, its position in
    platform.toml; a provider no declaration fits is shown by the id the federation reports for it.
    """
    entry = entry or config.space_named()
    space, support = entry.space, entry.support
    if view.name != space.name:
        return Health(False, "the study space is reported under another name than the declared one")
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
    missing = set(support.providers) - set(matched)
    unexpected = set(matched) - set(support.providers)
    if missing or strangers or unexpected or ambiguous:
        parts = []
        if missing:
            parts.append(f"missing {config.provider_labels(missing)}")
        if strangers:
            parts.append(f"{len(strangers)} reported provider(s) not declared: {_reported_ids(strangers)}")
        if ambiguous:
            parts.append(f"ambiguous match for {config.provider_labels(ambiguous)}")
        if unexpected:
            parts.append(f"declared but not in the support set: {config.provider_labels(unexpected)}")
        return Health(False, "the providers supporting the space differ from the declared set: " + "; ".join(parts))
    offline = [name for name, online in matched.items() if online[0] is False]
    if offline:
        return Health(False, f"declared provider(s) reported offline: {config.provider_labels(offline)}")
    return Health(True, f"ok: the space is supported by exactly the {len(matched)} declared provider(s)")


def _fault_gate(
    config: PlatformConfig, client: OnezoneClient, driver: FaultDriver | None, inner: HealthCheck
) -> FaultGate | None:
    """The fault gate around `inner` when platform.toml has a `[faults]` table, None when it has none; a table that is
    not valid raises ConfigError here, so a platform with an injector never runs without the gate. The Compose driver
    it recovers through finds a restored provider serving by reading through the check's own client."""
    settings = fault_settings_of(config)
    if settings is None:
        return None
    if driver is None:
        driver = compose_driver(settings, probe=provider_serves(config, settings, client))
    return FaultGate(settings, driver, inner)


class PlatformHealth:
    """The platform's `HealthCheck`; keep one for the campaign so that its token is reused until it nears its end, and
    the content type it measured at its first call is kept for the campaign."""

    def __init__(
        self,
        config: PlatformConfig,
        client: OnezoneClient,
        issuer: TokenIssuer,
        *,
        clock: Callable[[], float] = time.time,
        fault_driver: FaultDriver | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        links: bool = True,
        ingest_journals: Iterable[Path | str] = (),
    ) -> None:
        if config.principals.service is None:
            raise ConfigError(f"{config.file}: [principals]: key 'service' is required for the health check")
        self._config, self._client, self._issuer, self._clock = config, client, issuer, clock
        self._monotonic, self._sleep = monotonic, sleep  # the link check's waits (a test's Ticker)
        self._service = config.principals.service
        self._token: MintedToken | None = None
        self._lock = threading.Lock()
        self._content_type: ContentTypeVerdict | None = None
        self._links: LinkReport | None = None
        self._links_at = 0.0  # when the last link check ended, on `monotonic`
        self._links_passed = False  # whether any link check of this campaign passed
        self._links_lock = threading.Lock()
        self._with_links = links
        self._ingest_journals = tuple(Path(journal) for journal in ingest_journals)  # read at every call (O44)
        # The link check is not a kept pre-campaign check: a stuck link is remedied while the campaign waits, and the
        # gate's re-checks must see it pass (O35). It runs in `_regular`, after the space, at the first call and later.
        self._gate = PreCampaignHealth(self._regular, self._measure)
        self._faults = _fault_gate(config, client, fault_driver, self._gate)  # outermost: it holds the federation

    @property
    def content_type(self) -> ContentTypeVerdict | None:
        """What the content-type check found at the first call (None before it), so that a caller can tell a type that
        was measured and is wrong from one that could not be measured."""
        return self._content_type

    @property
    def links(self) -> LinkReport | None:
        """What the last link check found (None before the first, or while the content type or the space refuses):
        every pair's verdict, seconds and bytes, and the transfers it made (`LinkReport.to_json()`)."""
        return self._links

    def _measure(self) -> Health:
        self._content_type = measure_content_type(self._config, self._client)
        return self._content_type.health

    def _check_links(self) -> None:
        self._links = check_links(self._config, self._client, clock=self._monotonic, sleep=self._sleep, now=self._clock)
        self._links_at = self._monotonic()
        self._links_passed = self._links_passed or self._links.ok

    def _regular(self) -> Health:
        """Every call once the content type passed: the space, then the links (O35), at the first call and again
        whenever the last check is older than `[link_check] every_s` or failed, so a remedied link reopens the gate at
        its next re-check. The first check is waited for by every caller; a later one under way in another thread is
        not, and its caller answers with the last verdict."""
        verdict = self._space()
        if not verdict.ok or not self._with_links:
            return verdict
        settings = self._config.link_check
        every_s = settings.every_s if settings is not None else LinkCheckSettings.every_s
        if self._links_lock.acquire(blocking=self._links is None):
            try:
                last = self._links
                if last is None or not last.ok or self._monotonic() - self._links_at >= every_s:
                    self._check_links()
            finally:
                self._links_lock.release()
        report = self._links
        if report is None:  # pragma: no cover - the first check is waited for, so a report is there
            return Health(False, "the link check has not answered yet, so no run is admitted")
        if report.ok:
            return verdict
        if self._links_passed:
            return Health(False, f"a provider link stopped transferring during the campaign: {report.detail}")
        return Health(False, f"no run is admitted until every provider link transfers: {report.detail}")

    def _current_token(self) -> MintedToken:
        """Return the cached token, minting a new one when none is left or it is about to expire."""
        with self._lock:
            token = self._token
            if token is None or self._clock() > token.expires_at - REFRESH_BEFORE_S:
                spec = data_path_spec(self._config.space_ids(Plane.DATA_PATH))  # the data path's spaces, no other
                token = self._token = self._issuer.mint(spec, self._service, ceiling_s=TOKEN_CAP_S)
            return token

    def __call__(self) -> Health:
        """Return the platform's health: the ingests' baselines; with `[faults]`, the fault gate holding the federation
        around the rest; the content type kept from the first call; then the space. Never raise.

        While `[tls] insecure_tls` is on, every reading, healthy, failed or deferred, ends with a warning that no client
        of the federation checks its certificate (`tls.WARNING`); the verdict, a deferral included, is unchanged."""
        try:
            verdict = baseline_health(self._ingest_journals)  # first, and asking the federation nothing
            if verdict.ok:
                verdict = self._faults() if self._faults is not None else self._gate()
        except Exception as error:
            verdict = Health(False, f"health check raised {type(error).__name__}")
        return self._warned(verdict)

    def space(self) -> Health:
        """Each space the data path reaches against its declared support set alone (`_space`), without the content type
        check: what `__call__` answers once that check has passed, and the same warning when TLS is not verified; never
        raise."""
        return self._warned(self._space())

    def _warned(self, verdict: Health) -> Health:
        if self._config.tls.insecure:
            return verdict._replace(detail=f"{verdict.detail}; {WARNING}")
        return verdict

    def _space(self) -> Health:
        """Each declared space as Onezone reports it, against its declared support set; never raise.

        The scope is read once, with a data-path token, and every space the data path reaches is checked. With several
        spaces the first that fails is named by its position (`spaces[i]`), and a pass names how many were checked."""
        try:
            answer = self._client.query("/tokens/infer_access_token_scope", {"token": self._current_token().reveal()})
            if len(self._config.spaces) == 1:
                return compare(parse_scope(answer, self._config.space.id), self._config)
            checked = [entry for entry in self._config.spaces if Plane.DATA_PATH in entry.tokens]
            for entry in checked:
                verdict = compare(parse_scope(answer, entry.space.id), self._config, entry)
                if not verdict.ok:
                    return Health(False, f"{entry.position}: {verdict.detail}")
            return Health(True, f"ok: each of the {len(checked)} spaces is supported by its declared set")
        except _SAFE as error:
            return Health(False, f"health check failed: {error}")
        except Exception as error:
            return Health(False, f"health check raised {type(error).__name__}")

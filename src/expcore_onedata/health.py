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
provider is reported offline.

An answer that cannot be read is unhealthy, never a crash and never a pass: any failure, including an unexpected
exception, becomes `Health(False, reason)`. The reason names positions in the file (`providers[i]`), counts and the
ids the federation reports, never a token, URL, host or configured name.

When platform.toml has a `[faults]` table the check first runs the fault gate (`expcore_onedata.admission.FaultGate`),
before it asks Onezone anything: it recovers what a crashed run left open whenever no run is on the federation, then
fails while an injection is in progress or unsettled, because a provider taken down for one condition's run, and not
yet verifiably back, leaves the federation out of its baseline state for every other run, including runs of conditions
that use no injector. The gate is built in and cannot be left out; `fault_driver` replaces the Compose driver that
`[faults]` describes, for tests.

The check of the earlier study also read one known file back through its provider and compared its checksum with a study manifest. That
belongs to the study (its data) and not to the platform, so a study adds it with its own check.

**The content type the data gate streams on is part of this check (O05, O11), with no opt-out.** When any level of
`[sandbox.data_paths]` is REST, the first call measures it (`precampaign.content_type_check`: the provisioned
`[sandbox] content_check_file`, read whole and by range through the local provider) before anything else, and the
check keeps that verdict for the campaign, since a type once answered does not change: a refusal, or a type that could
not be measured, answers every call after it with its detail, so the gate stays closed and no run is admitted; a pass
lets every call answer as the space check does. With no REST data path nothing is read. The composition is
`precampaign.PreCampaignHealth`, built here, so a study that uses this check cannot leave the measurement out.
"""

import re
import threading
import time
from collections.abc import Callable

from expcore.campaign.healthgate import Health
from expcore.config.loading import ConfigError
from expcore_onedata.admission import FaultGate, fault_settings_of
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_recovery import FaultDriver
from expcore_onedata.fault_settings import compose_driver
from expcore_onedata.federation import ReportedProvider, SpaceView, declared_names, read_space
from expcore_onedata.precampaign import PreCampaignHealth, content_type_check
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import MintedToken, TokenIssuer, data_path_spec

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


def compare(view: SpaceView, config: PlatformConfig) -> Health:
    """Compare the providers the federation reports for the space with the declared set; any difference fails.

    Reported providers are matched to declared ones by provider id. A reported provider no declaration fits, and a
    declared provider that more than one reported provider fits (or a reported provider that fits several declarations),
    fail the check: the first match is never taken, so a second provider cannot hide behind the declared one.

    The detail holds no configured name, host, URL or id (O12). A declared provider is `providers[i]`, its position in
    platform.toml; a provider no declaration fits is shown by the id the federation reports for it.
    """
    if view.name != config.space.name:
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
    missing = set(config.support.providers) - set(matched)
    unexpected = set(matched) - set(config.support.providers)
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


def _fault_gate(config: PlatformConfig, driver: FaultDriver | None) -> FaultGate | None:
    """The fault gate when platform.toml has a `[faults]` table, None when it has none; a table that is not valid
    raises ConfigError here, so a platform with an injector never runs without the gate."""
    settings = fault_settings_of(config)
    if settings is None:
        return None
    return FaultGate(settings, driver if driver is not None else compose_driver(settings))


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
    ) -> None:
        if config.principals.service is None:
            raise ConfigError(f"{config.file}: [principals]: key 'service' is required for the health check")
        self._config, self._client, self._issuer, self._clock = config, client, issuer, clock
        self._service = config.principals.service
        self._token: MintedToken | None = None
        self._lock = threading.Lock()
        self._faults = _fault_gate(config, fault_driver)
        self._gate = PreCampaignHealth(self._space, lambda: content_type_check(config, client))

    def _current_token(self) -> MintedToken:
        """Return the cached token, minting a new one when none is left or it is about to expire."""
        with self._lock:
            token = self._token
            if token is None or self._clock() > token.expires_at - REFRESH_BEFORE_S:
                spec = data_path_spec(self._config.space.id)
                token = self._token = self._issuer.mint(spec, self._service, ceiling_s=TOKEN_CAP_S)
            return token

    def __call__(self) -> Health:
        """Return the platform's health: the content type kept from the first call, then the space; never raise."""
        try:
            return self._gate()
        except Exception as error:
            return Health(False, f"health check raised {type(error).__name__}")

    def _space(self) -> Health:
        """The space as Onezone reports it, against the declared support set; never raise."""
        try:
            if self._faults is not None:
                gate = self._faults()
                if not gate.ok:
                    return gate
            view = read_space(self._client, self._current_token().reveal(), self._config.space.id)
            return compare(view, self._config)
        except _SAFE as error:
            return Health(False, f"health check failed: {error}")
        except Exception as error:
            return Health(False, f"health check raised {type(error).__name__}")

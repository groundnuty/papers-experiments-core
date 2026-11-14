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
`ComposeDriver.restored_links`) for the link check of every pair the provider is in, and a pair that does not pass
fails the restore (`links_stuck`): the injection stays open and the fault gate fails, as for a restore that never
served, until a later clear finds every link transferring.
"""

from collections.abc import Callable

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_settings import FaultSettings
from expcore_onedata.linkcheck import check_links
from expcore_onedata.readretry import ONE_ATTEMPT

PROBE_TIMEOUT_S = 10.0


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
        """The link check (`linkcheck.check_links`) of every pair the restored provider is in, after its restore: None
        when each passed, else the failure, which names the pair and the remedy. A restarted provider's rtransfer link
        to a peer can come back stuck, with nothing logged, and the next run's reads through it would hang. Without a
        `[link_check]` nothing is checked: a federation with pairs and none is refused at its campaign's start."""
        if self._config.link_check is None:
            return None
        report = check_links(self._config, self._client, involving=self._provider(service))
        return None if report.ok else report.detail


def provider_serves(config: PlatformConfig, settings: FaultSettings, client: OnezoneClient) -> Callable[[str], bool]:
    """The probe for `compose_driver` (`ServingProbe`): whether the provider behind a service answers an authenticated
    REST read, and, through `ServingProbe.links`, whether its links transfer once it does."""
    return ServingProbe(config, settings, client)

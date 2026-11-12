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
"""

from collections.abc import Callable

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_settings import FaultSettings
from expcore_onedata.readretry import ONE_ATTEMPT

PROBE_TIMEOUT_S = 10.0


def _path(config: PlatformConfig, provider: str) -> str:
    """The read that shows a provider serving: its first declared space, or its configuration if it supports none."""
    for entry in config.spaces:
        if provider in entry.support.providers:
            return f"/spaces/{entry.space.id}"
    return "/configuration"


def provider_serves(config: PlatformConfig, settings: FaultSettings, client: OnezoneClient) -> Callable[[str], bool]:
    """The probe for `compose_driver`: `probe(service)` is True when the provider behind the service answers an
    authenticated REST read through the client, and False when it does not (yet)."""
    by_service = {service: provider for provider, service in settings.services.items()}

    def probe(service: str) -> bool:
        provider = by_service.get(service)
        if provider is None:
            raise ConfigError(f"{settings.file}: [faults.services]: the service is not one a provider is declared for")
        try:
            client.get(_path(config, provider), provider=provider, timeout_s=PROBE_TIMEOUT_S, retry=ONE_ATTEMPT)
        except PlatformError:  # its message names a position, never the provider; here it only says "not yet"
            return False
        return True

    return probe

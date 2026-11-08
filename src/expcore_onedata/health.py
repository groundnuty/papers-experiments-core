# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/health.py, blob d841c16205d7b5b545c0186eec9788b9965880e6
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/health.py:57-64 (the scope of a token)
#   changes: see git history of this file from its import commit
"""The platform's health check, run before and after every attempt (O04).

`PlatformHealth` implements the core's `HealthCheck` protocol: it is called with no arguments and returns
`Health(ok, detail)`. It reads Onezone's view of the study space with a short-lived data-path token of the configured
service principal, on the host and outside every run, and compares the providers that support the space with the
declared set (`[support] providers`). The declared set is the only expectation: a multi-provider federation passes when it
reports exactly its declared providers, and a provider that is reported but not declared fails the check as surely as a
declared one that is missing. The check also fails when the space's name differs from the declared one or a declared
provider is reported offline.

An answer that cannot be read is unhealthy, never a crash and never a pass: any failure, including an unexpected
exception, becomes `Health(False, reason)`. The reason names counts and configuration names, never a token, URL or host.

The check of the earlier study also read one known file back through its provider and compared its checksum with a study manifest. That
belongs to the study (its data) and not to the platform, so a study adds it with its own check.
"""

import threading
import time
from collections.abc import Callable

from expcore.campaign.healthgate import Health
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.federation import SpaceView, declared_name, read_space
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import MintedToken, TokenIssuer, data_path_spec

TOKEN_CAP_S = 300.0  # the check's own budget; its token outlives it by the configured margin
REFRESH_BEFORE_S = 120.0  # a cached token is replaced this long before it expires
_SAFE = (PlatformError, TokenRefused)  # whose messages hold no URL, id or value, so they can be shown


def compare(view: SpaceView, config: PlatformConfig) -> Health:
    """Compare the providers the federation reports for the space with the declared set; any difference fails."""
    if view.name != config.space.name:
        return Health(False, "the study space is reported under another name than the declared one")
    matched: dict[str, bool | None] = {}
    undeclared = 0
    for reported in view.providers:
        name = declared_name(config, reported)
        if name is None:
            undeclared += 1
        else:
            matched[name] = reported.online
    missing = sorted(set(config.support.providers) - set(matched))
    unexpected = sorted(set(matched) - set(config.support.providers))
    if missing or undeclared or unexpected:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if undeclared:
            parts.append(f"{undeclared} reported provider(s) not declared")
        if unexpected:
            parts.append(f"declared but not in the support set: {', '.join(unexpected)}")
        return Health(False, "the providers supporting the space differ from the declared set: " + "; ".join(parts))
    offline = sorted(name for name, online in matched.items() if online is False)
    if offline:
        return Health(False, f"declared provider(s) reported offline: {', '.join(offline)}")
    return Health(True, f"ok: the space is supported by exactly the {len(matched)} declared provider(s)")


class PlatformHealth:
    """The platform's `HealthCheck`; keep one for the campaign so that its token is reused until it nears its end."""

    def __init__(
        self,
        config: PlatformConfig,
        client: OnezoneClient,
        issuer: TokenIssuer,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if config.principals.service is None:
            raise ConfigError(f"{config.file}: [principals]: key 'service' is required for the health check")
        self._config, self._client, self._issuer, self._clock = config, client, issuer, clock
        self._service = config.principals.service
        self._token: MintedToken | None = None
        self._lock = threading.Lock()

    def _current_token(self) -> MintedToken:
        """Return the cached token, minting a new one when none is left or it is about to expire."""
        with self._lock:
            token = self._token
            if token is None or self._clock() > token.expires_at - REFRESH_BEFORE_S:
                spec = data_path_spec(self._config.space.id)
                token = self._token = self._issuer.mint(spec, self._service, ceiling_s=TOKEN_CAP_S)
            return token

    def __call__(self) -> Health:
        """Return the platform's health; never raise."""
        try:
            view = read_space(self._client, self._current_token().reveal(), self._config.space.id)
            return compare(view, self._config)
        except _SAFE as error:
            return Health(False, f"health check failed: {error}")
        except Exception as error:
            return Health(False, f"health check raised {type(error).__name__}")

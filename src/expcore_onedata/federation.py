# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/federation.py, blob 8195d16935d767970798b6c4ceb4b420f3bd633d
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The federation as Onezone reports it for the study space, and the agent's pin to its local provider (O03).

`read_space` asks Onezone for the scope of a token (`infer_access_token_scope`) and returns every provider that
supports the study space, as reported. Discovery therefore finds all of them, and a federation with several providers
shows several. The agent must not read through whichever one is found first, so `pin_provider` picks the one the
configuration declares as local (`[support] local`) and refuses when the space does not list it. Matching is by the
provider id when the file declares one, else by the host of the declared URL against the domain Onezone reports.

An answer that cannot be read is an error (`FederationUnreadable`), never an empty federation. No name, host or id is
written in this module: all of them come from the `PlatformConfig`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig, Provider


class FederationUnreadable(PlatformError):
    """Onezone's answer about the space is missing something the layer relies on."""


class PinRefused(RuntimeError):
    """The agent could not be pinned to its declared local provider."""


@dataclass(frozen=True)
class ReportedProvider:
    """A provider that supports the space, as Onezone describes it; a field Onezone left out is None."""

    id: str
    name: str | None
    domain: str | None
    online: bool | None


@dataclass(frozen=True)
class SpaceView:
    """The study space as the federation reports it: its name and every provider that supports it."""

    space_id: str
    name: str | None
    providers: tuple[ReportedProvider, ...]


@dataclass(frozen=True)
class Pinned:
    """The declared local provider, found among the space's providers."""

    provider: Provider
    reported: ReportedProvider


def _text(value: Any) -> str | None:
    """Return a value if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def parse_scope(answer: Any, space_id: str) -> SpaceView:
    """Read the space and its supporting providers out of a scope answer; raise FederationUnreadable if it has none."""
    try:
        scope = answer["dataAccessScope"]
        space = scope["spaces"][space_id]
        supports = space["supports"]
        details = scope.get("providers", {})
    except (KeyError, TypeError, AttributeError):
        raise FederationUnreadable("zone: the scope answer does not describe the study space") from None
    if not isinstance(supports, Mapping) or not supports or not isinstance(details, Mapping):
        raise FederationUnreadable("zone: the scope answer lists no supporting provider")
    providers = []
    for provider_id in sorted(supports):
        entry = details.get(provider_id)
        entry = entry if isinstance(entry, Mapping) else {}
        online = entry.get("online")
        providers.append(
            ReportedProvider(
                provider_id,
                _text(entry.get("name")),
                (_text(entry.get("domain")) or "").lower() or None,
                online if isinstance(online, bool) else None,
            )
        )
    return SpaceView(space_id, _text(space.get("name")), tuple(providers))


def read_space(client: OnezoneClient, token: str, space_id: str) -> SpaceView:
    """Ask Onezone which providers support the space, as seen by the token."""
    return parse_scope(client.query("/tokens/infer_access_token_scope", {"token": token}), space_id)


def is_declared(provider: Provider, reported: ReportedProvider) -> bool:
    """Tell whether a reported provider is a declared one: by id when declared, else by the URL's host."""
    if provider.id is not None:
        return reported.id == provider.id
    return reported.domain is not None and reported.domain == provider.host


def declared_name(config: PlatformConfig, reported: ReportedProvider) -> str | None:
    """Return the name of the declared provider a reported one is, or None when it is not declared."""
    return next((p.name for p in config.providers.values() if is_declared(p, reported)), None)


def pin_provider(view: SpaceView, local: Provider | None) -> Pinned:
    """Pin to the declared local provider among the space's providers, or refuse (O03).

    Several providers may support the space; only the declared one is returned. No declared provider, none of the
    space's providers being it, or more than one matching it, is a refusal.
    """
    if local is None:
        raise PinRefused("no local provider is declared, so the agent cannot be pinned to one")
    found = [r for r in view.providers if is_declared(local, r)]
    if not found:
        raise PinRefused(f"the declared local provider is not among the {len(view.providers)} providers of the space")
    if len(found) > 1:
        raise PinRefused("the declared local provider matches more than one provider of the space")
    return Pinned(local, found[0])


def pin_local_provider(view: SpaceView, config: PlatformConfig) -> Pinned:
    """Pin the agent to the configuration's local provider."""
    return pin_provider(view, config.local_provider)

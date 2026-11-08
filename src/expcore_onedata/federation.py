# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/federation.py, blob 8195d16935d767970798b6c4ceb4b420f3bd633d
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:494-505 (the providers from the scope)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/health.py:57-64 (the space and its supports)
#   changes: see git history of this file from its import commit
"""The federation as Onezone reports it for the study space, and the agent's pin to its local provider (O03).

`read_space` asks Onezone for the scope of a token (`infer_access_token_scope`) and returns every provider that
supports the study space, as reported. Discovery therefore finds all of them, and a federation with several providers
shows several. The agent must not read through whichever one is found first, so `pin_provider` picks the one the
configuration declares as local (`[support] local`) and refuses when the space does not list it. Matching is by the
provider id, which the file always declares (O04). Only a `Provider` built in code without an id is matched by the host
of its URL against the domain Onezone reports, and one host that fits several reported providers is ambiguous: it is
refused here and fails the health check, never resolved by taking the first.

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
    name: str
    domain: str
    online: bool


@dataclass(frozen=True)
class SpaceView:
    """The study space as the federation reports it: its name and every provider that supports it."""

    space_id: str
    name: str
    providers: tuple[ReportedProvider, ...]


@dataclass(frozen=True)
class Pinned:
    """The declared local provider, found among the space's providers."""

    provider: Provider
    reported: ReportedProvider


def _field(entry: Mapping[str, Any], key: str, kind: type, whose: str) -> Any:
    """Return a field of the scope answer that must be a non-empty string or a boolean; refuse naming the field."""
    value = entry.get(key)
    if not isinstance(value, kind) or (kind is str and not value):
        article = "a boolean" if kind is bool else "a non-empty string"
        raise FederationUnreadable(f"zone: the scope answer has no {key!r} for {whose}, or it is not {article}")
    return value


def parse_scope(answer: Any, space_id: str) -> SpaceView:
    """Read the space and its supporting providers out of a scope answer; raise FederationUnreadable if it has none."""
    try:
        scope = answer["dataAccessScope"]
        space = scope["spaces"][space_id]
        supports = space["supports"]
        details = scope.get("providers", {})
    except (KeyError, TypeError, AttributeError):
        raise FederationUnreadable("zone: the scope answer does not describe the study space") from None
    # The sources only iterate `supports` for provider ids (sandbox.py:500, health.py:64), so it is read as a mapping
    # keyed by id or as a list of ids. They read the space's `name` and `providers[id]["domain"]`; `providers[id].name`
    # and `.online` are not confirmed by any source, so a provider without either, or with another shape, is refused.
    listed = isinstance(supports, Mapping) or (isinstance(supports, list) and all(isinstance(i, str) for i in supports))
    ids = sorted(supports) if listed else []
    if not ids or not isinstance(details, Mapping):
        raise FederationUnreadable("zone: the scope answer lists no supporting provider")
    space_name = _field(space, "name", str, "the space")
    providers = []
    for provider_id in ids:
        entry = details.get(provider_id)
        if not isinstance(entry, Mapping):
            raise FederationUnreadable("zone: the scope answer has no entry in 'providers' for a supporting provider")
        providers.append(
            ReportedProvider(
                provider_id,
                _field(entry, "name", str, "a supporting provider"),
                _field(entry, "domain", str, "a supporting provider").lower(),
                _field(entry, "online", bool, "a supporting provider"),
            )
        )
    return SpaceView(space_id, space_name, tuple(providers))


def read_space(client: OnezoneClient, token: str, space_id: str) -> SpaceView:
    """Ask Onezone which providers support the space, as seen by the token."""
    return parse_scope(client.query("/tokens/infer_access_token_scope", {"token": token}), space_id)


def is_declared(provider: Provider, reported: ReportedProvider) -> bool:
    """Tell whether a reported provider is a declared one: by its id, or by the URL's host when it has no id."""
    if provider.id is not None:
        return reported.id == provider.id
    return reported.domain == provider.host


def declared_names(config: PlatformConfig, reported: ReportedProvider) -> list[str]:
    """Return the names of every declared provider a reported one fits: none, one, or (ambiguously) several."""
    return [p.name for p in config.providers.values() if is_declared(p, reported)]


def declared_name(config: PlatformConfig, reported: ReportedProvider) -> str | None:
    """Return the name of the declared provider a reported one is, or None when it is not declared.

    A reported provider that fits several declared ones is ambiguous and raises PinRefused, never the first of them.
    """
    names = declared_names(config, reported)
    if len(names) > 1:
        raise PinRefused("a reported provider fits more than one declared provider")
    return names[0] if names else None


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

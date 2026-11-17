"""Fake Onedata REST answers that carry a provider's or the zone's registered domain, and the search for a node's
name in them (O73).

Raw answers of the platform reach the agent without passing through onedata-mcp, and each carries the domain a service
registered under: the zone's scope answer (`providers.<id>.domain`), a no-connection error (`details.zoneDomain`,
`details.providerDomain`, `ctx.serviceDomain`) and the URLs inside an error's description. `answers(zone, provider)`
builds one of each channel for the domains given; the registration decides whether those are the layout's declared
names or a node's own, which is what `leaks` tells apart.
"""

import json
import re
from collections.abc import Iterable

CHANNELS = ("scope answer", "ctx.serviceDomain", "description URL", "details.providerDomain", "details.zoneDomain")


def answers(zone: str, provider: str) -> dict[str, tuple[int, str]]:
    """One answer per channel, `(status, body)`, for a zone and a provider registered under these domains."""
    return {
        "scope answer": (
            200,
            json.dumps({"providers": {"p-1": {"name": "alpha", "domain": provider, "online": True}}, "space": {}}),
        ),
        "ctx.serviceDomain": (
            503,
            json.dumps({"error": {"id": "errorServiceUnavailable", "details": {"ctx": {"serviceDomain": provider}}}}),
        ),
        "description URL": (
            503,
            json.dumps(
                {
                    "error": {
                        "id": "errorUnexpectedEndpointResponse",
                        "description": f"The peer answered with a redirect to https://{provider}:8443/api/v3/x.",
                    }
                }
            ),
        ),
        "details.providerDomain": (
            503,
            json.dumps(
                {"error": {"id": "errorNoConnectionToPeerOneprovider", "details": {"providerDomain": provider}}}
            ),
        ),
        "details.zoneDomain": (
            503,
            json.dumps({"error": {"id": "errorNoConnectionToOnezone", "details": {"zoneDomain": zone}}}),
        ),
    }


def node_patterns(words: Iterable[str], port: str = "8443") -> list[re.Pattern[str]]:
    """Each word (a node's name or address) as a token of its own, in any case, and the nodes' public port as one."""
    found = [re.compile(rf"(?<![\w.-]){re.escape(w)}(?![\w-])", re.IGNORECASE) for w in words]
    return [*found, re.compile(rf"(?<![\w.-]){port}(?![\w-])")]


def leaks(bodies: dict[str, tuple[int, str]], patterns: list[re.Pattern[str]]) -> dict[str, int]:
    """The channels whose body holds anything the patterns name, by the number of patterns that matched."""
    return {
        channel: hits
        for channel, (_, body) in bodies.items()
        if (hits := sum(1 for pattern in patterns if pattern.search(body)))
    }

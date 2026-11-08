# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/providers.py:915-923
"""What a record says about the route a request took: the URL without credentials, SDK versions, settings sent (B15)."""

import copy
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Request fields that carry the conversation, not settings; the transcript holds those.
CONVERSATION_FIELDS = ("messages", "tools", "system")
_UNPARSEABLE = "<unparseable url>"


def public_url(url: str | None) -> str | None:
    """Return the URL without credentials, query or fragment, so no record holds a secret that rode in a URL."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return _UNPARSEABLE
    if ":" in host:
        host = f"[{host}]"
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def sdk_version(package: str) -> str | None:
    """Return the installed version of an SDK, or None when it is not installed."""
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def settings_sent(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the request without its conversation: the settings the endpoint was actually sent.

    Taken from the request object itself, never from the configuration it was built from (B15).
    """
    return {k: copy.deepcopy(v) for k, v in request.items() if k not in CONVERSATION_FIELDS}

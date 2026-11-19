# Provenance: new
"""Tell a backend that sends a session header which attempt it serves (B20).

A model entry that sets `session_header` makes its adapter send one opaque id on every request of an attempt
(`expcore.models.fleetheaders`). The adapter is built before the attempt has its number, so the attempt binds the id
once it does. A backend that names no such header, or a plugin's own backend with no `bind_session`, is not touched.
"""

import json
from typing import Any

from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.store import Store
from expcore.models.fleetheaders import session_id


def bind_session(backend: Any, store: Store, run_key: str, attempt: int, lease_id: int | None) -> None:
    """Bind `backend` to the attempt when it sends a session header: the id is a hash of the stored campaign
    fingerprint's hash, the run key, the attempt number and the lease, so it never holds the run key itself."""
    bind = getattr(backend, "bind_session", None)
    if bind is None or not getattr(backend, "session_header", None):
        return
    stored = store.get_meta(FINGERPRINT_KEY)
    try:
        fingerprint = json.loads(stored) if stored else None
    except ValueError:
        fingerprint = None
    digest = fingerprint_sha256(fingerprint) if isinstance(fingerprint, dict) else None
    bind(session_id(digest, run_key, attempt, lease_id))

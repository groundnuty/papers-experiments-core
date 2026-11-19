# Provenance: new
"""The two optional headers of an `openai_compat` model behind a fleet's front (F01, F02).

`session_header` names a request header that carries one opaque id for the whole of an attempt, so the front can keep
an attempt's requests together. The id is `session_id`: a SHA-256 over the campaign, the run, the attempt and the lease,
cut to 32 hex characters. It never holds the run key, which may name a study's conditions.

`deployment_header` names a response header in which the front says which deployment served the reply. The adapter
reads it on every reply and records it on the turn (`clean_deployment_id` says what is kept); a reply without it, or
with a value that is not a plain identifier, has no deployment.

A header name is an HTTP token, and never one the client sets itself: the key rides in `Authorization`.
"""

import hashlib
import json
import re
import secrets

SESSION_ID_LENGTH = 32  # hex characters
DEPLOYMENT_ID_MAX = 128  # characters
HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
# Headers the HTTP client or the SDK sets itself; a model that named one would override it.
CLIENT_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "proxy-authorization",
        "transfer-encoding",
        "user-agent",
    }
)


def header_name_problem(name: object) -> str | None:
    """Why `name` cannot be a header a model entry names, or None: an HTTP token of at most 64 characters, and not one
    the client sets itself."""
    if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
        return "must be an HTTP header name (letters, digits and !#$%&'*+.^_`|~-, at most 64 characters)"
    if name.lower() in CLIENT_HEADERS or name.lower().startswith("x-stainless-"):
        return f"names {name!r}, a header the client sets itself"
    return None


def session_id(fingerprint_sha256: str | None, run_key: str, attempt: int, lease_id: int | None = None) -> str:
    """The opaque id of one attempt: the first 32 hex characters of the SHA-256 of the campaign fingerprint's hash, the
    run key, the attempt number and the lease (None for an attempt that ran on none), as a JSON list so no part can
    run into the next. Two attempts of one run differ, and so do two leases' attempts of one number (C41)."""
    text = json.dumps([fingerprint_sha256 or "", run_key, attempt, lease_id], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:SESSION_ID_LENGTH]


def random_session_id() -> str:
    """An id for a backend nobody bound to an attempt (an admission probe): one per backend, never the same twice."""
    return secrets.token_hex(SESSION_ID_LENGTH // 2)


def deployment_id_ok(value: object) -> bool:
    """Tell whether a deployment id is one the record keeps: printable text with no space at either end, 1 to 128
    characters."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= DEPLOYMENT_ID_MAX
        and value.isprintable()
        and value == value.strip()
    )


def clean_deployment_id(raw: object) -> str | None:
    """The deployment a reply's header names, as the record keeps it: the value without the space round it, or None for
    no header, an empty one, or one that is too long or not printable. A header is untrusted input."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value if deployment_id_ok(value) else None

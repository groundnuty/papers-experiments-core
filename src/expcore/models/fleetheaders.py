# Provenance: new
"""The two optional headers of an `openai_compat` model behind a fleet's front (B20).

`session_header` names a request header that carries one opaque id for the whole of an attempt, so the front can keep
an attempt's requests together. The id is `session_id`: a SHA-256 over the campaign, the run, the attempt and the lease,
cut to 32 hex characters. It never holds the run key, which may name a study's conditions.

`deployment_header` names a response header in which the front says which deployment served the reply. The adapter
reads it on every reply and records it on the turn (`clean_deployment_id` says what is kept); a reply without it, or
with a value that is not a plain identifier, has no deployment.

`extra_headers` is a table of request headers sent, with their values, on every request of the model (the fleet's front
routes a model's runs by a workload header). A name passes the same check as `session_header`; a value is 1 to 256
printable ASCII characters, never one that looks like a credential.

A header name is an HTTP token, and never one the client sets itself: the key rides in `Authorization`.
"""

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Mapping

SESSION_ID_LENGTH = 32  # hex characters
DEPLOYMENT_ID_MAX = 128  # characters
EXTRA_HEADER_VALUE_MAX = 256  # characters
CREDENTIAL_SCHEMES = ("bearer ", "basic ")  # a value that starts so is an Authorization value in the wrong header
# a name that has one of these in it is a credential's header (`X-API-Key`), whatever the value looks like
CREDENTIAL_NAME_PARTS = ("key", "token", "secret", "password", "auth", "credential")
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


def header_name_problem(name: object, request: bool = False) -> str | None:
    """Why `name` cannot be a header a model entry names, or None: an HTTP token of at most 64 characters, and not one
    the client sets itself. A request header (`session_header`) may not be one of the SDK's own (`OpenAI-Project` would
    be replaced by the attempt's id and a project-checking endpoint would refuse the request)."""
    if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
        return "must be an HTTP header name (letters, digits and !#$%&'*+.^_`|~-, at most 64 characters)"
    low = name.lower()
    if low in CLIENT_HEADERS or low.startswith("x-stainless-") or (request and low.startswith("openai-")):
        return f"names {name!r}, a header the client sets itself"
    return None


def header_value_problem(value: object) -> str | None:
    """Why `value` cannot be the value of an extra request header, or None: text of 1 to 256 printable ASCII characters
    (so no line break can split the request) with no space at either end, and not a bearer or basic credential. It
    never repeats the value."""
    if not isinstance(value, str):
        return "must be text"
    if not value:
        return "must not be empty"
    if len(value) > EXTRA_HEADER_VALUE_MAX:
        return f"must be at most {EXTRA_HEADER_VALUE_MAX} characters"
    if not all(" " <= c <= "~" for c in value):
        return "must be printable ASCII with no line break"
    if value != value.strip():
        return "must have no space at either end"
    if value.lower().startswith(CREDENTIAL_SCHEMES):
        return "looks like a credential, and a key is never sent in a header of the entry"
    return None


def extra_headers_problems(backend: str, headers: object, session_header: str | None) -> list[str]:
    """Why a model entry's `extra_headers` cannot be used, as messages that name the header and never its value; none
    for an empty table. Only an `openai_compat` model sends them; a name may not be the model's own `session_header`
    (any case), and two names that differ only in case would let one silently win."""
    if not isinstance(headers, Mapping):
        return ["extra_headers must be a table of header name to value"]
    if not headers:
        return []
    if backend != "openai_compat":
        return ["extra_headers applies to openai_compat models only"]
    own = session_header.lower() if isinstance(session_header, str) else None
    problems: list[str] = []
    seen: set[str] = set()
    for name, value in headers.items():
        if problem := header_name_problem(name, request=True):
            problems.append(f"extra_headers entry {name!r} {problem}")
            continue
        if name.lower() in seen:
            problems.append(f"extra_headers names {name!r} twice, ignoring case")
        seen.add(name.lower())
        if any(part in name.lower() for part in CREDENTIAL_NAME_PARTS):
            problems.append(f"extra_headers entry {name!r} names a credential's header; the key is never sent in one")
        if name.lower() == own:
            problems.append(f"extra_headers entry {name!r} is the model's session_header, which carries the attempt id")
        if problem := header_value_problem(value):
            problems.append(f"extra_headers entry {name!r}: the value {problem}")
    return problems


def session_id(fingerprint_sha256: str | None, run_key: str, attempt: int, lease_id: int | None = None) -> str:
    """The opaque id of one attempt: the first 32 hex characters of the SHA-256 of the campaign fingerprint's hash, the
    run key, the attempt number and the lease (None for an attempt that ran on none), as a JSON list so no part can
    run into the next. Two attempts of one run differ, and so do two leases' attempts of one number (C41)."""
    text = json.dumps([fingerprint_sha256 or "", run_key, attempt, lease_id], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:SESSION_ID_LENGTH]


def random_session_id() -> str:
    """An id for a backend nobody bound to an attempt (an admission probe): one per backend, never the same twice."""
    return secrets.token_hex(SESSION_ID_LENGTH // 2)


def deployment_switched(ids: Iterable[str | None]) -> bool:
    """Tell whether an attempt's turns show more than one deployment (B20): the ids of its turns, None for a turn whose
    reply named none, count as distinct values when at least one turn named one. A front that overflows to another
    service without strict stickiness shows as a turn without the header among turns with it; turns that all lack it
    are no switch (a model that is never served through a front). Visibility only: nothing is set aside for it."""
    seen = list(ids)
    return any(i is not None for i in seen) and len(set(seen)) > 1


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

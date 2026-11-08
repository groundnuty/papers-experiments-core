# Provenance: new
#   merged: [S1] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:113-117,197-204, day0/scripts/od.py:63-77
#   merged: [S2] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:120, day0/scripts/od.py:122
#   merged: [S3] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:342,345
#   merged: [S4] icsa-2027-experiments @ 6fc798cf, icsa/knowledge/live.py:36,52, icsa/knowledge/tokencheck.py:47-49
#   merged: [S5] onedata-mcp @ 0cc17ce, onedata_mcp/api/tokens.py:24-25, test/unit/api/test_tokens.py:20
#   merged: [S6] icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:494-505, icsa/harness/health.py:57-64
#   merged: [S7] onedata-mcp @ 0cc17ce, onedata_mcp/api/spaces.py:90
"""The one HTTP client of the platform layer, and the gate every mutation goes through (O09).

Every request the layer sends leaves through `OnezoneClient`; no other module imports httpx (a test holds that). The
wrapper keeps its `httpx.Client` private, so a caller cannot send around it, and the client is injectable: a test hands
it one built on a mock transport.

Reads and mutations are separate calls:
- `get` is a GET, and `query` a POST from a short list that only reads (examining a token, inferring its scope);
- `mutate` is any other method. It takes no name from its caller: the path must lie under `/spaces/{space id}`, the
  client reads that space's name from Onezone (`space_name`) right before sending and refuses unless the name starts
  with one of the configured prefixes (`[space] prefixes`). The name is read afresh for every mutation and never
  cached, because a space can be renamed out of the study's scope after an earlier read; a cache would go on
  authorising it. A path that does not lie under a space, a file id or a path of a provider among them, resolves to no
  space and is refused, so a mistaken or hostile target cannot reach a space outside the study's. What remains is the
  interval between that read and the platform acting on the request, which a client cannot close;
- `mint_own_token` is the one operation on credentials: a platform user mints its own temporary token with its own
  password (HTTP Basic), as the earlier code did. It acts on no space, so it has a call of its own; the password is
  registered as a secret when it is read and the minted value before the call returns (R12), and so is the admin token
  the client itself uses, when the client is built. Tokens are not revoked: they end with their lifetime.

The other halves of O09 (refuse to evict the data provider's own copy, tear down only logged ids, a teardown that finds
files left revokes nothing) belong to provisioning and the sandbox (step 4b); they call `mutate`.

A failure raises `PlatformError` with the status and Onedata's error id only. The message never holds a URL, a body or a
header, and names a provider by its position (`providers[1]`), so neither a token nor a federation name reaches a log
or a record through it.

Every REST path and payload the layer uses, with where it was confirmed. The sources `[S1]` to `[S7]` are the
`merged:` lines of this file's header (repository, commit, file:line); a path is confirmed when working code or a
client test in a source sends it and reads the answer as this layer does.

- Authentication: the header `X-Auth-Token` with an access token. Confirmed by [S2].
- `get`: `GET /spaces/{space}/users/{user}/privileges` and `GET /spaces/{space}/effective_users/{user}/privileges`; the
  answer key is `privileges`. Confirmed by [S3].
- `space_name`: `GET /spaces/{space}` with the admin token, the answer key `name`. Unconfirmed: see
  `UNVERIFIED_ENDPOINTS`.
- `query`: `POST /tokens/examine`, body `{"token"}`, sent with the admin token; the answer keys `id` and `caveats` are
  read. Confirmed by [S4] and [S5].
- `query`: `POST /tokens/infer_access_token_scope`, body `{"token"}`, no header; the answer's
  `dataAccessScope.spaces[*].name` and `.supports` and `.providers[*].domain` are read. Confirmed by [S6].
- `mint_own_token`: `POST /user/tokens/temporary` with `Authorization: Basic` of the user itself, body
  `{"type": {"accessToken": {}}, "caveats": [...]}`, answer key `token`. Confirmed by [S1].
- Provider base `/api/v3/oneprovider`, which step 4b uses; step 4a sends nothing to a provider. Confirmed by [S7].
(All Onezone paths are under `/api/v3/onezone`, confirmed by [S1].)

No source revokes a token, so this client has no call that does: a token ends with the lifetime it was minted with,
which derives from the attempt's whole-time ceiling (O06). `UNVERIFIED_ENDPOINTS` lists what no source confirms, and
each entry has a live contract test (`tests/live/test_contract.py`) that fails if the deployment answers otherwise.
"""

import base64
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from expcore.config.loading import ConfigError
from expcore.records.redact import register_secret
from expcore_onedata.config import PlatformConfig
from expcore_onedata.credentials import Principal

API_ZONE = "/api/v3/onezone"  # [S1] the prefix of every Onezone call
API_PROVIDER = "/api/v3/oneprovider"  # [S7]
AUTH_HEADER = "X-Auth-Token"  # [S2]
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
# POSTs that only read, and whether they are sent with the admin credential: the examination is [S4], [S5], called as
# an admin; the scope inference is [S6], called without a header.
READING_POSTS: Mapping[str, bool] = {"/tokens/examine": True, "/tokens/infer_access_token_scope": False}

# What no source confirms: (method, path, what is unconfirmed). Each has a test in tests/live/test_contract.py marked
# `covers:` with its method and path; a unit test holds that none is missing there.
UNVERIFIED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    (
        "GET",
        "/spaces/{space}",
        "the whole call: no source reads a space's details; the answer's name field is what the mutation gate checks",
    ),
    (
        "POST",
        "/tokens/examine",
        "the shape of the answer's subject (an id under subject); the sources read id and caveats [S4]",
    ),
    (
        "POST",
        "/tokens/infer_access_token_scope",
        "the providers' name and online fields; the sources read the space's name and supports and the domain [S6]",
    ),
)
_ERROR_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SPACE_ID = re.compile(r"[A-Za-z0-9_@=-][A-Za-z0-9_.@=-]{0,127}")  # one plain path segment, never "." or ".."
DEFAULT_TIMEOUT_S = 15.0


class PlatformError(RuntimeError):
    """A request failed or was answered with something unreadable; the message holds no URL, body or header."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OutOfScope(PlatformError):
    """A mutation's target is no study space, or a path leaves the API; the mutation was not sent."""


def _error_code(response: httpx.Response) -> str | None:
    """Return Onedata's error id from an error answer when it has the plain shape of one."""
    try:
        code = response.json()["error"]["id"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if isinstance(code, str) and _ERROR_ID.fullmatch(code) else None


def _check_path(path: str) -> str:
    """Refuse a path that is not a plain absolute path of the API, so a caller cannot name another host or climb out."""
    if not path.startswith("/") or path.startswith("//") or "://" in path or ".." in path.split("/") or "\\" in path:
        raise OutOfScope("the request path is not a plain path of the API")
    return path


def _target_space_id(path: str) -> str:
    """Return the id of the space a mutation's path lies under, or raise OutOfScope when it lies under none.

    Only `/spaces/{id}` and what is below it name a space. The path must be plain, with no percent-escape, query or
    fragment that the server could read as another path than the one read here.
    """
    if any(mark in path for mark in "%?#"):
        raise OutOfScope("the request path of a mutation must be plain, without an escape, query or fragment")
    parts = path.split("/")
    if len(parts) < 3 or parts[1] != "spaces" or not _SPACE_ID.fullmatch(parts[2]) or "" in parts[2:-1]:
        raise OutOfScope("the mutation does not lie under a space, so it resolves to no space; nothing was sent")
    return parts[2]


class OnezoneClient:
    """Reads, token operations and scoped mutations against Onezone and the declared providers."""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        http: httpx.Client | None = None,
        admin_token: str | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """Build the client; the admin credential comes from `admin_token` or else from the configured source."""
        self._config = config
        self._http = http if http is not None else httpx.Client(timeout=timeout_s)
        self._owns_http = http is None
        self._timeout_s = timeout_s
        self._environ = environ
        credential = admin_token if admin_token is not None else config.admin.read(environ)
        register_secret(credential)
        self._admin = {AUTH_HEADER: credential}

    def close(self) -> None:
        """Close the connection pool when the client built it."""
        if self._owns_http:
            self._http.close()

    def __repr__(self) -> str:
        """Name the client without its credential."""
        return f"OnezoneClient(zone={self._config.zone.name!r})"

    def _base(self, provider: str | None) -> tuple[str, str]:
        """Return the base URL and the label used in messages: `zone`, or `providers[i]` for the i-th declared one.

        The label is a role or a position in the file, never the provider's name, so a message holds no federation name.
        """
        if provider is None:
            return self._config.zone.url + API_ZONE, "zone"
        if provider not in self._config.providers:
            raise OutOfScope("the request names a provider that is not declared")
        return self._config.providers[provider].url + API_PROVIDER, self._config.provider_label(provider)

    def _send(
        self, method: str, path: str, *, provider: str | None, headers: Mapping[str, str], json: Any = None
    ) -> Any:
        """Send one request and return the decoded JSON; map every failure to a PlatformError without detail."""
        base, label = self._base(provider)
        url = base + _check_path(path)
        try:
            response = self._http.request(method, url, headers=dict(headers), json=json, timeout=self._timeout_s)
        except httpx.HTTPError as error:
            raise PlatformError(f"{label}: {method} failed ({type(error).__name__})") from None
        if response.status_code >= 400:
            code = _error_code(response)
            raise PlatformError(
                f"{label}: {method} answered {response.status_code}" + (f" ({code})" if code else ""),
                status=response.status_code,
                code=code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise PlatformError(f"{label}: {method} answered something that is not JSON") from None

    def get(self, path: str, *, provider: str | None = None, token: str | None = None) -> Any:
        """GET a path of Onezone or of a declared provider, with the admin credential or as the token given.

        The privilege reads of tokens.py are the layer's only callers in step 4a ([S3]).
        """
        headers = {AUTH_HEADER: token} if token is not None else self._admin
        return self._send("GET", path, provider=provider, headers=headers)

    def query(self, path: str, body: Mapping[str, Any]) -> Any:
        """POST to one of the endpoints that only read (`READING_POSTS`); any other path is refused."""
        if path not in READING_POSTS:
            raise OutOfScope("the path is not one of the read-only POSTs; use mutate for a request that changes state")
        headers = self._admin if READING_POSTS[path] else {}
        return self._send("POST", path, provider=None, headers=headers, json=dict(body))

    def space_name(self, space_id: str) -> str:
        """Read the name Onezone gives a space now; the answer is never kept, so none can authorise a later request.

        A space Onezone does not know, or an answer without a name, raises OutOfScope; any other failure of the read
        raises PlatformError. Neither message holds the id or a name.
        """
        if not isinstance(space_id, str) or not _SPACE_ID.fullmatch(space_id):
            raise OutOfScope("the target space id is not a plain id, so it resolves to no space")
        try:
            answer = self._send("GET", f"/spaces/{space_id}", provider=None, headers=self._admin)
        except PlatformError as error:
            if error.status == 404:
                raise OutOfScope("the target space is unknown to Onezone, so it resolves to no space") from None
            raise
        name = answer.get("name") if isinstance(answer, dict) else None
        if not isinstance(name, str) or not name:
            raise OutOfScope("Onezone's answer for the target space holds no name, so it resolves to no space")
        return name

    def mutate(self, method: str, path: str, *, provider: str | None = None, json: Any = None) -> Any:
        """Send a request that can change state; refuse it, before it is sent, unless it acts on a study space.

        The target is the space id in the path, resolved to its name by a read on Onezone made for this mutation alone;
        the caller names no space and no name, and no earlier answer is reused. The mutation is refused unless that name
        starts with a configured prefix, and when the path lies under no space.
        """
        if method not in MUTATING:
            raise OutOfScope(f"{method} is not a mutating method; use get or query")
        path = _check_path(path)
        self._base(provider)  # an undeclared provider is refused before anything is read
        name = self.space_name(_target_space_id(path))
        if not name.startswith(self._config.space.prefixes):
            raise OutOfScope("the target space is not one of the configured prefixes; nothing was sent")
        return self._send(method, path, provider=provider, headers=self._admin, json=json)

    def mint_own_token(self, principal: Principal, caveats: Sequence[Mapping[str, Any]]) -> str:
        """Mint a temporary access token for a user, as that user, with exactly these caveats; return the value.

        The user authenticates with its own password (HTTP Basic), read from its credential file the moment it is
        needed and registered as a secret, and the minted value is registered before it is returned (R12). [S1]
        """
        users = self._config.users
        if users is None:
            raise PlatformError("zone: platform.toml declares no [credentials.users], so no user can mint a token")
        try:
            password = users.read(principal.username, self._environ)
        except ConfigError as error:
            raise PlatformError(f"zone: a user's credential cannot be used ({error})") from None
        basic = base64.b64encode(f"{principal.username}:{password}".encode()).decode()
        body = {"type": {"accessToken": {}}, "caveats": [dict(c) for c in caveats]}
        answer = self._send(
            "POST", "/user/tokens/temporary", provider=None, headers={"Authorization": f"Basic {basic}"}, json=body
        )
        value = answer.get("token") if isinstance(answer, dict) else None
        if not isinstance(value, str) or not value:
            raise PlatformError("zone: the answer to a token request holds no token")
        register_secret(value)
        return value

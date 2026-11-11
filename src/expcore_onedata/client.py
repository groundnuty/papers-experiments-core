# Provenance: new
#   merged: [S1] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:113-117,197-204, day0/scripts/od.py:63-77
#   merged: [S2] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:120, day0/scripts/od.py:122
#   merged: [S3] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:342,345
#   merged: [S4] icsa-2027-experiments @ 6fc798cf, icsa/knowledge/live.py:36,52, icsa/knowledge/tokencheck.py:47-49
#   merged: [S5] onedata-mcp @ 0cc17ce, onedata_mcp/api/tokens.py:24-25, test/unit/api/test_tokens.py:20
#   merged: [S6] icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:494-505, icsa/harness/health.py:57-64
#   merged: [S7] onedata-mcp @ 0cc17ce, onedata_mcp/api/spaces.py:90
#   merged: [S8] icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:114-139,170-171,246-247
#   merged: [S8] icsa-2027-experiments @ 6fc798cf, icsa/scripted/mplus.py:144-152
#   merged: [S9] icsa-2027-experiments @ 6fc798cf, icsa/sandbox/mock_onedata.py:78-95
#   merged: [S10] onedata-mcp @ 0cc17ce, onedata_mcp/api/transfers.py:28-63, benchmark/fixture_runner.py:269-281
#   merged: [S10] onedata-mcp @ 0cc17ce, benchmark/oracles/placement.py:432-440
#   merged: [S11] icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:215-216,236-240
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
  authorising it. A file addressed by id (`/data/{id}`, a transfer job's `fileId`, a range read) is first resolved to
  the id of the space it lies in, by walking its parent links up to the root of a space the configuration knows
  (`file_space_id`, below), and that space's name is then read and checked the same way, afresh every time. Anything
  else, a path of a provider among it, resolves to no space and is refused, so a mistaken or hostile target cannot
  reach a space outside the study's. What remains is the interval between the name's read and the platform acting on
  the request, which a client cannot close;
- `mint_own_token` is the one operation on credentials: a platform user mints its own temporary token with its own
  password (HTTP Basic), as the earlier code did. It acts on no space, so it has a call of its own; the password is
  registered as a secret when it is read and the minted value before the call returns (R12), and so is the admin token
  the client itself uses, when the client is built. Tokens are not revoked: they end with their lifetime.

**The space of a file.** Onedata 25.0 gives a file no path and no space attribute, so the gate walks the file's
parent links: `GET /data/{id}?attribute=parent_id` through the provider the request is for, from the file up, until it
meets the root of a space the configuration knows (`[space] id`, whose root is the `fileId` of the provider's
`GET /spaces/{id}`). The root itself needs no parent read. A walk that reaches the top of a tree (a null parent) without
meeting a known root, or that has followed `MAX_SPACE_DEPTH` links without meeting one (a cycle among them), resolves to
no space (`OutOfScope`). The space is never decoded from the file's id: that encoding is undocumented and may change
between releases. A file's space does not change, so what a walk read is kept for the client's life, a run's or a
campaign's: each provider's root of the space, kept once a provider answered it, and each parent link, which names
files by their ids on every provider, kept only once its walk reached a known root; a walk that failed or was refused
keeps no link, so a later request walks again. A file moved within the space keeps its space, so a link it left behind
still leads to the right root. A file resolved before costs no request to a provider, a new file beside resolved ones
costs one, and a cold walk costs one root read and one parent read per level. The name is then one read on Onezone,
every time.

Every read the gate makes, the walk's and the name's, is the client's own: with the admin credential, through its own
connection pool with the platform's `[tls]`, from the core's process. None passes the data gate, so none is in the
gate's log or counted in what the agent was served, and a write sent as a data-path token (`mutate(..., token=)`) is
still resolved as the admin. When one of those reads fails, the request is refused unsent with `ScopeUnread`, which
keeps the read's status, error id and transport flag; a 404 of the target or of a directory above it is `OutOfScope`.
Both are `NotSent`, so a caller can tell a request the platform never saw from one it answered.

The other halves of O09 (refuse to evict the data provider's own copy, tear down only logged ids, a teardown that finds
files left revokes nothing) belong to provisioning and the sandbox (step 4b); they call `mutate`.

A failure raises `PlatformError` with the status and Onedata's error id only. The message never holds a URL, a body or a
header, and names a provider by its position (`providers[1]`), so neither a token nor a federation name reaches a log
or a record through it.

Every REST path and payload the layer uses, with where it was confirmed. The sources `[S1]` to `[S11]` are the
`merged:` lines of this file's header (repository, commit, file:line); a path is confirmed when working code or a
client test in a source sends it and reads the answer as this layer does. `[L1]` is the live contract tests
(`tests/live/test_contract.py`) and the direct reads recorded beside them, run against Onedata 25.0, the release the
deployments of record run, on the Compose federation of `deploy/compose-federation` on 10 Nov 2026: its day-0 reports,
`day0/2026-11-10-federation-facts.md`, which says which fact a test showed and which a direct read (an eviction's
record was read directly, since no live test ran one), and `day0/2026-11-10-federation-facts-parent-walk.md`, the run
after the gate's walk by parent links, with the reads that walk rests on.

- Authentication: the header `X-Auth-Token` with an access token. Confirmed by [S2].
- `get`: `GET /spaces/{space}/users/{user}/privileges` and `GET /spaces/{space}/effective_users/{user}/privileges`; the
  answer key is `privileges`. Confirmed by [S3].
- `space_name`: `GET /spaces/{space}` with the admin token, the answer key `name`. Confirmed by [L1].
- `query`: `POST /tokens/examine`, body `{"token"}`, sent with the admin token; the answer keys `id` and `caveats` are
  read. Confirmed by [S4] and [S5]; the subject's id, under `subject`, by [L1].
- `query`: `POST /tokens/infer_access_token_scope`, body `{"token"}`, no header; the answer's
  `dataAccessScope.spaces[*].name` and `.supports` and `.providers[*].domain` are read. Confirmed by [S6]; the
  providers' `name` and `online` by [L1]. A token past its time caveat is answered 400 `tokenCaveatUnverified` [L1].
- `mint_own_token`: `POST /user/tokens/temporary` with `Authorization: Basic` of the user itself, body
  `{"type": {"accessToken": {}}, "caveats": [...]}`, answer key `token`. Confirmed by [S1].
- Provider base `/api/v3/oneprovider`, which step 4b uses; step 4a sends nothing to a provider. Confirmed by [S7].
- Step 4b2 sends to a provider (all confirmed by [S8], whose code sends them and reads the answers as the layer does):
  `GET /spaces/{space}` (the key `fileId` is the space's root; [L1] shows it the same on every provider),
  `GET /data/{id}/children` (query `limit`, `attribute`, `token`; answer `children`, `isLast`, `nextPageToken`),
  `POST /data/{parent}/children` (query `name`, `type`; answer
  `fileId`), `PUT /data/{id}/content` (query `offset`, an octet-stream body, marked so: [L1] shows 25.0 answering
  415 to one without a `Content-Type`), `GET /data/{id}`, `DELETE /data/{id}`,
  `GET /data/{id}/distribution`, `POST /transfers` (body `type`, `dataSourceType`, `fileId`, `replicatingProviderId`
  or `evictingProviderId`) and `GET /transfers/{id}`. `read_range` is `GET /data/{id}/content` with a `Range` header,
  answered with a 206 and a `Content-Range`. `content_answer` is the same call whole or with `Range: bytes=0-0`, of
  which only the status and the `Content-Type` are read: the type it measures (`precampaign.py`),
  `application/octet-stream` for both, is confirmed by [L1].
- `file_space_id`: `GET /data/{id}` with the query `attribute=parent_id`, answered `{"parent_id": <id>}`, and
  `{"parent_id": null}` for a space's root. Confirmed by [L1] (a file two levels down, its directory and the root, on
  each of the three providers), which also refuted the `path` attribute the gate read before: [S9], the earlier
  study's mock of what the official client calls, answered it as `/{space name}/...`, and Onedata 25.0 answers
  `attribute=path` with 400 `badValueNotAllowed`. No release at hand answers it, so the gate no longer asks for it.
- The probes' transfer log (`transferlog.py`) reads `GET /spaces/{space}/transfers` on a provider (query `state`,
  `limit`, `page_token`; answer `transfers`, a list of ids, and `nextPageToken`, followed until empty), confirmed by
  [S10] for the states `ongoing` and `ended`, and each id's `GET /transfers/{id}`, whose `fileId` [S10] and state and
  counters [S11] are confirmed. The state `waiting`, the listing answer's `Date` header (`get_dated`, the provider's
  clock), and a record's `type`, provider ids and times (epoch seconds), for a replication and an eviction, are
  confirmed by [L1], which also shows the eviction counter named `filesEvicted` and the state `transferStatus`, as
  [S11] reads them.
(All Onezone paths are under `/api/v3/onezone`, confirmed by [S1].)

No source revokes a token, so this client has no call that does: a token ends with the lifetime it was minted with,
which derives from the attempt's whole-time ceiling (O06). `UNVERIFIED_ENDPOINTS` lists what neither a source nor
[L1] confirms; it is empty, since [L1] confirmed every such call the layer still makes. A call [L1] confirmed keeps its
live contract test (`tests/live/test_contract.py`), which fails if a deployment answers otherwise.
"""

import base64
import re
import socket
import threading
import time
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
UPLOAD_CONTENT_TYPE = "application/octet-stream"  # of every raw body a mutation sends; [L1]: 25.0 refuses one without
# POSTs that only read, and whether they are sent with the admin credential: the examination is [S4], [S5], called as
# an admin; the scope inference is [S6], called without a header.
READING_POSTS: Mapping[str, bool] = {"/tokens/examine": True, "/tokens/infer_access_token_scope": False}

# What no source and no live run confirms, or a live run refuted: (method, path, what). Each has a test in
# tests/live/test_contract.py marked `covers:` with its method and path; a unit test holds that none is missing there.
# The entries [L1] confirmed left the list, and so did the `path` attribute it refuted, which the gate no longer reads;
# their live tests stay, so a later release that answers otherwise fails.
UNVERIFIED_ENDPOINTS: tuple[tuple[str, str, str], ...] = ()
# The most parent links the gate follows from a file up to its space's root. A file deeper than this, or links that
# loop, resolve to no space: the walk is bounded in requests, and a study's tree is far shallower.
MAX_SPACE_DEPTH = 64
_ERROR_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ERRNO = re.compile(r"[a-z][a-z0-9_]{0,31}")  # a POSIX code as Onedata writes it: enoent, eacces
# How Onedata 25.0 answers a file or directory that is not there, beside the 404 other answers use ([L1]).
POSIX_ABSENT = (400, "posix", "enoent")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_FILE_ID = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # a file's object id: one plain path segment
_SPACE_ID = re.compile(r"[A-Za-z0-9_@=-][A-Za-z0-9_.@=-]{0,127}")  # one plain path segment, never "." or ".."
DEFAULT_TIMEOUT_S = 15.0


class PlatformError(RuntimeError):
    """A request failed or was answered with something unreadable; the message holds no URL, body or header.

    `code` is Onedata's error id and `errno` the POSIX code it gives with the id `posix` (`enoent`, `eacces`), each
    kept only when it has the plain shape of one; neither ever holds a value."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        transport: bool = False,
        errno: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.errno = errno
        self.transport = transport  # True only when no answer came back: a reset, a refusal or a timeout (O08)


class NotSent(PlatformError):
    """A request the client refused, or could not check, before sending it: the platform never saw it."""


class MintNotSent(NotSent):
    """A token request failed before it was sent (no credential for the user), so no token can exist."""


class OutOfScope(NotSent):
    """A mutation's target is no study space, or a path leaves the API; the mutation was not sent."""


class ScopeUnread(NotSent):
    """A read the gate makes to find a request's target space failed, so the request was not sent (fail closed).

    It keeps that read's status, error id and transport flag, so a caller can tell an outage from an answer."""


def _error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    """Return Onedata's error id and its POSIX errno from an error answer, each None unless it has a plain shape."""
    try:
        error = response.json()["error"]
        code = error["id"]
    except (ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(code, str) or not _ERROR_ID.fullmatch(code):
        return None, None
    details = error.get("details") if isinstance(error, dict) else None
    errno = details.get("errno") if isinstance(details, dict) else None
    return code, errno if isinstance(errno, str) and _ERRNO.fullmatch(errno) else None


def _answered(label: str, method: str, status: int, code: str | None, errno: str | None) -> str:
    """The message of an error answer: the label, the method, the status, and the error id with its errno."""
    shown = f"{code}: {errno}" if code and errno else code
    return f"{label}: {method} answered {status}" + (f" ({shown})" if shown else "")


def answered_absent(error: PlatformError) -> bool:
    """Tell whether a provider answered that a file or directory is not there: a 404, or exactly `POSIX_ABSENT`.

    Any other answer, another 400 or one whose body could not be read included, is not absence, and neither is a
    request the client never sent (`NotSent`), which nothing answered. The answer is one provider's: it says nothing
    of another provider or of the storage behind it, where a directory's delete may still be under way (#114)."""
    if isinstance(error, NotSent):
        return False
    return error.status == 404 or (error.status, error.code, error.errno) == POSIX_ABSENT


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


def _plain(path: str) -> list[str]:
    """Split a mutation's path, refusing an escape, query or fragment the server could read as another path."""
    if any(mark in path for mark in "%?#"):
        raise OutOfScope("the request path of a mutation must be plain, without an escape, query or fragment")
    return path.split("/")


def _parent_link(answer: Any) -> str | None:
    """Return the parent link of a provider's answer to `attribute=parent_id`: a plain file id, or None at the top.

    Anything else (no `parent_id` key, an empty or non-plain id, another type) is unreadable, a PlatformError, so the
    gate refuses the request unsent rather than guess where the file lies (O09)."""
    if not isinstance(answer, dict) or "parent_id" not in answer:
        raise PlatformError("the provider's answer for a parent link holds no parent_id")
    parent = answer["parent_id"]
    if parent is None:
        return None
    if not isinstance(parent, str) or not _FILE_ID.fullmatch(parent):
        raise PlatformError("the provider's parent link is not a plain file id")
    return parent


def _file_target(method: str, path: str, body: Any) -> str | None:
    """Return the file id a request acts on, or None when its path names no file.

    A file is named by `/data/{id}` or what is below it, or, for a transfer job, by the `fileId` in the body of a POST
    to `/transfers`. Anything else (`/lookup-file-id/..`, `/replicas-id/..`, a transfer cancel) names none and is
    refused by the caller, since nothing maps it to a space.
    """
    parts = _plain(path)
    if len(parts) >= 3 and parts[1] == "data" and "" not in parts[2:-1]:
        return parts[2] if _FILE_ID.fullmatch(parts[2]) else None
    if method == "POST" and path == "/transfers" and isinstance(body, dict):
        file_id = body.get("fileId")
        return file_id if isinstance(file_id, str) and _FILE_ID.fullmatch(file_id) else None
    return None


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
        """Build the client; the admin credential comes from `admin_token` or else from the configured source.

        The client it builds checks certificates as the platform's `[tls]` says (`tls.py`): its own default CAs, the
        configured CA bundle alone, or not at all. An injected `http` is the caller's, with its own TLS."""
        self._config = config
        self._http = http if http is not None else httpx.Client(timeout=timeout_s, verify=config.tls.client_verify())
        self._owns_http = http is None
        self._timeout_s = timeout_s
        self._environ = environ
        credential = admin_token if admin_token is not None else config.admin.read(environ)
        register_secret(credential)
        self._admin = {AUTH_HEADER: credential}
        # What the gate's walks read and keep (`file_space_id`): a file's space does not change. Guarded by the lock,
        # since one client may serve several threads; only a walk that reached a known root adds to them.
        self._parents: dict[str, str] = {}  # file id -> its parent's id
        self._roots: dict[tuple[str, str], str] = {}  # (provider, space id) -> the space's root file id
        self._walk_lock = threading.Lock()

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

    def provider_label(self, provider: str) -> str:
        """Name a declared provider for a message by its position in the file, `providers[i]`, never by its name."""
        return self._base(provider)[1]

    def _request(
        self,
        method: str,
        path: str,
        *,
        provider: str | None,
        headers: Mapping[str, str],
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
    ) -> httpx.Response:
        """Send one request and return the answer; a transport failure or a status of 400 or more is a PlatformError.

        A transport failure (a reset, a refusal, a timeout) has `transport` set and a real answer has its status, so
        a caller that retries can tell the two apart (O08).
        """
        base, label = self._base(provider)
        url = base + _check_path(path)
        try:
            response = self._http.request(
                method,
                url,
                headers=dict(headers),
                json=json,
                params=dict(params) if params else None,
                content=content,
                timeout=timeout_s if timeout_s is not None else self._timeout_s,
            )
        except httpx.HTTPError as error:
            raise PlatformError(f"{label}: {method} failed ({type(error).__name__})", transport=True) from None
        if response.status_code >= 400:
            code, errno = _error_fields(response)
            raise PlatformError(
                _answered(label, method, response.status_code, code, errno),
                status=response.status_code,
                code=code,
                errno=errno,
            )
        return response

    def _send(
        self,
        method: str,
        path: str,
        *,
        provider: str | None,
        headers: Mapping[str, str],
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        """Send one request and return the decoded JSON; map every failure to a PlatformError without detail."""
        response = self._request(
            method,
            path,
            provider=provider,
            headers=headers,
            json=json,
            params=params,
            content=content,
            timeout_s=timeout_s,
        )
        return self._decoded(response, method, provider)

    def _decoded(self, response: httpx.Response, method: str, provider: str | None) -> Any:
        """Return an answer's decoded JSON, or a PlatformError without detail when it is not JSON."""
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise PlatformError(f"{self._base(provider)[1]}: {method} answered something that is not JSON") from None

    def get_dated(
        self, path: str, *, provider: str | None = None, params: Mapping[str, Any] | None = None
    ) -> tuple[Any, str | None]:
        """GET as `get` does with the admin credential, and return with the decoded answer its `Date` header as sent,
        or None when it has none. The transfer log estimates a provider's clock offset from it (`transferlog.py`)."""
        response = self._request("GET", path, provider=provider, headers=self._admin, params=params)
        return self._decoded(response, "GET", provider), response.headers.get("date")

    def get(
        self,
        path: str,
        *,
        provider: str | None = None,
        token: str | None = None,
        params: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        """GET a path of Onezone or of a declared provider, with the admin credential or as the token given.

        The privilege reads of tokens.py and the listings, block maps and transfer records of step 4b2 are its callers.
        """
        headers = {AUTH_HEADER: token} if token is not None else self._admin
        return self._send("GET", path, provider=provider, headers=headers, params=params, timeout_s=timeout_s)

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

    def _space_roots(self, provider: str) -> dict[str, str]:
        """Return root file id -> space id for the spaces the configuration knows, their roots as `provider` gives them.

        A root is the `fileId` of the provider's `GET /spaces/{id}`, read once per provider and kept once it was
        answered. An absence (`answered_absent`) is a space the provider does not hold, which resolves to no space
        (OutOfScope); any other failure, or an answer without a plain id, raises PlatformError and keeps nothing."""
        space_id = self._config.space.id
        with self._walk_lock:
            root = self._roots.get((provider, space_id))
        if root is None:
            try:
                answer = self._send("GET", f"/spaces/{space_id}", provider=provider, headers=self._admin)
            except PlatformError as error:
                if answered_absent(error):
                    raise OutOfScope(
                        "the configured space is unknown to that provider, so the target resolves to no space"
                    ) from None
                raise
            root = answer.get("fileId") if isinstance(answer, dict) else None
            if not isinstance(root, str) or not _FILE_ID.fullmatch(root):
                raise PlatformError(f"{self.provider_label(provider)}: the space's answer holds no plain root file id")
            with self._walk_lock:
                self._roots[(provider, space_id)] = root
        return {root: space_id}

    def _parent_of(self, node: str, provider: str, read: dict[str, str]) -> str | None:
        """Return a file's parent id, from what earlier walks kept, what this walk read, or a read now; None at the top.

        The read is `GET /data/{id}?attribute=parent_id` with the admin credential. An absence (`answered_absent`) is
        a file or directory the provider does not know, which resolves to no space (OutOfScope); any other failure, or
        an answer that is no plain id and no null, raises PlatformError. A link read now goes into `read`, which the
        caller keeps only once its walk has reached a known root."""
        with self._walk_lock:
            kept = self._parents.get(node)
        if kept is not None:
            return kept
        if node in read:
            return read[node]
        try:
            answer = self._send(
                "GET", f"/data/{node}", provider=provider, headers=self._admin, params={"attribute": "parent_id"}
            )
        except PlatformError as error:
            if answered_absent(error):
                raise OutOfScope(
                    "the target file, or a directory above it, is unknown to its provider, so it resolves to no space"
                ) from None
            raise
        try:
            parent = _parent_link(answer)
        except PlatformError as error:
            raise PlatformError(f"{self.provider_label(provider)}: {error}") from None
        if parent is not None:
            read[node] = parent
        return parent

    def file_space_id(self, file_id: str, provider: str) -> str:
        """Return the id of the configured space a file lies in, found by walking its parent links up to its root.

        See "The space of a file" in this module's docstring. A file id that is not plain, a file or directory on the
        way that the provider does not know, a walk that ends above every known root, and one longer than
        `MAX_SPACE_DEPTH` raise OutOfScope; a read that fails or is answered with something unreadable raises
        PlatformError. No message holds an id, a path or a name.
        """
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise OutOfScope("the target file id is not a plain id, so it resolves to no space")
        roots = self._space_roots(provider)
        read: dict[str, str] = {}  # the links this walk read: kept only once it reaches a known root
        node = file_id
        for depth in range(MAX_SPACE_DEPTH + 1):
            if node in roots:
                with self._walk_lock:
                    self._parents.update(read)
                return roots[node]
            if depth == MAX_SPACE_DEPTH:
                break
            parent = self._parent_of(node, provider, read)
            if parent is None:
                raise OutOfScope(
                    "the target file lies in no space this configuration knows, so it resolves to no space"
                )
            node = parent
        raise OutOfScope(
            f"the target file lies more than {MAX_SPACE_DEPTH} directories below a known space's root, or its "
            "parent links loop, so it resolves to no space"
        )

    def file_space_name(self, file_id: str, provider: str) -> str:
        """Return the name Onezone gives, now, the space a file lies in: its space's id (`file_space_id`), then that
        space's name, read afresh (`space_name`), so a space renamed since an earlier call changes the answer (O09)."""
        return self.space_name(self.file_space_id(file_id, provider))

    def _target_name(self, method: str, path: str, provider: str | None, body: Any) -> str:
        """Return the name of the space a request acts on: the space in its path, or the space of the file it names.

        A read made for it that fails is refused as not sent (ScopeUnread), keeping that read's status, error id and
        transport flag; a target that resolves to no space stays OutOfScope."""
        parts = _plain(path)
        try:
            if len(parts) >= 3 and parts[1] == "spaces":
                return self.space_name(_target_space_id(path))
            file_id = _file_target(method, path, body)
            if file_id is None or provider is None:
                raise OutOfScope(
                    "the mutation names no space and no file, so it resolves to no space; nothing was sent"
                )
            return self.file_space_name(file_id, provider)
        except NotSent:
            raise
        except PlatformError as error:
            raise ScopeUnread(
                f"the gate could not read the target's space ({error}); nothing was sent",
                status=error.status,
                code=error.code,
                errno=error.errno,
                transport=error.transport,
            ) from None

    def _require_in_scope(self, name: str) -> None:
        if not name.startswith(self._config.space.prefixes):
            raise OutOfScope("the target space is not one of the configured prefixes; nothing was sent")

    def mutate(
        self,
        method: str,
        path: str,
        *,
        provider: str | None = None,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
        token: str | None = None,
    ) -> Any:
        """Send a request that can change state; refuse it, before it is sent, unless it acts on a study space.

        The target is the space id in the path, or the file the path or the body of a transfer job names. The space id
        is resolved to a name by a read on Onezone made for this mutation alone; the caller names no space and no
        name, and no earlier answer is reused. The mutation is refused unless that name starts with a configured
        prefix, and when the request names no space and no file.

        It is sent with the admin credential, or as `token` when one is given: day-0 sends a write as a data-path token
        to see it refused. The gate applies either way, and is resolved with the admin credential. A raw body
        (`content`) is sent as `UPLOAD_CONTENT_TYPE`, which Onedata 25.0 requires of a content write ([L1]: 415
        without it).
        """
        if method not in MUTATING:
            raise OutOfScope(f"{method} is not a mutating method; use get or query")
        path = _check_path(path)
        self._base(provider)  # an undeclared provider is refused before anything is read
        self._require_in_scope(self._target_name(method, path, provider, json))
        headers = {AUTH_HEADER: token} if token is not None else dict(self._admin)
        if content is not None:
            headers["Content-Type"] = UPLOAD_CONTENT_TYPE
        return self._send(
            method,
            path,
            provider=provider,
            headers=headers,
            json=json,
            params=params,
            content=content,
            timeout_s=timeout_s,
        )

    def read_range(
        self, path: str, *, provider: str, offset: int, length: int, timeout_s: float | None = None
    ) -> bytes:
        """Read `length` bytes from `offset` of a file's content through a declared provider; return the bytes.

        This is a read, but a read through a provider that lacks the bytes makes it fetch them: the file's block map on
        that provider grows (on-the-fly replication). It changes state, so it passes the same gate as a mutation: the
        path must be `/data/{id}/content`, and the file's space must have a name that starts with a configured prefix,
        or nothing is sent. The answer must be a 206 whose `Content-Range` starts at `offset` and matches the body, and
        the body must be `length` bytes unless the range ends at the end of the file; anything else is refused. [S8]
        """
        parts = _plain(path)
        if len(parts) != 4 or parts[1] != "data" or parts[3] != "content":
            raise OutOfScope("a range read is of /data/{id}/content only")
        if offset < 0 or length < 1:
            raise OutOfScope("a range read needs a non-negative offset and a positive length")
        path = _check_path(path)
        self._base(provider)
        self._require_in_scope(self._target_name("GET", path, provider, None))
        headers = {**self._admin, "Range": f"bytes={offset}-{offset + length - 1}"}
        response = self._request("GET", path, provider=provider, headers=headers, timeout_s=timeout_s)
        label = self._base(provider)[1]
        found = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
        if response.status_code != 206 or found is None:
            raise PlatformError(f"{label}: a range read was not answered with a 206 and a Content-Range")
        first, last, total = (int(found.group(i)) for i in (1, 2, 3))
        body = response.content
        if (
            first != offset
            or last - first + 1 != len(body)
            or len(body) > length
            or (len(body) < length and last + 1 != total)
        ):
            raise PlatformError(f"{label}: a range read was answered with another range than the one asked for")
        return body

    def content_answer(
        self, path: str, *, provider: str, first_byte: bool, timeout_s: float | None = None
    ) -> tuple[int, str | None]:
        """Ask a provider for a file's content, whole or its first byte; return the answer's status and Content-Type.

        The body is never read: the answer is closed once its head has arrived, so a large file costs nothing. It passes
        the same gate as `read_range`, since a read through a provider that lacks the bytes fetches them: the path must
        be `/data/{id}/content` and the file's space must have a configured prefix, or nothing is sent. A transport
        failure or a status of 400 or more is a `PlatformError`. The pre-campaign check is its caller (O05).
        """
        parts = _plain(path)
        if len(parts) != 4 or parts[1] != "data" or parts[3] != "content":
            raise OutOfScope("a content read is of /data/{id}/content only")
        path = _check_path(path)
        base, label = self._base(provider)
        self._require_in_scope(self._target_name("GET", path, provider, None))
        headers = {**self._admin, **({"Range": "bytes=0-0"} if first_byte else {})}
        try:
            with self._http.stream(
                "GET", base + path, headers=headers, timeout=timeout_s if timeout_s is not None else self._timeout_s
            ) as response:
                status, content_type = response.status_code, response.headers.get("Content-Type")
                code, errno = (None, None)
                if status >= 400 and response.read() is not None:
                    code, errno = _error_fields(response)
        except httpx.HTTPError as error:
            raise PlatformError(f"{label}: GET failed ({type(error).__name__})", transport=True) from None
        if status >= 400:
            raise PlatformError(_answered(label, "GET", status, code, errno), status=status, code=code, errno=errno)
        return status, content_type

    def mint_own_token(self, principal: Principal, caveats: Sequence[Mapping[str, Any]]) -> str:
        """Mint a temporary access token for a user, as that user, with exactly these caveats; return the value.

        The user authenticates with its own password (HTTP Basic), read from its credential file the moment it is
        needed and registered as a secret, and the minted value is registered before it is returned (R12). [S1]
        """
        users = self._config.users
        if users is None:
            raise MintNotSent("zone: platform.toml declares no [credentials.users], so no user can mint a token")
        try:
            password = users.read(principal.username, self._environ)
        except ConfigError as error:
            raise MintNotSent(f"zone: a user's credential cannot be used ({error})") from None
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


def tcp_connect(host: str, port: int, timeout_s: float) -> float:
    """Open a TCP connection to a host and return the milliseconds it took; raise OSError when it cannot be opened.

    A raw connection sends no request, so it cannot change anything; it lives here because this module is the only one
    of the layer that touches the network (a test holds that). Day-0's reachability check samples it.
    """
    began = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout_s):
        pass
    return (time.perf_counter() - began) * 1000

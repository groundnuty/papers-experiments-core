# Provenance: new
"""The one HTTP client of the platform layer, and the gate every mutation goes through (O09).

Every request the layer sends leaves through `OnezoneClient`; no other module imports httpx (a test holds that). The
wrapper keeps its `httpx.Client` private, so a caller cannot send around it, and the client is injectable: a test hands
it one built on a mock transport.

Reads and mutations are separate calls:
- `get` is a GET, and `query` a POST from a short list that only reads (examining a token, inferring its scope);
- `mutate` is any other method, and it names the space it acts on: it refuses before sending anything unless the
  space's name starts with one of the configured prefixes (`[space] prefixes`), so a mistaken or hostile name cannot
  reach a space outside the study's;
- `mint_temporary_token` and `revoke_temporary_tokens` are the two operations on credentials. They act on a user's
  tokens, not on a space, so they have calls of their own; the minted value is registered as a secret (R12) before the
  call returns, and the credential the client itself uses is registered when the client is built.

The other halves of O09 (refuse to evict the data provider's own copy, tear down only logged ids, a teardown that finds
files left revokes nothing) belong to provisioning and the sandbox (step 4b); they call `mutate`.

A failure raises `PlatformError` with the status and Onedata's error id only. The message never holds a URL, a body or a
header, so neither a token nor a federation name reaches a log or a record through it.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from expcore.records.redact import register_secret
from expcore_onedata.config import PlatformConfig

API_ZONE = "/api/v3/onezone"
API_PROVIDER = "/api/v3/oneprovider"
AUTH_HEADER = "X-Auth-Token"
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
# POSTs that only read, and whether they are sent with the admin credential.
READING_POSTS: Mapping[str, bool] = {"/tokens/examine": True, "/tokens/infer_access_token_scope": False}
_ERROR_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_USER_ID = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
DEFAULT_TIMEOUT_S = 15.0


class PlatformError(RuntimeError):
    """A request failed or was answered with something unreadable; the message holds no URL, body or header."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OutOfScope(PlatformError):
    """A mutation named a space outside the configured prefixes, or a path that leaves the API; nothing was sent."""


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
        """Return the base URL and the label used in messages: Onezone, or one declared provider."""
        if provider is None:
            return self._config.zone.url + API_ZONE, "zone"
        if provider not in self._config.providers:
            raise OutOfScope("the request names a provider that is not declared")
        return self._config.providers[provider].url + API_PROVIDER, f"provider {provider}"

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
        """GET a path of Onezone or of a declared provider, with the admin credential or as the token given."""
        headers = {AUTH_HEADER: token} if token is not None else self._admin
        return self._send("GET", path, provider=provider, headers=headers)

    def query(self, path: str, body: Mapping[str, Any]) -> Any:
        """POST to one of the endpoints that only read (`READING_POSTS`); any other path is refused."""
        if path not in READING_POSTS:
            raise OutOfScope("the path is not one of the read-only POSTs; use mutate for a request that changes state")
        headers = self._admin if READING_POSTS[path] else {}
        return self._send("POST", path, provider=None, headers=headers, json=dict(body))

    def mutate(self, method: str, path: str, *, space_name: str, provider: str | None = None, json: Any = None) -> Any:
        """Send a request that can change state on a space; refuse, before sending, a space outside the prefixes."""
        if method not in MUTATING:
            raise OutOfScope(f"{method} is not a mutating method; use get or query")
        if not isinstance(space_name, str) or not space_name.startswith(self._config.space.prefixes):
            raise OutOfScope("the space is not one of the configured prefixes; nothing was sent")
        return self._send(method, path, provider=provider, headers=self._admin, json=json)

    def mint_temporary_token(self, user_id: str, caveats: Sequence[Mapping[str, Any]]) -> str:
        """Mint an access token for a user with exactly these caveats; register its value as a secret and return it."""
        if not _USER_ID.fullmatch(user_id):
            raise OutOfScope("the user id is not a plain id")
        body = {"type": {"accessToken": {}}, "caveats": [dict(c) for c in caveats]}
        answer = self._send(
            "POST", f"/users/{quote(user_id, safe='')}/tokens/temporary", provider=None, headers=self._admin, json=body
        )
        value = answer.get("token") if isinstance(answer, dict) else None
        if not isinstance(value, str) or not value:
            raise PlatformError("zone: the answer to a token request holds no token")
        register_secret(value)
        return value

    def revoke_temporary_tokens(self, user_id: str) -> None:
        """Revoke every temporary token of a user; Onezone cannot revoke one of them alone."""
        if not _USER_ID.fullmatch(user_id):
            raise OutOfScope("the user id is not a plain id")
        self._send("DELETE", f"/users/{quote(user_id, safe='')}/tokens/temporary", provider=None, headers=self._admin)

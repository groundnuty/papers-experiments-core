"""An in-process Onezone for the platform layer's tests: tokens, privileges and the scope of the study space.

It extends `fake_onedata.MockOnedata`, whose scope answer it shares, and answers through an `httpx.MockTransport`, so
a test hands the platform client `fake.client_http()` and no socket is opened. It implements the calls the layer makes:

  POST   /api/v3/onezone/tokens/examine                           (admin credential)
  POST   /api/v3/onezone/tokens/infer_access_token_scope          (the token in the body)
  POST   /api/v3/onezone/user/tokens/temporary                    (HTTP Basic of the user itself)
  GET    /api/v3/onezone/spaces/{space}                           (admin credential; the study space and `other_spaces`)
  GET    /api/v3/onezone/spaces/{space}/users/{user}/privileges            (direct)
  GET    /api/v3/onezone/spaces/{space}/effective_users/{user}/privileges  (effective)

A test bends it through plain attributes: `mint_hook` changes the caveats the fake stores for a token (what a server
that dropped or added one would do), `examine_hook` replaces an examination's answer, `status_override` makes every call
fail with that status, and `providers`/`supports` (from MockOnedata) change the federation's view of the space.
Every request is recorded in `.requests`, so a test can assert that a refused mutation sent nothing.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
import secrets
from collections.abc import Callable, Iterable
from typing import Any

import httpx
from fake_onedata import MockOnedata, Request

ZONE = "/api/v3/onezone"


class FakeFederation(MockOnedata):
    """Onezone with one study space, its providers, and the users and tokens a test creates."""

    ADMIN = "fake-admin-credential-0001"

    def __init__(self, *, provider_host: str = "alpha.test", **kwargs: Any) -> None:
        super().__init__({}, provider_host=provider_host, **kwargs)
        self.admin_token = self.ADMIN
        self.issued: dict[str, dict] = {}
        self.privileges: dict[tuple[str, str], dict[str, list[str]]] = {}
        self.mint_hook: Callable[[str, list[dict]], list[dict]] | None = None
        self.blank_mint_answer = False  # store the token but answer without it, as a broken server might
        self.examine_hook: Callable[[dict], Any] | None = None
        self.status_override: int | None = None
        self.other_spaces: dict[str, str] = {}  # id -> name of the spaces besides the study space that Onezone knows
        self.users: dict[str, tuple[str, str]] = {}  # login name -> (password, user id)
        self._sequence = itertools.count(1)

    # -- setup helpers ----------------------------------------------------------
    def add_user(self, username: str, password: str, user_id: str) -> None:
        """Register a user who can log in with HTTP Basic and mint its own tokens."""
        self.users[username] = (password, user_id)

    def grant(self, user: str, direct: Iterable[str], effective: Iterable[str] | None = None) -> None:
        """Give a user's privileges in the study space, direct and effective (the same unless said otherwise)."""
        direct = sorted(direct)
        self.privileges[(self.SPACE_ID, user)] = {
            "direct": direct,
            "effective": direct if effective is None else sorted(effective),
        }

    def add_provider(self, provider_id: str, domain: str, *, supports: bool = True, online: bool = True) -> None:
        """Add a provider the federation reports; by default it supports the space."""
        self.providers[provider_id] = {"name": provider_id, "domain": domain, "version": "25.0.0", "online": online}
        if supports:
            self.supports[provider_id] = {"readonly": False}

    def client_http(self) -> httpx.Client:
        """Return an httpx client whose transport is this fake."""
        return httpx.Client(transport=httpx.MockTransport(self.respond))

    # -- answers ---------------------------------------------------------------
    def _accepts(self, token: str | None) -> bool:
        return token == self.token or token in self.issued

    @staticmethod
    def _error(status: int, error_id: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"id": error_id, "details": {}}})

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer one request the way the real federation would, as far as the layer relies on it."""
        body = request.content
        with self._lock:
            self.requests.append(
                Request(
                    request.method,
                    request.url.path,
                    dict(request.url.params),
                    request.headers.get("Range"),
                    request.headers.get("X-Auth-Token"),
                    body,
                )
            )
        if self.status_override is not None:
            return self._error(self.status_override, "internalServerError")
        route = request.url.path.removeprefix(ZONE) if request.url.path.startswith(ZONE) else None
        if route is None:
            return self._error(404, "notFound")
        sent = json.loads(body) if body else {}
        if request.method == "POST" and route == "/tokens/infer_access_token_scope":
            if not self._accepts(sent.get("token")):
                return self._error(401, "unauthorized")
            return httpx.Response(200, json=self._scope())
        if request.method == "POST" and route == "/user/tokens/temporary":
            return self._mint(request.headers.get("Authorization"), sent)
        if request.headers.get("X-Auth-Token") != self.admin_token:
            return self._error(401, "unauthorized")
        return self._admin_route(request.method, route, sent)

    def _admin_route(self, method: str, route: str, sent: dict) -> httpx.Response:
        """Answer the calls that need the admin credential."""
        if method == "POST" and route == "/tokens/examine":
            return self._examine(sent.get("token"))
        space = re.fullmatch(r"/spaces/([^/]+)", route)
        if space and method == "GET":
            names = {self.SPACE_ID: self.space_name, **self.other_spaces}
            if space.group(1) not in names:
                return self._error(404, "notFound")
            return httpx.Response(200, json={"spaceId": space.group(1), "name": names[space.group(1)]})
        found = re.fullmatch(r"/spaces/([^/]+)/(users|effective_users)/([^/]+)/privileges", route)
        if found and method == "GET":
            entry = self.privileges.get((found.group(1), found.group(3)))
            if entry is None:
                return self._error(404, "notFound")
            kind = "direct" if found.group(2) == "users" else "effective"
            return httpx.Response(200, json={"privileges": entry[kind]})
        return self._error(404, "notFound")

    def _login(self, authorization: str | None) -> str | None:
        """Return the user id of a valid HTTP Basic login, else None."""
        if not authorization or not authorization.startswith("Basic "):
            return None
        try:
            name, _, password = base64.b64decode(authorization[6:], validate=True).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        known = self.users.get(name)
        return known[1] if known and known[0] == password else None

    def _mint(self, authorization: str | None, sent: dict) -> httpx.Response:
        """Issue a temporary token to the user who logged in; like the real one it needs a time caveat."""
        user = self._login(authorization)
        if user is None:
            return self._error(401, "unauthorized")
        caveats = list(sent.get("caveats") or [])
        if not any(c.get("type") == "time" for c in caveats):
            return self._error(400, "badValueTokenCaveats")
        stored = self.mint_hook(user, caveats) if self.mint_hook else caveats
        value = f"fedtok-{next(self._sequence)}-{secrets.token_hex(12)}"
        self.issued[value] = {
            "id": f"tid-{secrets.token_hex(6)}",
            "subject": {"type": "user", "id": user},
            "caveats": stored,
        }
        if self.blank_mint_answer:
            return httpx.Response(201, json={})
        return httpx.Response(200, json={"token": value})

    def _examine(self, value: Any) -> httpx.Response:
        """Describe an issued token, or fail the way an unknown one does."""
        record = self.issued.get(value) if isinstance(value, str) else None
        if record is None:
            return self._error(400, "badValueToken")
        answer: Any = {
            "id": record["id"],
            "persistence": "temporary",
            "subject": record["subject"],
            "type": {"accessToken": {}},
            "caveats": record["caveats"],
        }
        if self.examine_hook:
            answer = self.examine_hook(answer)
        if isinstance(answer, bytes):
            return httpx.Response(200, content=answer)
        return httpx.Response(200, json=answer)

"""An in-process Onezone for the platform layer's tests: tokens, privileges and the scope of the study space.

It extends `fake_onedata.MockOnedata`, whose scope answer it shares, and answers through an `httpx.MockTransport`, so
a test hands the platform client `fake.client_http()` and no socket is opened. It implements the calls the layer makes:

  POST   /api/v3/onezone/tokens/examine                           (admin credential)
  POST   /api/v3/onezone/tokens/infer_access_token_scope          (the token in the body)
  POST   /api/v3/onezone/users/{user}/tokens/temporary            (admin credential)
  DELETE /api/v3/onezone/users/{user}/tokens/temporary            (admin credential)
  GET    /api/v3/onezone/spaces/{space}/users/{user}/privileges            (direct)
  GET    /api/v3/onezone/spaces/{space}/effective_users/{user}/privileges  (effective)

A test bends it through plain attributes: `mint_hook` changes the caveats the fake stores for a token (what a server
that dropped or added one would do), `examine_hook` replaces an examination's answer, `status_override` makes every call
fail with that status, and `providers`/`supports` (from MockOnedata) change the federation's view of the space.
Every request is recorded in `.requests`, so a test can assert that a refused mutation sent nothing.
"""

from __future__ import annotations

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
        self.examine_hook: Callable[[dict], Any] | None = None
        self.status_override: int | None = None
        self.revoked_users: list[str] = []
        self._sequence = itertools.count(1)

    # -- setup helpers ----------------------------------------------------------
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
        if request.headers.get("X-Auth-Token") != self.admin_token:
            return self._error(401, "unauthorized")
        return self._admin_route(request.method, route, sent)

    def _admin_route(self, method: str, route: str, sent: dict) -> httpx.Response:
        """Answer the calls that need the admin credential."""
        if method == "POST" and route == "/tokens/examine":
            return self._examine(sent.get("token"))
        found = re.fullmatch(r"/users/([^/]+)/tokens/temporary", route)
        if found and method == "POST":
            return self._mint(found.group(1), sent)
        if found and method == "DELETE":
            self.revoked_users.append(found.group(1))
            self.issued = {v: r for v, r in self.issued.items() if r["subject"]["id"] != found.group(1)}
            return httpx.Response(204)
        found = re.fullmatch(r"/spaces/([^/]+)/(users|effective_users)/([^/]+)/privileges", route)
        if found and method == "GET":
            entry = self.privileges.get((found.group(1), found.group(3)))
            if entry is None:
                return self._error(404, "notFound")
            kind = "direct" if found.group(2) == "users" else "effective"
            return httpx.Response(200, json={"privileges": entry[kind]})
        return self._error(404, "notFound")

    def _mint(self, user: str, sent: dict) -> httpx.Response:
        """Issue a temporary token; like the real one it needs a time caveat."""
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

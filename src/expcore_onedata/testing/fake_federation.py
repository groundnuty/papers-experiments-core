# Provenance: new
"""An in-process Onezone for the platform layer's tests: tokens, privileges and the scope of the study space."""

from __future__ import annotations

import base64
import itertools
import json
import re
import secrets
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from expcore_onedata.testing.fake_onedata import MockOnedata, Request

ZONE = "/api/v3/onezone"


class FakeFederation(MockOnedata):
    """Onezone with one study space (and more by `add_space`), its providers, and the users and tokens a test creates.

    It extends `MockOnedata`, whose scope answer it shares, and answers through an `httpx.MockTransport`: a test hands
    the platform client `fake.client_http()` and no socket is opened.

    Models: the calls the platform layer makes of Onezone,

      POST   /api/v3/onezone/tokens/examine                           (admin credential)
      POST   /api/v3/onezone/tokens/infer_access_token_scope          (the token in the body)
      POST   /api/v3/onezone/tokens/verify_access_token               (the token in the body)
      POST   /api/v3/onezone/user/tokens/temporary                    (HTTP Basic of the user itself)
      GET    /api/v3/onezone/user/tokens/temporary                    (HTTP Basic: its generation of temporary tokens)
      DELETE /api/v3/onezone/user/tokens/temporary                    (HTTP Basic: revokes all of its temporary tokens)
      GET    /api/v3/onezone/spaces/{space}                           (admin credential; the study space and more)
      GET    /api/v3/onezone/spaces/{space}/users/{user}/privileges            (direct)
      GET    /api/v3/onezone/spaces/{space}/effective_users/{user}/privileges  (effective)
      GET    /api/v3/onezone/users/{user}/effective_privileges                 (the user's Onezone admin privileges)
      GET    /api/v3/onezone/user/effective_clusters                           (as a token: its user's clusters)

    A temporary token needs a time caveat, as the real one does, and is stored with the caveats the server keeps and
    the generation of its user's temporary tokens when it was minted (`generations[user id]`, 1 until a revocation).
    The user's own DELETE raises that generation by one and answers 204, with no privilege, which revokes every
    temporary token of that user and of no other: `verify_access_token` and `infer_access_token_scope` then answer a
    token of an older generation 400 `tokenRevoked`, before any caveat is looked at, as Onezone compares generations
    first. `verify_access_token` refuses a token with a data-access caveat 400 `tokenCaveatUnverified`, as Onezone does
    unless it is asked to allow them, and answers any other token 200 with its subject. A
    space besides the study space is added with `add_space(id, name, provider_ids)`: Onezone knows its name and a
    token's scope shows it supported by those providers; `grant(user, privileges, space=id)` sets a user's privileges
    in it. A user's Onezone admin privileges are `admin_privileges[user id]`, none unless a test sets them, and the
    clusters a user is an effective member of are `clusters[user id]`, none unless a test sets them; they are answered
    to a token the fake issued, for that token's subject, as the real call answers the user it is sent as.

    Does not model: any other Onezone call (users, groups, spaces made or changed, provider registration); the expiry
    of a token (its time caveat is stored, never enforced); the revocation of a named token, or by an admin; the
    propagation of a revocation to the providers (the fake's data API never checks a generation); how a token's other
    caveats
    limit what it may read (the data API is `FakeData`'s, which enforces only the read-only caveat); which spaces a
    token's scope shows (every token's scope shows the study space and every added space, whoever its subject is and
    whatever its caveats, so a test adds a space to make a user a member of one more); the effective privileges a space
    would derive from groups (`grant` sets both lists, and a test sets them apart to make a difference); files in an
    added space (`FakeData`'s data API serves the study space's tree only).

    A test bends it through plain attributes: `mint_hook` changes the caveats the fake stores for a token (what a
    server that dropped or added one would do), `examine_hook` replaces an examination's answer, `scope_hook` replaces
    a scope answer and `scope_status` makes the scope call alone fail with that status, `clusters_hook` and
    `clusters_status` do the same for the clusters call, `revoke_status` makes a revocation fail with that status,
    `revoke_bumps = False` makes it answer 204 and leave the generation as it was, `verify_override` (a status and an
    error id, or None for a 200) replaces every answer of `verify_access_token`, `status_override` makes every
    call fail with that status, and `providers`/`supports` (from MockOnedata) change the federation's view of the
    space. Every request is recorded in `.requests`, so a test can assert that a refused mutation sent nothing.
    """

    ADMIN = "fake-admin-credential-0001"

    def __init__(self, *, provider_host: str = "alpha.test", **kwargs: Any) -> None:
        super().__init__({}, provider_host=provider_host, **kwargs)
        self.admin_token = self.ADMIN
        self.issued: dict[str, dict] = {}
        self.privileges: dict[tuple[str, str], dict[str, list[str]]] = {}
        self.mint_hook: Callable[[str, list[dict]], list[dict]] | None = None
        self.blank_mint_answer = False  # store the token but answer without it, as a broken server might
        self.examine_hook: Callable[[dict], Any] | None = None
        self.scope_hook: Callable[[dict], Any] | None = None
        self.scope_status: int | None = None
        self.clusters_hook: Callable[[dict], Any] | None = None
        self.clusters_status: int | None = None
        self.status_override: int | None = None
        self.admin_privileges: dict[str, list[str]] = {}  # user id -> its effective Onezone admin privileges
        self.clusters: dict[str, list[str]] = {}  # user id -> the ids of the clusters it is an effective member of
        self.other_spaces: dict[str, str] = {}  # id -> name of the spaces besides the study space that Onezone knows
        self.space_supports: dict[str, dict] = {}  # id -> supports of an added space, as the scope answer shows it
        self.users: dict[str, tuple[str, str]] = {}  # login name -> (password, user id)
        self.generations: dict[str, int] = {}  # user id -> the generation of its temporary tokens (1 when absent)
        self.revoke_status: int | None = None
        self.revoke_bumps = True
        self.verify_override: tuple[int, str | None] | None = None
        self._sequence = itertools.count(1)

    # -- setup helpers ----------------------------------------------------------
    def add_user(self, username: str, password: str, user_id: str) -> None:
        """Register a user who can log in with HTTP Basic and mint its own tokens."""
        self.users[username] = (password, user_id)

    def grant(
        self, user: str, direct: Iterable[str], effective: Iterable[str] | None = None, *, space: str | None = None
    ) -> None:
        """Give a user's privileges in the study space (or the space `space` names), direct and effective (the same
        unless said otherwise)."""
        direct = sorted(direct)
        self.privileges[(space or self.SPACE_ID, user)] = {
            "direct": direct,
            "effective": direct if effective is None else sorted(effective),
        }

    def add_space(self, space_id: str, name: str, supports: Iterable[str]) -> None:
        """Add a space besides the study space: Onezone knows its name, and a token's scope shows it supported by these
        provider ids (`space_supports`), as a federation study's second space is."""
        self.other_spaces[space_id] = name
        self.space_supports[space_id] = {provider: {"readonly": False} for provider in supports}

    def _scope(self) -> dict:
        """The scope answer, with every added space beside the study space."""
        answer = super()._scope()
        for space_id, supports in self.space_supports.items():
            answer["dataAccessScope"]["spaces"][space_id] = {"name": self.other_spaces[space_id], "supports": supports}
        return answer

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
            if self.scope_status is not None:
                return self._error(self.scope_status, "internalServerError")
            if not self._accepts(sent.get("token")):
                return self._error(401, "unauthorized")
            if self._stale(sent.get("token")):
                return self._error(400, "tokenRevoked")
            return httpx.Response(200, json=self.scope_hook(self._scope()) if self.scope_hook else self._scope())
        if request.method == "POST" and route == "/tokens/verify_access_token":
            return self._verify(sent.get("token"))
        if request.method == "POST" and route == "/user/tokens/temporary":
            return self._mint(request.headers.get("Authorization"), sent)
        if request.method in ("GET", "DELETE") and route == "/user/tokens/temporary":
            return self._temporary(request.method, request.headers.get("Authorization"))
        if request.method == "GET" and route == "/user/effective_clusters":
            return self._user_clusters(request.headers.get("X-Auth-Token"))
        if request.headers.get("X-Auth-Token") != self.admin_token:
            return self._error(401, "unauthorized")
        return self._admin_route(request.method, route, sent)

    def _user_clusters(self, token: str | None) -> httpx.Response:
        """Answer the clusters of the user a token issued here belongs to; the call is the current user's, so it takes
        no admin credential and answers no other."""
        if self.clusters_status is not None:
            return self._error(self.clusters_status, "internalServerError")
        record = self.issued.get(token) if isinstance(token, str) else None
        if record is None:
            return self._error(401, "unauthorized")
        answer = {"clusters": list(self.clusters.get(record["subject"]["id"], []))}
        return httpx.Response(200, json=self.clusters_hook(answer) if self.clusters_hook else answer)

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
        admin = re.fullmatch(r"/users/([^/]+)/effective_privileges", route)
        if admin and method == "GET":
            return httpx.Response(200, json={"privileges": self.admin_privileges.get(admin.group(1), [])})
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
            "generation": self.generations.get(user, 1),
        }
        if self.blank_mint_answer:
            return httpx.Response(201, json={})
        return httpx.Response(200, json={"token": value})

    def _stale(self, value: Any) -> bool:
        """Whether an issued token is of an older generation than its user's: revoked."""
        record = self.issued.get(value) if isinstance(value, str) else None
        if record is None or "generation" not in record:
            return False
        return bool(record["generation"] < self.generations.get(record["subject"]["id"], 1))

    def _temporary(self, method: str, authorization: str | None) -> httpx.Response:
        """Read (GET) or raise (DELETE) the generation of the logged-in user's temporary tokens, with no privilege."""
        user = self._login(authorization)
        if user is None:
            return self._error(401, "unauthorized")
        if method == "GET":
            return httpx.Response(200, json={"generation": self.generations.get(user, 1)})
        if self.revoke_status is not None:
            return self._error(self.revoke_status, "internalServerError")
        if self.revoke_bumps:
            self.generations[user] = self.generations.get(user, 1) + 1
        return httpx.Response(204)

    def _verify(self, value: Any) -> httpx.Response:
        """Verify an access token: the generation first, then the data-access caveats, as Onezone does."""
        if self.verify_override is not None:
            status, error_id = self.verify_override
            return httpx.Response(200, json={"subject": {}}) if error_id is None else self._error(status, error_id)
        record = self.issued.get(value) if isinstance(value, str) else None
        if record is None:
            return self._error(400, "badValueToken")
        if self._stale(value):
            return self._error(400, "tokenRevoked")
        if any(str(c.get("type", "")).startswith("data.") for c in record["caveats"]):
            return self._error(400, "tokenCaveatUnverified")
        return httpx.Response(200, json={"subject": record["subject"], "ttl": 3600})

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

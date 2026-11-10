"""Bootstrap the federation through its REST interfaces: the admin's token, the users, the providers' registration,
the spaces, their support and the study space's members with exactly their privileges.

Every step reads first and changes only what is missing or different, so up and reset run it alike. The calls are the
ones the images' own demo scripts make (named and temporary invite tokens, the panel's support call), with certificate
verification on. Nothing here prints a token or a password.
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fedlayout import ADMIN, PRINCIPALS, PROVIDERS, SPACES, SUPPORT_BYTES, ZONE, Layout, Service
from fedpki import ensure_secret, require_private, write_secret
from fedrest import PANEL_API, PANEL_USER, PROVIDER_API, ZONE_API, Rest, RestError, basic, bearer, wait_until

INVITE_LIFETIME_S = 900


@dataclass
class Ids:
    """What the federation gave out: ids by the names this deployment uses."""

    admin: str = ""
    users: dict[str, str] = field(default_factory=dict)
    providers: dict[str, str] = field(default_factory=dict)
    spaces: dict[str, str] = field(default_factory=dict)


class Bootstrap:
    """The steps of up and reset that talk to the federation."""

    def __init__(self, rest: Rest, layout: Layout) -> None:
        self.rest = rest
        self.layout = layout
        self.ids = Ids()
        self._admin_token: str | None = None

    # -- credentials ---------------------------------------------------------------------------------------------------

    def admin_password(self) -> str:
        """The zone admin's password: the zone panel's emergency passphrase, which the zone gives its admin."""
        return ensure_secret(self.layout.passphrase(ZONE.name))

    def admin_token(self) -> str:
        """The admin's access token, read once from its 0600 file."""
        if self._admin_token is None:
            path = self.layout.admin_token
            require_private(path)
            self._admin_token = path.read_text(encoding="utf-8").strip()
        return self._admin_token

    def zone(self, method: str, path: str, label: str, *, auth: dict[str, str] | None = None, **options: Any) -> Any:
        """Call Onezone's REST interface, as the admin's token unless `auth` says otherwise."""
        headers = auth if auth is not None else bearer(self.admin_token())
        return self.rest.json(method, ZONE.url + ZONE_API + path, label=f"zone {label}", headers=headers, **options)

    def provider(
        self,
        service: Service,
        method: str,
        path: str,
        label: str,
        *,
        headers: dict[str, str] | None = None,
        **options: Any,
    ) -> Any:
        """Call a provider's REST interface as the admin's token, with any extra headers."""
        url = service.url + PROVIDER_API + path
        sent = {**bearer(self.admin_token()), **(headers or {})}
        return self.rest.json(method, url, label=f"{service.name} {label}", headers=sent, **options)

    def panel(self, service: Service, method: str, path: str, label: str, **options: Any) -> Any:
        """Call a provider's panel, through its 443, as the admin's token (the admin owns every provider cluster)."""
        url = service.url + PANEL_API + path
        return self.rest.json(
            method, url, label=f"{service.name} panel {label}", headers=bearer(self.admin_token()), **options
        )

    # -- the zone ------------------------------------------------------------------------------------------------------

    def wait_zone(self, timeout_s: float) -> float:
        """Wait until the zone answers its configuration and its admin can sign in; return the seconds it took."""
        admin = basic(ADMIN, self.admin_password())

        def ready() -> bool:
            self.rest.call("GET", ZONE.url + ZONE_API + "/configuration", label="zone configuration")
            self.ids.admin = self.zone("GET", "/user", "user", auth=admin)["userId"]
            return True

        return wait_until("the zone's admin signing in", ready, timeout_s)

    def ensure_admin_token(self) -> None:
        """Make the admin's named access token once; the core examines tokens and reads privileges with it."""
        path = self.layout.admin_token
        if path.exists():
            self.zone("GET", "/user", "user")  # the token still works
            return
        body = {"name": f"expcore-admin-{secrets.token_hex(4)}", "type": {"accessToken": {}}}
        answer = self.zone(
            "POST", "/user/tokens/named", "named token", auth=basic(ADMIN, self.admin_password()), json=body
        )
        write_secret(path, answer["token"])

    def ensure_users(self) -> None:
        """Make each fixed user once, with a password of its own in its 0600 file, and read its id."""
        for user in PRINCIPALS:
            password = ensure_secret(self.layout.password(user))
            own = basic(user, password)
            try:
                self.ids.users[user] = self.zone("GET", "/user", "user", auth=own)["userId"]
                continue
            except RestError as error:
                if error.status not in (401, 403):
                    raise
            body = {"username": user, "password": password, "groups": []}
            self.rest.json(
                "POST",
                ZONE.url + PANEL_API + "/zone/users",
                label="zone panel users",
                headers=basic(PANEL_USER, self.admin_password()),
                json=body,
            )
            self.ids.users[user] = self.zone("GET", "/user", "user", auth=own)["userId"]

    def ensure_registration_tokens(self) -> None:
        """Write each provider's registration token once: the provider reads it from its read-only mount."""
        for service in PROVIDERS:
            path = self.layout.registration_token(service.name)
            if path.exists():
                continue
            body = {
                "name": f"expcore-register-{service.name}-{secrets.token_hex(4)}",
                "type": {"inviteToken": {"inviteType": "registerOneprovider", "adminUserId": self.ids.admin}},
            }
            write_secret(path, self.zone("POST", "/user/tokens/named", "registration token", json=body)["token"])

    # -- the providers -------------------------------------------------------------------------------------------------

    def wait_providers(self, timeout_s: float) -> float:
        """Wait until every provider reports its id and the zone sees it online; return the seconds it took."""

        def online() -> bool:
            for service in PROVIDERS:
                found = self.rest.json(
                    "GET", service.url + PROVIDER_API + "/configuration", label=f"{service.name} configuration"
                )
                if not found.get("providerId"):
                    return False
                self.ids.providers[service.name] = found["providerId"]
                seen = self.zone("GET", f"/providers/{found['providerId']}", "provider")
                if seen.get("online") is not True:
                    return False
            return True

        return wait_until("every provider coming online", online, timeout_s)

    # -- spaces, support and members -----------------------------------------------------------------------------------

    def temporary_invite(self, invite: dict[str, Any]) -> str:
        """A short-lived invite token of the admin's."""
        caveats = [{"type": "time", "validUntil": int(time.time()) + INVITE_LIFETIME_S}]
        body = {"type": {"inviteToken": invite}, "caveats": caveats}
        return self.zone("POST", "/user/tokens/temporary", "invite token", json=body)["token"]

    def ensure_spaces(self) -> None:
        """Make each space once, as the admin, who owns it."""
        listed = self.zone("GET", "/user/spaces", "spaces")["spaces"]
        names = {self.zone("GET", f"/user/spaces/{sid}", "space")["name"]: sid for sid in listed}
        for name in SPACES:
            if name not in names:
                self.zone("POST", "/user/spaces", "spaces", json={"name": name})
                listed = self.zone("GET", "/user/spaces", "spaces")["spaces"]
                names = {self.zone("GET", f"/user/spaces/{sid}", "space")["name"]: sid for sid in listed}
            self.ids.spaces[name] = names[name]

    def ensure_support(self, timeout_s: float) -> None:
        """Have each space supported by exactly its providers, and wait until each of them serves its root."""
        by_name = {service.name: service for service in PROVIDERS}
        for space, supporters in SPACES.items():
            sid = self.ids.spaces[space]
            current = set(self.zone("GET", f"/spaces/{sid}/providers", "space providers")["providers"])
            for name in supporters:
                if self.ids.providers[name] in current:
                    continue
                service = by_name[name]
                storage = self.panel(service, "GET", "/provider/storages", "storages")["ids"][0]
                token = self.temporary_invite({"inviteType": "supportSpace", "spaceId": sid})
                body = {"token": token, "size": SUPPORT_BYTES, "storageId": storage}
                self.panel(service, "POST", "/provider/spaces", "support", json=body)
            extra = current - {self.ids.providers[name] for name in supporters}
            if extra:
                raise RuntimeError(f"{space} is supported by {len(extra)} provider(s) it should not be")
            for name in supporters:
                service = by_name[name]
                wait_until(
                    f"{name} serving the root of {space}",
                    lambda s=service, i=sid: bool(self.provider(s, "GET", f"/spaces/{i}", "space").get("fileId")),
                    timeout_s,
                    interval_s=2.0,
                )

    def ensure_members(self, space: str) -> None:
        """Make each fixed user a member of the space holding exactly its privileges, directly and effectively."""
        sid = self.ids.spaces[space]
        members = set(self.zone("GET", f"/spaces/{sid}/users", "space users")["users"])
        for user, wanted in PRINCIPALS.items():
            uid = self.ids.users[user]
            if uid not in members:
                token = self.temporary_invite({"inviteType": "userJoinSpace", "spaceId": sid})
                own = basic(user, ensure_secret(self.layout.password(user)))
                self.zone("POST", "/user/spaces/join", "join space", auth=own, json={"token": token})
            path = f"/spaces/{sid}/users/{uid}/privileges"
            current = set(self.zone("GET", path, "member privileges")["privileges"])
            grant, revoke = sorted(set(wanted) - current), sorted(current - set(wanted))
            if grant or revoke:
                self.zone("PATCH", path, "member privileges", json={"grant": grant, "revoke": revoke})
            for kind in ("users", "effective_users"):
                held = self.zone("GET", f"/spaces/{sid}/{kind}/{uid}/privileges", "member privileges")["privileges"]
                if sorted(held) != sorted(wanted):
                    raise RuntimeError(f"a fixed user of {space} holds other {kind} privileges than declared")

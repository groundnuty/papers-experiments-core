"""Bootstrap the federation through its REST interfaces: the admin's token, the users, the providers' registration,
the spaces, their support by their owners and mirrors, and each space's members with exactly their privileges.

Every step reads first and changes only what is missing or different, so up and both resets run it alike. The calls
are the ones the images' own demo scripts make (named and temporary invite tokens, the panel's support call), with
certificate verification on. Nothing here prints a token or a password.

An imported space is held by its owner on an imported POSIX storage of its own, which the owner's panel creates before
the support (`ensure_import_storages`) and the support names by its storage name, never by its place in a list: a
provider with two storages lists them in no order a support may rely on. The owner's support of an imported space asks
for one initial scan of the storage and no later one, which only adds the files it finds; `wait_import` waits for it.
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fedlayout import IMPORT_MOUNT, REGULAR_STORAGE, Layout, import_storage_name
from fedpki import ensure_secret, require_private, write_secret
from fedrest import PANEL_API, PANEL_USER, PROVIDER_API, ZONE_API, Rest, RestError, basic, bearer, wait_until
from fedspec import ADMIN, Service, Space

INVITE_LIFETIME_S = 900
IMPORT_POLL_S = 5.0
SCAN_KEYS = (  # onepanel's auto_storage_import_info_model
    "status",
    "start",
    "stop",
    "createdFiles",
    "modifiedFiles",
    "deletedFiles",
    "unmodifiedFiles",
    "failedFiles",
    "totalScans",
)
SCAN_STATES = {"enqueued", "running", "aborting", "completed", "failed", "aborted"}
SCAN_ENDED_BADLY = {"failed", "aborted"}
# One scan when the space is supported, none after it (continuous scan off), and a forced scan would only add files:
# op-worker's maybe_update_file leaves a file unmodified when detect_modifications is off.
IMPORT_CONFIG = {
    "mode": "auto",
    "autoStorageImportConfig": {"continuousScan": False, "detectModifications": False, "detectDeletions": False},
}


@dataclass
class Ids:
    """What the federation gave out: ids by the names this deployment uses, the imported storages by (provider, space)
    and each imported space's scan record once it completed."""

    admin: str = ""
    users: dict[str, str] = field(default_factory=dict)
    providers: dict[str, str] = field(default_factory=dict)
    spaces: dict[str, str] = field(default_factory=dict)
    storages: dict[tuple[str, str], str] = field(default_factory=dict)
    scans: dict[str, dict[str, Any]] = field(default_factory=dict)


def same_setting(details: dict[str, Any], key: str, wanted: Any) -> bool:
    """Whether a storage's details hold exactly the wanted value of a setting, of the same type: `1` is not `True`. A
    storage that does not say it is read-only is not."""
    actual = details.get(key, False) if key == "readonly" else details.get(key)
    return type(actual) is type(wanted) and actual == wanted


def scan_record(space: str, answer: Any) -> dict[str, Any]:
    """The keys of a scan's info the facts keep, once the answer is one a wait can act on: a known status, and no file
    the scan failed on. A failed or aborted scan is refused here, so a wait never goes on past one."""
    status = answer.get("status") if isinstance(answer, dict) else None
    if status not in SCAN_STATES:
        raise RuntimeError(f"the storage import of {space}: the answer holds no known status")
    if status in SCAN_ENDED_BADLY:
        raise RuntimeError(f"the storage import of {space} {status}")
    failed = answer.get("failedFiles")
    if isinstance(failed, int) and failed > 0:
        raise RuntimeError(f"the storage import of {space}: {failed} file(s) failed")
    return {key: answer.get(key) for key in SCAN_KEYS}


class Bootstrap:
    """The steps of up and the resets that talk to the federation."""

    def __init__(self, rest: Rest, layout: Layout) -> None:
        self.rest = rest
        self.layout = layout
        self.spec = layout.spec
        self.ids = Ids()
        self._admin_token: str | None = None

    # -- credentials ---------------------------------------------------------------------------------------------------

    def admin_password(self) -> str:
        """The zone admin's password: the zone panel's emergency passphrase, which the zone gives its admin."""
        return ensure_secret(self.layout.passphrase(self.spec.zone.name))

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
        url = self.spec.zone.url + ZONE_API + path
        return self.rest.json(method, url, label=f"zone {label}", headers=headers, **options)

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
            self.rest.call("GET", self.spec.zone.url + ZONE_API + "/configuration", label="zone configuration")
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
        for user in (principal.name for principal in self.spec.principals):
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
                self.spec.zone.url + PANEL_API + "/zone/users",
                label="zone panel users",
                headers=basic(PANEL_USER, self.admin_password()),
                json=body,
            )
            self.ids.users[user] = self.zone("GET", "/user", "user", auth=own)["userId"]

    def ensure_registration_tokens(self) -> None:
        """Write each provider's registration token once: the provider reads it from its read-only mount."""
        for service in self.spec.providers:
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
            for service in self.spec.providers:
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
        for name in (space.name for space in self.spec.spaces):
            if name not in names:
                self.zone("POST", "/user/spaces", "spaces", json={"name": name})
                listed = self.zone("GET", "/user/spaces", "spaces")["spaces"]
                names = {self.zone("GET", f"/user/spaces/{sid}", "space")["name"]: sid for sid in listed}
            self.ids.spaces[name] = names[name]

    def storages(self, service: Service) -> dict[str, tuple[str, dict[str, Any]]]:
        """A provider's storages by name, as (id, details), each read from its panel (`GET /provider/storages`, then
        one `GET` per id): the list names them by id alone, in no order a caller may rely on. A name two storages share
        is refused, since neither could then be told from the other."""
        found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for storage_id in self.panel(service, "GET", "/provider/storages", "storages")["ids"]:
            details = self.panel(service, "GET", f"/provider/storages/{storage_id}", "storage")
            if not isinstance(details, dict) or not isinstance(details.get("name"), str):
                raise RuntimeError(f"a storage of {service.name} has no name in the panel's answer")
            found.setdefault(details["name"], []).append((storage_id, details))
        for name, same in found.items():
            if len(same) > 1:
                raise RuntimeError(f"{service.name} has {len(same)} storages named {name}; refused")
        return {name: same[0] for name, same in found.items()}

    def ensure_import_storages(self) -> None:
        """Have each imported space's owner hold an imported storage named for it, as the layout declares it, and keep
        its id. A storage found by that name must already be as declared; it is never changed. Onezone refuses to
        change `importedStorage` once a storage supports a space (oz-worker's check_imported_storage_value,
        ERR_STORAGE_IN_USE), so a storage that differs is refused, and down, then up, makes it anew."""
        for space in self.spec.spaces:
            if space.imported is None:
                continue
            service, name = self.spec.provider(space.owner), import_storage_name(space.name)
            wanted = {
                "type": "posix",
                "mountPoint": f"{IMPORT_MOUNT}/{space.name}",
                "importedStorage": True,
                "readonly": space.imported.readonly,
            }
            found = self.storages(service).get(name)
            if found is None:
                self.panel(service, "POST", "/provider/storages", "storages", json={name: wanted})
                found = self.storages(service).get(name)
            if found is None:
                raise RuntimeError(f"{service.name} has no storage {name} after creating it")
            storage_id, details = found
            differing = [key for key, value in wanted.items() if not same_setting(details, key, value)]
            if differing:
                raise RuntimeError(
                    f"the storage {name} of {service.name} differs from spaces.{space.name}.import in "
                    f"{', '.join(differing)}; it is never changed, since Onezone refuses to change a storage that "
                    "supports a space: down, then up, makes it anew"
                )
            self.ids.storages[(space.owner, space.name)] = storage_id

    def storage_for(self, service: Service, space: Space) -> str:
        """The id of the storage a provider supports a space on, found by its name: the import storage named for the
        space when the provider owns an imported space, the provider's own `posix` for every other support."""
        name = import_storage_name(space.name) if space.imported and service.name == space.owner else REGULAR_STORAGE
        found = self.storages(service).get(name)
        if found is None:
            raise RuntimeError(f"{service.name} has no storage named {name}, which its support of {space.name} needs")
        return found[0]

    def support(self, service: Service, space: Space, sid: str) -> None:
        """Have one provider support one space, on the storage its role calls for; the owner of an imported space also
        asks for the one scan."""
        body = {
            "token": self.temporary_invite({"inviteType": "supportSpace", "spaceId": sid}),
            "size": space.support_bytes,
            "storageId": self.storage_for(service, space),
        }
        if space.imported and service.name == space.owner:
            body["storageImport"] = IMPORT_CONFIG
        self.panel(service, "POST", "/provider/spaces", "support", json=body)

    def ensure_support(self, timeout_s: float) -> None:
        """Have each space supported by exactly its owner and its mirrors, and wait until each of them serves its
        root."""
        for declared in self.spec.spaces:
            space, supporters = declared.name, declared.supporters
            sid = self.ids.spaces[space]
            current = set(self.zone("GET", f"/spaces/{sid}/providers", "space providers")["providers"])
            for name in supporters:
                if self.ids.providers[name] not in current:
                    self.support(self.spec.provider(name), declared, sid)
            extra = current - {self.ids.providers[name] for name in supporters}
            if extra:
                raise RuntimeError(f"{space} is supported by {len(extra)} provider(s) it should not be")
            for name in supporters:
                service = self.spec.provider(name)
                wait_until(
                    f"{name} serving the root of {space}",
                    lambda s=service, i=sid: bool(self.provider(s, "GET", f"/spaces/{i}", "space").get("fileId")),
                    timeout_s,
                    interval_s=2.0,
                )

    def wait_import(self, timeout_s: float) -> dict[str, dict[str, Any]]:
        """Wait, within `timeout_s` in all, until the scan of every imported space has completed, asking each owner's
        panel for its info; return each space's scan record (`SCAN_KEYS`), which the facts keep.

        A scan that failed or was aborted, or that failed on a file, is a RuntimeError naming the space, so a federation
        whose imported space is short of files never counts as up. An answer the panel cannot give yet is waited on."""
        deadline = time.monotonic() + timeout_s
        for space in (s for s in self.spec.spaces if s.imported is not None):
            what = f"the storage import of {space.name} completing"
            remaining = max(deadline - time.monotonic(), 0.0)
            wait_until(what, lambda s=space: self.scan_completed(s), remaining, interval_s=IMPORT_POLL_S)
        return dict(self.ids.scans)

    def scan_completed(self, space: Space) -> bool:
        """Read an imported space's scan from its owner's panel; keep its record and return True once it completed."""
        path = f"/provider/spaces/{self.ids.spaces[space.name]}/storage-import/auto/info"
        record = scan_record(space.name, self.panel(self.spec.provider(space.owner), "GET", path, "storage import"))
        if record["status"] == "completed":
            self.ids.scans[space.name] = record
        return record["status"] == "completed"

    def ensure_members(self, space: str) -> None:
        """Make each fixed user declared a member of the space a member of it holding exactly its privileges, directly
        and effectively."""
        sid = self.ids.spaces[space]
        members = set(self.zone("GET", f"/spaces/{sid}/users", "space users")["users"])
        for user, wanted in ((p.name, p.privileges) for p in self.spec.members(space)):
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

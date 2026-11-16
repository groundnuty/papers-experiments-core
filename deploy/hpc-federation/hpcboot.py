"""The HPC federation's bootstrap, `platform.toml` and link check: the Compose federation's own code, over the services'
real addresses (DESIGN.md, sections 5 and 6).

- **The services' URLs** are `https://<node FQDN>:8443` (the shim moves the worker's 443 there), so `HpcService` is the
  Compose `Service` with the node's name in place of the loopback port; the Compose bootstrap (`fedboot.Bootstrap`), the
  baseline (`fedcontent.Content`) and the facts (`fedfacts`) then run unchanged.
- **The storage** is the one posix storage the bundle's `finish.sh` adds to each provider, owned by the job's uid on
  the node's tmpfs, with Onedata's LUMA local feed: each support also maps the space's default credentials, and every
  user of the federation, to that uid (as `bundle/onboard.py` does), or the users could reach none of its files.
- **`platform.toml`** is rendered from the layout's own template by the Compose renderer, plus the HPC values a
  template may name: `@hpc_command@` (the argv prefix of this driver, for `[faults] command`), `@hpc_settle_timeout_s@`
  (above the stop's whole bound) and `@hpc_token_margin_s@` (the least `[tokens] margin_s` the platform layer accepts
  with that settle time and the template's other `[faults]` values, `fault_settings.restore_token_margin_s`).
- **The link check** is `fedlinks.check_after_up`: the platform layer's own, through the rendered file, writing
  `link-check.json` in the root through `expcore_onedata.linkrecord`.
"""

import json
import math
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

import tomllib
from fedboot import Bootstrap
from fedcontent import Content
from fedfacts import facts_of, placeholders, render, write_private
from fedlayout import Layout
from fedlinks import check_after_up
from fedrest import Rest, RestError
from fedspec import Service, Space, Spec

from expcore_onedata.fault_settings import FaultSettings, restore_token_margin_s

HTTPS_PORT = 8443  # every service's public port: its 443, moved by the port shim
ZONE_TIMEOUT_S = 900.0
PROVIDER_TIMEOUT_S = 900.0
SUPPORT_TIMEOUT_S = 600.0
TRANSFER_TIMEOUT_S = 600.0
LUMA = "/provider/storages/{storage}/luma/local_feed/storage_access"
# The keys of [faults] that are no bound of the restore: every other key the platform layer's settings know (its
# numbers, and the backend once it has one) enters the margin as the platform layer itself would read it.
NOT_A_BOUND = {
    "file",
    "sanctioned",
    "journal",
    "compose_file",
    "services",
    "project",
    "command",
    "docker",
    "config_sha256",
}


@dataclass(frozen=True)
class HpcService(Service):
    """A service of the layout, at its node's FQDN on the public port."""

    host: str = ""

    @property
    def url(self) -> str:
        return f"https://{self.host}:{HTTPS_PORT}"


def hpc_spec(spec: Spec, hosts: Mapping[str, str]) -> Spec:
    """The layout with every service at its node's FQDN (`hosts`, by service name); every other value as declared."""

    def placed(service: Service) -> HpcService:
        return HpcService(service.name, service.port, service.domain, service.memory_limit, hosts[service.name])

    return replace(spec, zone=placed(spec.zone), providers=tuple(placed(p) for p in spec.providers))


class HpcBootstrap(Bootstrap):
    """The Compose bootstrap over the bundle's providers: one posix storage each, owned by the job's uid, with LUMA's
    local feed mapping each space and each user to that uid."""

    def __init__(self, rest: Rest, layout: Layout, owner: Tuple[int, int]) -> None:
        super().__init__(rest, layout)
        self.owner = owner  # the uid and gid the storage belongs to (finish.sh adds it as the job user)
        self._mapped: set = set()  # the providers whose users are mapped already

    def storage_for(self, service: Service, space: Space) -> str:
        """The provider's one storage: `finish.sh` adds exactly one, whatever its name."""
        found = self.storages(service)
        if len(found) != 1:
            raise RuntimeError(f"{service.name} has {len(found)} storages, not the one posix storage finish.sh adds")
        return next(iter(found.values()))[0]

    def support(self, service: Service, space: Space, sid: str) -> None:
        """Support the space, then map its default credentials, and every user of the federation, to the job's uid on
        the provider's storage."""
        super().support(service, space, sid)
        storage = self.storage_for(service, space)
        uid, gid = self.owner
        path = LUMA.format(storage=storage) + f"/posix_compatible/default_credentials/{sid}"
        self.panel(service, "PUT", path, "space credentials", json={"uid": uid, "gid": gid})
        if service.name not in self._mapped:
            self.map_users(service, storage)
            self._mapped.add(service.name)

    def map_users(self, service: Service, storage: str) -> None:
        """Map the admin and every principal to the job's uid on the storage; a mapping already there is read back and
        must name that uid."""
        uid = self.owner[0]
        path = LUMA.format(storage=storage) + "/all/onedata_user_to_credentials"
        for user in (self.ids.admin, *(self.ids.users[p.name] for p in self.spec.principals)):
            body = {
                "onedataUser": {"mappingScheme": "onedataUser", "onedataUserId": user},
                "storageUser": {"storageCredentials": {"type": "posix", "uid": str(uid)}, "displayUid": uid},
            }
            try:
                self.panel(service, "POST", path, "user mapping", json=body)
            except RestError as error:
                if error.status is None or error.status >= 500:
                    raise
                held = self.panel(service, "GET", f"{path}/{user}", "user mapping")
                credentials = held.get("storageCredentials", {}) if isinstance(held, dict) else {}
                if str(credentials.get("uid")) != str(uid):
                    raise RuntimeError(f"a user of the federation is mapped to another uid on {service.name}") from None


def bring_up(boot: Bootstrap) -> None:
    """The bootstrap after the services are up: the admin's token, the users, the providers online, the spaces, their
    support and their members."""
    boot.wait_zone(ZONE_TIMEOUT_S)
    boot.ensure_admin_token()
    boot.ensure_users()
    boot.wait_providers(PROVIDER_TIMEOUT_S)
    boot.ensure_spaces()
    boot.ensure_support(SUPPORT_TIMEOUT_S)
    for space in boot.spec.spaces:
        if boot.spec.members(space.name):
            boot.ensure_members(space.name)


def token_margin_s(rendered: str, settle_timeout_s: int) -> int:
    """The least `[tokens] margin_s` of a platform whose `[faults]` holds these values and this settle time: the
    platform layer's own rule (`restore_token_margin_s`), so the file `up` renders is never refused at load for it."""
    table = tomllib.loads(rendered).get("faults", {})
    known = {item.name for item in fields(FaultSettings)} - NOT_A_BOUND
    declared = {key: value for key, value in table.items() if key in known}
    settings = FaultSettings(
        file=Path("platform.toml"),
        sanctioned=False,
        journal=Path("fault-journal"),
        compose_file=Path("-"),
        services={},
        **{**declared, "settle_timeout_s": float(settle_timeout_s)},
    )
    return math.ceil(restore_token_margin_s(settings))


def hpc_values(command: Sequence[str], settle_timeout_s: int) -> Dict[str, str]:
    """The HPC values a platform template may name, but the token margin (`render_platform` derives it)."""
    return {
        "hpc_command": json.dumps(list(command)),  # a TOML array of basic strings is JSON's
        "hpc_settle_timeout_s": str(settle_timeout_s),
    }


def render_platform(template: str, facts: dict, command: Sequence[str], settle_timeout_s: int) -> str:
    """The platform file: the Compose placeholders from the facts, then the HPC ones; the token margin last, from the
    rest of the rendered file."""
    values = {**placeholders(facts), **hpc_values(command, settle_timeout_s), "hpc_token_margin_s": "0"}
    first = render(template, values)
    if "@hpc_token_margin_s@" not in template:
        return first
    values["hpc_token_margin_s"] = str(token_margin_s(first, settle_timeout_s))
    return render(template, values)


def write_facts(layout: Layout, boot: Bootstrap, command: Sequence[str], settle_timeout_s: int) -> str:
    """facts.json, and platform.toml from the layout's template with the HPC values."""
    facts = facts_of(boot)
    layout.facts.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    template = layout.spec.platform_template
    if template is None:
        return "facts.json written; the layout names no platform_template, so no platform.toml"
    text = render_platform(template.read_text(encoding="utf-8"), facts, command, settle_timeout_s)
    write_private(layout.platform, text)
    return "facts.json and platform.toml written"


def core_phase(
    layout: Layout,
    owner: Tuple[int, int],
    command: Sequence[str],
    settle_timeout_s: int,
    *,
    rest: Callable[[Path], Any] = Rest,
    bootstrap: Callable[[Any, Layout, Tuple[int, int]], Bootstrap] = HpcBootstrap,
    steps: Callable[[Bootstrap], None] = bring_up,
    content: Callable[[Bootstrap], Any] = Content,
    links: Callable[[Layout, Dict[str, str]], str] = check_after_up,
) -> str:
    """Bootstrap the federation, write its baseline, its facts and platform.toml, and run the link check; return what
    up prints, or raise naming the step that failed. The services must be up, and the root must hold the zone's
    passphrase and the CA."""
    passphrase = layout.passphrase(layout.spec.zone.name)
    if not passphrase.is_file() or not layout.ca_pem.is_file():
        raise RuntimeError("the root holds no zone passphrase or no CA: the zone's export did not run")
    client = rest(layout.ca_pem)
    try:
        boot = bootstrap(client, layout, owner)
        steps(boot)
        if not layout.facts.exists():
            content(boot).reset(TRANSFER_TIMEOUT_S)
        written = write_facts(layout, boot, command, settle_timeout_s)
        checked = links(layout, dict(boot.ids.spaces))
    finally:
        client.close()
    return f"{written}\n{checked}"


def load_owner(text: str) -> Optional[Tuple[int, int]]:
    """The uid and gid a node's facts name, or None."""
    data = json.loads(text)
    uid, gid = data.get("uid"), data.get("gid")
    if isinstance(uid, int) and isinstance(gid, int) and uid > 0 and gid >= 0:
        return uid, gid
    return None

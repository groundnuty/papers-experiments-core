"""The HPC federation's bootstrap, `platform.toml` and link check: the Compose federation's own code, over the services'
real addresses (DESIGN.md, sections 5 and 6).

- **The services' URLs** are `https://<node FQDN>:8443` (the shim moves the worker's 443 there), so `HpcService` is the
  Compose `Service` with the node's name in place of the loopback port; the Compose bootstrap (`fedboot.Bootstrap`), the
  baseline (`fedcontent.Content`) and the facts (`fedfacts`) then run unchanged.
- **The storage** is the one posix storage the bundle's `finish.sh` adds to each provider, owned by the job's uid on
  the node's tmpfs, with Onedata's LUMA local feed: each support also maps the space's default credentials, and every
  user of the federation, to that uid (as `bundle/onboard.py` does), or the users could reach none of its files.
- **`platform.toml`** is rendered from the layout's own template by the Compose renderer, plus the HPC values a
  template may name: `@hpc_stop_bound_s@` (a stop's whole bound, `[hpc] stop_seconds` + 90 s), `@hpc_settle_timeout_s@`
  (above it), `@hpc_command@` (this driver's argv prefix) and `@hpc_token_margin_s@`: the least `[tokens] margin_s`
  the platform layer's own loader accepts for the rest of the file (`least_margin_s`).
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
HPC_POLL_S = 10.0  # the HPC backend's poll: every status is a step on a node's one control core
PROVISIONAL_MARGIN_S = 10**9  # the margin the platform file is first written with: above any the loader's rule asks


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


def rule_margin_s(rendered: str, settle_timeout_s: float) -> int:
    """The least `[tokens] margin_s` by the restore's rule (`restore_token_margin_s`) over every `[faults]` key the
    platform layer's settings know, the HPC backend's settle time and poll standing in where the file leaves them out:
    for a core whose loader cannot read this `[faults]` (one without the HPC backend)."""
    table = tomllib.loads(rendered).get("faults", {})
    known = {item.name for item in fields(FaultSettings)} - NOT_A_BOUND
    declared = {key: value for key, value in table.items() if key in known}
    declared.setdefault("settle_timeout_s", float(settle_timeout_s))
    declared.setdefault("poll_s", HPC_POLL_S)
    settings = FaultSettings(
        file=Path("platform.toml"),
        sanctioned=False,
        journal=Path("fault-journal"),
        compose_file=Path("-"),
        services={},
        **declared,
    )
    return math.ceil(restore_token_margin_s(settings))


def least_margin_s(path: Path, rendered: str, settle_timeout_s: float) -> int:
    """The least `[tokens] margin_s` the platform layer's own loader accepts for the platform file at `path`, written
    with a provisional margin no rule refuses: `load_faults` reads it as a campaign would, and its rule gives the least
    (so a term the loader adds, or a measured stop bound, moves it with no change here). A core whose loader refuses
    this `[faults]` for another reason has no HPC backend yet; the rule over the keys it knows stands in."""
    from expcore.config.loading import ConfigError
    from expcore_onedata.config import load_platform
    from expcore_onedata.fault_settings import load_faults

    try:
        settings = load_faults(path, load_platform(path))
    except ConfigError:
        return rule_margin_s(rendered, settle_timeout_s)
    return math.ceil(restore_token_margin_s(settings))


def hpc_values(command: Sequence[str], stop_bound_s: int, settle_timeout_s: int) -> Dict[str, str]:
    """The HPC values a platform template may name, but the token margin (`write_platform` derives it)."""
    return {
        "hpc_command": json.dumps(list(command)),  # a TOML array of basic strings is JSON's
        "hpc_stop_bound_s": str(stop_bound_s),
        "hpc_settle_timeout_s": str(settle_timeout_s),
    }


def write_platform(path: Path, template: str, facts: dict, values: Mapping[str, str]) -> str:
    """Write the platform file: the Compose placeholders from the facts, the HPC ones, and the token margin last, the
    least the platform layer accepts for the rest of the file. Returns the text written."""
    filled = {**placeholders(facts), **values, "hpc_token_margin_s": str(PROVISIONAL_MARGIN_S)}
    text = render(template, filled)
    write_private(path, text)
    if "@hpc_token_margin_s@" not in template:
        return text
    filled["hpc_token_margin_s"] = str(least_margin_s(path, text, float(values["hpc_settle_timeout_s"])))
    text = render(template, filled)
    write_private(path, text)
    return text


def write_facts(layout: Layout, boot: Bootstrap, values: Mapping[str, str]) -> str:
    """facts.json, and platform.toml from the layout's template with the HPC values."""
    facts = facts_of(boot)
    layout.facts.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    template = layout.spec.platform_template
    if template is None:
        return "facts.json written; the layout names no platform_template, so no platform.toml"
    write_platform(layout.platform, template.read_text(encoding="utf-8"), facts, values)
    return "facts.json and platform.toml written"


def core_phase(
    layout: Layout,
    owner: Tuple[int, int],
    values: Mapping[str, str],
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
        written = write_facts(layout, boot, values)
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

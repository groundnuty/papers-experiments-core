"""The HPC federation's bootstrap, `platform.toml` and link check: the Compose federation's own code, over the services'
real addresses (DESIGN.md, sections 5 and 6).

- **The services' URLs** are `https://<node FQDN>:8443` (the shim moves the worker's 443 there), so `HpcService` is the
  Compose `Service` with the node's name in place of the loopback port; the Compose bootstrap (`fedboot.Bootstrap`), the
  baseline (`fedcontent.Content`) and the facts (`fedfacts`) then run unchanged.
- **The storage** is the one posix storage the bundle's `finish.sh` adds to each provider, owned by the job's uid on
  the node's tmpfs, with Onedata's LUMA local feed: each support also maps the space's default credentials, and every
  user of the federation, to that uid (as `bundle/onboard.py` does), or the users could reach none of its files. The
  owner of a shared imported space (O59) has a second, `import-<space>`, on its tree on the shared filesystem, made
  with the same ids (`hpcimport.import_storage_extra`) and mapped the same way, on the storage its support names; the
  core's phase waits for the scan, and verifies each space against its manifest (`fedimport.verify_imports`) over the
  rendered `platform.toml`, before the link check.
- **`platform.toml`** is rendered from the layout's own template by the Compose renderer, plus the HPC values a template
  may name: `@hpc_root@` (the root, so every path into it is absolute and a copy of the file beside a study reads the
  root's files), `@hpc_stop_bound_s@` (a stop's whole bound, `[hpc] stop_seconds` + 90 s), `@hpc_settle_timeout_s@`
  (above it), `@hpc_command@` (this driver's argv prefix), `@hpc_direct_storages@` (an inline table of each direct-I/O
  import's storage id to its tree's directory, for a `[sandbox.mount]`; `{}` with none) and `@hpc_token_margin_s@`: the
  least `[tokens] margin_s` the platform layer's own loader accepts for the rest of the file (`least_margin_s`).
  The site's values come from `hpcpins` (the agent's sandbox's pins and `@study_artifacts@`, the study's own directory
  on the nodes), and each space's `@data_manifest.<space>@` from `hpcdata` (DESIGN.md, section 6a).
- **The link check** is `fedlinks.check_after_up`: the platform layer's own, through the rendered file, writing
  `link-check.json` in the root through `expcore_onedata.linkrecord`.
"""

import json
import math
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

import hpcdata
import hpcimport
import tomllib
from fedboot import Bootstrap
from fedcontent import Content
from fedfacts import facts_of, placeholders, render, write_private
from fedimport import verify_imports
from fedlayout import Layout, import_storage_name
from fedlinks import check_after_up
from fedrest import Rest, RestError
from fedspec import Service, Space, Spec

from expcore_onedata.fault_settings import FaultSettings, restore_token_margin_s
from expcore_onedata.importmanifest import Manifest

HTTPS_PORT = 8443  # every service's public port: its 443, moved by the port shim
HPC_STORAGE = "posix-local"  # the posix storage bundle/finish.sh adds to every provider, owned by the job's uid
ZONE_TIMEOUT_S = 900.0
PROVIDER_TIMEOUT_S = 900.0
SUPPORT_TIMEOUT_S = 600.0
TRANSFER_TIMEOUT_S = 600.0
IMPORT_TIMEOUT_S = 1800.0  # in all, for every imported space's one scan to complete (the Compose federation's bound)
VERIFY_TIMEOUT_S = 1800.0  # for each imported space's check against its manifest, once the scan has completed
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
    local feed mapping each space and each user to that uid; an owner of a shared imported space has a second."""

    def __init__(self, rest: Rest, layout: Layout, owner: Tuple[int, int]) -> None:
        super().__init__(rest, layout)
        self.owner = owner  # the uid and gid the storage belongs to (finish.sh adds it as the job user)
        self._mapped: set = set()  # the (provider, storage) pairs whose users are mapped already

    def import_storage_extra(self, service: Service) -> Dict[str, Any]:
        """What an imported storage is made with beyond what the layout declares: the job user's ids, the local LUMA
        feed the mappings below need, and the canonical path type (O59), as finish.sh makes the regular storage."""
        return hpcimport.import_storage_extra(*self.owner)

    def storage_for(self, service: Service, space: Space) -> str:
        """The storage a provider supports a space on, found by its name, never by its place in a list (an owner of an
        imported space has two): the import storage named for the space for its owner, `finish.sh`'s posix storage for
        every other support."""
        name = import_storage_name(space.name) if space.imported and service.name == space.owner else HPC_STORAGE
        found = self.storages(service).get(name)
        if found is None:
            raise RuntimeError(f"{service.name} has no storage named {name}, which its support of {space.name} needs")
        return found[0]

    def support(self, service: Service, space: Space, sid: str) -> None:
        """Support the space, then map its default credentials, and every user of the federation, to the job's uid on
        the storage the support names: the calls are `hpcimport.luma_requests`, and each storage of a provider maps the
        users once."""
        super().support(service, space, sid)
        storage = self.storage_for(service, space)
        users = []
        if (service.name, storage) not in self._mapped:
            users = [self.ids.admin, *(self.ids.users[p.name] for p in self.spec.principals)]
        for method, path, body in hpcimport.luma_requests(storage, sid, users, *self.owner):
            self.luma(service, method, path, body)
        self._mapped.add((service.name, storage))

    def luma(self, service: Service, method: str, path: str, body: Dict[str, Any]) -> None:
        """Send one LUMA request; a user mapping already there is read back and must name the job's uid."""
        if method == "PUT":
            self.panel(service, "PUT", path, "space credentials", json=body)
            return
        try:
            self.panel(service, "POST", path, "user mapping", json=body)
        except RestError as error:
            if error.status is None or error.status >= 500:
                raise
            user, uid = body["onedataUser"]["onedataUserId"], body["storageUser"]["storageCredentials"]["uid"]
            held = self.panel(service, "GET", f"{path}/{user}", "user mapping")
            credentials = held.get("storageCredentials", {}) if isinstance(held, dict) else {}
            if str(credentials.get("uid")) != uid:
                raise RuntimeError(f"a user of the federation is mapped to another uid on {service.name}") from None


def bring_up(boot: Bootstrap) -> None:
    """The bootstrap after the services are up: the admin's token, the users, the providers online, the spaces, the
    owners' imported storages, their support, their members, and the scan of each imported space."""
    boot.wait_zone(ZONE_TIMEOUT_S)
    boot.ensure_admin_token()
    boot.ensure_users()
    boot.wait_providers(PROVIDER_TIMEOUT_S)
    boot.ensure_spaces()
    boot.ensure_import_storages()  # an owner's imported storage, before any support names it (no-op with no import)
    boot.ensure_support(SUPPORT_TIMEOUT_S)
    for space in boot.spec.spaces:
        if boot.spec.members(space.name):
            boot.ensure_members(space.name)
    boot.wait_import(IMPORT_TIMEOUT_S)  # each import's one scan complete (no-op with no import)


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


def has_hpc_backend() -> bool:
    """Whether this core's `[faults]` loader knows the HPC backend (M4): its settings carry a `backend`."""
    return "backend" in {item.name for item in fields(FaultSettings)}


def least_margin_s(path: Path, rendered: str, settle_timeout_s: float) -> int:
    """The least `[tokens] margin_s` the platform layer's own loader accepts for the platform file at `path`, written
    with a provisional margin no rule refuses: `load_faults` reads it as a campaign would, and its rule gives the least
    (so a term the loader adds, or a measured stop bound, moves it with no change here). Its refusal of the file fails
    up: a platform.toml the campaign would refuse is never reported ready. Only a core without the HPC backend, or a
    file without `[faults]`, takes the restore's rule over the keys it knows."""
    if not has_hpc_backend() or "faults" not in tomllib.loads(rendered):
        return rule_margin_s(rendered, settle_timeout_s)
    from expcore_onedata.config import load_platform
    from expcore_onedata.fault_settings import load_faults

    return math.ceil(restore_token_margin_s(load_faults(path, load_platform(path))))


def hpc_values(command: Sequence[str], stop_bound_s: int, settle_timeout_s: int, root: Path) -> Dict[str, str]:
    """The HPC values a platform template may name, but the token margin (`write_platform` derives it). `root` is the
    federation's root, as an absolute path in a basic string of TOML: the template's paths into the root are written
    with it (`@hpc_root@/secrets/...`), so a copy of the file elsewhere (beside a study, `shards.py step`) reads the
    root's own files."""
    text = str(root)
    if not os.path.isabs(text) or any(c in text for c in '"\\') or not text.isprintable():
        raise ValueError("the federation's root is no absolute path a TOML string can hold as it is")
    return {
        "hpc_command": json.dumps(list(command)),  # a TOML array of basic strings is JSON's
        "hpc_stop_bound_s": str(stop_bound_s),
        "hpc_settle_timeout_s": str(settle_timeout_s),
        "hpc_root": text,
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


def direct_storages_value(layout: Layout, boot: Bootstrap, environ: Mapping[str, str]) -> str:
    """The value of `@hpc_direct_storages@`: an inline TOML table of each direct-I/O import's storage id (the bootstrap
    made it) to the tree's directory on the host, for a template's `[sandbox.mount]` (M5); `{}` when no import declares
    a direct mount. Strings are JSON's, which a basic string of TOML is. The platform file that holds the paths is the
    root's own, never committed."""
    if not any(space.imported.direct_io for space in layout.shared_imports()):
        return "{}"
    mapping = hpcimport.direct_storages(layout, boot.ids.storages, environ)
    return "{" + ", ".join(f"{json.dumps(key)} = {json.dumps(path)}" for key, path in mapping.items()) + "}"


def core_phase(
    layout: Layout,
    owner: Tuple[int, int],
    values: Mapping[str, str],
    *,
    environ: Optional[Mapping[str, str]] = None,
    rest: Callable[[Path], Any] = Rest,
    bootstrap: Callable[[Any, Layout, Tuple[int, int]], Bootstrap] = HpcBootstrap,
    steps: Callable[[Bootstrap], None] = bring_up,
    content: Callable[[Bootstrap], Any] = Content,
    links: Callable[[Layout, Dict[str, str]], str] = check_after_up,
    verify: Callable[..., Any] = verify_imports,
    data: Callable[[Bootstrap], Callable[[str], Any]] = hpcdata.content_reader,
) -> str:
    """Bootstrap the federation, write its baseline, each space's data manifest, its facts and platform.toml, verify
    every shared import against its manifest over the rendered file and every baseline space's data against its own
    (read back through `data`, O63; an ingested space's after the ingest, `verify_ingested`), and run the link check;
    return what up prints, or raise naming the step that failed. The
    services must be up, and the root must hold the zone's passphrase and the CA. `environ` is where the imports'
    variables are read (this process's when None): a core step's own is an allowlist that carries none."""
    passphrase = layout.passphrase(layout.spec.zone.name)
    if not passphrase.is_file() or not layout.ca_pem.is_file():
        raise RuntimeError("the root holds no zone passphrase or no CA: the zone's export did not run")
    env = os.environ if environ is None else environ
    client = rest(layout.ca_pem)
    try:
        boot = bootstrap(client, layout, owner)
        steps(boot)
        if not layout.facts.exists():
            content(boot).reset(TRANSFER_TIMEOUT_S)
        manifests = hpcdata.data_manifests(layout, env, values)
        filled = {**values, "hpc_direct_storages": direct_storages_value(layout, boot, env),
                  **hpcdata.values(manifests)}  # fmt: skip
        written = write_facts(layout, boot, filled)
        # before the link check, so a platform file that leaves the sharing out fails up with no link record written
        verify(layout, env, timeout_s=VERIFY_TIMEOUT_S)
        held = hpcdata.check(layout, manifests, data(boot))  # no space is ready on a claim: its data is read back
        # (an ingested space holds nothing yet: `verify_ingested` reads it back after the ingest)
        checked = links(layout, dict(boot.ids.spaces))
    finally:
        client.close()
    return f"{written}\n{held}\n{checked}"


def verify_ingested(
    layout: Layout,
    owner: Tuple[int, int],
    manifests: Mapping[str, Manifest],
    *,
    rest: Callable[[Path], Any] = Rest,
    bootstrap: Callable[[Any, Layout, Tuple[int, int]], Bootstrap] = HpcBootstrap,
    data: Callable[[Bootstrap], Callable[[str], Any]] = hpcdata.content_reader,
) -> Dict[str, str]:
    """Read each ingested space back through its owner and compare it with its manifest (O57): what makes each other
    than the manifest, in counts alone (empty for one that is exactly it), never a path. A fresh process holds none of
    the ids the bootstrap made, so the spaces' ids are read from facts.json, which `up` wrote; the services' addresses
    are the placed layout's. `rest`, `bootstrap` and `data` are the core phase's own, for a stand-in."""
    try:
        recorded = json.loads(layout.facts.read_text(encoding="utf-8"))["spaces"]
        ids = {name: space["id"] for name, space in recorded.items()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        why = type(error).__name__
        raise RuntimeError(f"{layout.facts.name} cannot be read as the facts up writes ({why})") from None
    client = rest(layout.ca_pem)
    try:
        boot = bootstrap(client, layout, owner)
        boot.ids.spaces.update(ids)
        return hpcdata.verify(manifests, data(boot))
    finally:
        client.close()


def load_owner(text: str) -> Optional[Tuple[int, int]]:
    """The uid and gid a node's facts name, or None."""
    data = json.loads(text)
    uid, gid = data.get("uid"), data.get("gid")
    if isinstance(uid, int) and isinstance(gid, int) and uid > 0 and gid >= 0:
        return uid, gid
    return None

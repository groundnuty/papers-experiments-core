"""The HPC federation's bootstrap, `platform.toml` and link check: the Compose federation's own code, over the services'
real addresses (DESIGN.md, sections 5 and 6).

- **The services' URLs** are `https://<declared name>:8443` (the shim moves the worker's 443 there), so `HpcService` is
  the Compose `Service` with the layout's declared name in place of the loopback port (O73: the services register
  under it, and the root's address map says where it is dialled); the Compose bootstrap (`fedboot.Bootstrap`), the
  baseline (`fedcontent.Content`) and the facts (`fedfacts`) then run unchanged, the client given the map.
- **The storage** is the one posix storage the bundle's `finish.sh` adds to each provider at `/volumes/posix`, owned by
  the job's uid, on the node's tmpfs or, with `HPC_STORAGE_ROOT`, bound from the provider's own directory on scratch
  (`hpcstorage`; the same storage in the cell either way), with Onedata's LUMA local feed: each support also maps the
  space's default credentials, and every user of the federation, to that uid (as `bundle/onboard.py` does), or the
  users could reach none of its files. The
  owner of a shared imported space (O59) has a second, `import-<space>`, on its tree on the shared filesystem, made
  with the same ids (`hpcimport.import_storage_extra`) and mapped the same way, on the storage its support names; the
  core's phase waits for the scan, and verifies each space against its manifest (`fedimport.verify_imports`) over the
  rendered `platform.toml`, before the link check.
- **`platform.toml`** is rendered from the layout's own template by the Compose renderer, plus the HPC values a template
  may name: `@hpc_root@` (the root, so every path into it is absolute and a copy of the file beside a study reads the
  root's files), `@hpc_stop_bound_s@` (the service's own stop bound, `[hpc] stop_seconds` + 90 s),
  `@hpc_step_launch_wait_s@` (what a control step may wait to start, `[hpc] step_launch_wait_s`),
  `@hpc_command_timeout_s@` (the floor of every relay verb's bound: that and 60 s), `@hpc_settle_timeout_s@` (above
  the stop and the wait), `@hpc_command@` (this driver's argv prefix), `@hpc_direct_storages@` (an inline table of
  each direct-I/O import's storage id to its tree's directory, for a `[sandbox.mount]`; `{}` with none),
  `@hpc_addresses@` (the inline table `{ file, resolver }` that is the value of the template's top-level
  `addresses = @hpc_addresses@`, O73: the address map's file and the resolve launcher; the render adds no line the
  template does not have) and
  `@hpc_token_margin_s@`: the least `[tokens] margin_s` the platform layer's own loader accepts for the rest of the file
  (`least_margin_s`).
  The site's values come from `hpcpins` (the agent's sandbox's pins and `@study_artifacts@`, the study's own directory
  on the nodes), and each space's `@data_manifest.<space>@` from `hpcdata` (DESIGN.md, section 6a).
  A template that declares `[sandbox.apptainer]` also names `@hpc_provider_storage@` (`hpcpins.values`: `tmpfs`, or
  `shared_filesystem` with a scratch root), which the platform layer records in the campaign fingerprint, and the
  rendered file is read back for it (`check_provider_storage`): a federation on scratch and one on tmpfs are two
  experiments.
- **The render identity** (O74, `hpcidentity`) is written beside it, once the platform file is rendered: the hashes of
  the template, of the rendered platform's fingerprint and of each pin it holds, and the digest of the study's artifacts
  tree when the site declares one (`up` took it in its preflight and the state file carries it here), as
  `render-identity.json` in the root, which a shard's step compares with the one its plan expects.
- **`[placement]`** is always in the rendered file, complete (`hpcplacement.complete`, O79): the platform layer requires
  it (`rangemap.load_placement`) and a reader that finds none patches in a default of its own, which is how a setting
  drifts unseen. The template declares `holder`, the one setting the core has no default for (`up` refuses one that
  does not, before anything is submitted); the render writes every other setting at the core's default, read from
  `PlacementSettings`' fields, beside what the template declared.
- **The failover order** (O98, `hpcfailover`): with `[hpc] outage_targets` declared in the layout, right after the
  platform file is written and before the link check, every space the rendered file holds for one of those providers
  and that has a mirror must have a mirror whose provider id sorts below the owner's, or `up` fails and the member is
  replaced; the passing order is written as `failover-order.json` (labels only). The file's spaces and providers are
  matched to the layout's through the bootstrap's ids, and the record of an earlier run is removed before the platform
  file is replaced (`write_facts`). Nothing is declared in a layout that does not name `outage_targets`, and `up` is
  then what it was.
- **The link check** is `fedlinks.check_after_up`: the platform layer's own, through the rendered file, writing
  `link-check.json` in the root through `expcore_onedata.linkrecord`.
"""

import json
import math
import os
import re
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

import hpcdata
import hpcfailover
import hpcidentity
import hpcimport
import hpcnames
import hpcpins
import hpcplacement
import tomllib
from fedboot import Bootstrap
from fedcontent import Content
from fedfacts import facts_of, placeholders, render, write_private
from fedimport import verify_imports
from fedlayout import Layout, import_storage_name
from fedlinks import check_after_up
from fedrest import Rest, RestError
from fedspec import Service, Space, Spec
from fedsupport import check_rendered_support
from fedtools import check_rendered, require_study_artifacts
from hpcsettings import DEFAULT_STEP_LAUNCH_WAIT_S, VERB_WORK_S

from expcore_onedata.addresses import AddressMap, AddressMapError
from expcore_onedata.fault_settings import FaultSettings, restore_token_margin_s
from expcore_onedata.importmanifest import Manifest

HTTPS_PORT = 8443  # every service's public port: its 443, moved by the port shim
PROVIDER_STORAGE = "hpc_provider_storage"  # the value hpcpins.values fills: the providers' storage kind (O57)
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
    """A service of the layout, at its declared name on the public port (the name it registers under)."""

    host: str = ""

    @property
    def url(self) -> str:
        return f"https://{self.host}:{HTTPS_PORT}"


def hpc_spec(spec: Spec, hosts: Mapping[str, str]) -> Spec:
    """The layout with every service at its declared name (`hosts`, by service name, from `hpcnames.declared_names`);
    every other value as declared. The service keeps the layout's own domain, so its `domain` is the declared name."""

    def placed(service: Service) -> HpcService:
        return HpcService(
            service.name, service.port, service.federation_domain, service.memory_limit, hosts[service.name]
        )

    return replace(spec, zone=placed(spec.zone), providers=tuple(placed(p) for p in spec.providers))


class HpcBootstrap(Bootstrap):
    """The Compose bootstrap over the bundle's providers: one posix storage each, owned by the job's uid, with LUMA's
    local feed mapping each space and each user to that uid; an owner of a shared imported space has a second."""

    def __init__(self, rest: Rest, layout: Layout, owner: Tuple[int, int]) -> None:
        super().__init__(rest, layout)
        self.owner = owner  # the uid and gid the storage belongs to (finish.sh adds it as the job user)
        self._mapped: set = set()  # the (provider, storage) pairs whose users are mapped already
        self.registered: Optional[Dict[str, str]] = None  # what each service reported as its domain (O73), once read

    def read_registered(self) -> Dict[str, str]:
        """What each service reports as its domain (O73), kept for the facts and the checks of `core_phase`."""
        self.registered = hpcnames.registered_domains(self)
        return self.registered

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
    owners' imported storages, their support and the support sets read back against the declaration (O35), their
    members, and the scan of each imported space."""
    boot.wait_zone(ZONE_TIMEOUT_S)
    boot.ensure_admin_token()
    boot.ensure_users()
    boot.wait_providers(PROVIDER_TIMEOUT_S)
    boot.ensure_spaces()
    boot.ensure_import_storages()  # an owner's imported storage, before any support names it (no-op with no import)
    boot.ensure_support(SUPPORT_TIMEOUT_S)
    boot.verify_support()  # each space's providers as the zone lists them, against the layout's declaration (O35)
    for space in boot.spec.spaces:
        if boot.spec.members(space.name):
            boot.ensure_members(space.name)
    boot.audit_slot_users()  # an existing slot user is verified and recorded matched, never changed (O83)
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


def hpc_values(
    command: Sequence[str],
    stop_bound_s: int,
    settle_timeout_s: int,
    root: Path,
    *,
    step_launch_wait_s: int = DEFAULT_STEP_LAUNCH_WAIT_S,
    command_timeout_s: int = DEFAULT_STEP_LAUNCH_WAIT_S + VERB_WORK_S,
) -> Dict[str, str]:
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
        "hpc_step_launch_wait_s": str(step_launch_wait_s),
        "hpc_command_timeout_s": str(command_timeout_s),
        "hpc_settle_timeout_s": str(settle_timeout_s),
        "hpc_root": text,
    }


def check_provider_storage(rendered: str, values: Mapping[str, str]) -> None:
    """Refuse a rendered file whose `[sandbox.apptainer]` does not declare the providers' storage kind the site has
    (`values["hpc_provider_storage"]`, O57; `hpcpins.check_declared_kind`): the kind is in the campaign fingerprint, so
    a file that leaves it out, or hard-codes another, would let shards on tmpfs and on scratch merge."""
    hpcpins.check_declared_kind(rendered, values.get(PROVIDER_STORAGE))


def write_platform(path: Path, template: str, facts: dict, values: Mapping[str, str]) -> str:
    """Write the platform file: the Compose placeholders from the facts, the HPC ones, and the token margin last, the
    least the platform layer accepts for the rest of the file, and `[placement]` complete (`hpcplacement.complete`) in
    both. Returns the text written. The kind of storage the providers have is checked in the first render, before
    anything is written (`check_provider_storage`)."""
    filled = {**placeholders(facts), **values, "hpc_token_margin_s": str(PROVISIONAL_MARGIN_S)}
    text = rendered(template, filled)
    check_provider_storage(text, values)
    # O73: a `[sandbox.federation]` table is usable only once the providers registered the declared names, which the
    # facts record: refused here, before the file is written or loaded
    hpcnames.check_federation_table(text, registered_of(facts))
    text = hpcplacement.complete(text)
    write_private(path, text)
    if "@hpc_token_margin_s@" not in template:
        return text
    filled["hpc_token_margin_s"] = str(least_margin_s(path, text, float(values["hpc_settle_timeout_s"])))
    text = hpcplacement.complete(rendered(template, filled))
    write_private(path, text)
    return text


def rendered(template: str, filled: Mapping[str, str]) -> str:
    """The template with its `@slots@` replaced, and nothing added, moved or removed (a study's conformance check holds
    a render to that). Once the root holds an address map (`hpc_addresses`, the table `hpcnames.addresses_value` gives)
    the template's own `addresses = @hpc_addresses@` is where the platform layer's clients find the address each
    declared name is dialled at (O73); a map with no such line, or the line with no map, is refused naming the slot."""
    given = hpcnames.ADDRESSES_SLOT in filled
    hpcnames.check_addresses_slot(template, map_written=given)
    text = render(template, dict(filled))
    if given:
        hpcnames.check_rendered_addresses(text, filled[hpcnames.ADDRESSES_SLOT])
    return text


def registered_of(facts: Mapping[str, Any]) -> Dict[str, str]:
    """The domains the services registered under, by service name, as facts.json records them (`write_facts`)."""
    found: Dict[str, str] = {}
    zone = facts.get("zone", {}).get("registered_domain")
    if zone:
        found["zone"] = zone
    for name, provider in facts.get("providers", {}).items():
        if provider.get("registered_domain"):
            found[name] = provider["registered_domain"]
    return found


def write_facts(layout: Layout, boot: Bootstrap, values: Mapping[str, str]) -> str:
    """facts.json, and platform.toml from the layout's template with the HPC values, and the render identity of that
    platform (O74): the template is read once, and what is hashed is the text that was rendered.

    The failover record of an earlier run of the phase is removed first (O98, `hpcfailover.invalidate`): it was of the
    platform file this replaces, and a run that fails below, or whose layout declares no outage, must not leave it."""
    hpcfailover.invalidate(layout.root)
    facts = facts_of(boot)
    registered = getattr(boot, "registered", None)
    if registered:  # O73: what each service reported as its domain; with the DNS check, which is advisory
        facts["zone"]["registered_domain"] = registered.get("zone")
        for name, provider in facts["providers"].items():
            provider["registered_domain"] = registered.get(name)
        facts["dns_check"] = dict(hpcnames.DNS_CHECK)
    layout.facts.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    template = layout.spec.platform_template
    if template is None:
        return "facts.json written; the layout names no platform_template, so no platform.toml"
    if layout.address_map.is_file():  # the platform layer's clients dial each declared name at its mapped address
        launcher = layout.toolshim / hpcnames.LAUNCHER  # the one `up` copied into the root, for a Python server
        table = hpcnames.addresses_value(layout.address_map, launcher if launcher.is_file() else None)
        values = {**values, hpcnames.ADDRESSES_SLOT: table}
    text = template.read_text(encoding="utf-8")
    require_study_artifacts(text, values.get("study_artifacts"))
    write_platform(layout.platform, text, facts, values)
    hpcidentity.write(layout.root, hpcidentity.build(text, values, layout.platform))
    check_rendered(
        layout, facts
    )  # O83: the slots the file declares, against the layout's principals; as a campaign loads it
    check_rendered_support(layout, facts)  # the file's own support sets, against the zone's (O35)
    return f"facts.json and platform.toml written; {hpcidentity.FILE} written"


def direct_storages_value(layout: Layout, boot: Bootstrap, environ: Mapping[str, str]) -> str:
    """The value of `@hpc_direct_storages@`: an inline TOML table of each direct-I/O import's storage id (the bootstrap
    made it) to the tree's directory on the host, for a template's `[sandbox.mount]` (M5); `{}` when no import declares
    a direct mount. Strings are JSON's, which a basic string of TOML is. The platform file that holds the paths is the
    root's own, never committed."""
    if not any(space.imported.direct_io for space in layout.shared_imports()):
        return "{}"
    mapping = hpcimport.direct_storages(layout, boot.ids.storages, environ)
    return "{" + ", ".join(f"{json.dumps(key)} = {json.dumps(path)}" for key, path in mapping.items()) + "}"


def declares_names(layout: Layout) -> bool:
    """Whether the layout's services are reached by their declared names (O73): `hpc_spec` (`placed_layout`'s) makes
    each an `HpcService`, whose URL carries the name, which resolves nowhere on the site. The services of a Compose
    layout are at loopback ports, and it declares no name to dial."""
    return any(isinstance(service, HpcService) for service in layout.spec.services)


def root_address_map(layout: Layout) -> AddressMap:
    """The address map the root holds (`up` writes it before any service is launched), or an `AddressMapError` naming
    the file by its place in the root: it is missing, it cannot be used and the map's own reason says why, or it holds
    no entry for some service of the layout (how many, and which by the layout's own labels). Never a declared name, an
    address or a path of the host."""
    shown = layout.address_map.relative_to(layout.root).as_posix()
    why = (
        "every service of this federation is at a name that resolves nowhere, so the login node's client could reach "
        "none without it (`up` writes it before it launches any service)"
    )
    try:
        found = AddressMap.load(layout.address_map)
    except AddressMapError as error:
        if not os.path.lexists(layout.address_map):
            raise AddressMapError(f"{shown} is missing: {why}") from None
        raise AddressMapError(f"{shown} cannot be used ({error}): {why}") from None
    declared = {s.name: s.host for s in layout.spec.services if isinstance(s, HpcService)}
    lacking = [name for name, host in declared.items() if found.address_of(host) is None]
    if lacking:  # a map that loads is no proof it holds every owner: the one it lacks would be dialled by DNS alone
        raise AddressMapError(
            f"{shown} holds no entry for {len(lacking)} of {len(declared)} services ({', '.join(lacking)}): a "
            "service with no entry is dialled by its declared name alone, which resolves nowhere"
        )
    return found


def service_client(layout: Layout, rest: Callable[..., Any]) -> Any:
    """The one constructor of the login node's REST client of the federation's services (O73): the core phase's and
    `verify-data`'s alike, so neither can reach a service by name only. On a layout of declared names every URL carries
    a name that resolves nowhere, so the client is given the root's address map (`addresses`) and its absence or an
    unusable one is refused (`AddressMapError`), never answered with a client that has none. A layout that declares no
    name (a Compose one) is at loopback ports and gets the client with none. `tests/deploy/test_rest_constructors.py`
    fails on a client of the services built anywhere else that is not listed with its reason."""
    if not declares_names(layout):
        return rest(layout.ca_pem)
    return rest(layout.ca_pem, addresses=root_address_map(layout))


def core_phase(
    layout: Layout,
    owner: Tuple[int, int],
    values: Mapping[str, str],
    *,
    environ: Optional[Mapping[str, str]] = None,
    rest: Callable[..., Any] = Rest,
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
    client = service_client(layout, rest)
    try:
        boot = bootstrap(client, layout, owner)
        steps(boot)
        read = getattr(
            boot, "read_registered", None
        )  # the HPC bootstrap reads what each service registered under (O73)
        if read is not None:
            read()
        registered = getattr(boot, "registered", None)
        if registered is not None:  # O73: every service registered under its declared name, or nothing reaches it
            hpcnames.check_registered(hpcnames.declared_names(layout.spec), registered)
        if not layout.facts.exists():
            content(boot).reset(TRANSFER_TIMEOUT_S)
        manifests = hpcdata.data_manifests(layout, env, values)
        filled = {**values, "hpc_direct_storages": direct_storages_value(layout, boot, env),
                  **hpcdata.values(manifests)}  # fmt: skip
        written = write_facts(layout, boot, filled)
        admitted = hpcfailover.admit(layout, boot.ids)  # O98: with an outage declared, a mirror is asked first
        if layout.spec.platform_template is not None:
            hpcdata.check_rendered(
                layout, manifests
            )  # only a manifest up renders is checked: no path of the template's own
        # before the link check, so a platform file that leaves the sharing out fails up with no link record written
        verify(layout, env, timeout_s=VERIFY_TIMEOUT_S)
        held = hpcdata.check(layout, manifests, data(boot))  # no space is ready on a claim: its data is read back
        # (an ingested space holds nothing yet: `verify_ingested` reads it back after the ingest)
        checked = links(layout, dict(boot.ids.spaces))
    finally:
        client.close()
    return f"{written}\n{admitted}\n{held}\n{checked}" if admitted else f"{written}\n{held}\n{checked}"


READINGS_KEYS = ("os.id", "os.version_id", "glibc", "kernel")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}")
_GLIBC = re.compile(r"glibc [0-9][A-Za-z0-9._+~-]{0,31}")
_REASON = re.compile(r"[A-Za-z0-9._ -]{1,80}")


def _unknown_readings(reason: str) -> Dict[str, Any]:
    return {
        "os": {"id": "unknown", "version_id": "unknown"},
        "glibc": "unknown",
        "kernel": "unknown",
        "unknown": dict.fromkeys(READINGS_KEYS, reason),
    }


def load_readings(found: Any) -> Dict[str, Any]:
    """A node's OS release, glibc version and kernel release as its facts step printed them (O87), rebuilt from the
    known keys, each a plain string or "unknown" beside its reason. Recorded and never refused: a step that printed
    none (an older bundle's), or anything of another shape, is unknown for that reason. The shape is
    `expcore_onedata.sandbox.hostfacts.checked`'s, which the tests hold this to."""
    if not isinstance(found, Mapping):
        return _unknown_readings("the step gave none" if found is None else "not a mapping")
    os_part, reasons = found.get("os"), found.get("unknown", {})
    if not isinstance(os_part, Mapping) or not isinstance(reasons, Mapping):
        return _unknown_readings("not the expected shape")
    values = (os_part.get("id"), os_part.get("version_id"), found.get("glibc"), found.get("kernel"))
    shapes = (_WORD, _WORD, _GLIBC, _WORD)
    if not all(isinstance(v, str) and (v == "unknown" or shape.fullmatch(v)) for v, shape in zip(values, shapes)):
        return _unknown_readings("not the expected shape")
    why = {k: v for k, v in reasons.items() if k in READINGS_KEYS and isinstance(v, str) and _REASON.fullmatch(v)}
    if len(why) != len(reasons) or {k for k, v in zip(READINGS_KEYS, values) if v == "unknown"} != set(why):
        return _unknown_readings("not the expected shape")
    out: Dict[str, Any] = {"os": {"id": values[0], "version_id": values[1]}, "glibc": values[2], "kernel": values[3]}
    if why:
        out["unknown"] = dict(sorted(why.items()))
    return out


def recorded_space_ids(layout: Layout) -> Dict[str, str]:
    """The Onezone ID of each space as facts.json records it, which the core's phase writes from the ids its bootstrap
    found or made (a space missing from the zone is made again by name, under a new ID, and the file then holds that
    one); RuntimeError, in fixed words that name no path, when the file cannot be read as the facts `up` writes."""
    try:
        recorded = json.loads(layout.facts.read_text(encoding="utf-8"))["spaces"]
        return {name: space["id"] for name, space in recorded.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        why = type(error).__name__
        raise RuntimeError(f"{layout.facts.name} cannot be read as the facts up writes ({why})") from None


def verify_ingested(
    layout: Layout,
    owner: Tuple[int, int],
    manifests: Mapping[str, Manifest],
    *,
    rest: Callable[..., Any] = Rest,
    bootstrap: Callable[[Any, Layout, Tuple[int, int]], Bootstrap] = HpcBootstrap,
    data: Callable[[Bootstrap], Callable[[str], Any]] = hpcdata.content_reader,
) -> Dict[str, str]:
    """Read each ingested space back through its owner and compare it with its manifest (O57): what makes each other
    than the manifest, in counts alone (empty for one that is exactly it), never a path. A fresh process holds none of
    the ids the bootstrap made, so the spaces' ids are read from facts.json, which `up` wrote; the services' addresses
    are the placed layout's, dialled through the root's address map as the core phase's are (`service_client`, O73:
    every service's URL carries its declared name, which resolves nowhere on the site, so a root with no usable map is
    refused). `rest`, `bootstrap` and `data` are the core phase's own, for a stand-in."""
    ids = recorded_space_ids(layout)
    client = service_client(layout, rest)
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

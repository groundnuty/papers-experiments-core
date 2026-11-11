"""The federation a study declares: its Compose project, its services and their loopback ports, its spaces with the
provider that owns each and the providers that mirror it, its users, support sizes and memory limits.

    uv run python deploy/compose-federation/federation.py --layout PATH up

The layout is a TOML file. Without --layout it is `federation.toml` beside this file, which declares the deployment
this directory always had: one Onezone and three Oneproviders. A space's owner is the provider its data is written
through and kept on; its mirrors are other providers that support it and may hold replicas, which the per-run reset
evicts. Every key is checked when the file is read, and a refusal names the file and the key. The two images stay
pinned by the deployment itself (fedlayout.IMAGES): a layout cannot name another.
"""

import hashlib
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_LAYOUT = HERE / "federation.toml"
LOOPBACK = "127.0.0.1"  # the only address anything is published on
ZONE = "zone"  # the zone's service name
ADMIN = "admin"  # the zone's admin user, whose password is the zone panel's emergency passphrase
RESERVED_SERVICES = frozenset({ZONE, "janitor"})  # the generated Compose file's own service names
DEFAULT_MEMORY = "6g"
DEFAULT_SUPPORT_BYTES = 10 << 30
PORTS = (1024, 65535)
UNIT = {"m": 1 << 20, "g": 1 << 30}

PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")  # a project name as Compose accepts it
DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_NAME = re.compile(rf"(?:{DOMAIN_LABEL}\.)+test")  # under the reserved .test, so it resolves nowhere else
SERVICE_NAME = re.compile(r"[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?")  # a Compose service name and a domain label
SPACE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
USER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
PRIVILEGE = re.compile(r"space_[a-z_]+")
MEMORY = re.compile(r"([1-9][0-9]{0,5})([mg])")

TOP_KEYS = {
    "project",
    "domain",
    "address",
    "study_space",
    "memory_limit",
    "support_bytes",
    "platform_template",
    "zone",
    "providers",
    "spaces",
    "principals",
}
SERVICE_KEYS = {"port", "memory_limit"}
SPACE_KEYS = {"owner", "mirrors", "support_bytes"}
PRINCIPAL_KEYS = {"privileges", "spaces"}


class LayoutError(ValueError):
    """A layout file that cannot be used; the message names the file and the key."""


@dataclass(frozen=True)
class Service:
    """One Compose service: its name, its loopback port, the federation's domain and its memory limit."""

    name: str
    port: int
    federation_domain: str
    memory_limit: str = DEFAULT_MEMORY

    @property
    def domain(self) -> str:
        """Its domain on the project's network, which resolves nowhere else."""
        return f"{self.name}.{self.federation_domain}"

    @property
    def url(self) -> str:
        return f"https://{LOOPBACK}:{self.port}"

    @property
    def memory_bytes(self) -> int:
        return memory_bytes(self.memory_limit)


@dataclass(frozen=True)
class Space:
    """A space, the provider that owns its data, and the providers that mirror it."""

    name: str
    owner: str
    mirrors: tuple[str, ...]
    support_bytes: int = DEFAULT_SUPPORT_BYTES

    @property
    def supporters(self) -> tuple[str, ...]:
        """Every provider that supports the space, the owner first."""
        return (self.owner, *self.mirrors)


@dataclass(frozen=True)
class Principal:
    """A fixed user, the spaces it is a member of, and exactly the privileges it holds in each."""

    name: str
    privileges: tuple[str, ...]
    spaces: tuple[str, ...]


@dataclass(frozen=True)
class Spec:
    """A declared layout, checked."""

    source: str  # the layout file's name, for records; never its path
    digest: str  # SHA-256 of the file's bytes
    project: str  # the Compose project name: every container and network carries it, and the root's mark holds it
    domain: str
    zone: Service
    providers: tuple[Service, ...]
    spaces: tuple[Space, ...]
    study_space: str
    principals: tuple[Principal, ...]
    platform_template: Path | None = None

    @property
    def services(self) -> tuple[Service, ...]:
        return (self.zone, *self.providers)

    def provider(self, name: str) -> Service:
        return next(service for service in self.providers if service.name == name)

    def space(self, name: str) -> Space:
        return next(space for space in self.spaces if space.name == name)

    @property
    def holder(self) -> str:
        """The data provider: the study space's owner, through which its baseline is written."""
        return self.space(self.study_space).owner

    def members(self, space: str) -> tuple[Principal, ...]:
        return tuple(p for p in self.principals if space in p.spaces)

    @property
    def memory_bytes(self) -> int:
        """The most the federation's services may take together: the sum of their limits."""
        return sum(service.memory_bytes for service in self.services)


def memory_bytes(limit: str) -> int:
    number, unit = MEMORY.fullmatch(limit).groups()
    return int(number) * UNIT[unit]


class _Reader:
    """Checks one layout file's values; every refusal names the file and the key."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def fail(self, key: str, problem: str) -> LayoutError:
        return LayoutError(f"{self.path}: {key}: {problem}")

    def table(self, value: Any, key: str, allowed: set[str], *, required: set[str] = frozenset()) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise self.fail(key or "the file", "must be a table")
        prefix = f"{key}." if key else ""
        for name in sorted(set(value) - allowed):
            raise self.fail(f"{prefix}{name}", "is not a key of a layout here")
        for name in sorted(required - set(value)):
            raise self.fail(f"{prefix}{name}", "is required")
        return value

    def named_tables(self, value: Any, key: str, pattern: re.Pattern[str], what: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping) or not value:
            raise self.fail(key, f"must be a table of at least one {what}")
        for name in value:
            if not pattern.fullmatch(name):
                raise self.fail(f"{key}.{name}", f"is not a usable {what} name (pattern {pattern.pattern})")
        return value

    def string(self, value: Any, key: str, pattern: re.Pattern[str], what: str) -> str:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise self.fail(key, f"must be {what} (pattern {pattern.pattern})")
        return value

    def integer(self, value: Any, key: str, low: int, high: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise self.fail(key, f"must be an integer from {low} to {high}")
        return value

    def name(self, value: Any, key: str, allowed: set[str], what: str) -> str:
        if not isinstance(value, str):
            raise self.fail(key, f"must be a {what} name")
        if value not in allowed:
            raise self.fail(key, f"names a {what} the layout does not declare ({value})")
        return value

    def names(self, value: Any, key: str, allowed: set[str], what: str, *, empty: bool) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or (not value and not empty):
            raise self.fail(key, f"must be a list of {what} names" + ("" if empty else ", at least one"))
        if len(set(value)) != len(value):
            raise self.fail(key, f"names a {what} twice")
        unknown = [v for v in value if v not in allowed]
        if unknown:
            raise self.fail(key, f"names a {what} the layout does not declare ({unknown[0]})")
        return tuple(value)


def load_spec(path: Path | None = None) -> Spec:
    """Read and check a layout file; the default is `federation.toml` beside this file."""
    path = DEFAULT_LAYOUT if path is None else Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LayoutError(f"{path}: cannot be read ({type(error).__name__})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise LayoutError(f"{path}: is not a TOML file ({error})") from None
    return _spec(_Reader(path), data, path, hashlib.sha256(raw).hexdigest())


def _spec(read: _Reader, data: Any, path: Path, digest: str) -> Spec:
    top = read.table(data, "", TOP_KEYS, required={"project", "domain", "study_space", "zone", "providers", "spaces"})
    project = read.string(top["project"], "project", PROJECT_NAME, "a Compose project name")
    domain = read.string(top["domain"], "domain", DOMAIN_NAME, "a domain under .test")
    if top.get("address", LOOPBACK) != LOOPBACK:
        raise read.fail("address", f"must be {LOOPBACK}: every port is published on the host's loopback only")
    memory = read.string(top.get("memory_limit", DEFAULT_MEMORY), "memory_limit", MEMORY, "a size such as 6g or 512m")
    support = read.integer(top.get("support_bytes", DEFAULT_SUPPORT_BYTES), "support_bytes", 1 << 20, 1 << 50)

    services: list[Service] = []
    ports: dict[int, str] = {}

    def service(name: str, value: Any, key: str) -> Service:
        table = read.table(value, key, SERVICE_KEYS, required={"port"})
        port = read.integer(table["port"], f"{key}.port", *PORTS)
        if port in ports:
            raise read.fail(f"{key}.port", f"{port} is also the port of {ports[port]}")
        ports[port] = name
        limit = read.string(table.get("memory_limit", memory), f"{key}.memory_limit", MEMORY, "a size such as 6g")
        made = Service(name, port, domain, limit)
        services.append(made)
        return made

    zone = service(ZONE, top["zone"], "zone")
    declared = read.named_tables(top["providers"], "providers", SERVICE_NAME, "provider")
    for name in declared:
        if name in RESERVED_SERVICES:
            raise read.fail(f"providers.{name}", "is a service name the Compose file uses itself")
    providers = tuple(service(name, value, f"providers.{name}") for name, value in declared.items())

    spaces = []
    for name, value in read.named_tables(top["spaces"], "spaces", SPACE_NAME, "space").items():
        key = f"spaces.{name}"
        table = read.table(value, key, SPACE_KEYS, required={"owner"})
        owner = read.name(table["owner"], f"{key}.owner", set(declared), "provider")
        mirrors = read.names(table.get("mirrors", []), f"{key}.mirrors", set(declared), "provider", empty=True)
        if owner in mirrors:
            raise read.fail(f"{key}.mirrors", f"names the space's owner ({owner}), which holds its data already")
        size = read.integer(table.get("support_bytes", support), f"{key}.support_bytes", 1 << 20, 1 << 50)
        spaces.append(Space(name, owner, mirrors, size))
    space_names = {space.name for space in spaces}
    study = read.name(top["study_space"], "study_space", space_names, "space")

    users = read.named_tables(top["principals"], "principals", USER_NAME, "user") if "principals" in top else {}
    principals = []
    for name, value in users.items():
        key = f"principals.{name}"
        if name == ADMIN:
            raise read.fail(key, "is the zone's admin user")
        table = read.table(value, key, PRINCIPAL_KEYS, required={"privileges"})
        rights = table["privileges"]
        usable = isinstance(rights, list) and all(isinstance(r, str) and PRIVILEGE.fullmatch(r) for r in rights)
        if not usable or not rights:
            raise read.fail(f"{key}.privileges", f"must be a list of space privileges (pattern {PRIVILEGE.pattern})")
        if len(set(rights)) != len(rights):
            raise read.fail(f"{key}.privileges", "names a privilege twice")
        member_of = read.names(table.get("spaces", [study]), f"{key}.spaces", space_names, "space", empty=False)
        principals.append(Principal(name, tuple(rights), member_of))

    template = None
    if "platform_template" in top:
        if not isinstance(top["platform_template"], str) or not top["platform_template"]:
            raise read.fail("platform_template", "must be a path relative to the layout file")
        template = (path.parent / top["platform_template"]).resolve()
        if not template.is_file():
            raise read.fail("platform_template", "names no file, relative to the layout file")

    return Spec(
        source=path.name,
        digest=digest,
        project=project,
        domain=domain,
        zone=zone,
        providers=providers,
        spaces=tuple(spaces),
        study_space=study,
        principals=tuple(principals),
        platform_template=template,
    )

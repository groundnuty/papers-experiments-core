"""What the throwaway federation is: its services, ports, spaces, users, baseline files and where its data lives.

Every name here is invented for this test federation. The one place on disk is the root (`EXPCORE_FED_ROOT`, default
/mnt/volume2/expcore-federation): down removes it whole, and nothing else outside this directory is written. An
override must lie at or below the default, with no link on the way, or every command refuses before it touches a file
or a container: down deletes what the root holds.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT = "expcore-fed"  # the Compose project name: every container and network carries it
ROOT_MARKER = ".expcore-fed-root"  # written by up into a root it made or found empty; holds the project name
HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "compose.yaml"
TEMPLATE = HERE / "platform.toml.template"
DEFAULT_ROOT = "/mnt/volume2/expcore-federation"
LOOPBACK = "127.0.0.1"  # the only address anything is published on
IMAGES = (
    "docker.onedata.org/onezone:ID-ba7a778696",
    "docker.onedata.org/oneprovider:ID-765309c84f",
)
DOMAIN = "expcore-fed.test"  # a reserved test domain: it resolves only on the project's network
NODE = "node1"  # each service's one cluster node


@dataclass(frozen=True)
class Service:
    """One Compose service: its name, its domain on the project's network and its port on the host's loopback."""

    name: str
    port: int

    @property
    def domain(self) -> str:
        return f"{self.name}.{DOMAIN}"

    @property
    def url(self) -> str:
        return f"https://{LOOPBACK}:{self.port}"


ZONE = Service("zone", 29443)
PROVIDERS = (Service("pine", 29444), Service("birch", 29445), Service("cedar", 29446))
SERVICES = (ZONE, *PROVIDERS)
HOLDER = "pine"  # the data provider: the baseline files are written through it

ADMIN = "admin"  # the zone's admin user, whose password is the zone panel's emergency passphrase
STUDY_SPACE = "alpha-space"
SPACES = {  # each space and the providers that support it
    STUDY_SPACE: ("pine", "birch", "cedar"),
    "beta-space": ("birch", "cedar"),  # outside the study's prefix: a mutation must never reach it
}
SUPPORT_BYTES = 10 << 30
VIEW_READ = ("space_view", "space_read_data")
PRINCIPALS = {  # the fixed users of the study space, each holding exactly these privileges in it
    "alpha-reader": VIEW_READ,  # the data path's user
    "alpha-tools": VIEW_READ,  # a tool-server user, for a condition with the baseline rights
    "alpha-health": VIEW_READ,  # the health check's user
}

CONTENT_FILE = "hello.txt"  # a small file at the space root: the content test reads it
REPLICATED_FILE = "replicated.bin"  # replicated from the holder to the next provider, so a transfer record exists
LISTING_DIR = "listing-1200"  # a directory of more than 1000 small entries, for the complete-listing test
LISTING_ENTRIES = 1200


@dataclass(frozen=True)
class BaselineFile:
    """A synthetic file of the baseline: its path in the space and its size."""

    path: str
    size: int

    def content(self) -> bytes:
        """Deterministic bytes, so every reset writes the same file."""
        out = bytearray()
        counter = 0
        while len(out) < self.size:
            out += hashlib.sha256(f"{self.path}:{counter}".encode()).digest()
            counter += 1
        return bytes(out[: self.size])


def baseline(space: str) -> tuple[BaselineFile, ...]:
    """The files a space holds at baseline, directories implied by their paths."""
    if space == STUDY_SPACE:
        named = (
            BaselineFile(CONTENT_FILE, 64),
            BaselineFile(REPLICATED_FILE, 1 << 20),
            BaselineFile("samples/sample-1.bin", 64 << 10),
            BaselineFile("samples/sample-2.bin", 256 << 10),
        )
        listing = tuple(BaselineFile(f"{LISTING_DIR}/entry-{i:04d}.txt", 16) for i in range(LISTING_ENTRIES))
        return named + listing
    return (BaselineFile("beta.txt", 64),)


def checked_root(value: str, base: Path = Path(DEFAULT_ROOT)) -> Path:
    """The federation's root from a setting, refused unless it is an absolute path at or below `base` with no symbolic
    link from `base` down to it: down deletes everything the root holds, so a root elsewhere, or one a link redirects,
    would delete data the federation does not own."""
    if not value or not os.path.isabs(value):
        raise ValueError(f"EXPCORE_FED_ROOT must be an absolute path at or below {base}")
    root = Path(os.path.normpath(value))
    if root != base and base not in root.parents:
        raise ValueError(f"EXPCORE_FED_ROOT must lie at or below {base}, the only place the federation may write")
    for step in (base, *(p for p in reversed(root.parents) if base in p.parents), root):
        if step.is_symlink():
            raise ValueError(f"EXPCORE_FED_ROOT may not pass through a symbolic link ({step})")
    return root


@dataclass(frozen=True)
class Layout:
    """The paths below the federation's root."""

    root: Path

    @classmethod
    def from_environment(cls) -> "Layout":
        return cls(checked_root(os.environ.get("EXPCORE_FED_ROOT", DEFAULT_ROOT)))

    @property
    def marker(self) -> Path:
        """The file that makes the root the federation's own: down's janitor deletes nothing in a root without it."""
        return self.root / ROOT_MARKER

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def users(self) -> Path:
        return self.secrets / "users"

    @property
    def ca_pem(self) -> Path:
        return self.root / "ca" / "federation-ca.pem"

    @property
    def ca_key(self) -> Path:
        return self.secrets / "ca.key"

    @property
    def admin_token(self) -> Path:
        return self.secrets / "admin.token"

    @property
    def facts(self) -> Path:
        return self.root / "facts.json"

    @property
    def platform(self) -> Path:
        return self.root / "platform.toml"

    @property
    def journal(self) -> Path:
        return self.root / "fault-journal"

    def passphrase(self, service: str) -> Path:
        return self.secrets / f"{service}.passphrase"

    def env_file(self, service: str) -> Path:
        return self.secrets / f"{service}.env"

    def registration_token(self, provider: str) -> Path:
        return self.secrets / f"{provider}.regtoken"

    def password(self, user: str) -> Path:
        return self.users / f"{user}.pass"

    def persistence(self, service: str) -> Path:
        return self.root / service / "persistence"

    def storage(self, service: str) -> Path:
        return self.root / service / "storage"

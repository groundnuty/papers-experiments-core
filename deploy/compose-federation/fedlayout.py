"""Where the throwaway federation's data lives, what its spaces hold at baseline, and what no layout may change.

What the federation is (its project, services, ports, spaces and users) is declared in a layout file (fedspec.py). The
one place on disk is the root (`EXPCORE_FED_ROOT`): down removes it whole, and nothing else outside this directory is
written. The root lies in the allowed parent (`EXPCORE_FED_ROOT_PARENT`, default /mnt/volume2/expcore-federation), with
no link from the parent down to it, or every command refuses before it touches a file or a container: down deletes what
the root holds. Under the default parent the root defaults to the parent itself; under any other parent the root must
be named and lie strictly below it, so a parent that holds other data is never itself the root.
"""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fedspec import Import, Space, Spec, load_spec

ROOT_MARKER = ".expcore-fed-root"  # written by up into a root it made or found empty; holds the project name
NESTED_DEPTH = 8  # how many levels below a root up and down look for another federation's marked root
HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "compose.yaml"  # the default layout's generated Compose file, committed for review
DEFAULT_ROOT = "/mnt/volume2/expcore-federation"
IMAGES = (  # pinned by the deployment: a layout cannot name another
    "docker.onedata.org/onezone:ID-ba7a778696",
    "docker.onedata.org/oneprovider:ID-765309c84f",
)
NODE = "node1"  # each service's one cluster node
REGULAR_STORAGE = "posix"  # the name of every provider's batch-mode storage (fedcompose.provider_config): its own disk
IMPORT_MOUNT = "/volumes/import"  # where an owner's service sees its imported spaces' directories, one below it each


def import_storage_name(space: str) -> str:
    """The name of the imported storage that holds a space: one storage per imported space, so each has its own
    directory and its own scan."""
    return f"import-{space}"


CONTENT_FILE = "hello.txt"  # a small file at the study space's root: the content test reads it
REPLICATED_FILE = "replicated.bin"  # replicated from the owner to the first mirror, so a transfer record exists
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


def baseline(space: str, study_space: str) -> tuple[BaselineFile, ...]:
    """The files a space holds at baseline, directories implied by their paths: the study space's named files and its
    listing directory, and one small file in every other space, named after it."""
    if space == study_space:
        named = (
            BaselineFile(CONTENT_FILE, 64),
            BaselineFile(REPLICATED_FILE, 1 << 20),
            BaselineFile("samples/sample-1.bin", 64 << 10),
            BaselineFile("samples/sample-2.bin", 256 << 10),
        )
        listing = tuple(BaselineFile(f"{LISTING_DIR}/entry-{i:04d}.txt", 16) for i in range(LISTING_ENTRIES))
        return named + listing
    return (BaselineFile(f"{space.removesuffix('-space')}.txt", 64),)


def baseline_plan(spec: Spec) -> dict[str, tuple[BaselineFile, ...]]:
    """What every declared space holds after up and after reset --clean: its baseline files, or nothing at all for a
    space declared `baseline = false`, the study space included."""
    return {space.name: baseline(space.name, spec.study_space) if space.baseline else () for space in spec.spaces}


def checked_parent(value: str) -> Path:
    """The allowed parent from a setting, refused unless it is an absolute path other than `/`."""
    if not value or not os.path.isabs(value):
        raise ValueError("EXPCORE_FED_ROOT_PARENT must be an absolute path")
    parent = Path(os.path.normpath(value))
    if parent == Path("/"):
        raise ValueError("EXPCORE_FED_ROOT_PARENT may not be /: the root must lie in a directory of its own")
    return parent


def checked_root(value: str, base: Path = Path(DEFAULT_ROOT), *, strict: bool = False) -> Path:
    """The federation's root from a setting, refused unless it is an absolute path at or below `base` (strictly below,
    when `strict`) with no symbolic link from `base` down to it: down deletes everything the root holds, so a root
    elsewhere, or one a link redirects, would delete data the federation does not own."""
    where = "strictly below" if strict else "at or below"
    if not value or not os.path.isabs(value):
        raise ValueError(f"EXPCORE_FED_ROOT must be an absolute path {where} {base}")
    root = Path(os.path.normpath(value))
    if base not in root.parents and (strict or root != base):
        raise ValueError(f"EXPCORE_FED_ROOT must lie {where} {base}, the only place the federation may write")
    for step in (base, *(p for p in reversed(root.parents) if base in p.parents), root):
        if step.is_symlink():
            raise ValueError(f"EXPCORE_FED_ROOT may not pass through a symbolic link ({step})")
    return root


def root_from_environment(environ: Mapping[str, str]) -> tuple[Path, Path]:
    """The allowed parent and the root, from EXPCORE_FED_ROOT_PARENT and EXPCORE_FED_ROOT.

    Under the default parent the root may be the parent itself, and is by default, as it always was: down removes it
    and up makes it again. Any other parent must exist, and the root must be named and lie strictly below it, so a
    parent that holds other data is never used as the root and never emptied by down."""
    configured = environ.get("EXPCORE_FED_ROOT_PARENT")
    parent = checked_parent(DEFAULT_ROOT if configured is None else configured)
    default = parent == Path(DEFAULT_ROOT)
    value = environ.get("EXPCORE_FED_ROOT")
    if value is None and not default:
        raise ValueError(
            f"EXPCORE_FED_ROOT must name a directory strictly below EXPCORE_FED_ROOT_PARENT ({parent}): a parent "
            "other than the default is never the root itself"
        )
    root = checked_root(DEFAULT_ROOT if value is None else value, parent, strict=not default)
    if root != parent and not parent.is_dir():
        raise ValueError(f"EXPCORE_FED_ROOT_PARENT must be an existing directory ({parent})")
    return parent, root


@dataclass(frozen=True)
class Layout:
    """The declared layout, the paths below the federation's root, and the allowed parent the root was checked
    against."""

    root: Path
    parent: Path = Path(DEFAULT_ROOT)
    spec: Spec = field(default_factory=load_spec)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, spec: Spec | None = None) -> "Layout":
        parent, root = root_from_environment(os.environ if environ is None else environ)
        return cls(root, parent, load_spec() if spec is None else spec)

    @property
    def owned(self) -> bool:
        """Whether the root carries this layout's mark: the file up writes, holding the declared project's name."""
        return self.marker.is_file() and self.marker.read_text(encoding="utf-8").strip() == self.spec.project

    @property
    def parent_is_default(self) -> bool:
        return self.parent == Path(DEFAULT_ROOT)

    @property
    def outer_roots(self) -> list[Path]:
        """Marked roots this root lies in, at any height up to `/`, the parent's own ancestors included: their down
        would delete this one."""
        return [p for p in self.root.parents if os.path.isfile(p / ROOT_MARKER)]

    @property
    def inner_roots(self) -> list[Path]:
        """Marked roots inside this root, down to NESTED_DEPTH levels below it, never through a link: this one's down
        would delete them. A directory the host user cannot read is one a service wrote as root inside its own data,
        where no federation's up makes a root, so the search passes it by."""
        if not os.path.isdir(self.root) or os.path.islink(self.root):
            return []
        found = []
        for directory, subdirectories, files in os.walk(self.root, followlinks=False):
            depth = len(Path(directory).relative_to(self.root).parts)
            if depth and ROOT_MARKER in files:
                found.append(Path(directory))
            if depth >= NESTED_DEPTH:
                subdirectories.clear()
        return found

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
    def address_map(self) -> Path:
        """The HPC federation's address map (O73): each declared name's node address, private, which only a federation
        whose names resolve nowhere has."""
        return self.secrets / "address-map.json"

    @property
    def toolshim(self) -> Path:
        """The directory of the launcher a Python tool server is started through (O73): the HPC federation's `up`
        copies it into the root, so a copy of the platform file beside a study reads the root's own."""
        return self.root / "toolshim"

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

    def imported(self, space: str) -> Import:
        """A space's import declaration; refused for a space that has none."""
        declared = self.spec.space(space).imported
        if declared is None:
            raise ValueError(f"{space} is not an imported space: spaces.{space} has no import")
        return declared

    def import_dir(self, space: str) -> Path:
        """Where a writable import's copy of its source lies: in the owner's directory of the root, so down removes
        it with the root. A read-only import has no copy: its source is mounted as it is."""
        self.imported(space)
        return self.root / self.spec.space(space).owner / "import" / space

    def shared_imports(self) -> list[Space]:
        """The spaces whose imported tree other federations import too (`import.shared`, O59), in the layout's order."""
        return [s for s in self.spec.spaces if s.imported is not None and s.imported.shared]

    def owned_imports(self, provider: str) -> list[Space]:
        """The imported spaces a provider owns: the only spaces whose tree it mounts. A mirror keeps a regular storage
        and mounts nothing of an import."""
        return [s for s in self.spec.spaces if s.imported is not None and s.owner == provider]

    def import_manifest(self, space: str, environ: Mapping[str, str]) -> Path:
        """The file of an imported space's manifest, from the variable its layout names (`import.manifest_env`).

        As for the source, the layout never holds the path, and what the variable names is checked before anything
        reads it: an absolute path of an existing regular file with no `..` step and no symbolic link on the way, not
        inside the federation's root (down empties it) and not inside the tree of any imported space whose variable is
        set, its own included: a scan would import it as a file of the dataset, and the listing would then hold an
        entry the manifest does not. A refusal names the variable and never says what it holds."""
        declared = self.imported(space)
        name = declared.manifest_env
        if name is None:
            raise ValueError(f"spaces.{space}.import declares no manifest_env: it has no manifest to name")
        value = environ.get(name)
        if value is None:
            raise ValueError(f"{name} is not set; it must name the manifest file of the dataset of {space}")
        if not value or not os.path.isabs(value):
            raise ValueError(f"{name} must be an absolute path of the manifest file of the dataset of {space}")
        manifest = Path(value)
        if ".." in manifest.parts:
            raise ValueError(f"{name} may not hold a '..' step: a link before it would change where it leads")
        for step in (*reversed(manifest.parents), manifest):
            if step.is_symlink():
                raise ValueError(f"{name} may not pass through a symbolic link")
        if not manifest.is_file():
            raise ValueError(f"{name} must be an existing regular file")
        if manifest == self.root or self.root in manifest.parents:
            raise ValueError(f"{name} lies inside the root of the federation, which down empties")
        for other in self.spec.spaces:
            tree = environ.get(other.imported.source_env, "") if other.imported is not None else ""
            if tree and os.path.isabs(tree) and Path(tree) in manifest.parents:
                raise ValueError(
                    f"{name} lies inside the tree of {other.name}, where a scan would import it as a file of the "
                    "dataset"
                )
        return manifest

    def load_manifest(self, space: str, environ: Mapping[str, str]) -> Any:
        """The manifest of an imported space, checked (`import_manifest`) and loaded by the core (`load_manifest`).

        The core's refusal of a manifest names the file it read, which is the host's own path, so it is caught here and
        said again with the file's place taken out: a message of this layer names the variable and never a path."""
        from expcore.config.loading import ConfigError
        from expcore_onedata.importmanifest import load_manifest

        path = self.import_manifest(space, environ)
        try:
            return load_manifest(path)
        except ConfigError as error:
            variable = self.imported(space).manifest_env
            raise ValueError(
                f"the manifest named by {variable} cannot be used: {str(error).replace(str(path), 'it')}"
            ) from None

    def import_source(self, space: str, environ: Mapping[str, str]) -> Path:
        """The host directory of an imported space's dataset, from the variable its layout names.

        The layout never holds a path, so the variable is the only way to one, and what it names is checked before
        anything reads, copies or mounts it: an absolute path of an existing directory with no `..` step and no
        symbolic link on the way, which is not the root, inside it, or holding it (down empties the root, and the
        copy or the mount would meet it). A refusal names the variable and never says what it holds."""
        name = self.imported(space).source_env
        value = environ.get(name)
        if value is None:
            raise ValueError(f"{name} is not set; it must name the directory of the dataset of {space}")
        if not value or not os.path.isabs(value):
            raise ValueError(f"{name} must be an absolute path of the directory of the dataset of {space}")
        source = Path(value)  # drops a trailing separator and `.` steps, keeps `..`
        if ".." in source.parts:
            raise ValueError(f"{name} may not hold a '..' step: a link before it would change where it leads")
        for step in (*reversed(source.parents), source):
            if step.is_symlink():
                raise ValueError(f"{name} may not pass through a symbolic link")
        if not source.is_dir():
            raise ValueError(f"{name} must be an existing directory")
        if source == self.root:
            raise ValueError(f"{name} is the root of the federation, which down empties")
        if self.root in source.parents:
            raise ValueError(f"{name} lies inside the root of the federation, which down empties")
        if source in self.root.parents:
            raise ValueError(f"{name} holds the root of the federation, which down empties")
        return source

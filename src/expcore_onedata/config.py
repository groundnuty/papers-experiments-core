# Provenance: new
"""platform.toml: the one place a federation's names, addresses and identifiers are written (O12).

No other module of the platform layer holds a provider, space, host or user name; each looks the value up in the
`PlatformConfig` this module loads. Loading validates everything and names the file and the key in every message. A
message never echoes a value: a mistake in a URL or a token source cannot print the federation's names, and a table
of named entries is named by position (`providers[2]`, `conditions[0]`) and never by the name it is filed under.

```toml
[zone]                          # Onezone
name = "zone"                   # the label the layer uses for it in messages
url = "https://..."             # scheme and host (and port) only

[providers.<name>]              # the providers, by the name the rest of the file uses
url = "https://..."
id = "..."                      # the id Onezone reports for the provider; reported providers are matched by it (O04)

[space]
name = "..."
id = "..."
prefixes = ["..."]              # a mutation may only name a space whose name starts with one of these (O09)

[support]
providers = ["<name>", ...]     # the declared supporting set; health fails on any difference (O04)
local = "<name>"                # the agent's own provider; the agent is pinned to it (O03)
                                # or, for several spaces, [[spaces]] in place of both tables (spaces.py)

[credentials.admin]             # the admin access token that examines tokens and reads privileges: exactly one of
env = "VARIABLE_NAME"           #   the name of an environment variable, or
file = "path"                   #   a file (relative to this one), never the value itself; mode 0600, owned by you

[credentials.users]             # each platform user's own password, from which that user mints its own tokens
directory_env = "VARIABLE_NAME" # the name of an environment variable that holds the directory of the files, or
directory = "path"              #   the directory (relative to this file); exactly one of the two
pattern = "{user}.pass"         # optional: the file of a user, one {user}, mode 0600, owned by you; never the password

[tokens]                        # optional; the lifetime rule of tokens.py
margin_s = 900                  # a token outlives the run's whole-time ceiling by at least this
max_excess_s = 600              # and by at most this much more
mint_headroom_s = 300           # the lifetime asked for beyond the minimum, for the seconds before examination
max_ttl_s = 86400               # optional: the longest lifetime the platform grants
revocation_settle_s = 330       # a slot's revoked tokens count as gone this long after the revocation is confirmed;
                                #   at least 315, the propagation bound (300 s cache purge + 15 s session recheck)

[principals.data_path]          # optional fixed users for runs that lease none (day-0, health, live checks)
username = "..."                # the login name whose credential file is read
user_id = "..."                 # the id Onezone gives the user: its privileges, and the subject of its tokens
[principals.toolserver]         # likewise; never the same user as data_path
[principals.service]            # likewise; the health check's own user

[tls]                           # optional: the certificate check of every client of the federation (tls.py)
ca_file = "federation-ca.pem"   # a CA bundle the federation's certificate chains to; verification stays on
insecure_tls = true             # or: verification off, for a disposable test federation only; never both

[toolserver]
command = "path/to/executable"
args = []
start_timeout_s = 120
clone = "path"                  # optional, with commit: the checkout the server runs from, verified clean
commit = "..."

[toolserver.conditions.<name>]
allow = ["tool", ...]           # the server-side allow-list of this condition
rights = ["space_view", ...]    # the platform privileges of its principal and the caveats of its token
manifest = "path.json"          # the frozen manifest the client checks the server against (L13)

[sandbox]                       # optional: images, network pool, limits, mount, data paths (see sandbox/settings.py)
```

Two more tables are accepted here and read by the modules that use them, each validated against this file's providers:
`[placement]` (the data provider, the bounds of placement and the declared range map; `rangemap.load_placement`) and
`[slots.<name>]` (the leased platform users, one per token plane; `users.load_slots`). `[faults]` (the sanction, journal
and services of the fault injector; `faults.load_faults`) is read the same way. `[link_check]` (the probe space of the
pre-campaign link check, never a study space, and its bounds; `linksettings.load_link_check`, O35) is read when the
file is loaded, into `link_check`, and kept out of the fingerprint: it says what is checked before a campaign, not what
a run measures.

**Several spaces** (`spaces.py`). The one-space form's single space is `spaces[0]`, labelled `space`, and its
fingerprint is unchanged. With several spaces, `space` and `support` refuse (there is no one space to give), and each
consumer names the space it acts on by its label: provisioning, placement, eviction and the probes are bound to one
(`provision.Provisioner`), a token plane's caveats and privilege checks reach every space it is scoped to
(`space_ids`), a mutation passes the gate when its target lies under any declared prefix (`prefixes`), and the spaces
enter the campaign fingerprint, so a resume under another set is refused.
"""

import hashlib
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from expcore.config.loading import ConfigError, Section, load_toml
from expcore_onedata.credentials import DEFAULT_PATTERN, PLAIN_ID, Principal, SecretSource, UserCredentials
from expcore_onedata.linksettings import LinkCheckSettings, load_link_check
from expcore_onedata.rights import declares_qos, declares_write, rights_problem
from expcore_onedata.sandbox.settings import SandboxSettings, load_sandbox
from expcore_onedata.spaces import PLANES, Space, StudySpace, Support, load_spaces
from expcore_onedata.tls import TlsSettings, load_tls, moved

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
# The shortest settle a revocation may have: the bound of its propagation to a provider, the cache purge 300 s after
# the provider lost its connection to Onezone plus the 15 s an open session waits before it checks its token again.
REVOCATION_SETTLE_MIN_S = 315
REVOCATION_SETTLE_BOUND = (
    f"at least {REVOCATION_SETTLE_MIN_S} s, the bound of a revocation's propagation: a provider purges its cache 300 s "
    "after it lost its connection to Onezone, and an open session checks its token again every 15 s"
)


@dataclass(frozen=True)
class Zone:
    """Onezone."""

    name: str
    url: str


@dataclass(frozen=True)
class Provider:
    """One provider the federation may report, with the id Onezone gives it.

    The file always declares the id and reported providers are matched by it. `id` is None only for a provider built in
    code without one, which is then matched by its URL's host and refused as ambiguous when that host fits several.
    """

    name: str
    url: str
    id: str | None = None

    @property
    def host(self) -> str:
        """The URL's host, lower case, which Onezone reports as the provider's domain."""
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True)
class TokenSettings:
    """The lifetime rule's numbers (O06), and how long a confirmed revocation takes to settle (C19).

    `revocation_settle_s` covers its propagation to the providers: pushed at once over Graph Sync, and at worst the
    300 s after which a provider whose connection to Onezone broke purges its cache, plus the 15 s an open session
    waits before it checks its token again (`users.py`). A shorter one than that bound, `REVOCATION_SETTLE_MIN_S`
    (315 s), is refused, here and by `revocation.revoke_slot`."""

    margin_s: int = 900
    max_excess_s: int = 600
    mint_headroom_s: int = 300
    max_ttl_s: int | None = None
    revocation_settle_s: int = 330


@dataclass(frozen=True)
class Principals:
    """Fixed platform users for the runs that lease none; a leased user comes from the lease instead."""

    data_path: Principal | None = None
    toolserver: Principal | None = None
    service: Principal | None = None


@dataclass(frozen=True)
class Condition:
    """What the tool server gives one condition: its allow-list, its rights and its frozen manifest."""

    name: str
    allow: tuple[str, ...]
    rights: tuple[str, ...]
    manifest: Path

    @property
    def writes(self) -> bool:
        """Whether the condition declares any write right."""
        return declares_write(self.rights)

    @property
    def manages_qos(self) -> bool:
        """Whether this one condition declares the right to manage QoS requirements (O33). It is not what a caller of
        `SpaceReset.run` passes as `qos_rights` on its own: a study's reset runs at the start of a run and clears what
        earlier runs left, so `qos_rights` is whether any run since the last verified reset could have set a
        requirement, and a caller combines this over every such run, never the next run's alone."""
        return declares_qos(self.rights)


@dataclass(frozen=True)
class ToolServer:
    """The tool server's executable and the conditions it serves."""

    command: str
    args: tuple[str, ...]
    start_timeout_s: float
    clone: Path | None
    commit: str | None
    conditions: Mapping[str, Condition] = field(default_factory=dict)


@dataclass(frozen=True)
class PlatformConfig:
    """Everything the platform layer knows about a federation, validated."""

    file: Path
    zone: Zone
    providers: Mapping[str, Provider]
    spaces: tuple[StudySpace, ...]  # the one-space form's single space, or the entries of [[spaces]]
    admin: SecretSource
    users: UserCredentials | None
    tokens: TokenSettings
    principals: Principals
    toolserver: ToolServer | None
    sandbox: SandboxSettings | None = None
    sha256: str = ""  # of the file as loaded: a record names the configuration by this, never by a value in it
    tls: TlsSettings = field(default_factory=TlsSettings)  # every client's one certificate check (`[tls]`)
    spaces_table: bool = False  # the spaces came from [[spaces]], one entry or several, not from [space]/[support]
    link_check: LinkCheckSettings | None = None  # the pre-campaign link check's probe space and bounds (O35)

    @property
    def probe_space(self) -> Space | None:
        """The link check's probe space, which only a request bound to it reaches (`OnezoneClient.mutate`), or None."""
        return None if self.link_check is None else self.link_check.space

    def space_named(self, label: str | None = None) -> StudySpace:
        """Return the space a label names, or, with none, the only one; refuse, naming no label, when there is none
        such, or when several are declared and none is named."""
        if label is None:
            if len(self.spaces) != 1:
                raise ConfigError(f"{self.file}: [[spaces]]: several spaces are declared, so name one by its label")
            return self.spaces[0]
        found = [entry for entry in self.spaces if entry.label == label]
        if not found:
            raise ConfigError(f"{self.file}: [[spaces]]: the space asked for is not declared")
        return found[0]

    @property
    def space(self) -> Space:
        """The only space; refused when several are declared, since then a caller must name one (`space_named`)."""
        return self.space_named().space

    @property
    def support(self) -> Support:
        """The only space's support set; refused when several are declared (`space_named(label).support`)."""
        return self.space_named().support

    @property
    def prefixes(self) -> tuple[str, ...]:
        """Every declared space's prefixes, each once: a mutation's target must start with one of them (O09)."""
        return tuple(dict.fromkeys(p for entry in self.spaces for p in entry.space.prefixes))

    def space_ids(self, plane: str) -> tuple[str, ...]:
        """The ids of the spaces a token plane is scoped to, in the file's order."""
        if plane not in PLANES:
            raise ValueError(f"unknown token plane; known: {', '.join(PLANES)}")
        return tuple(entry.space.id for entry in self.spaces if plane in entry.tokens)

    @property
    def local_provider(self) -> Provider:
        """The agent's own provider, the same in every declared space."""
        return self.providers[self.spaces[0].support.local]

    def provider_label(self, name: str) -> str:
        """Name a declared provider for a message by its position in the file, `providers[i]`, never by its name."""
        return f"providers[{list(self.providers).index(name)}]"

    def provider_labels(self, names: Iterable[str]) -> str:
        """Name several declared providers, in the file's order, joined for a message."""
        order = list(self.providers)
        return ", ".join(self.provider_label(n) for n in sorted(set(names), key=order.index))

    def fingerprint(self) -> dict[str, Any]:
        """The platform's half of the campaign fingerprint (`StudyPlugin.platform`): what a resume must not change.

        It is the TLS setting by its mode, with the bundle's SHA-256 under ca_file, never a path or a value (O12), and,
        whenever the `[[spaces]]` form is used (one entry or several), each entry's SHA-256 in the file's order
        (`StudySpace.digest`), so a resume under another set, order, support, data provider or token planes is refused.
        The one-space form, `[space]` and `[support]`, adds nothing: its fingerprint is as it was.

        With a `[sandbox]`, it also holds every level of the data path with its kind and a REST level's listing mode
        (O21), so a resume after a level changed its data path or its listing is refused. A declared
        `[sandbox.served_bytes]` puts the caps of the tables of the bytes served there too (O22), since they decide what
        a run's evidence can show; without the table the fingerprint keeps the shape it had before the caps could be
        declared, so a campaign that ran under the defaults still resumes.

        When the file has `[faults]`, it holds the fault injector's sanction and targets too
        (`fault_settings.faults_fingerprint`), never a path, a name or a value (O12). A file without `[faults]` keeps
        the fingerprint it had before the fault injector existed.
        """
        from expcore_onedata.fault_settings import faults_fingerprint  # it reads this module's config: import when used

        out: dict[str, Any] = {"tls": self.tls.record()}
        if self.spaces_table:
            out["spaces"] = [entry.digest() for entry in self.spaces]
        if self.sandbox is not None:
            out["data_paths"] = self.sandbox.data_path_record()
            if self.sandbox.served_caps_declared:
                out["served_bytes"] = self.sandbox.served_caps.record()
        faults = faults_fingerprint(self)
        if faults is not None:
            out["faults"] = faults
        return out

    def condition_label(self, name: str) -> str:
        """Name a declared condition for a message by its position in the file, `conditions[i]`, never by its name."""
        order = list(self.toolserver.conditions) if self.toolserver else []
        return f"conditions[{order.index(name)}]"

    def condition(self, name: str) -> Condition:
        """Return a condition of the tool server; raise ConfigError, without naming it, when there is none."""
        server = self.toolserver
        if server is None or name not in server.conditions:
            raise ConfigError(f"{self.file}: [toolserver.conditions]: the condition asked for is not declared")
        return server.conditions[name]


def _url(section: Section, key: str) -> str:
    """Read a URL that is a scheme, a host and perhaps a port, with no credentials, path, query or fragment."""
    value = section.string(key)
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 -- reading it is the check: a port out of range raises
    except ValueError:
        raise section.fail(key, "is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or not HOSTNAME.fullmatch(parts.hostname):
        raise section.fail(key, "must be an http or https URL with a host name")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise section.fail(key, "must hold the scheme, the host and perhaps the port only")
    return value.rstrip("/")


def _name(section: Section, key: str | None, value: str, what: str) -> str:
    """Check a name the file uses for something; the message never holds the name."""
    if not NAME.fullmatch(value):
        raise section.fail(key, f"{what} must be letters, digits, '_' and '-', starting with a letter")
    return value


def _entry(table: Section, index: int, name: str) -> Section:
    """Return one named entry of a table of named tables, known in messages by its position and never by its name."""
    value = table.data[name]
    if not isinstance(value, Mapping):
        raise table.fail(None, f"entry {index} must be a table")
    return Section(value, table.file, f"{table.where}[{index}]")


def _number(section: Section, key: str, default: float, *, minimum: float, integer: bool = False) -> float:
    """Read an optional finite number at or above a minimum."""
    if key not in section.data:
        return default
    value = section.data[key]
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value < minimum:
        noun = "an integer" if integer else "a number"
        raise section.fail(key, f"must be {noun} of at least {minimum:g}")
    return value


def _secret_source(section: Section, base: Path, *, require: bool, environ: Mapping[str, str] | None) -> SecretSource:
    """Read `env` or `file` (exactly one) and, when asked, check that the secret can be read."""
    section.reject_unknown(("env", "file"))
    if ("env" in section.data) == ("file" in section.data):
        raise section.fail(None, "must have exactly one of 'env' and 'file'")
    if "env" in section.data:
        name = section.string("env")
        if not ENV_NAME.fullmatch(name):
            raise section.fail("env", "must be the name of an environment variable, not its value")
        source = SecretSource("env", name)
    else:
        source = SecretSource("file", str((base / section.string("file")).resolve()))
    if require:
        try:
            source.read(environ)
        except ConfigError as error:
            raise section.fail(None, f"its source cannot be read: {error}") from None
    return source


def _providers(root: Section) -> dict[str, Provider]:
    """Read [providers.*]; at least one, each with a URL and an id, no id twice."""
    table = root.section("providers")
    if not table.data:
        raise table.fail(None, "must declare at least one provider")
    out: dict[str, Provider] = {}
    for index, name in enumerate(table.data):
        entry = _entry(table, index, name)
        _name(entry, None, name, "its name")
        entry.reject_unknown(("url", "id"))
        out[name] = Provider(name, _url(entry, "url"), entry.string("id"))
    ids = [provider.id for provider in out.values()]
    if len(set(ids)) != len(ids):
        raise table.fail(None, "must give each provider its own id")
    return out


def _tokens(section: Section) -> TokenSettings:
    """Read the optional [tokens] table."""
    section.reject_unknown(("margin_s", "max_excess_s", "mint_headroom_s", "max_ttl_s", "revocation_settle_s"))
    base = TokenSettings()
    max_ttl = _number(section, "max_ttl_s", 0, minimum=1, integer=True) if "max_ttl_s" in section.data else None
    settings = TokenSettings(
        int(_number(section, "margin_s", base.margin_s, minimum=1, integer=True)),
        int(_number(section, "max_excess_s", base.max_excess_s, minimum=0, integer=True)),
        int(_number(section, "mint_headroom_s", base.mint_headroom_s, minimum=0, integer=True)),
        None if max_ttl is None else int(max_ttl),
        _settle(section, base.revocation_settle_s),
    )
    if settings.mint_headroom_s > settings.max_excess_s:
        raise section.fail("mint_headroom_s", "must not exceed max_excess_s, or a fresh token fails its examination")
    return settings


def _settle(section: Section, default: int) -> int:
    """Read the optional `revocation_settle_s`: an integer no shorter than the propagation bound, so a slot is never
    freed while a provider may still accept a revoked token from its cache."""
    if "revocation_settle_s" not in section.data:
        return default
    value = section.data["revocation_settle_s"]
    if isinstance(value, bool) or not isinstance(value, int) or value < REVOCATION_SETTLE_MIN_S:
        raise section.fail("revocation_settle_s", f"must be an integer of {REVOCATION_SETTLE_BOUND}")
    return value


def _principal(section: Section, key: str) -> Principal | None:
    """Read one optional principal table: its login name and its Onezone id."""
    if key not in section.data:
        return None
    entry = section.section(key)
    entry.reject_unknown(("username", "user_id"))
    values = {name: entry.string(name) for name in ("username", "user_id")}
    for name, value in values.items():
        if not PLAIN_ID.fullmatch(value):
            raise entry.fail(name, "must be a plain name of letters, digits and _.@-")
    return Principal(**values)


def _principals(section: Section) -> Principals:
    """Read the optional [principals] tables; the two planes never share a user, by name or by id."""
    section.reject_unknown(("data_path", "toolserver", "service"))
    principals = Principals(*(_principal(section, key) for key in ("data_path", "toolserver", "service")))
    data, tool = principals.data_path, principals.toolserver
    if data is not None and tool is not None and (data.username == tool.username or data.user_id == tool.user_id):
        raise section.fail("toolserver", "must be a different principal from data_path")
    return principals


def _user_credentials(section: Section, base: Path) -> UserCredentials:
    """Read [credentials.users]: a directory of per-user files, named by a variable or a path, and a file pattern."""
    section.reject_unknown(("directory_env", "directory", "pattern"))
    if ("directory_env" in section.data) == ("directory" in section.data):
        raise section.fail(None, "must have exactly one of 'directory_env' and 'directory'")
    pattern = section.optional_string("pattern") or DEFAULT_PATTERN
    if pattern.count("{user}") != 1 or re.search(r"[/\\]|\.\.|\{(?!user\})", pattern) or pattern.startswith("."):
        raise section.fail("pattern", "must be a file name with one {user} and no path")
    if "directory_env" in section.data:
        name = section.string("directory_env")
        if not ENV_NAME.fullmatch(name):
            raise section.fail("directory_env", "must be the name of an environment variable, not a path")
        return UserCredentials("env", name, pattern)
    return UserCredentials("dir", str((base / section.string("directory")).resolve()), pattern)


def _check_fixed_users(
    users: UserCredentials | None, principals: Principals, file: Path, environ: Mapping[str, str] | None
) -> None:
    """With credentials required, every fixed principal must have a credential file: presence and mode only."""
    fixed = [p for p in (principals.data_path, principals.toolserver, principals.service) if p is not None]
    if not fixed:
        return
    if users is None:
        raise ConfigError(f"{file}: [credentials]: key 'users' is required when [principals] declares a user")
    for principal in fixed:
        try:
            users.check(principal.username, environ)
        except ConfigError as error:
            raise ConfigError(f"{file}: [principals]: {error}") from None


def _condition(name: str, section: Section, base: Path) -> Condition:
    """Read one condition: allow-list, rights, manifest. `section` is known in messages by its position."""
    _name(section, None, name, "its name")
    section.reject_unknown(("allow", "rights", "manifest"))
    allow, rights = section.strings("allow"), section.strings("rights")
    if not allow or len(set(allow)) != len(allow):
        raise section.fail("allow", "must list at least one tool, each once")
    problem = rights_problem(rights)
    if problem is not None:
        raise section.fail("rights", problem)
    manifest = (base / section.string("manifest")).resolve()
    if not manifest.is_file():
        raise section.fail("manifest", "is not a file; freeze the server's manifest first")
    return Condition(name, allow, tuple(sorted(rights)), manifest)


def _toolserver(section: Section, base: Path) -> ToolServer:
    """Read [toolserver] and its conditions."""
    if "insecure_tls" in section.data:
        raise moved(section, "insecure_tls")
    section.reject_unknown(("command", "args", "start_timeout_s", "clone", "commit", "conditions"))
    clone, commit = section.optional_string("clone"), section.optional_string("commit")
    if (clone is None) != (commit is None):
        raise section.fail("commit" if commit is None else "clone", "is needed beside the other: both or neither")
    conditions = section.section("conditions")
    if not conditions.data:
        raise conditions.fail(None, "must declare at least one condition")
    return ToolServer(
        section.string("command"),
        section.strings("args"),
        float(_number(section, "start_timeout_s", 120.0, minimum=1)),
        (base / clone).resolve() if clone else None,
        commit,
        MappingProxyType(
            {n: _condition(n, _entry(conditions, index, n), base) for index, n in enumerate(conditions.data)}
        ),
    )


def load_platform(
    path: str | os.PathLike[str], *, environ: Mapping[str, str] | None = None, require_secrets: bool = True
) -> PlatformConfig:
    """Load and validate platform.toml; raise ConfigError naming the file and the key, never a value.

    With `require_secrets` (the default, for a run) the admin credential's source must exist and hold something, and
    each fixed principal must have a credential file that others cannot read, so a missing variable or file fails here
    and not at the first request. Only the presence and mode of a user's file are checked, never its contents.
    """
    file = Path(path).resolve()
    try:  # the same errors load_toml gives, so a missing file is a configuration error, not a crash
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
    except FileNotFoundError:
        raise ConfigError(f"{file}: file not found") from None
    except OSError as error:
        raise ConfigError(f"{file}: cannot be read ({type(error).__name__})") from None
    root = Section(load_toml(file), file, "platform.toml")
    root.reject_unknown(
        (
            "zone",
            "providers",
            "space",
            "support",
            "spaces",
            "credentials",
            "tokens",
            "principals",
            "toolserver",
            "tls",
            "sandbox",
            "placement",  # read by rangemap.load_placement
            "slots",  # read by users.load_slots
            "faults",  # read by faults.load_faults
            "link_check",  # read by linksettings.load_link_check, below
        )
    )
    zone = root.section("zone")
    zone.reject_unknown(("name", "url"))
    providers = _providers(root)
    credentials = root.section("credentials")
    credentials.reject_unknown(("admin", "users"))
    users = _user_credentials(credentials.section("users"), file.parent) if "users" in credentials.data else None
    principals = _principals(Section(root.table("principals"), file, "[principals]"))
    tls = load_tls(Section(root.table("tls"), file, "[tls]"), file.parent)
    if require_secrets:
        _check_fixed_users(users, principals, file, environ)
    spaces = load_spaces(root, providers)
    return PlatformConfig(
        file=file,
        zone=Zone(_name(zone, "name", zone.string("name"), "the zone name"), _url(zone, "url")),
        providers=MappingProxyType(providers),
        spaces=spaces,
        spaces_table="spaces" in root.data,
        admin=_secret_source(credentials.section("admin"), file.parent, require=require_secrets, environ=environ),
        users=users,
        tokens=_tokens(Section(root.table("tokens"), file, "[tokens]")),
        principals=principals,
        toolserver=_toolserver(root.section("toolserver"), file.parent) if "toolserver" in root.data else None,
        sandbox=load_sandbox(
            Section(root.table("sandbox"), file, "[sandbox]"),
            file.parent,
            tls=tls,
            providers=tuple(providers),
            local=spaces[0].support.local,  # the same in every entry (load_spaces, O03)
        )
        if "sandbox" in root.data
        else None,
        sha256=digest,
        tls=tls,
        link_check=load_link_check(root, providers, spaces),
    )

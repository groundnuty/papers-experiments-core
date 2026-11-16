# Provenance: new
"""The run-shaping settings of the platform's fingerprint (C06): every declared setting that shapes a run, after its
default and in one canonical form, and nothing that is one federation's own (O12).

`PlatformConfig.fingerprint()` is what a resume must not change and what two shards must share to merge, so it holds
every setting of platform.toml that shapes a run, and only those: what the agent sees and can do (its images, its
interpreter and user, the mount's path and I/O mode, the placeholder it is given, its tool server's conditions), the
limits and timings it runs under (the sandbox's limits, the kill's grace, the token lifetime, the fault injector's
waits and heal), the code and images it runs (by digest), the data it reads and where it lies (the spaces, the data
provider, the declared range map). A setting is out when it is one federation's or one host's own (an address, an id, a
port, a host, network or Compose project name, a path, the command that starts a tool on that host), a secret or where
one is read from, or when it decides only what is checked before a campaign (`[link_check]`, `content_check_file`) or
how the reset reaches a state it then verifies (`[placement]`'s waits and concurrency, which the core records and
compares at every resume instead, C35). Two renders of one template on two federations, with their own addresses, ids,
ports and roots, so give one fingerprint, and a changed setting that shapes a run gives another.

Canonical: each value is the effective one, after its default, so a key declared at its default and one left out are
one experiment; each number is of its field's type, so `2` and `2.0` are one value and one canonical JSON; a set (an
allow-list, the trust paths, a support set) is sorted, and an order that matters (oneclient's arguments) is kept. A
configured name is never here: a provider is its label (`providers[i]`), and a value that can hold a name, a path or an
address (oneclient's extra arguments) or that sits where a token goes (the placeholder token) is its SHA-256. An image
is its pinned id, never its name, which can hold a registry's host or the SIF's path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from typing import TYPE_CHECKING, Any

from expcore.config.loading import ConfigError, load_toml

if TYPE_CHECKING:  # config imports this module: the types only, for the annotations
    from expcore_onedata.config import PlatformConfig, TokenSettings, ToolServer
    from expcore_onedata.sandbox.settings import SandboxSettings
    from expcore_onedata.spaces import StudySpace


def digest_of(value: Any) -> str:
    """The SHA-256 of a value's canonical JSON (sorted keys, no spaces): how a value that may hold a name is held."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def tokens_record(tokens: TokenSettings) -> dict[str, Any]:
    """`[tokens]` after its defaults: the lifetime rule of a run's tokens and the settle of a revocation (O06, C19). A
    token that outlives its run by less fails the run's last check of its tool plane, so every number is in."""
    return {
        "margin_s": int(tokens.margin_s),
        "max_excess_s": int(tokens.max_excess_s),
        "mint_headroom_s": int(tokens.mint_headroom_s),
        "max_ttl_s": None if tokens.max_ttl_s is None else int(tokens.max_ttl_s),
        "revocation_settle_s": int(tokens.revocation_settle_s),
    }


def support_record(config: PlatformConfig) -> dict[str, Any]:
    """The one-space form's `[support]` and its space's prefixes: the providers that support the space, in the file's
    order of `[providers]`, and the agent's own, each by its label (`providers[i]`), and the prefixes a mutation's
    target must start with. The space's name and id are the federation's (`up` makes the id anew); the `[[spaces]]`
    form holds all of this in each entry's digest."""
    entry: StudySpace = config.spaces[0]
    supporting = [name for name in config.providers if name in entry.support.providers]
    return {
        "providers": [config.provider_label(name) for name in supporting],
        "local": config.provider_label(entry.support.local),
        "prefixes": sorted(entry.space.prefixes),
    }


def sandbox_record(sandbox: SandboxSettings) -> dict[str, Any]:
    """`[sandbox]`'s settings that shape a run beside its data paths, served caps and Apptainer pins (which keep their
    own keys): the backend; every image by its pinned id; every limit; the mount's path in the agent, its I/O mode and
    oneclient's extra arguments (by their SHA-256); the paths in the agent's image that trust the run's CA; the agent's
    user and interpreter; the kill's grace at a cell's deadline; and the agent's placeholder token by its SHA-256. Not
    here: the work root, the name prefix, the label namespace, the privilege command and earlyoom's name for the driver
    (the host's own), the network pools and `[sandbox.federation]` (the host's and the federation's addresses and
    names), the mount's storages and check file (the federation's storage ids and the host's paths), and
    `content_check_file`, read once before a campaign."""
    from expcore_onedata.sandbox.settings import Limits  # it imports the sandbox's modules: import when used

    mount = sandbox.mount
    return {
        "backend": sandbox.backend,
        "images": {
            "agent": sandbox.agent.id,
            "gate": sandbox.gate.id,
            "oneclient": None if sandbox.oneclient is None else sandbox.oneclient.id,
        },
        "limits": {
            item.name: float(getattr(sandbox.limits, item.name))
            if item.name == "cpus"
            else int(getattr(sandbox.limits, item.name))
            for item in fields(Limits)
        },
        "mount": None
        if mount is None
        else {"path": mount.path, "io_mode": mount.io_mode, "extra_args_sha256": digest_of(list(mount.extra_args))},
        "trust_paths": sorted(set(sandbox.trust_paths)),
        "agent_uid": int(sandbox.agent_uid),
        "python": sandbox.python,
        "kill_grace_s": float(sandbox.kill_grace_s),
        "placeholder_token_sha256": hashlib.sha256(sandbox.placeholder_token.encode("utf-8")).hexdigest(),
    }


def toolserver_record(server: ToolServer, file: Any) -> dict[str, Any]:
    """`[toolserver]`'s settings that shape a run: the pinned commit of its checkout (None when none is pinned), the
    longest its start may take, and each condition by its name with its allow-list, its rights and its frozen
    manifest's SHA-256. Not here: the command, its arguments and the clone's path, which are the host's (a path, and
    in the arguments often the federation's address); the code they start is the commit's, and what it serves is the
    manifest, which the client holds the server to (L13). A manifest that cannot be read is refused naming its
    condition's position, never its path."""
    conditions: dict[str, Any] = {}
    for index, (name, condition) in enumerate(server.conditions.items()):
        try:
            manifest = hashlib.sha256(condition.manifest.read_bytes()).hexdigest()
        except OSError as error:
            raise ConfigError(
                f"{file}: [toolserver.conditions]: conditions[{index}]: key 'manifest' cannot be read "
                f"({type(error).__name__})"
            ) from None
        conditions[name] = {
            "allow": sorted(condition.allow),
            "rights": sorted(condition.rights),
            "manifest_sha256": manifest,
        }
    return {
        "commit": server.commit,
        "start_timeout_s": float(server.start_timeout_s),
        "conditions": conditions,
    }


def placement_record(config: PlatformConfig) -> dict[str, Any] | None:
    """`[placement]`'s settings that shape a run, or None without the table: the data provider by its label and the
    declared range map by its digest (`RangeMap.digest`), which say where the bytes a run reads lie. Its waits,
    concurrency, QoS scan and shortcut decide how a reset reaches a state it then verifies, not that state: they are
    recorded and compared at every resume (`Placement.reset_settings`, C35), never here."""
    if "placement" not in load_toml(config.file):
        return None
    from expcore_onedata.rangemap import load_placement  # it reads this module's config: import when used

    placement = load_placement(config.file, config)
    return {"holder": config.provider_label(placement.settings.holder), "map_sha256": placement.map.digest()}


__all__ = [
    "digest_of",
    "placement_record",
    "sandbox_record",
    "support_record",
    "tokens_record",
    "toolserver_record",
]

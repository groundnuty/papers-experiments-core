# Provenance: new
"""The run-shaping settings of the platform's fingerprint (C06): every declared setting that shapes a run, after its
default and in one canonical form, and nothing that is one federation's own (O12).

`PlatformConfig.fingerprint()` is what a resume must not change and what two shards must share to merge, so it holds
every setting of platform.toml that shapes a run, and only those: what the agent sees and can do (its images, its
interpreter and user, the mount's path and I/O mode, the placeholder it is given, its tool server's conditions), the
limits and timings it runs under (the sandbox's limits, the kill's grace, the token lifetime, the fault injector's
waits and heal, the bounds its instruments read during the run), the code and images it runs (by digest), the data it
reads and where it lies (the spaces, the data provider, the declared range map). A setting is out when it is one
federation's or one host's own (an address, an id, a port, a host, network or Compose project name, a path, the
command that starts a tool on that host), a secret or where one is read from, or when it decides only what is checked
before a campaign (`[link_check]`, `content_check_file`) or how a reset reaches a state it then verifies, or how many
requests are in flight (`[placement]`'s concurrency, QoS scan and reset settings, which the core records and compares
at every resume instead, C35). Two renders of one template on two federations, with their own addresses, ids,
ports and roots, so give one fingerprint, and a changed setting that shapes a run gives another.

Canonical: each value is the effective one, after its default, so a key declared at its default and one left out are
one experiment; each number is of its field's type, so `2` and `2.0` are one value and one canonical JSON; a set (an
allow-list, the trust paths, a support set) is sorted, and an order that matters (oneclient's arguments) is kept. A
configured name is never here: a provider is its label (`providers[i]`), a condition its name's SHA-256, and a value
that can hold a name, a path or an address (oneclient's extra arguments) or that sits where a token goes (the
placeholder token) is its SHA-256. An image is its pinned id, never its name, which can hold a registry's host or the
SIF's path.
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
    """The one-space form's `[space]` and `[support]`: the providers that support the space, in the file's order of
    `[providers]`, and the agent's own, each by its label (`providers[i]`); the space's name, under which the agent
    sees it on a mount, and the prefixes a mutation's target must start with, each by its SHA-256, since a name is
    private (O12). A layout declares the name, so every federation of it has the same one; the id is the federation's
    (`up` makes it anew) and is not here. The `[[spaces]]` form holds all of this in each entry's digest."""
    entry: StudySpace = config.spaces[0]
    supporting = [name for name in config.providers if name in entry.support.providers]
    return {
        "providers": [config.provider_label(name) for name in supporting],
        "local": config.provider_label(entry.support.local),
        "name_sha256": hashlib.sha256(entry.space.name.encode("utf-8")).hexdigest(),
        "prefixes_sha256": digest_of(sorted(entry.space.prefixes)),
    }


def sandbox_record(sandbox: SandboxSettings) -> dict[str, Any]:
    """`[sandbox]`'s settings that shape a run beside its data paths, served caps and Apptainer pins (which keep their
    own keys): the backend; on Docker every image by its pinned id (on Apptainer the SIF is in its pins); every limit;
    the mount's path in the agent, its I/O mode and oneclient's extra arguments (by their SHA-256); the paths in the
    agent's image that trust the run's CA; the agent's user and interpreter; the kill's grace at a cell's deadline; and
    the agent's placeholder token by its SHA-256. Not here: the work root, the name prefix, the label namespace, the
    privilege command and earlyoom's name for the driver (the host's own), the network pools and `[sandbox.federation]`
    (the host's and the federation's addresses and names), the mount's storages and check file (the federation's
    storage ids and the host's paths), and `content_check_file`, read once before a campaign."""
    from expcore_onedata.sandbox.settings import Limits  # it imports the sandbox's modules: import when used

    mount = sandbox.mount
    images = {  # the Apptainer backend's one image is its SIF, pinned in `sandbox_backend` (O56)
        "images": {
            "agent": sandbox.agent.id,
            "gate": sandbox.gate.id,
            "oneclient": None if sandbox.oneclient is None else sandbox.oneclient.id,
        }
    }
    return {
        "backend": sandbox.backend,
        **(images if sandbox.apptainer is None else {}),
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
    longest its start may take, and each condition, keyed by its name's SHA-256 (a message never names a condition,
    O12, and the key still binds the name to its settings whatever the file's order), with its allow-list, its rights
    and its frozen manifest's SHA-256. Not here: the command, its arguments and the clone's path, which are the
    host's (a path, and in the arguments often the federation's address); the code they start is the commit's, and
    what it serves is the manifest, which the client holds the server to (L13). A manifest that cannot be read is
    refused naming its condition's position, never its path."""
    conditions: dict[str, Any] = {}
    for index, (name, condition) in enumerate(server.conditions.items()):
        try:
            manifest = hashlib.sha256(condition.manifest.read_bytes()).hexdigest()
        except OSError as error:
            raise ConfigError(
                f"{file}: [toolserver.conditions]: conditions[{index}]: key 'manifest' cannot be read "
                f"({type(error).__name__})"
            ) from None
        conditions[hashlib.sha256(name.encode("utf-8")).hexdigest()] = {
            "allow": sorted(condition.allow),
            "rights": sorted(condition.rights),
            "manifest_sha256": manifest,
        }
    return {
        "commit": server.commit,
        "start_timeout_s": float(server.start_timeout_s),
        "conditions": conditions,
    }


# `[placement]`'s bounds that a run's own instruments read inside its attempt, by their type: the platform probes and
# their transfer log, whose snapshot is the run's record (`probes.py`, `transferlog.py`: the run's window's allowance,
# the listing's settle, a probe's retries), and progressive ingest, which writes and confirms the parts that arrive
# during the run (`ingest.py`, `ingestbase.py`: its polls, waits, chunks and connections). They decide what the record
# can show and when a part is confirmed, or the run is set aside.
PLACEMENT_IN_RUN = {
    "clock_skew_allowance_s": float,
    "transfer_settle_s": float,
    "transfer_settle_polls": int,
    "probe_attempts": int,
    "probe_backoff_s": float,
    "poll_s": float,
    "settle_timeout_s": float,
    "consecutive_polls": int,
    "transfer_timeout_s": float,
    "request_timeout_s": float,
    "chunk_bytes": int,
    "fresh_connection_bytes": int,
}


def placement_record(config: PlatformConfig) -> dict[str, Any] | None:
    """`[placement]`'s settings that shape a run, or None without the table: the data provider by its label and the
    declared range map by its digest (`RangeMap.digest`), which say where the bytes a run reads lie, and every bound a
    run's instruments read inside its attempt (`PLACEMENT_IN_RUN`), after its default. Its concurrency, its QoS scan
    and its fulfilment wait, and the reset's concurrency, shortcut and wait for the reset lock, decide only how a reset
    reaches a state it then verifies, or how many requests are in flight, and change no reading's result: they are
    recorded and compared at every resume (`Placement.reset_settings`, C35), never here."""
    if "placement" not in load_toml(config.file):
        return None
    from expcore_onedata.rangemap import load_placement  # it reads this module's config: import when used

    placement = load_placement(config.file, config)
    bounds = {key: kind(getattr(placement.settings, key)) for key, kind in PLACEMENT_IN_RUN.items()}
    return {
        "holder": config.provider_label(placement.settings.holder),
        "map_sha256": placement.map.digest(),
        **bounds,
    }


__all__ = [
    "PLACEMENT_IN_RUN",
    "digest_of",
    "placement_record",
    "sandbox_record",
    "support_record",
    "tokens_record",
    "toolserver_record",
]

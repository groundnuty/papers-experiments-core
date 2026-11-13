# Provenance: new
"""What a study may pass to the sidecar's oneclient, and how a run's metadata names it (O36, O37).

The core declares everything oneclient would otherwise decide at run time: the I/O mode (`io_mode`), the TLS check (the
platform's `[tls]`), the configuration file (never read: `--config /dev/null`), the environment (`env -u` of the
three variables of the image and of oneclient's own that could set an option) and, for direct I/O, where each storage
is (one `--override` per entry of `[sandbox.mount.storages]`) and the log's buffering. A study's `extra_args` reach the
same argv, so none of them may set what the core sets. `mount_problem` also holds a mount's direct-I/O keys to its mode:
a direct mount needs its storage mapping and its check file, a proxy mount may have neither (O37). `extra_arg_problem`
names the reason an argument is refused, and the same check runs where the settings are built (`MountSettings`), where
the platform file is loaded and where the command is made, so settings built in code cannot give oneclient two
force-I/O options or a second configuration.

`LONG_OPTIONS` and `SHORT_OPTIONS` are the option names oneclient 25.0 (tag `bb8a63e3`) accepts on its command line,
read from `src/options/options.cc`. A run's metadata keeps an extra argument's name only when it is one of them
(`option_name`): any other word that starts with a dash may be a value (a path, an identifier) and is never kept (O12).
"""

from __future__ import annotations

import itertools
import posixpath
import re
from collections.abc import Sequence

IO_MODE_FLAGS = {
    "direct": "--force-direct-io",
    "proxy": "--force-proxy-io",
}  # oneclient's own option for each mode (O36)
IO_MODES = tuple(IO_MODE_FLAGS)
DIRECT = "direct"
CONFIG_OPTION = "--config"
CONFIG_NULL = "/dev/null"  # an empty file: boost's parser reads no option from it, so no configuration file is parsed
INSECURE_OPTION = "--insecure"
OVERRIDE_OPTION = "--override"  # `-r`: one helper parameter of one storage, `<storage id>:<name>:<value>` (O37)
# With direct I/O the sidecar passes it, so glog writes each INFO line as it is logged and the start check sees every
# line its test read caused (`logging.cc`: FLAGS_logbuflevel -1; otherwise INFO waits up to 30 s in a buffer, O37)
DISABLE_LOG_BUFFERING = "--disable-log-buffering"
UNSET_ENV = ("ONECLIENT_INSECURE", "ONECLIENT_OVERRIDE", "ONECLIENT_CONFIG")  # removed from the sidecar's environment

# The long options of the oneclient binary (`options.cc`, `withLongName`), without OneS3's, which it does not accept.
LONG_OPTION_NAMES = """
help version unmount config ignore-env host port insecure token space space-id log-dir custom-ca-dir
io-trace-log message-trace-log log-read-write-perf force-proxy-io force-direct-io
buffer-scheduler-thread-count communicator-pool-size communicator-thread-count scheduler-thread-count
storage-helper-thread-count no-buffer no-xattr provider-timeout storage-timeout disable-read-events
no-fullblock-read read-buffer-min-size read-buffer-max-size read-buffer-prefetch-duration
write-buffer-min-size write-buffer-max-size read-buffers-total-size write-buffers-total-size
write-buffer-flush-delay min-block-prefetch-size seqrd-prefetch-threshold rndrd-prefetch-threshold
rndrd-prefetch-eval-frequency rndrd-prefetch-block-threshold rndrd-prefetch-cluster-window
rndrd-prefetch-cluster-block-threshold rndrd-prefetch-cluster-window-grow-factor prefetch-mode
cluster-prefetch-threshold-random metadata-cache-size readdir-prefetch-size dir-cache-drop-after
tag-on-create tag-on-modify override emulate-available-space foreground debug verbose-log-level
disable-log-buffering single-thread opt only-full-replicas hard-link-count enable-archivematica
public-data-mode show-space-ids monitoring-type monitoring-level-basic monitoring-level-full
monitoring-period graphite-url graphite-namespace-prefix authentication no_check_certificate
force-fullblock-read
"""
LONG_OPTIONS = frozenset(LONG_OPTION_NAMES.split())
SHORT_SWITCHES = frozenset("hVuifds")  # a short option that takes no value: more letters may follow it
SHORT_VALUED = frozenset("cHPtlrvo")  # one that takes a value: the rest of the word, or the next word, is its value
SHORT_OPTIONS = SHORT_SWITCHES | SHORT_VALUED  # `-Z` (the Onezone host) is OneS3's

UNKNOWN = "<value>"  # what the metadata keeps of a word that starts with a dash and is not an option of oneclient

TLS_REFUSAL = "must not carry the TLS flag: the platform's [tls] alone sets it"
IO_MODE_ARGS_REFUSAL = "must not carry a force-I/O option: the table's io_mode alone sets it"
CONFIG_REFUSAL = (
    "must not carry the config-file option: the sidecar always passes --config /dev/null, so oneclient reads no "
    "configuration file"
)
ENV_REFUSAL = (
    "must not carry an environment setting of oneclient (ONECLIENT_...): its environment is closed, except for the "
    "token the sidecar hands it"
)
OVERRIDE_REFUSAL = (
    "must not carry a storage override (--override, -r): it changes where oneclient reads a storage, which "
    "[sandbox.mount.storages] alone declares, for direct I/O"
)
LOG_BUFFERING_REFUSAL = (
    "must not carry --disable-log-buffering with direct I/O: the sidecar passes it, and oneclient refuses a switch "
    "given twice"
)
ENV_SETTING = re.compile(r"ONECLIENT_[A-Za-z0-9_]*=", re.IGNORECASE)
# A storage id as oneclient takes it in an override: it splits the value at every ':' (`options.h`,
# `getOverrideParams`), so an id has none, and none of the characters a docker or a shell argument would read
STORAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
HOST_PATH_UNSAFE = re.compile(r'[,"\x00-\x1f\x7f]')  # in a `--mount` value a comma ends the field, a quote opens one
SPACE_PART = re.compile(r"[^/\x00-\x1f\\]{1,255}")  # one name of a path under the mount


def abbreviates(argument: str, option: str) -> bool:
    """Whether a long option `argument` (with or without `=value`) is `option` or a prefix of it, which boost's parser
    takes for it when it is unique among oneclient's options. Every such prefix of more than the dashes is counted: a
    prefix that is ambiguous is an error in oneclient anyway."""
    name = argument.split("=", 1)[0]
    return len(name) > 2 and name.startswith("--") and option.startswith(name)


def short_letters(argument: str) -> str:
    """The short options a single-dash argument sets, as boost reads it: each letter that is a switch, then the first
    one that takes a value, whose value is the rest of the word (`-fc/x` sets `f` and `c`; `-l/path/ci` sets only
    `l`). It stops at a letter oneclient does not have, which it refuses anyway. A long option or a word with no
    dash sets none."""
    if not argument.startswith("-") or argument.startswith("--"):
        return ""
    letters = ""
    for letter in argument[1:]:
        if letter not in SHORT_OPTIONS:
            break
        letters += letter
        if letter in SHORT_VALUED:
            break
    return letters


def extra_arg_problem(argument: str) -> str | None:
    """Why a study's extra argument may not reach oneclient, or None when it may: it sets the TLS check, the I/O mode,
    the configuration file or oneclient's environment, which the core declares. The reason never holds the argument."""
    if abbreviates(argument, INSECURE_OPTION) or "i" in short_letters(argument):
        return TLS_REFUSAL
    if any(abbreviates(argument, flag) for flag in IO_MODE_FLAGS.values()):
        return IO_MODE_ARGS_REFUSAL
    if abbreviates(argument, CONFIG_OPTION) or "c" in short_letters(argument):
        return CONFIG_REFUSAL
    if abbreviates(argument, OVERRIDE_OPTION) or "r" in short_letters(argument):
        return OVERRIDE_REFUSAL
    if ENV_SETTING.match(argument):
        return ENV_REFUSAL
    return None


def mode_arg_problem(io_mode: object, argument: str) -> str | None:
    """Why an extra argument may not reach oneclient in this I/O mode, or None: `extra_arg_problem`'s reasons, and with
    direct I/O the log-buffering switch, which the sidecar then passes itself."""
    problem = extra_arg_problem(argument)
    if problem is None and io_mode == DIRECT and abbreviates(argument, DISABLE_LOG_BUFFERING):
        return LOG_BUFFERING_REFUSAL
    return problem


DIRECT_NEEDS = (
    'is "direct", which reads the local provider\'s storage itself, so the mount needs [sandbox.mount.storages], each '
    "storage the sidecar reads by its id and the directory that holds it on the host, and direct_check_file, a file "
    'that provider holds in full, which the start reads to show direct I/O in effect; or declare "proxy", which reads '
    "every space through the provider and needs neither"
)
PROXY_REFUSES = (
    'is read with io_mode = "direct" alone: a "proxy" mount reads every space through the provider and never a '
    "storage, so it would do nothing"
)
STORAGE_ID_RULE = (
    "the storage id must be letters, digits, '.', '_' and '-', at most 255, starting with a letter or a digit "
    "(oneclient splits an override at every ':')"
)
HOST_PATH_RULE = (
    "must be the directory that holds the storage on the host: one absolute, normalised path, not the root, without a "
    "comma, a quote or a control character (it is bound with --mount)"
)
CHECK_FILE_RULE = (
    "must be a space's directory under the mount, then the path of a file in that space: plain names joined by '/', at "
    "least two, no '.' or '..'"
)


def host_path_problem(path: object) -> bool:
    """Whether a host path of a storage is not one plain absolute directory path `--mount` can carry."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or path == "/":
        return True
    return posixpath.normpath(path) != path or HOST_PATH_UNSAFE.search(path) is not None


def nested(one: str, other: str) -> bool:
    """Whether one directory path is, holds or lies under the other."""
    return one == other or one.startswith(other.rstrip("/") + "/") or other.startswith(one.rstrip("/") + "/")


def storages_problem(storages: Sequence[tuple[object, object]]) -> str | None:
    """Why a storage mapping, in its declared order, cannot be given to the sidecar, or None. An entry is named by its
    position, never by its id or its path (O12): every one is a configured value."""
    for position, (storage_id, path) in enumerate(storages):
        if not isinstance(storage_id, str) or not STORAGE_ID.fullmatch(storage_id):
            return f"entry {position}: {STORAGE_ID_RULE}"
        if host_path_problem(path):
            return f"entry {position}: {HOST_PATH_RULE}"
    for (i, (_, a)), (j, (_, b)) in itertools.combinations(enumerate(storages), 2):
        if nested(str(a), str(b)):
            return f"entries {i} and {j}: one storage's directory is, holds or lies under the other's"
    return None


def check_file_problem(value: object) -> bool:
    """Whether the direct check file is not a space's directory and a path in it, under the mount."""
    parts = value.split("/") if isinstance(value, str) else []
    return len(parts) < 2 or any(part in (".", "..") or not SPACE_PART.fullmatch(part) for part in parts)


def direct_keys_problem(
    io_mode: object, storages: Sequence[tuple[object, object]] | None, check_file: object
) -> tuple[str, str] | None:
    """The key and the reason of a mount whose direct-I/O keys do not fit its mode, or None (O37). `storages` is None
    when the mount declares no mapping; `check_file` is None when it declares no check file. With `"direct"` both are
    required and must be well formed; with `"proxy"` neither may be there."""
    if io_mode != DIRECT:
        if storages is not None:
            return "storages", PROXY_REFUSES
        return ("direct_check_file", PROXY_REFUSES) if check_file is not None else None
    if storages is None:
        return "io_mode", DIRECT_NEEDS
    if not storages:
        return "storages", "must map at least one storage, the local provider's that the sidecar reads"
    problem = storages_problem(storages)
    if problem is not None:
        return "storages", problem
    if check_file is None:
        return "direct_check_file", "is required with direct I/O: the start reads it to show direct I/O in effect"
    return ("direct_check_file", CHECK_FILE_RULE) if check_file_problem(check_file) else None


def mount_problem(
    io_mode: object,
    extra_args: Sequence[str],
    storages: Sequence[tuple[object, object]] = (),
    check_file: object = None,
) -> str | None:
    """Why a mount's I/O mode, extra arguments and direct-I/O keys cannot be given to oneclient, or None: the mode is
    one of `IO_MODES`, no extra argument sets what the core declares, and a direct mount maps its storages and names
    its check file, which a proxy mount does not (O36, O37). An empty `storages` is no mapping."""
    if io_mode not in IO_MODES:
        return f"[sandbox.mount] io_mode must be one of: {', '.join(IO_MODES)}"
    for argument in extra_args:
        problem = mode_arg_problem(io_mode, argument)
        if problem is not None:
            return f"[sandbox.mount] extra_args {problem}"
    found = direct_keys_problem(io_mode, tuple(storages) or None, check_file)
    return None if found is None else f"[sandbox.mount] {found[0]} {found[1]}"


def option_name(argument: str) -> str | None:
    """The name of the option a word that starts with a dash is, for a record: `--name` of a known long option (its
    `=value` dropped), `-x` of a known short one (an attached value dropped), `<value>` for a dash-word that is neither,
    and None for a word with no dash, which is a value."""
    if not argument.startswith("-"):
        return None
    if argument.startswith("--"):
        name = argument.split("=", 1)[0]
        return name if name[2:] in LONG_OPTIONS else UNKNOWN
    return argument[:2] if argument[1:2] in SHORT_OPTIONS else UNKNOWN

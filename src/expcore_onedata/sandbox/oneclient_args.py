# Provenance: new
"""What a study may pass to the sidecar's oneclient, and how a run's metadata names it (O36).

The core declares everything oneclient would otherwise decide at run time: the I/O mode (`io_mode`), the TLS check (the
platform's `[tls]`), the configuration file (never read: `--config /dev/null`), the environment (`env -u` of the
three variables of the image and of oneclient's own that could set an option) and the log directory (`--log-dir`, where
the run's own directory is bound so the log is part of its evidence, O05). A study's `extra_args` reach the same argv,
so none of them may set what the core sets. `extra_arg_problem` is the one check of them: it names the reason an
argument is refused, and it runs where the settings are built (`MountSettings`), where the platform file is loaded and
where the command is made, so settings built in code cannot give oneclient two force-I/O options, a second
configuration or a second log directory.

`LONG_OPTIONS` and `SHORT_OPTIONS` are the option names oneclient 25.0 (tag `bb8a63e3`) accepts on its command line,
read from `src/options/options.cc`. A run's metadata keeps an extra argument's name only when it is one of them
(`option_name`): any other word that starts with a dash may be a value (a path, an identifier) and is never kept (O12).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

IO_MODE_FLAGS = {
    "direct": "--force-direct-io",
    "proxy": "--force-proxy-io",
}  # oneclient's own option for each mode (O36)
IO_MODES = tuple(IO_MODE_FLAGS)
CONFIG_OPTION = "--config"
CONFIG_NULL = "/dev/null"  # an empty file: boost's parser reads no option from it, so no configuration file is parsed
INSECURE_OPTION = "--insecure"
LOG_DIR_OPTION = "--log-dir"  # the sandbox's own (`containers.sidecar_command`); oneclient takes it once
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
ENV_SETTING = re.compile(r"ONECLIENT_[A-Za-z0-9_]*=", re.IGNORECASE)


def log_dir_refusal(option: str) -> str:
    """Why `[sandbox.mount] extra_args` may not carry `option`, the log directory's, as written without its value."""
    return (
        f"must not carry oneclient's log directory ({option}): the sandbox sets it, and binds the run's own directory "
        "there so the log is part of the run's evidence"
    )


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
    the configuration file, the log directory or oneclient's environment, which the core declares. The reason never
    holds a value: the log directory's names the option as written (`-l`, `--log-d`), its value dropped.

    oneclient reads its command line with boost::program_options in its default style, so the log directory has more
    forms than `-l` and `--log-dir`: an attached value (`-l/x`), switches clustered before it (`-fl /x`, which
    `short_letters` reads) and any prefix of the long name (`--log-d`; `--l`, `--lo`, `--log` and `--log-` also start
    `--log-read-write-perf`, which oneclient refuses as ambiguous, and are refused here all the same)."""
    if abbreviates(argument, INSECURE_OPTION) or "i" in short_letters(argument):
        return TLS_REFUSAL
    if any(abbreviates(argument, flag) for flag in IO_MODE_FLAGS.values()):
        return IO_MODE_ARGS_REFUSAL
    if abbreviates(argument, CONFIG_OPTION) or "c" in short_letters(argument):
        return CONFIG_REFUSAL
    if abbreviates(argument, LOG_DIR_OPTION):
        return log_dir_refusal(argument.split("=", 1)[0])
    if "l" in short_letters(argument):
        return log_dir_refusal("-l")
    if ENV_SETTING.match(argument):
        return ENV_REFUSAL
    return None


def mount_problem(io_mode: object, extra_args: Sequence[str]) -> str | None:
    """Why a mount's I/O mode and extra arguments cannot be given to oneclient, or None: the mode is one of `IO_MODES`
    and no extra argument sets what the core declares."""
    if io_mode not in IO_MODES:
        return f"[sandbox.mount] io_mode must be one of: {', '.join(IO_MODES)}"
    for argument in extra_args:
        problem = extra_arg_problem(argument)
        if problem is not None:
            return f"[sandbox.mount] extra_args {problem}"
    return None


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

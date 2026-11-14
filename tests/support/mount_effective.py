"""The live check that the sidecar's oneclient runs with its argv and the token, and nothing else (O36).

Two reads from the running sidecar, as root, through the host's docker: oneclient's own INFO log, whose start lines name
the options it runs with (`oneclient_start`), and the names, never the values, of the variables in its environment. The
sidecar's start is `sh -c SIDECAR_START`, which becomes `env`, which becomes oneclient, so oneclient is the container's
first process and `/proc/1/environ` is the environment it was started with. Only names leave this module: the
token's value is never read, and a value of a start line (the provider's host, the mount point) is kept out of the
facts the live tests write.
"""

from collections.abc import Iterable
from typing import Any

from expcore_onedata.sandbox import oneclient_start as start
from expcore_onedata.sandbox.containers import ENV_ACCESS_TOKEN, SIDECAR_LOG_DIR, sidecar_options, sidecar_route
from expcore_onedata.sandbox.hostops import HostOps
from expcore_onedata.sandbox.oneclient_args import UNSET_ENV
from expcore_onedata.sandbox.settings import SandboxSettings
from expcore_onedata.sandbox.teardown import SIDECAR_MOUNT

ENV_PREFIX = "ONECLIENT_"
# The variables oneclient 25.0 reads an option from (`withEnvName` in `src/options/options.cc`): with or without the
# prefix, in any case (`optionsParser.cc`, `parseEnvironment`). The OneS3 ones are not the oneclient binary's.
ENV_OPTION_NAMES = """
access_token authorization_token config custom_ca_dir disable_log_buffering force_fullblock_read fuse_debug
fuse_foreground fuse_single_thread graphite_namespace_prefix graphite_url insecure log_dir
monitoring_level_basic monitoring_level_full monitoring_period monitoring_type mountpoint
no_check_certificate onezone_host override provider_host provider_hostname provider_port space space_id
tag_on_create tag_on_modify verbose_log_level
"""
ENV_OPTIONS = frozenset(ENV_OPTION_NAMES.split())
NAMES_OF_THE_ENVIRONMENT = "tr '\\0' '\\n' < /proc/1/environ | cut -d= -f1"  # names only: the cut drops every value


def option_env_names(names: Iterable[str]) -> list[str]:
    """The names, of a process's environment, that oneclient would read an option from, sorted."""
    out = set()
    for name in names:
        bare = name.upper().removeprefix(ENV_PREFIX).lower()
        if bare in ENV_OPTIONS:
            out.add(name)
    return sorted(out)


def _unquoted(value: str) -> str:
    """A value without the one pair of double quotes that surrounds it, if it has one."""
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def logged_settings(log_text: str) -> dict[str, str]:
    """The start lines of oneclient's INFO log by name (`oneclient_start.start_settings`), with a quoted value read as
    what it quotes: oneclient 25.0 logs the mountpoint as `Mountpoint: "/mnt/oneclient"` while the argv gives it bare,
    which made every start line compare as a difference on the 14 Nov slot, the mountpoint's alone."""
    return {name: _unquoted(value) for name, value in start.start_settings(log_text).items()}


def effective_facts(host: HostOps, sidecar: str, settings: SandboxSettings, provider_host: str) -> dict[str, Any]:
    """What the running sidecar's oneclient says it runs with against what the sandbox's argv sets, and which variables
    of its environment it reads an option from. Words and counts only: the names of the lines that differ, never their
    values. `option_env_names` is expected to be the token's variable alone, `mismatched` empty."""
    facts: dict[str, Any] = {"log_read": False, "env_read": False}
    expected = start.expected_settings(
        sidecar_options(settings), sidecar_route(settings, provider_host)[1], SIDECAR_MOUNT, settings.mount.extra_args
    )
    log = host.docker("exec", "-u", "0", sidecar, "cat", f"{SIDECAR_LOG_DIR}/oneclient.INFO", check=False, timeout=60)
    if log.returncode == 0:
        found = logged_settings(log.stdout)
        problems = start.differences(found, expected)
        names = [problem.split(":", 1)[0] for problem in problems]
        private = {start.PROVIDER, start.MOUNTPOINT}  # a host and a path: their lines say that they differ, not how
        facts.update(log_read=True, logged=len(found), expected=len(expected), mismatched=names)
        facts["problems"] = [p if n not in private else f"{n}: differs" for p, n in zip(problems, names, strict=True)]
    env = host.docker("exec", "-u", "0", sidecar, "sh", "-c", NAMES_OF_THE_ENVIRONMENT, check=False, timeout=60)
    if env.returncode == 0:
        variables = env.stdout.split()
        facts.update(env_read=True, option_env_names=option_env_names(variables))
        facts["unset_present"] = sorted(set(UNSET_ENV) & set(variables))
        facts["token_variable_present"] = ENV_ACCESS_TOKEN in variables
    return facts

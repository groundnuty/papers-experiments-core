# Provenance: new
"""The effective options of the sidecar's oneclient, as it logs them at its start, against what the argv set (O36).

oneclient 25.0 writes the options it runs with as INFO lines of `logging.cc` before it serves a request (`startLogging`,
`src/logging.cc`): the force-I/O switches, the certificate check, the provider it connects to, the buffering, the
monitoring and the mountpoint. `start_settings` reads them from the log; `expected_settings` gives what they must
say when the sidecar's argv is all that set an option and the token the only input besides it, which is what
`sidecar_command` arranges (`--config /dev/null`, `env -u` of the variables that could set another option, O36). A
line that differs shows an input the core did not close: the image's environment, a configuration file, or an option
it did not declare.

Only the options a start line shows are compared; an option oneclient does not log at its start (a buffer size, a thread
count) is not covered. The expected values for an option the argv does not set are oneclient's own defaults
(`options.h`, `options.cc`): they hold for a mount without `extra_args`, and `expected_settings` refuses to guess
for one with them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

START_LINE = re.compile(r"^I\d{4} \d\d:\d\d:\d\d\.\d+\s+\d+ logging\.cc:\d+\] ([^:]+): (.*)$")
# oneclient's defaults for the options that its start lines show and the argv does not set (`options.h`, `options.cc`)
DEFAULT_PROVIDER_TIMEOUT_S = "120"
PROVIDER = "Connecting to Oneprovider"
MOUNTPOINT = "Mountpoint"
FORCED_DIRECT = "Forced direct IO"
FORCED_PROXY = "Forced proxy IO"
VERIFY = "Verify server certificate"
DEFAULTS = {
    "Verbose logging level": "0",
    "File read events disabled": "0",
    "IO buffered": "1",
    "Oneprovider connection timeout [s]": DEFAULT_PROVIDER_TIMEOUT_S,
    "Monitoring enabled": "0",
    "Read/write perf log enabled": "0",
}


def start_settings(log_text: str) -> dict[str, str]:
    """The `name: value` start lines of oneclient's INFO log (`logging.cc`), by name; the first of a name wins."""
    found: dict[str, str] = {}
    for line in log_text.splitlines():
        parsed = START_LINE.match(line)
        if parsed and parsed.group(1) not in found:
            found[parsed.group(1)] = parsed.group(2).strip()
    return found


def expected_settings(
    options: Sequence[str], provider_host: str, mountpoint: str, extra_args: Sequence[str] = ()
) -> dict[str, str]:
    """What the start lines must say when the argv is `options` (the sandbox's own, `sidecar_options`) and nothing else
    sets an option: `provider_host` is the host the sidecar dials (`sidecar_route`) and `mountpoint` the sidecar's mount
    point. A study's `extra_args` may set any other option, so with them there is nothing to expect: `ValueError`."""
    if extra_args:
        raise ValueError("a mount with extra arguments has options this check does not model")
    return {
        PROVIDER: provider_host,
        FORCED_DIRECT: "1" if "--force-direct-io" in options else "0",
        FORCED_PROXY: "1" if "--force-proxy-io" in options else "0",
        VERIFY: "0" if "--insecure" in options else "1",
        MOUNTPOINT: mountpoint,
        **DEFAULTS,
    }


def differences(found: Mapping[str, str], expected: Mapping[str, str]) -> list[str]:
    """Each expected line the log does not show as expected, by its name: missing, or the value it holds against the
    expected one. Lines the log has beyond the expected ones are not differences (they are not options the argv
    sets)."""
    out = []
    for name, value in expected.items():
        if name not in found:
            out.append(f"{name}: not logged, expected {value!r}")
        elif found[name] != value:
            out.append(f"{name}: logged {found[name]!r}, expected {value!r}")
    return out

# Provenance: new
"""The start-time check that a direct mount reads the local provider's storage itself (O37).

A sandbox whose `[sandbox.mount] io_mode` is `"direct"` is handed out only once this check has shown direct I/O in
effect; otherwise its start is refused, so a direct run never reads through the provider unnoticed. It has two inputs.

**The probe** (`probe_source`), run in the agent like the read-only probes, on `direct_check_file`: the file's size,
its storage (`org.onedata.storage_id`) and how much of it the local provider holds (`org.onedata.replication_progress`,
a whole percentage, floored: `fsLogic.cc`, `getxattr`, lines 2063-2075), then, only when that is `100%`, a read of the
whole file within `READ_DEADLINE_S` by a child process it kills at the deadline. A file the local provider does not
hold in full is never read: a read of a block it lacks would fetch the block and place it there, the change a study
measures. The deadline matters because the POSIX helper reads an override's path without checking it: a wrong host
directory gives ENOENT, which oneclient retries with delays before the application sees it.

**The sidecar's log** (`LOG_FILE` in oneclient's log directory), read after the probe. A read that returned every byte
is not evidence on its own: oneclient started with `--force-direct-io` still falls back to the provider for a file
whose direct read fails with EPERM or EACCES (`fsLogic.cc`, `readInternal`, lines 1176-1203: it logs the ERROR line,
puts the file in its force-proxy cache and reads it again through `performAutoIOStorageDetection` with proxy forced).
The storage's access type is no evidence either: forced detection marks it DIRECT before it has tried anything
(`helpersCache.h`, `performForcedDirectIOStorageDetection`, lines 626-629). So the check requires the start lines to
say `Forced direct IO: 1` and `Forced proxy IO: 0`, and refuses on any line of `MARKERS`, each a line oneclient
25.0 (tag `bb8a63e3`) logs at the default verbosity when a read is not a direct one. With direct I/O the sidecar
passes `--disable-log-buffering` (`containers.sidecar_options`), so an INFO line is in the file once it is logged;
without it glog holds INFO lines up to 30 s.

`judge` gives the problems, each in words with no id, path or name, and the record the run's metadata keeps
(`mount.direct.check`): the start lines' values, the file's size and the bytes read, the seconds, the share held, the
check file's storage by its position in `[sandbox.mount.storages]`, and the names of the marker lines found.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from expcore_onedata.sandbox import oneclient_start as start

LOG_FILE = "oneclient.INFO"  # glog's link to the current INFO log, which holds every severity
READ_DEADLINE_S = 60.0  # the probe's whole read of the check file
PROBE_MARK = "expcore-direct-io-probe"  # in the probe's source, so a stand-in host can tell it from the other cells
HELD_IN_FULL = "100%"
# Lines that show a read that is not a direct one, with where oneclient 25.0 (bb8a63e3) logs each at verbosity 0
MARKERS: Mapping[str, re.Pattern[str]] = {
    # helpersCache.h:467-470: detection, which forced direct never runs; it does for a file that fell back
    "automatic_detection": re.compile(r"Performing automatic storage access type detection"),
    # fsLogic.cc:1184-1185: a direct read refused with EPERM or EACCES, read again through the provider
    "proxy_fallback": re.compile(r"failed due to insufficient permissions"),
    # helpersCache.h:497-500: automatic detection gave the provider's helper
    "switched_to_proxy": re.compile(r"switching to proxy mode"),
    # helpersCache.h:644-648: the provider gives no direct helper for the storage
    "not_direct_on_provider": re.compile(r"is not accessible in direct IO mode"),
    # helpersCache.h:666-675: a POSIX storage without an override, found by scanning the sidecar's mounts
    "storage_not_mapped": re.compile(r"attempting storage mountpoint detection"),
    # storageAccessManager.cc:54-60: an override's path under no mount of the sidecar
    "mount_point_not_mounted": re.compile(r"is not one of mounts available in the system"),
    # fuseFileHandle.cc:69-71: no helper for the file's storage
    "no_storage_helper": re.compile(r"Could not create storage helper"),
}

PROBE = """
# {mark}
import errno, json, os, signal, sys, time
path, deadline = {path!r}, {deadline_s!r}
out = {{"read": None}}
def named(error):
    return errno.errorcode.get(error.errno, str(error.errno))
def attribute(name):
    return os.getxattr(path, "org.onedata." + name).decode().strip().strip('"')
try:
    out["size"] = os.stat(path).st_size
    out["storage_id"] = attribute("storage_id")
    out["held"] = attribute("replication_progress")
except OSError as error:
    out["error"] = named(error)
    print(json.dumps(out))
    sys.exit(0)
if out["held"] != {held!r} or out["size"] == 0:
    print(json.dumps(out))
    sys.exit(0)
reader, writer = os.pipe()
child = os.fork()
if child == 0:
    os.close(reader)
    try:
        count = 0
        with open(path, "rb", buffering=0) as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                count += len(chunk)
        said = {{"read": count}}
    except OSError as error:
        said = {{"read_error": named(error)}}
    os.write(writer, json.dumps(said).encode())
    os._exit(0)
os.close(writer)
began = time.monotonic()
while os.waitpid(child, os.WNOHANG) == (0, 0):
    if time.monotonic() - began > deadline:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        out["blocked"] = True
        break
    time.sleep(0.02)
out["seconds"] = round(time.monotonic() - began, 3)
said = os.read(reader, 4096)
if said:
    out.update(json.loads(said))
print(json.dumps(out))
"""


def probe_source(path: str, deadline_s: float = READ_DEADLINE_S) -> str:
    """The Python source the agent runs on the check file at `path` (its place under the agent's mount): one line of
    JSON with `size`, `storage_id`, `held`, and when it read, `read` (bytes), `seconds`, and `read_error` or
    `blocked`; `error` names a failure to stat the file or read its attributes. Never a byte of the file."""
    return PROBE.format(mark=PROBE_MARK, path=path, deadline_s=float(deadline_s), held=HELD_IN_FULL)


def parse_probe(stdout: str) -> dict[str, Any]:
    """The probe's line of JSON, or an empty answer when it printed none."""
    try:
        found = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return found if isinstance(found, dict) else {}


def marker_lines(log_text: str) -> list[str]:
    """The names of the `MARKERS` that some line of the log matches, in `MARKERS`' order."""
    return [name for name, pattern in MARKERS.items() if pattern.search(log_text)]


def _probe_problems(probe: Mapping[str, Any], storage: int | None) -> list[str]:
    """What the probe shows wrong with the check file's read, in words with no id or path."""
    if not probe:
        return ["the probe of the check file gave no answer"]
    if "error" in probe:
        return [f"the check file could not be examined through the mount ({probe['error']})"]
    problems = []
    if storage is None:
        problems.append("the check file's storage is not one of [sandbox.mount.storages]")
    size, read = probe.get("size"), probe.get("read")
    if size == 0:
        problems.append("the check file is empty: a read of it proves nothing")
    elif probe.get("held") != HELD_IN_FULL:
        held = probe.get("held") if isinstance(probe.get("held"), str) and len(probe["held"]) <= 4 else "?"
        problems.append(f"the local provider holds {held} of the check file, not all of it: it was not read")
    elif probe.get("blocked"):
        problems.append(f"the read of the check file did not return within {READ_DEADLINE_S:g} s")
    elif "read_error" in probe:
        problems.append(f"the read of the check file failed ({probe['read_error']})")
    elif read != size:
        problems.append(f"the read of the check file read {read} of {size} bytes")
    return problems


def judge(probe: Mapping[str, Any], log_text: str, storage_ids: Sequence[str]) -> tuple[list[str], dict[str, Any]]:
    """The problems that show direct I/O not in effect (none when it is) and the check's record (module docstring).
    `storage_ids` are the mapped storages' ids in the file's order; the record names the check file's by position."""
    found = start.start_settings(log_text)
    forced_direct, forced_proxy = found.get(start.FORCED_DIRECT), found.get(start.FORCED_PROXY)
    storage_id = probe.get("storage_id")
    storage = storage_ids.index(storage_id) if storage_id in storage_ids else None
    lines = marker_lines(log_text)
    problems = []
    if forced_direct != "1":
        problems.append(f"{start.FORCED_DIRECT}: logged {forced_direct!r}, expected '1'")
    if forced_proxy != "0":
        problems.append(f"{start.FORCED_PROXY}: logged {forced_proxy!r}, expected '0'")
    problems += _probe_problems(probe, storage)
    problems += [f"the sidecar's oneclient logged a line of {name}" for name in lines]
    record = {
        "forced_direct": forced_direct,
        "forced_proxy": forced_proxy,
        "size": probe.get("size"),
        "read": probe.get("read"),
        "seconds": probe.get("seconds"),
        "held": probe.get("held"),
        "storage": storage,
        "lines": lines,
    }
    return problems, record

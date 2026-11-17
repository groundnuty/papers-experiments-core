"""A stand-in for the shard manifest's Python, for the tests that run `shards.py plan` and `lines`, and `fleet.py plan`.

Those commands compute each shard's campaign fingerprint (O63: `shardresume.study_fingerprint`) by running the
manifest's Python on a copy of the study with its federation's `platform.toml` beside it, as `python -I -B -c CODE
COPY`. The example study's plugin has no platform half, so its real fingerprint never changes with that file: a real
interpreter could not show the check refuse a federation that renders another setting. The stand-in is an executable
that takes the same arguments, ignores the code and prints the fingerprint of the copy it is given, from the copy's
`platform.toml`: the top-level tables of `sections` (a study whose plugin fingerprints those settings) or, with
`real_platform`, the platform's own fingerprint (`PlatformConfig.fingerprint()`, as a study's plugin takes it). A
`# study-setting: X` comment line adds a key only a study's plugin could see, which `PlatformConfig.fingerprint()`
leaves out. Each call is recorded in `calls.txt` beside the stand-in (the SHA-256 of the platform file it was handed),
so a test shows each fingerprint was taken beside its own federation's file.

`plan` and `lines` also ask the same interpreter which core it imports (C06: `shards.identify_core`, `python -I -B -c
CODE` with no study copy); the stand-in answers that call too, with a known core by default or as a real interpreter
(`core_python`), and records no call in `calls.txt` for it.

A study's plugin may refuse to be built without a variable the operator declares in the manifest's `pass_env` (the first
study's refuses without its artifacts directory). The stand-in stands in for such a plugin with `needs`: it ends with
status 4 when one of those names is not in its environment, and each fingerprint call (refused or not) records the whole
environment it was given, one JSON object a line in `environments.jsonl`, so a test shows which variable reached the
program and which never did. The core probe is neither refused for it nor recorded: it builds no plugin.
"""

import json
import shlex
import stat
import sys
from pathlib import Path

SECTIONS = ("faults", "placement")
"""The tables of a platform file a stand-in study fingerprints by default: settings that shape a run."""

PROGRAM = """import hashlib, json, os, re, sys, tomllib
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
config = json.loads((here / "standin.json").read_text(encoding="utf-8"))
if config["fail"]:
    sys.stderr.write("standin: a path that must never be shown: /private/place/of/the/federation\\n")
    raise SystemExit(config["fail"])
if not sys.argv[2]:  # no study copy: the core probe (`shards.identify_core`), which asks what core the Python imports
    if config["core_python"]:  # a real interpreter's answer, as its own process: it is not made to run, so it fails
        os.execv(config["core_python"], [config["core_python"], "-I", "-B", "-c", sys.argv[1]])
    print(json.dumps(config["core"]))
    raise SystemExit(0)
with open(here / "environments.jsonl", "a", encoding="utf-8") as environments:
    environments.write(json.dumps(dict(os.environ)) + "\\n")
absent = [name for name in config["needs"] if name not in os.environ]
if absent:  # the study's plugin, built without a variable it needs
    sys.stderr.write("standin: the plugin refuses to be built without " + ", ".join(absent) + "\\n")
    raise SystemExit(4)
fingerprint = {"seeds": {"1": True}}
platform_file = Path(sys.argv[2]) / "platform.toml"
seen = "none"
if platform_file.is_file():
    raw = platform_file.read_bytes()
    seen = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    if config["real_platform"]:
        from expcore_onedata.config import load_platform

        platform = load_platform(platform_file, require_secrets=False).fingerprint()
    else:
        data = tomllib.loads(text)
        platform = {name: data[name] for name in config["sections"] if name in data}
    if platform:
        fingerprint["platform"] = platform
    note = re.search(r"^# study-setting: (.*)$", text, re.MULTILINE)
    if note:
        fingerprint["study_setting"] = note.group(1)
with open(here / "calls.txt", "a", encoding="utf-8") as calls:
    calls.write(seen + "\\n")
print(json.dumps(fingerprint, sort_keys=True))
"""


CORE = {"known": True, "stale": False, "why": "a stand-in's core"}
"""What the stand-in says of its core when `shards.identify_core` asks (O63 with C06): a commit that is known and not
stale, so `plan` and `lines` go on to print their lines."""


def standin_python(
    directory: Path,
    *,
    sections: tuple[str, ...] = SECTIONS,
    real_platform: bool = False,
    fail: int = 0,
    core: dict | None = None,
    core_python: Path | str | None = None,
    needs: tuple[str, ...] = (),
) -> Path:
    """The stand-in's executable in `directory` (made if need be), to name as a manifest's `python`. `fail` is an exit
    status it ends with after writing a message that holds a path to stderr, to show that message is never repeated.
    Asked which core it imports (`python -I -B -c CODE`, no study copy), it prints `core` (`CORE` by default), or, with
    `core_python`, becomes that interpreter running the same arguments, so a real core's answer is the one given.
    Asked for a fingerprint, it ends with status 4 unless each name of `needs` is in its environment."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "standin.py").write_text(PROGRAM, encoding="utf-8")
    (directory / "standin.json").write_text(
        json.dumps(
            {
                "sections": list(sections),
                "real_platform": real_platform,
                "fail": fail,
                "core": core or CORE,
                "core_python": str(core_python) if core_python else None,
                "needs": list(needs),
            }
        ),
        encoding="utf-8",
    )
    executable = directory / "python"
    script = (
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -B {shlex.quote(str(directory / "standin.py"))} "$4" "$5"\n'
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def seen_platforms(directory: Path) -> list[str]:
    """The SHA-256 of the platform file each call of the stand-in in `directory` was handed, in order."""
    calls = directory / "calls.txt"
    return calls.read_text(encoding="utf-8").split() if calls.exists() else []


def seen_environments(directory: Path) -> list[dict[str, str]]:
    """The whole environment each fingerprint call of the stand-in in `directory` was given, in order."""
    seen = directory / "environments.jsonl"
    return [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()] if seen.exists() else []


__all__ = ["SECTIONS", "seen_environments", "seen_platforms", "standin_python"]

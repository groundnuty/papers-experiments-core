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
"""

import json
import shlex
import stat
import sys
from pathlib import Path

SECTIONS = ("faults", "placement")
"""The tables of a platform file a stand-in study fingerprints by default: settings that shape a run."""

PROGRAM = """import hashlib, json, re, sys, tomllib
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
config = json.loads((here / "standin.json").read_text(encoding="utf-8"))
if config["fail"]:
    sys.stderr.write("standin: a path that must never be shown: /private/place/of/the/federation\\n")
    raise SystemExit(config["fail"])
fingerprint = {"seeds": {"1": True}}
platform_file = Path(sys.argv[1]) / "platform.toml"
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


def standin_python(
    directory: Path, *, sections: tuple[str, ...] = SECTIONS, real_platform: bool = False, fail: int = 0
) -> Path:
    """The stand-in's executable in `directory` (made if need be), to name as a manifest's `python`. `fail` is an exit
    status it ends with after writing a message that holds a path to stderr, to show that message is never repeated."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "standin.py").write_text(PROGRAM, encoding="utf-8")
    (directory / "standin.json").write_text(
        json.dumps({"sections": list(sections), "real_platform": real_platform, "fail": fail}), encoding="utf-8"
    )
    executable = directory / "python"
    script = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -B {shlex.quote(str(directory / "standin.py"))} "$5"\n'
    executable.write_text(script, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def seen_platforms(directory: Path) -> list[str]:
    """The SHA-256 of the platform file each call of the stand-in in `directory` was handed, in order."""
    calls = directory / "calls.txt"
    return calls.read_text(encoding="utf-8").split() if calls.exists() else []


__all__ = ["SECTIONS", "seen_platforms", "standin_python"]

# Provenance: new
"""Which vendored wheels the study-token list skips: a public release, proven by the study's `uv.lock`.

The study-token list holds short private names (hosts, users, spaces, providers), and a public library's code can
hold the same word by chance. Such a library is not the author's to leak through, so the line is drawn by provenance:
a vendored wheel is a public index release, and the study-token list is not applied to it, only when its SHA-256
equals a hash that the study's `uv.lock` records for a wheel of that package, at that name and version, from the
pypi.org registry (`source = { registry = "https://pypi.org/simple" }`). A matching name and version are not enough:
the bytes must be the published artifact's.

- A wheel the build packs again from installed files (`wheels.repack`) is never the published artifact's bytes (its
  RECORD is written anew, the installer's files are left out, its entries are dated 1980), so it is never exempt. To
  exempt a release, supply its published wheel with `--wheels DIR`: it goes in byte for byte once its code is shown to
  be what is installed, and those bytes are what the lock records.
- The core's own wheel, and the wheel of any distribution installed from a URL, a repository or a directory
  (editable or not), always gets the list, whatever the lock says.
- With no lock, a lock that cannot be read (one warning names it), or a package the lock does not hold, every wheel
  gets the list. A broken or unrelated lock thus refuses nothing on its own: the scan fails closed.
- The anonymisation list and the identity an anonymised package must not hold apply to every wheel, always.

The lock is the nearest `uv.lock` from the study directory up to the top of its repository, and no further; a study
outside any repository has only the one in its own directory. `PACKAGE.json` lists every exempt wheel with the lock's
hash (`study_tokens_exempt`), which a reader can check against the index.
"""

import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from expcore.package.wheels import Wheel
from expcore.provenance.environment import CORE_DISTRIBUTION, canonical
from expcore.provenance.gitobjects import GitError, run_git

LOCK_FILE = "uv.lock"
PYPI = "https://pypi.org/simple"

LockHashes = Mapping[tuple[str, str], frozenset[str]]


def _top(study_root: Path) -> Path | None:
    """The top of the repository the study lies in; None outside any."""
    try:
        top = run_git(study_root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    except (GitError, OSError):
        return None
    return Path(top).resolve() if top else None


def find_lock(study_root: Path) -> Path | None:
    """The study's `uv.lock`: the nearest from its directory up to the top of its repository; None if there is none."""
    here = Path(study_root).resolve()
    top = _top(here)
    places = [here] if top is None or (here != top and top not in here.parents) else [here, *here.parents]
    for place in places:
        if (place / LOCK_FILE).is_file():
            return place / LOCK_FILE
        if place == top:
            break
    return None


def _from_pypi(source: object) -> bool:
    if not isinstance(source, dict) or not isinstance(source.get("registry"), str):
        return False
    url = urlsplit(source["registry"])
    return (url.scheme, url.netloc, url.path.rstrip("/"), url.query) == ("https", "pypi.org", "/simple", "")


def pypi_hashes(lock: Path, warn: Callable[[str], None] | None = None) -> dict[tuple[str, str], frozenset[str]]:
    """The SHA-256 of every wheel the lock records from the pypi.org registry, by (canonical name, version).

    A lock that cannot be read holds none: `warn` (standard error by default) gets one line naming it.
    """
    try:
        data = tomllib.loads(Path(lock).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        say = warn or (lambda line: print(f"warning: {line}", file=sys.stderr))
        say(f"{lock}: cannot be read ({type(error).__name__}), so no wheel is exempt from the study-token list")
        return {}
    found: dict[tuple[str, str], set[str]] = {}
    packages = data.get("package")
    for entry in packages if isinstance(packages, list) else []:
        if not isinstance(entry, dict) or not _from_pypi(entry.get("source")):
            continue  # another registry, a URL, a repository or a directory: no proof of a public release
        name, version, wheels = entry.get("name"), entry.get("version"), entry.get("wheels")
        if not isinstance(name, str) or not isinstance(version, str) or not isinstance(wheels, list):
            continue
        digests = {
            wheel["hash"].removeprefix("sha256:").lower()
            for wheel in wheels
            if isinstance(wheel, dict) and isinstance(wheel.get("hash"), str) and wheel["hash"].startswith("sha256:")
        }
        found.setdefault((canonical(name), version), set()).update(digests)
    return {key: frozenset(value) for key, value in found.items()}


def lock_hashes(study_root: Path, warn: Callable[[str], None] | None = None) -> LockHashes:
    """The pypi.org wheel hashes of the study's lock; empty when it has none or it cannot be read."""
    lock = find_lock(study_root)
    return {} if lock is None else pypi_hashes(lock, warn)


def token_exempt(wheels: Sequence[Wheel], hashes: LockHashes) -> dict[str, str]:
    """The wheels the study-token list skips, by path in the package, each with the lock's hash of it."""
    exempt = {}
    for wheel in wheels:
        if wheel.direct or wheel.name == CORE_DISTRIBUTION:
            continue
        if wheel.sha256 in hashes.get((canonical(wheel.name), wheel.version), frozenset()):
            exempt[wheel.path] = wheel.sha256
    return exempt


__all__ = ["LOCK_FILE", "PYPI", "find_lock", "lock_hashes", "pypi_hashes", "token_exempt"]

"""rootclaim.py: one worker per Compose federation across every campaign of its host (C53; `composesite.py`).

Each campaign drives a host's static members from a site directory of its own (`--fleet`), so a member's records there
say nothing to another campaign. The claim is the federation's own: `<root>/.expcore-worker-claim.json`, naming the
site directory and member (one generation of it) whose worker the root serves. `launch` takes it, under the root's
claim lock, before the worker starts, and refuses a root another site directory (another campaign) claims; within one
site directory the members naming one root keep their own rules (`composesite.root_holder`, `observe`'s order). Only
`retired` releases it, for the member that holds it (after the worker is proven gone and its evidence collected), or a
launch whose worker ended at once with nothing of it left. `observe` reads a root another campaign claims as `down`:
not joinable, not counted. A claim that cannot be read is another campaign's: fail closed. A root with no claim but a
worker on it (launched before claims existed: `composesite.workers_on`) refuses a launch too. A campaign that never
retires its member (its controller is gone for good) holds the root until its operator removes the claim. Root
evidence is an allow-list, so the claim is never archived.
"""

import contextlib
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

CLAIM, LOCK = ".expcore-worker-claim.json", ".expcore-worker-claim.lock"
FORMAT = "expcore-compose-claim/1"


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    """The root's claim held by one launch or retirement at a time, of any campaign on the host."""
    descriptor = os.open(root / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def holder(root: Path) -> tuple[str, str] | None:
    """The site directory and member whose worker the root serves, or None when no claim is there. ValueError for a
    claim that is a link, or not one of this format."""
    path = root / CLAIM
    if path.is_symlink():
        raise ValueError("the root's claim is a link, never followed")
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(f"the root's claim cannot be read ({type(error).__name__})") from None
    if not isinstance(found, dict) or found.get("format") != FORMAT:
        raise ValueError("the root's claim is not one of this format")
    fleet, member = found.get("fleet"), found.get("member")
    if not isinstance(fleet, str) or not isinstance(member, str):
        raise ValueError("the root's claim names no site directory and member")
    return fleet, member


def elsewhere(root: Path, fleet: Path) -> bool:
    """Whether another site directory (another campaign) claims the root; a claim that cannot be read is another's."""
    try:
        found = holder(root)
    except ValueError:
        return True
    return found is not None and found[0] != os.path.normpath(fleet)


def take(root: Path, fleet: Path, member: str, channel: str, federation: str) -> None:
    """The root claimed for the member, written whole (a draft beside it, flushed, renamed over it). Held under
    `locked(root)`, after `elsewhere` said no."""
    record = {"format": FORMAT, "fleet": os.path.normpath(fleet), "member": member, "channel": channel,
              "federation": federation, "at": time.time()}  # fmt: skip
    descriptor, draft = tempfile.mkstemp(prefix=f"{CLAIM}.", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, root / CLAIM)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(draft)
        raise


def release(root: Path, fleet: Path, member: str) -> bool:
    """The member's claim on the root removed; a claim of another is left. Held under `locked(root)`."""
    try:
        if holder(root) != (os.path.normpath(fleet), member):
            return False
    except ValueError:
        return False
    (root / CLAIM).unlink()
    return True

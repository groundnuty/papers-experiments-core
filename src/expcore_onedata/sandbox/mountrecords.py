# Provenance: new
"""The operator's tool for the sandbox's mount records (C19, O15): list them, and clear one after manual verification.

A record holds the mounts one sandbox made (`sandbox.MOUNT_RECORDS` under the work root), each with its identity in the
mount table (`hostops.mount_identity`). The sandbox removes its own record once its teardown shows none of those
identities anywhere in the whole mount table. A record holding a mount that was never identified (its mount command
failed, or the table could not be read right after it was made, and the start failed) is never removed that way: no
path check shows where such a mount went, so the probe keeps its lease held (`SandboxProbe`) until an operator clears
the record, as an indeterminate token line holds its slot until an operator removes it.

The procedure, with the platform file the campaign runs with:

  1. `python -m expcore_onedata.sandbox.mountrecords --platform platform.toml list` prints each record by its id (the
     hexadecimal end of its sandbox's id), with how many of its mounts were never identified and how many the mount
     table shows now, by identity or at or under a recorded path.
  2. Verify by hand that nothing of that run is mounted anywhere: `findmnt` on the host, looking for the run's
     directory under the work root and for the record's mount ids; the run's containers are already gone when its
     record is all that is left.
  3. `python -m expcore_onedata.sandbox.mountrecords --platform platform.toml clear <id> --verified` removes the
     record. It refuses without `--verified`, for an id that names no record or more than one, when the mount table
     cannot be read, while the table still shows a mount of the record, and for a record it cannot read or that is
     not one: such a record says nothing of what is mounted, so the operator inspects it and fixes it by hand.

Exit codes: 0 done, 1 refused, 2 a usage error. The output names records by id and counts only, never a path, a lease
key or any other configured value (O12).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import DockerHost, HostOps, describe_error, mount_identity, parse_mountinfo
from expcore_onedata.sandbox.sandbox import MOUNT_RECORDS


def _record_id(file: Path) -> str:
    return file.stem.rsplit("-", 1)[-1]


def _read(file: Path) -> dict[str, Any] | None:
    """A record's document, or None when it cannot be read or is not one."""
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mounts = document.get("mounts") if isinstance(document, dict) else None
    if not isinstance(mounts, list) or not all(isinstance(m, dict) and isinstance(m.get("path"), str) for m in mounts):
        return None
    return document


def _shown(document: dict[str, Any], table: list[dict[str, str]]) -> int:
    """How many of the record's mounts the table shows now: by identity, or at or under the recorded path."""
    count = 0
    for mount in document["mounts"]:
        identity, path = mount_identity(mount), mount["path"].rstrip("/")
        if any(
            (identity is not None and mount_identity(row) == identity)
            or row["mountpoint"] == path
            or row["mountpoint"].startswith(path + "/")
            for row in table
        ):
            count += 1
    return count


def _table(host: HostOps) -> list[dict[str, str]] | None:
    try:
        return parse_mountinfo(host.read_text("/proc/self/mountinfo"))
    except (OSError, SandboxError) as e:
        print(f"the mount table cannot be read: {describe_error(e)}", file=sys.stderr)
        return None


def _list(files: list[Path], host: HostOps) -> int:
    table = _table(host)
    for file in files:
        document = _read(file)
        if document is None:
            print(f"{_record_id(file)} unreadable")
            continue
        unidentified = sum(1 for m in document["mounts"] if mount_identity(m) is None)
        shown = "unknown" if table is None else _shown(document, table)
        print(f"{_record_id(file)} mounts={len(document['mounts'])} unidentified={unidentified} in_table={shown}")
    return 0 if table is not None else 1


def _clear(files: list[Path], host: HostOps, record_id: str, verified: bool) -> int:
    if not verified:
        print("refused: clear only after verifying by hand that nothing of the run is mounted, then pass --verified")
        return 2
    named = [f for f in files if _record_id(f) == record_id]
    if len(named) != 1:
        print(f"refused: {len(named)} records have the id {record_id}")
        return 1
    table = _table(host)
    if table is None:
        print("refused: the mount table cannot be read")
        return 1
    document = _read(named[0])
    if document is None:  # it says nothing of what is mounted: never cleared by this tool
        print(f"refused: record {record_id} cannot be read or is not a record: inspect it and fix it by hand")
        return 1
    if shown := _shown(document, table):
        print(f"refused: the mount table still shows {shown} mount(s) of record {record_id}")
        return 1
    named[0].unlink()
    print(f"cleared {record_id}")
    return 0


def main(argv: Sequence[str] | None = None, *, work_root: Path | None = None, host: HostOps | None = None) -> int:
    """The command line; `work_root` and `host` stand in for the platform file and the real host (tests)."""
    parser = argparse.ArgumentParser(prog="python -m expcore_onedata.sandbox.mountrecords", description=__doc__)
    parser.add_argument("--platform", help="the platform.toml whose [sandbox] work_root holds the records")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="each record by id, with its unidentified mounts and those the table shows")
    clear = commands.add_parser("clear", help="remove one record, after verifying by hand that nothing is mounted")
    clear.add_argument("record_id")
    clear.add_argument("--verified", action="store_true", help="nothing of the run is mounted: checked by hand")
    args = parser.parse_args(argv)
    if work_root is None:
        if not args.platform:
            parser.error("--platform is required")
        from expcore_onedata.config import load_platform  # the run's configuration, read only here

        settings = load_platform(args.platform, require_secrets=False).sandbox
        if settings is None:
            parser.error("the platform file has no [sandbox] section")
        work_root = settings.work_root
    host = host if host is not None else DockerHost()
    directory = Path(work_root) / MOUNT_RECORDS
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    if args.command == "list":
        return _list(files, host)
    return _clear(files, host, args.record_id, args.verified)


if __name__ == "__main__":
    raise SystemExit(main())

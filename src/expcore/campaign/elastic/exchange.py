# Provenance: new
"""The login node's side of one dispatcher tick for a site (#462 step 3, C43): one call, every federation of the site.

The dispatcher runs `python -I -B -m expcore.campaign.elastic.exchange` on the site's login node over one ssh call, the
request (`expcore-elastic-exchange/1`) on standard input, and reads one JSON reply on standard output. For each
federation the request names, in this order (`channel` gives the files and their rules):

1. `deliver`: each lease file is created in the inbox under its lease's name, only if no file has the name, so a lease
   sent again is never written twice and never over its tombstone;
2. `settle`: for each lease the dispatcher found `issued` with no file confirmed (a crash or a lost reply between
   issue and send), the inbox is listed and, when its name is absent, a tombstone takes the name. Exactly one of a
   late delivery and the tombstone holds the name, and the reply says which;
3. a fresh listing of the inbox, after the writes, says what each named lease's file is (`lease`, `tombstone`,
   `absent` or `other`) and whether a drain was asked: the dispatcher marks a lease sent only on `lease` here;
4. a listing of the outbox, then the heartbeat, completions, returns and exit marker it shows. Nothing is removed: the
   ledger reads the same file twice as once. An outbox that cannot be listed is `null`, and the dispatcher reads the
   site as unread.

First of all, `contact.json` in the channel root is written with this host's clock: the login watchdog reads it on the
same clock. The reply carries no secret and no file but the channel's own.
"""

import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from expcore.campaign.elastic import channel as ch

FORMAT = "expcore-elastic-exchange/1"
MODULE = "expcore.campaign.elastic.exchange"
MAX_REQUEST = 1 << 24


class RequestError(ValueError):
    """The request is not one this verb runs; nothing was written."""


def _label(value: Any, what: str) -> str:
    if not isinstance(value, str) or not ch.LABEL.fullmatch(value):
        raise RequestError(f"{what} {value!r} is not an operator label")
    return value


def _lease_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RequestError(f"a lease id is a whole number from 1, not {value!r}")
    return value


def _delivery(record: Any, label: str, site: str) -> tuple[int, dict[str, Any]]:
    if not isinstance(record, dict) or record.get("format") != ch.LEASE_FORMAT:
        raise RequestError("a delivery is a lease file of the channel's format")
    lease = record.get("lease")
    if record.get("federation") != label or record.get("site") != site or not isinstance(lease, dict):
        raise RequestError(f"a delivery for federation {label} names another federation or site, or no lease")
    return _lease_id(lease.get("lease_id")), record


def _inbox_state(inbox: Path, names: set[str], label: str, lease_id: int) -> str:
    name = ch.lease_name(lease_id)
    if name not in names:
        return "absent"
    record = ch.read_listed(inbox, names, name)
    if record is None or record.get("federation") != label:
        return "other"
    if ch.is_tombstone(record):
        return "tombstone" if record.get("cancelled") == lease_id else "other"
    lease = record.get("lease")
    return "lease" if isinstance(lease, dict) and lease.get("lease_id") == lease_id else "other"


def _outbox(outbox: Path) -> dict[str, Any] | None:
    try:
        names = ch.listing(outbox)
    except OSError:
        return None
    found: dict[str, Any] = {"heartbeat": ch.read_listed(outbox, names, ch.HEARTBEAT), "exited": None}
    found["exited"] = ch.read_listed(outbox, names, ch.EXITED)
    for kind, key in (("completion", "completions"), ("return", "returns")):
        items = []
        for name in sorted(names):
            lease_id = ch.named_id(name, kind)
            record = None if lease_id is None else ch.read_listed(outbox, names, name)
            if record is not None:
                items.append({"lease_id": lease_id, "record": record})
        found[key] = items
    return found


def _federation(root: Path, site: str, entry: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    label = _label(entry.get("label"), "federation")
    deliveries = [_delivery(record, label, site) for record in entry.get("deliver", ())]
    settles = [_lease_id(value) for value in entry.get("settle", ())]
    directory = ch.federation_dir(root, label)
    inbox = directory / ch.INBOX
    answer: dict[str, Any] = {"inbox": {}, "drain": False, "outbox": None, "error": None}
    try:
        ch.make_dirs(directory)
        for lease_id, record in deliveries:
            ch.create_json(inbox / ch.lease_name(lease_id), record)
        for lease_id in settles:
            if ch.lease_name(lease_id) not in ch.listing(inbox):
                ch.create_json(inbox / ch.lease_name(lease_id), ch.tombstone(label, site, lease_id))
        names = ch.listing(inbox)
    except OSError as error:
        answer["error"] = f"{type(error).__name__}: {error.strerror or error}"
        return label, answer
    for lease_id in [lease_id for lease_id, _ in deliveries] + settles:
        answer["inbox"][str(lease_id)] = _inbox_state(inbox, names, label, lease_id)
    answer["drain"] = ch.DRAIN in names
    answer["outbox"] = _outbox(directory / ch.OUTBOX)
    return label, answer


def handle(request: Any, clock: Any = time.time) -> dict[str, Any]:
    """Run one tick's request for a site; RequestError for a request of another form, before anything is written."""
    if not isinstance(request, dict) or request.get("format") != FORMAT:
        raise RequestError(f"the request is not {FORMAT}")
    site = _label(request.get("site"), "site")
    root = request.get("root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise RequestError("the channel root is an absolute path")
    entries = request.get("federations")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise RequestError("federations is a list of objects")
    contact = request.get("contact")
    if not isinstance(contact, dict):
        raise RequestError("contact is an object")
    for entry in entries:  # every federation's request is checked before anything is written
        label = _label(entry.get("label"), "federation")
        if not isinstance(entry.get("deliver", []), list) or not isinstance(entry.get("settle", []), list):
            raise RequestError(f"federation {label}: deliver and settle are lists")
        for record in entry.get("deliver", []):
            _delivery(record, label, site)
        for value in entry.get("settle", []):
            _lease_id(value)
    root_path = Path(root)
    now = clock()
    reply: dict[str, Any] = {"format": FORMAT, "site": site, "at": now, "contact": None, "federations": {}}
    try:
        root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        ch.write_json(root_path / ch.CONTACT, {**contact, "format": ch.CONTACT_FORMAT, "site": site, "at": now})
        reply["contact"] = "written"
    except OSError as error:
        reply["contact"] = f"{type(error).__name__}"
    for entry in entries:
        label, answer = _federation(root_path, site, entry)
        reply["federations"][label] = answer
    return reply


def main(stdin: BinaryIO | None = None, stdout: TextIO | None = None, argv: Sequence[str] | None = None) -> int:
    """Read the request on standard input and print the reply; exit 2 for a request of another form."""
    source = sys.stdin.buffer if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    try:
        data = source.read(MAX_REQUEST + 1)
        if len(data) > MAX_REQUEST:
            raise RequestError("the request is too long")
        reply = handle(json.loads(data.decode("utf-8")))
    except (RequestError, ValueError) as error:
        print(f"exchange refused: {error}", file=sys.stderr)
        return 2
    sink.write(json.dumps(reply, sort_keys=True, allow_nan=False) + "\n")
    sink.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

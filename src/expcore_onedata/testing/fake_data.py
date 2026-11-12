# Provenance: new
"""The fake federation with its providers' data API: files, block maps, range reads, transfers and their delays."""

from __future__ import annotations

import itertools
import json
import re
import threading
from dataclasses import dataclass, field
from email.utils import formatdate

import httpx

from expcore_onedata.testing.fake_federation import FakeFederation
from expcore_onedata.testing.fake_onedata import Request

PROVIDER = "/api/v3/oneprovider"
ROOT_ID = "root-0001"

# The shapes of the data API's errors a `FakeData` can answer with (`FakeData.error_shape`).
ERRORS_25_0 = "25.0"  # as seen live on Onedata 25.0: an entry that is gone answers 400 posix/enoent, and so on
ERRORS_LEGACY = "legacy"  # this fake's first shape: 404 notFound, 403 forbidden, no Content-Type check; not seen live
ERROR_SHAPES = (ERRORS_25_0, ERRORS_LEGACY)


def _frozen_clock() -> float:
    """The clock a `FakeData` has until a test binds one: it never moves, which `delete_delay_s` refuses (a delete under
    way could never land)."""
    return 0.0


Answer = tuple[int, str | None, str | None]  # (status, error id, errno); an error id of None: a body no error object

# Where the shapes of a complete listing were measured on Onedata 25.0: how a children page and a transfer page end.
LISTING_END_FACTS = "deploy/compose-federation/day0/2026-11-12-federation-facts-listing-end.md"
_LAST_PAGE_TOKEN = "end-"  # how this fake marks the token a last children page carries, to refuse the page after it

# Where what `FakeData` answers while a delete is under way (`delete_delay_s`) was recorded on Onedata 25.0: a deleted
# entry reading as 400 posix/enoent with its parent link kept (the first), another provider's directory sizes following
# a delete about 5 s later (the second), and a directory delete finishing after the call returns, taking the files of a
# directory made again under its name (the third, a module docstring and not a dated fact; #29).
DELETE_FACTS = (
    "deploy/compose-federation/day0/2026-11-10-federation-facts-parent-walk.md",
    "deploy/compose-federation/day0/2026-11-11-federation-facts-posix-and-fuse.md",
    "deploy/compose-federation/fedcontent.py",
)

# What each shape sets: how an entry that is not there, one a reader does not show yet, a space root not there yet and
# a read-only write are answered, whether a deleted entry's parent link still reads, and whether an upload must say it
# is an octet stream. A test may set any of these attributes after the shape, to bend one answer.
_SHAPES: dict[str, dict[str, object]] = {
    ERRORS_25_0: {
        "missing_answer": (400, "posix", "enoent"),
        "unseen_answer": (400, "posix", "enoent"),
        "root_enoent_answer": (400, "posix", "enoent"),
        "readonly_answer": (400, "posix", "eacces"),
        "keep_deleted_parents": True,
        "require_octet_stream": True,
    },
    ERRORS_LEGACY: {
        "missing_answer": (404, "notFound", None),
        "unseen_answer": (404, "notFound", None),
        "root_enoent_answer": (404, "posix", "enoent"),
        "readonly_answer": (403, "forbidden", None),
        "keep_deleted_parents": False,
        "require_octet_stream": False,
    },
}


@dataclass
class Node:
    """A file or a directory of the space."""

    file_id: str
    name: str
    kind: str  # "REG" or "DIR"
    parent: str | None
    content: bytearray = field(default_factory=bytearray)
    created: float = 0.0  # the fake's clock when it was made
    origin: str | None = None  # the provider it was made on: the others show it only after the visibility delay


@dataclass
class Transfer:
    """A replication, eviction or migration job, when it was scheduled, started and ended, and how often it was asked
    about. `provider_id` is the replicating provider (replication, migration) or the evicting one (eviction);
    `source_id` is a migration's evicting provider."""

    kind: str
    file_id: str
    provider_id: str
    asked: int = 0
    ended: bool = False
    source_id: str | None = None
    scheduled: int = 0
    started: int = 0
    finished: int = 0
    waiting: bool = False
    bytes_replicated: int | None = None  # the counter the record reports, when a test scripts it
    noop: bool = False  # an eviction that completed and evicted nothing (`evictions_noop_first`)
    deleted: bool = False  # the file was deleted when the job was scheduled: it ended at once, with no file processed

    @property
    def replicates(self) -> bool:
        return self.kind in ("replication", "migration")

    @property
    def state(self) -> str:
        return "ended" if self.ended else "waiting" if self.waiting else "ongoing"


@dataclass
class _Deleting:
    """A delete that was answered and has not landed: the entry, where it stood, the provider the DELETE went to and
    when it lands (on `clock`)."""

    file_id: str
    parent: str | None
    name: str
    kind: str
    via: str
    lands: float


def _merge(blocks: list[list[int]]) -> list[list[int]]:
    """Merge overlapping or adjacent [offset, length] blocks."""
    merged: list[list[int]] = []
    for off, length in sorted(blocks):
        if merged and off <= merged[-1][0] + merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], off + length - merged[-1][0])
        else:
            merged.append([off, length])
    return merged


class FakeData(FakeFederation):
    """Onezone plus the data API of every provider it reports, with files, block maps and transfers.

    Models: the calls of `/api/v3/oneprovider`, chosen by the request's host (a provider is the one whose domain is
    that host),

      GET    /spaces/{space}                        the space's root directory (ENOENT for `root_enoent_polls` calls)
      GET    /data/{id}                             attributes, `parent_id` among them; asked for a path, 400 as in 25.0
      GET    /data/{id}/children                    paged by `limit` and `token`, ending as 25.0's do
      POST   /data/{id}/children?name&type          create a file or a directory
      PUT    /data/{id}/content?offset              write bytes; the writing provider holds them. Without `offset`
                                                    the file is truncated first, as the API describes
      GET    /data/{id}/content  (Range)            206; a provider lacking the bytes fetches them on the fly
      GET    /data/{id}/distribution                the block map of every provider
      DELETE /data/{id}                             remove a file or a directory with what is under it
      POST   /transfers, GET /transfers/{id}        replication, eviction and migration jobs
      GET    /spaces/{space}/transfers              the ids of the space's jobs in one state (`limit`, `page_token`),
                                                    ending as 25.0's do

    and, from Onezone, `GET /spaces/{space}` (the live name). Time is counted in calls, and where a delay is in
    seconds it is seconds of the fake's own `clock`, never of the wall, so a test never waits.

    **Block maps.** Each provider holds ranges of each file (`GET /data/{id}/distribution`, `held`). The provider a file
    is made on holds all of it (`add_file`'s `holder`) or the bytes it wrote. A range read through a provider that lacks
    the bytes fetches them there, on the fly, and they show in its map after `fetch_lag` polls of the map. A
    replication places the whole file on its target, and an eviction removes all of the evicting provider's blocks of
    the file (a migration does both); no provider's map changes another's.

    **Listings** end as Onedata 25.0's were measured to (`LISTING_END_FACTS`). A children page has the keys
    `nextPageToken`, `isLast` and `children`, in that order: `isLast` is true only on a page shorter than its limit,
    the token is a non-empty string on every page, the last included, so a page the directory's last entries fill is
    followed by an empty page with `isLast` true, and the page after a last page answers 400 `posix` `eagain`. A
    transfer page has `transfers` and `nextPageToken`, no `isLast`: the token is a string on a full page and null on
    the last, so a full page is followed by an empty one with a null token.

    **Per-reader views.** A file or a directory is made on one provider, and every other provider shows it only
    after `visibility_delay_s` seconds of `clock` (`visibility_delay_by_provider` gives a reader its own; the default is
    0, at once). Until then that reader's attributes, children, content and block-map reads of it answer
    `unseen_answer`, and the listing of its parent leaves it out. About 6 s was seen live on Onedata 25.0. A test can
    also hold a file back from one reader for a number of reads instead of seconds (`unseen`, `unseen_after_create`):
    each read of the file through that provider answers `unseen_answer` until they are used up; the listing is not
    counted and shows the file.

    **Pending deletes** (`delete_delay_s`; the default 0 finishes every delete at once, everywhere, so nothing changes
    for a test that does not set it). With a delay, a DELETE of a file or a directory is answered 204 and takes that
    many seconds of `clock` to land (`land_deletes()` lands them without waiting), so a study can test that its code
    waits before it uses the name again. In between, by what is recorded (`DELETE_FACTS`):

    - Recorded. The provider the DELETE went to answers the entry as a deleted one at once (`missing_answer`), with a
      deleted file's `parent_id` still reading (`keep_deleted_parents`). A provider finishes deleting a directory after
      the call returns, and a directory made again under the same name before then loses its files when it does (the
      reason a federation reset never deletes a directory and makes it again). So the create of the name is accepted
      there, and when the delete lands the files under the directory made again go with it. Other providers' directory
      sizes were seen to follow a delete about 5 s late.
    - Assumed, not recorded; a live check can confirm or correct each. (a) The providers the DELETE did not go to keep
      showing the entry, in reads and listings, until it lands, and refuse its name (409, as for any taken name);
      a second DELETE through them changes nothing. (b) Every provider lands it at once, after the one delay. (c) A
      file's delete is late in the same way (a directory's is what was recorded), and a file's name made again loses
      nothing. (d) The late removal of a directory takes files at any depth of the directory made again and no
      directory. (e) A listing through the DELETE's provider already leaves out a directory still going (a deleted
      file was seen gone from one). (f) A provider that still shows the old entry shows one made again beside it,
      once the visibility delay lets it. (g) What is under a directory deleted through a provider answers as deleted
      there at once too: the facts cited have rows for the deleted file and the deleted directory themselves, none
      for what is under it. (h) A transfer scheduled through that provider for an entry it answers as deleted is
      refused as one of an entry that is not there (400, as with no delay).

    A delay needs a `clock` that moves. The default never does, so a delete would never land and a test of a name used
    again would pass for no reason: the first DELETE that would pend with it raises `ValueError` naming
    `delete_delay_s` and how to bind one (`make_federation(clock=ticker.clock)` or `fake.clock = ticker.clock`, a
    `Ticker`'s). The delay and the clock are plain attributes a test sets in either order, so the check is made at that
    DELETE and not when the delay is set. A delay of 0 needs no clock.

    `fake.nodes` still holds an entry whose delete is under way, until it lands, though `id_of` and `exists` already
    treat it as gone: a test that reads `nodes` directly sees it there.

    Provider calls are answered one at a time, under the fake's lock, so threads may call it at once (a core's bulk
    reads do); a delete that is due lands under that lock, as the first call after its time.

    **Error shapes**, chosen with `error_shape` and documented by the version each belongs to:

    - `ERRORS_25_0` (the default), as measured live on Onedata 25.0 (the dated facts in
      `deploy/compose-federation/day0/`): an entry that is deleted or not visible yet, and a space root that is not
      there yet, answer 400 with error id `posix` and `errno` `enoent`, while a deleted entry's `parent_id` still
      reads its old parent; a write made with a token that has the read-only caveat answers 400 `posix`/`eacces`; an
      upload (`PUT` of content) without `Content-Type: application/octet-stream` answers 415. The statuses and the
      `posix` errors are what was seen. A 415 was seen for an upload with no Content-Type at all; that this fake
      refuses any other type too, and the error id of its 415, `unsupportedMediaType`, are its own.
    - `ERRORS_LEGACY`: the shape this fake had before those observations: 404 `notFound` for an entry that is gone
      or not shown yet (404 `posix`/`enoent` for the space root), no parent link for a deleted entry, 403 `forbidden`
      for a read-only write, and no Content-Type check. No live version is known to answer so; it is kept for a study
      that targets an older platform.

    Setting `error_shape` sets `missing_answer`, `unseen_answer`, `root_enoent_answer`, `readonly_answer`,
    `keep_deleted_parents` and `require_octet_stream` to that version's; a test sets one of them after it to bend one
    answer (another 400, say, to see the core fail closed on it). A job not found yet answers `transfer_unseen_answer`,
    404 `notFound` in both shapes, since no 25.0 answer for it is recorded.

    A job carries the times the platform reports: `scheduleTime`, `startTime` and `finishTime`, taken from `clock`.
    The fake does not decide which jobs fall in a run's window: the core does, from those times and the providers'
    `Date` headers, so a job made with `add_transfer(..., scheduled=<before the run>)` and not ended is reported as the
    core reports it, carried over into the run and not counted in its transfer bytes. A stand-in that sums every job it
    holds counts it, and differs from the core there.

    Does not model: a transfer's duration (it ends after `transfer_flip_after` polls of its record, or when a test ends
    it), partial replication (a replication places the whole file, as Onedata's does), QoS requirements, file
    metadata other than the id, name, type, size and parent, storage backends beyond one per provider, concurrent
    writers, quotas, symbolic links, a token's space or interface caveats (only the read-only caveat is enforced, and
    `enforce_readonly=False` turns that off), a delete that other providers show late (unless `delete_delay_s` is set),
    the visibility delay for scheduling a transfer of a file the provider does not show yet (the job is accepted),
    eviction of part of a file (it removes all of a provider's blocks), other errors than those above, and any
    provider call that is not listed above.

    A test bends the fake through plain attributes:

    - `error_shape` (also a constructor argument): `ERRORS_25_0` or `ERRORS_LEGACY`; any other value is refused;
    - `visibility_delay_s` (also a constructor argument) and `visibility_delay_by_provider`: seconds of `clock` before
      a new file shows on a provider it was not made on, for all readers and for one;
    - `fetch_lag`: polls of the file's block map before bytes fetched by a range read or a replication show in it;
    - `onthefly_places`: False makes a range read answer without ever adding a block (a provider that does not cache);
    - `transfers_place_blocks`: False makes a replication end as `completed` with no block placed;
    - `evictions_remove_blocks`: False makes an eviction end as `completed` and leave the blocks;
      `evictions_noop_first` does that, with `filesEvicted` 0, for the next N evictions only;
    - `onthefly_unit`: a range read through a provider lacking the bytes places at least this many from its offset;
    - `transfer_flip_after`: polls of a transfer before it ends; `transfer_visible_after`: polls before it is found at
      all, each answered `transfer_unseen_answer`; a fresh space root answers `root_enoent_answer` for
      `root_enoent_polls` reads;
    - `resets`: a list of `[substring, count]` or `[substring, count, provider id]`: the next `count` provider calls
      whose route contains the substring (to that provider, when named) fail with a connection reset;
    - `statuses`: a list of `[substring, count, status]` likewise answering with that HTTP status;
    - `fail_deletes`, `ignore_deletes`: every DELETE answers 500, or answers 204 and removes nothing;
    - `delete_delay_s` (also a constructor argument): seconds of `clock` a delete stays under way after it is answered
      (0: none); `land_deletes()` ends every one now (see "Pending deletes");
    - `enforce_readonly`: False lets a token with the read-only caveat write (a broken enforcement);
      `readonly_answer` is how such a write is refused;
    - `unseen` (`(provider id, file id)` to reads) and `unseen_after_create` (provider id to reads, for every file made
      from then on): a provider that sees a new file only after so many reads of it, each answered `unseen_answer`;
    - `missing_answer`: how a file or directory that is not there is answered; `keep_deleted_parents`: a deleted
      entry's `parent_id` still answers its old parent; `answer_missing_as_25(answer)` sets both, as 25.0 has them;
    - `require_octet_stream`: a content write (`PUT /data/{id}/content`) without `Content-Type:
      application/octet-stream` is a 415; `content_types` lists the Content-Type of every content write, in order;
    - `corrupt_writes`: True stores every write with its first byte inverted, so the content differs from what was sent;
    - `writes_before_reset`: the content writes that succeed before the next one fails with a connection reset, and
      every later one with it (None: never);
    - `children_omit`, `children_fields`: keys every children page leaves out, and keys it answers with another value;
    - `content_type`: the Content-Type of a file's content, read whole or by range (None: the header is left out);
    - `clock`: the epoch seconds a job's schedule, start and finish times are taken from (a test binds its own);
    - `transfer_omit`: field names a job's record leaves out; `transfer_list_body`: an answer that replaces the
      listing's;
    - `provider_view`: `(job id, provider id)` to what that provider says of the job where it differs: `state` (the
      list it is in, and its record's status) and record fields such as `bytesReplicated`;
    - `listing_lag`: job id to how many listing answers, counted from the first, leave the job out (its record is read
      at once, as a real provider's is while its listing lags);
    - every data-API answer has a `Date` header of `clock` moved by `provider_skew` (provider id to seconds ahead,
      negative when behind); `date_override` (provider id to a header value, or None for none) replaces it.

    `add_transfer` makes a job that no request to `/transfers` made, as a QoS requirement or another client does, with
    the times and the state a test gives it. `provider_calls` lists `(provider id, method, route)` of every provider
    call, so a test can assert what was sent where.
    """

    # Set by `error_shape`, each to that version's (see _SHAPES); a test may set one after it.
    missing_answer: Answer
    unseen_answer: Answer
    root_enoent_answer: Answer
    readonly_answer: Answer
    keep_deleted_parents: bool
    require_octet_stream: bool

    def __init__(
        self,
        *,
        error_shape: str = ERRORS_25_0,
        visibility_delay_s: float = 0.0,
        delete_delay_s: float = 0.0,
        lose_recreated: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        # Re-entrant: `respond` holds it for a whole provider route, and the helpers the route calls take it again.
        self._lock = threading.RLock()
        self.error_shape = error_shape  # which version's error answers the data API gives: see ERROR_SHAPES
        self.visibility_delay_s = visibility_delay_s  # seconds of `clock` before a new entry shows on another provider
        self.visibility_delay_by_provider: dict[str, float] = {}  # provider id -> its own delay, over the one above
        self.delete_delay_s = (
            delete_delay_s  # seconds of `clock` a DELETE stays under way after it is answered (0: none)
        )
        # A directory made again under the name of one whose delete is under way loses its files when that delete lands.
        # Not reproduced live (12 Nov facts), so off: an opt-in for testing defensive waits.
        self.lose_recreated = lose_recreated
        self._deleting: dict[str, _Deleting] = {}  # entry id -> the delete of it that has not landed
        self.nodes: dict[str, Node] = {ROOT_ID: Node(ROOT_ID, "", "DIR", None)}
        self.blocks: dict[tuple[str, str], list[list[int]]] = {}
        self.pending: dict[tuple[str, str], list[list[int]]] = {}  # [offset, length, polls left]
        self.transfers: dict[str, Transfer] = {}
        self.provider_calls: list[tuple[str, str, str]] = []
        self.fetch_lag = 0
        self.onthefly_places = True
        self.transfers_place_blocks = True
        self.evictions_remove_blocks = True
        self.evictions_noop_first = 0  # the next N evictions complete with filesEvicted 0 and remove nothing, as 25.0's
        # do when scheduled right after a provider fetched the bytes on the fly
        self.onthefly_unit: int | None = (
            None  # a range read places [offset, offset + max(length, unit)), clipped to the
        )
        # file, as 25.0 does with 1 MiB; None places exactly the range read
        self.transfer_flip_after = 1
        self.transfer_visible_after = 0
        self.root_enoent_polls = 0
        # A provider that sees a new file only after some reads: (provider id, file id) -> reads of it answered as
        # not there; `unseen_after_create` (provider id -> reads) sets that for every file made from then on.
        self.unseen: dict[tuple[str, str], int] = {}
        self.unseen_after_create: dict[str, int] = {}
        self.transfer_unseen_answer: Answer = (404, "notFound", None)  # a job not seen yet, in either shape
        self.page_size = 1000
        self.id_suffix = ""  # appended to every file id made from now on, for ids with characters like "="
        self.fail_deletes = False  # every DELETE answers 500
        self.omit_blocks = False  # the block map's storage backends carry no "blocks" field
        self.omit_providers: set[str] = set()  # provider ids the block map leaves out (a partial answer)
        self.ignore_deletes = False  # every DELETE answers 204 and removes nothing
        self.enforce_readonly = True  # a mutation made with a token that has the read-only caveat is refused
        self.deleted_parents: dict[str, str | None] = {}  # a deleted entry's parent, read if `keep_deleted_parents`
        self.resets: list[list] = []
        self.statuses: list[list] = []
        self.content_type: str | None = "application/octet-stream"  # of a file's content, whole or by range
        self.content_types: list[str | None] = []  # the Content-Type of every write of content, in order
        self.corrupt_writes = False  # every write is stored with its first byte inverted
        self.writes_before_reset: int | None = None  # writes of content that succeed before the rest are reset
        self.children_omit: set[str] = set()  # keys every children page leaves out
        self.children_fields: dict = {}  # keys every children page answers with this value instead
        self.clock = _frozen_clock  # epoch seconds of a job's schedule, start and finish times; a test binds its own
        self.transfer_omit: set[str] = set()  # fields a job's record leaves out
        self.transfer_list_body: dict | None = None  # replaces every answer of the listing
        self.provider_view: dict[tuple[str, str], dict] = {}  # (job, provider id) -> its listed state, record fields
        self.listing_lag: dict[str, int] = {}  # job id -> listing answers it is left out of, from the first one
        self.provider_skew: dict[str, float] = {}  # provider id -> seconds its clock runs ahead (negative: behind)
        self.date_override: dict[str, str | None] = {}  # provider id -> the Date header it sends (None: none)
        self.listings = 0  # the listing answers given
        self._ids = itertools.count(1)
        self._root_polls = 0

    @property
    def error_shape(self) -> str:
        """The shape of the data API's errors: `ERRORS_25_0` (the default) or `ERRORS_LEGACY`."""
        return self._error_shape

    @error_shape.setter
    def error_shape(self, shape: str) -> None:
        if shape not in ERROR_SHAPES:
            raise ValueError(f"error_shape must be one of {ERROR_SHAPES}, not {shape!r}")
        self._error_shape = shape
        for name, value in _SHAPES[shape].items():
            setattr(self, name, value)

    # -- setup helpers ---------------------------------------------------------
    def provider_of(self, host: str) -> str | None:
        """Return the id of the provider that answers for a host."""
        return next((i for i, p in self.providers.items() if p["domain"] == host), None)

    def add_file(self, path: str, content: bytes, *, holder: str | None = None) -> str:
        """Create a file at a path of the space (directories on the way as needed); the holder has all its blocks.

        The holder is also the provider the entries are made on, so another provider shows them only after the
        visibility delay (none by default)."""
        holder = holder or self.PROVIDER_ID
        parent = ROOT_ID
        *dirs, name = path.strip("/").split("/")
        with self._lock:
            for part in dirs:
                parent = self._child(parent, part) or self._create(parent, part, "DIR", holder)
            file_id = self._create(parent, name, "REG", holder)
            self.nodes[file_id].content = bytearray(content)
            if content:
                self.blocks[(file_id, holder)] = [[0, len(content)]]
        return file_id

    def id_of(self, path: str) -> str:
        """Return the id of a path of the space; raise KeyError when there is none."""
        node = ROOT_ID
        with self._lock:
            for part in path.strip("/").split("/"):
                child = self._child(node, part)
                if child is None:
                    raise KeyError(path)
                node = child
        return node

    def held(self, path: str, provider_id: str) -> list[list[int]]:
        """Return the blocks a provider holds of a file, merged."""
        return _merge([list(b) for b in self.blocks.get((self.id_of(path), provider_id), [])])

    def exists(self, path: str) -> bool:
        """Tell whether a path of the space exists."""
        try:
            self.id_of(path)
        except KeyError:
            return False
        return True

    def add_transfer(
        self,
        kind: str,
        path: str,
        provider_id: str,
        *,
        scheduled: float,
        source_id: str | None = None,
        ended: bool = True,
        waiting: bool = False,
        finished: float | None = None,
        bytes_replicated: int | None = None,
    ) -> str:
        """Make a job no request to `/transfers` made (a QoS requirement's, another client's) and return its id; one
        that has ended has placed or removed its blocks, and finished at `finished` (default: when it was scheduled).

        `scheduled` is when the job started, on `clock`'s scale. A job made before a run's window opens, with
        `ended=False`, is still running when the window does: the core reports it as carried over, never as a transfer
        of the run. `end_transfer` ends it later, inside the window, and places its blocks then."""
        tid = f"tid-{next(self._ids):04d}"
        self.transfers[tid] = Transfer(
            kind,
            self.id_of(path),
            provider_id,
            source_id=source_id,
            scheduled=int(scheduled),
            started=0 if waiting else int(scheduled),
            waiting=waiting,
            bytes_replicated=bytes_replicated,
        )
        if ended:
            self.end_transfer(tid, at=scheduled if finished is None else finished)
        return tid

    def end_transfer(self, tid: str, *, at: float | None = None) -> None:
        """End a job now (or at `at`): a replication places the file's blocks, an eviction removes them."""
        transfer = self.transfers[tid]
        transfer.ended, transfer.waiting = True, False
        transfer.started = transfer.started or transfer.scheduled
        transfer.finished = int(self.clock() if at is None else at)
        self._finish(transfer)

    # -- tree ------------------------------------------------------------------
    def _child(self, parent: str, name: str, provider: str | None = None) -> str | None:
        """The id of the entry of that name under a parent, after the deletes that are due have landed. An entry whose
        delete is under way is not found by the provider it was deleted through (nor by `None`, which is how a test's
        helpers ask), and is found by the others, which still show it."""
        with self._lock:
            self._settle_deletes()
            found = (
                n for n in self.nodes.values() if n.parent == parent and n.name == name and not self._left(n, provider)
            )
            return next((n.file_id for n in found), None)

    def _left(self, node: Node, provider: str | None) -> bool:
        """Tell whether an entry's delete is under way and `provider`, the one it was sent through, answers it gone."""
        pending = self._deleting.get(node.file_id)
        return pending is not None and (provider is None or pending.via == provider)

    def _hidden(self, node: Node, provider: str) -> bool:
        """Tell whether an entry, or a directory above it, was deleted through `provider` and is still going."""
        if not self._deleting:
            return False
        current: Node | None = node
        while current is not None:
            if self._left(current, provider):
                return True
            current = self.nodes.get(current.parent) if current.parent is not None else None
        return False

    def _under(self, file_id: str) -> list[str]:
        """The ids of an entry and of everything under it."""
        ids = [file_id]
        for child in [n.file_id for n in self.nodes.values() if n.parent == file_id]:
            ids.extend(self._under(child))
        return ids

    def _remove(self, node: Node, provider: str) -> None:
        """Delete an entry with what is under it: now, or, with a `delete_delay_s`, by answering it deleted through
        `provider` at once and leaving it to the others until `delete_delay_s` has passed. A delete of an entry whose
        own delete, or a directory above it, is already under way changes nothing."""
        if self.delete_delay_s <= 0:
            self._delete(node.file_id)
            return
        if self.clock is _frozen_clock:
            raise ValueError(
                f"delete_delay_s is {self.delete_delay_s}, but this fake's clock never moves, so the delete would "
                "never land and a test of a name used again would pass for no reason: bind a clock that moves, "
                "make_federation(clock=ticker.clock) or fake.clock = ticker.clock for a Ticker, and advance it with "
                "ticker.sleep(...)"
            )
        current: Node | None = node
        while current is not None:
            if current.file_id in self._deleting:
                return
            current = self.nodes.get(current.parent) if current.parent is not None else None
        for member in self._under(node.file_id):  # a gone entry's parent link reads, as it does once the delete lands
            self.deleted_parents[member] = self.nodes[member].parent
        lands = self.clock() + self.delete_delay_s
        self._deleting[node.file_id] = _Deleting(node.file_id, node.parent, node.name, node.kind, provider, lands)

    def land_deletes(self) -> None:
        """Land every delete that is under way now, without waiting for its time, as `end_transfer` ends a job."""
        with self._lock:
            for pending in sorted(self._deleting.values(), key=lambda p: p.lands):
                self._land(pending)

    def _settle_deletes(self) -> None:
        """Land the deletes whose time has come, in the order they fell due. Landing changes the tree, so it is done
        under the lock that every read of the tree is made under."""
        with self._lock:
            if self._deleting:
                now = self.clock()
                for pending in sorted((p for p in self._deleting.values() if p.lands <= now), key=lambda p: p.lands):
                    self._land(pending)

    def _land(self, pending: _Deleting) -> None:
        """Finish a delete. It removes the entry and what is under it, and nothing made again under its name since:
        that was seen live (`FakeData`, "Pending deletes"). With `lose_recreated`, a directory's late removal also takes
        the files of a directory made again under its name; a file's, and a name made again that is a file, lose
        nothing either way."""
        if pending.file_id not in self._deleting:  # taken with a directory above it that landed first
            return
        del self._deleting[pending.file_id]
        if pending.kind == "DIR" and self.lose_recreated:
            again = next(
                (
                    n.file_id
                    for n in self.nodes.values()
                    if (n.parent, n.name, n.kind) == (pending.parent, pending.name, "DIR")
                    and n.file_id != pending.file_id
                    and n.file_id not in self._deleting
                ),
                None,
            )
            for file_id in [i for i in self._under(again) if self.nodes[i].kind == "REG"] if again else []:
                self._delete(file_id)
        self._delete(pending.file_id)

    def _create(self, parent: str, name: str, kind: str, origin: str | None = None) -> str:
        file_id = f"fid-{next(self._ids):04d}{self.id_suffix}"
        self.nodes[file_id] = Node(file_id, name, kind, parent, created=self.clock(), origin=origin)
        if kind == "REG":  # a provider that sees a file sees the directories above it, made before it
            for provider_id, reads in self.unseen_after_create.items():
                self.unseen[(provider_id, file_id)] = reads
        return file_id

    def _visible(self, node: Node, provider: str) -> bool:
        """Tell whether a provider shows an entry: the one it was made on does at once, the others after their delay."""
        delay = self.visibility_delay_by_provider.get(provider, self.visibility_delay_s)
        return node.origin in (None, provider) or delay <= 0 or self.clock() >= node.created + delay

    def _shown(self, node: Node, provider: str) -> bool:
        """Tell whether a provider shows an entry to a read of it: not while `unseen` holds reads of it back (this read
        uses one up), and not before its visibility delay has run out."""
        lag = (provider, node.file_id)
        if self.unseen.get(lag, 0) > 0:
            self.unseen[lag] -= 1
            return False
        return self._visible(node, provider)

    def _delete(self, file_id: str) -> None:
        for child in [n.file_id for n in self.nodes.values() if n.parent == file_id]:
            self._delete(child)
        self.deleted_parents[file_id] = self.nodes[file_id].parent
        self._deleting.pop(file_id, None)
        del self.nodes[file_id]
        for table in (self.blocks, self.pending):
            for key in [k for k in table if k[0] == file_id]:
                del table[key]

    # -- answers ---------------------------------------------------------------
    @staticmethod
    def _error(status: int, error_id: str, errno: str | None = None) -> httpx.Response:
        details = {"errno": errno} if errno is not None else {}
        return httpx.Response(status, json={"error": {"id": error_id, "details": details}})

    def answer_missing_as_25(self, answer: Answer = (400, "posix", "enoent")) -> None:
        """Answer what is not there as Onedata 25.0 does: 400 posix enoent instead of a 404, while a deleted entry's
        `parent_id` still answers its old parent (both read on the Compose federation). `answer` replaces the 400."""
        self.missing_answer = answer
        self.keep_deleted_parents = True

    def _missing(self, file_id: str, query: httpx.QueryParams, method: str) -> httpx.Response:
        """A file or directory that is not there: `missing_answer`, except a deleted entry's kept parent link."""
        kept = self.deleted_parents if self.keep_deleted_parents else {}
        if method == "GET" and file_id in kept and query.get("attribute") == "parent_id":
            return httpx.Response(200, json={"parent_id": kept[file_id]})
        return self._answer(self.missing_answer)

    def _answer(self, answer: Answer) -> httpx.Response:
        """An error answer from (status, error id, errno); an error id of None is a body that is no error object."""
        status, error_id, errno = answer
        if error_id is None:
            return httpx.Response(status, text="<html><body>Bad Request</body></html>")
        return self._error(status, error_id, errno)

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer a provider call here and anything else as the federation does."""
        if not request.url.path.startswith(PROVIDER):
            return super().respond(request)
        route = request.url.path.removeprefix(PROVIDER)
        provider = self.provider_of(request.url.host)
        with self._lock:
            self.provider_calls.append((provider or "?", request.method, route))
            self.requests.append(
                Request(
                    request.method,
                    request.url.path,
                    dict(request.url.params),
                    request.headers.get("Range"),
                    request.headers.get("X-Auth-Token"),
                    request.content,
                )
            )
            for fault in self.resets:
                if fault[1] > 0 and fault[0] in route and (len(fault) < 3 or fault[2] == provider):
                    fault[1] -= 1
                    raise httpx.ConnectError("connection reset")
            for fault in self.statuses:
                if fault[1] > 0 and fault[0] in route:
                    fault[1] -= 1
                    return self._error(fault[2], "internalServerError")
        if provider is None:
            return self._error(404, "notFound")
        if route == "/configuration":
            return httpx.Response(200, json={"version": "25.0.0", "build": "test", "name": provider})
        token = request.headers.get("X-Auth-Token")
        if token != self.admin_token and not self._accepts(token):
            return self._error(401, "unauthorized")
        with self._lock:  # a route reads the tree and may land deletes in it: one at a time, whatever threads call
            answer = self._provider_route(request, provider, route)
        return self._dated(answer, provider)

    def _dated(self, response: httpx.Response, provider: str) -> httpx.Response:
        """Give an answer the Date header of the provider's clock: the fake's clock moved by the provider's skew, in
        whole seconds as HTTP has it, or the override a test set (None: no header at all)."""
        if provider in self.date_override:
            if self.date_override[provider] is not None:
                response.headers["Date"] = self.date_override[provider]
            return response
        response.headers["Date"] = formatdate(self.clock() + self.provider_skew.get(provider, 0.0), usegmt=True)
        return response

    def _admin_route(self, method: str, route: str, sent: dict) -> httpx.Response:
        """Onezone's calls that need the admin credential: a provider's online flag, then what the federation has."""
        found = re.fullmatch(r"/providers/([^/]+)", route)
        if method == "GET" and found:
            entry = self.providers.get(found.group(1))
            if entry is None:
                return self._error(404, "notFound")
            return httpx.Response(200, json={"online": entry["online"], "name": entry["name"]})
        return super()._admin_route(method, route, sent)

    def _provider_route(self, request: httpx.Request, provider: str, route: str) -> httpx.Response:
        method, query = request.method, request.url.params
        issued = self.issued.get(request.headers.get("X-Auth-Token", ""))
        if (
            self.enforce_readonly
            and issued is not None
            and method in ("POST", "PUT", "DELETE")
            and any(c.get("type") == "data.readonly" for c in issued["caveats"])
        ):
            return self._answer(self.readonly_answer)
        self._settle_deletes()
        found = re.fullmatch(r"/spaces/([^/]+)", route)
        if found and method == "GET":
            return self._space_root(found.group(1))
        found = re.fullmatch(r"/data/([^/]+)(/children|/content|/distribution)?", route)
        if found:
            node = self.nodes.get(found.group(1))
            if node is None or self._hidden(node, provider):  # a delete through this provider that is under way: gone
                return self._missing(found.group(1), query, method)
            if not self._shown(node, provider):  # this provider does not see the new entry yet
                return self._answer(self.unseen_answer)
            part = found.group(2) or ""
            if part == "" and method == "GET":
                if "path" in query.get_list("attribute"):  # Onedata 25.0 has no path attribute ([L1] in client.py)
                    return self._error(400, "badValueNotAllowed")
                return httpx.Response(200, json=self._attributes(node))
            if part == "" and method == "DELETE":
                if self.fail_deletes:
                    return self._error(500, "internalServerError")
                if not self.ignore_deletes:
                    self._remove(node, provider)
                return httpx.Response(204)
            if part == "/children" and method == "GET":
                return self._list(node, query, provider)
            if part == "/children" and method == "POST":
                return self._make(node, query, provider)
            if part == "/content" and method == "PUT":
                return self._put_content(request, node, provider)
            if part == "/content" and method == "GET":
                return self._read(node, provider, request.headers.get("Range"))
            if part == "/distribution" and method == "GET":
                return httpx.Response(200, json=self._distribution(node))
        if route == "/transfers" and method == "POST":
            return self._schedule(request, provider)
        found = re.fullmatch(r"/spaces/([^/]+)/transfers", route)
        if found and method == "GET":
            return self._list_transfers(found.group(1), query, provider)
        found = re.fullmatch(r"/transfers/([^/]+)", route)
        if found and method == "GET":
            return self._transfer(found.group(1), provider)
        return self._error(404, "notFound")

    def _space_root(self, space: str) -> httpx.Response:
        if space != self.SPACE_ID:
            return self._error(404, "notFound")
        self._root_polls += 1
        if self._root_polls <= self.root_enoent_polls:
            return self._answer(self.root_enoent_answer)
        return httpx.Response(200, json={"fileId": ROOT_ID, "name": self.space_name})

    def _attributes(self, node: Node) -> dict:
        """A file's attributes as Onedata 25.0 names them; `parent_id` is its parent's id, null for a space's root."""
        return {
            "fileId": node.file_id,
            "file_id": node.file_id,
            "name": node.name,
            "type": node.kind,
            "size": len(node.content),
            "parent_id": node.parent,
        }

    def add_tree(self, root_id: str, path: str) -> str:
        """Make a tree that is no part of the study space: a root of its own, with no parent, and a path below it;
        return the id of the path's last entry. It stands for a file of a space this configuration does not know."""
        self.nodes[root_id] = Node(root_id, "", "DIR", None)
        parent = root_id
        for part in path.strip("/").split("/"):
            parent = self._child(parent, part) or self._create(parent, part, "DIR")
        return parent

    def _list(self, node: Node, query: httpx.QueryParams, provider: str) -> httpx.Response:
        """A page of a directory's children, shaped as Onedata 25.0's (`LISTING_END_FACTS`): the keys `nextPageToken`,
        `isLast` and `children`, in that order; a token on every page, the last included; `isLast` true only on a page
        shorter than its limit, so a page the directory's last entries fill is followed by an empty last page; and a
        400 `posix` `eagain` for the page after a last page."""
        if node.kind != "DIR":
            return self._error(400, "posix")
        token = query.get("token")
        if token is not None and token.startswith(_LAST_PAGE_TOKEN):
            return self._error(400, "posix", "eagain")
        limit = min(int(query.get("limit", self.page_size)), self.page_size)
        if limit < 1:
            return self._error(400, "badValueNotInRange")
        shown = (
            n
            for n in self.nodes.values()
            if n.parent == node.file_id and self._visible(n, provider) and not self._left(n, provider)
        )
        children = sorted(shown, key=lambda n: n.name)
        start = int(token or "0")
        page = children[start : start + limit]
        last = len(page) < limit
        body: dict = {
            "nextPageToken": f"{_LAST_PAGE_TOKEN if last else ''}{start + len(page)}",
            "isLast": last,
            "children": [self._attributes(c) for c in page],
        }
        body = {key: value for key, value in {**body, **self.children_fields}.items() if key not in self.children_omit}
        return httpx.Response(200, json=body)

    def _make(self, parent: Node, query: httpx.QueryParams, provider: str) -> httpx.Response:
        name, kind = query.get("name"), query.get("type")
        if parent.kind != "DIR" or not name or kind not in ("REG", "DIR"):
            return self._error(400, "badValueNotAllowed")
        if self._child(parent.file_id, name, provider) is not None:
            return self._error(409, "posix")
        return httpx.Response(201, json={"fileId": self._create(parent.file_id, name, kind, provider)})

    def _put_content(self, request: httpx.Request, node: Node, provider: str) -> httpx.Response:
        """A write of content: 415 unless it says it is an octet stream when the shape requires that, a reset once the
        writes run out, and without `offset` a truncation before the bytes are written, as the API describes."""
        sent_type = request.headers.get("Content-Type")
        self.content_types.append(sent_type)
        if self.require_octet_stream and sent_type != "application/octet-stream":
            return self._error(415, "unsupportedMediaType")  # 25.0 refuses a raw body that does not say so
        if self.writes_before_reset is not None:
            if self.writes_before_reset <= 0:
                raise httpx.ConnectError("connection reset")
            self.writes_before_reset -= 1
        offset = request.url.params.get("offset")
        if offset is None:
            node.content = bytearray()
            for table in (self.blocks, self.pending):
                for key in [k for k in table if k[0] == node.file_id]:
                    del table[key]
        body = request.content
        if self.corrupt_writes and body:
            body = bytes([body[0] ^ 0xFF]) + body[1:]
        return self._write(node, provider, int(offset or "0"), body)

    def _write(self, node: Node, provider: str, offset: int, body: bytes) -> httpx.Response:
        end = offset + len(body)
        if len(node.content) < end:  # a write past the end pads with zeros, as a sparse file does
            node.content.extend(b"\0" * (end - len(node.content)))
        node.content[offset:end] = body
        if body:
            held = self.blocks.setdefault((node.file_id, provider), [])
            held.append([offset, len(body)])
            self.blocks[(node.file_id, provider)] = _merge(held)
        return httpx.Response(204)

    def _read(self, node: Node, provider: str, header: str | None) -> httpx.Response:
        found = re.fullmatch(r"bytes=(\d+)-(\d+)", header or "")
        typed = {} if self.content_type is None else {"Content-Type": self.content_type}
        if found is None:
            return httpx.Response(200, content=bytes(node.content), headers=typed)
        first, last = int(found.group(1)), min(int(found.group(2)), len(node.content) - 1)
        if first > last:
            return self._error(416, "rangeNotSatisfiable")
        length = last - first + 1
        if not self._holds(node.file_id, provider, first, length) and self.onthefly_places:
            placed = (
                length
                if self.onthefly_unit is None
                else min(max(length, self.onthefly_unit), len(node.content) - first)
            )
            self.pending.setdefault((node.file_id, provider), []).append([first, placed, self.fetch_lag])
            self._promote(node.file_id, provider)
        return httpx.Response(
            206,
            content=bytes(node.content[first : last + 1]),
            headers={"Content-Range": f"bytes {first}-{last}/{len(node.content)}", **typed},
        )

    def _holds(self, file_id: str, provider: str, offset: int, length: int) -> bool:
        return any(
            o <= offset and offset + length <= o + n for o, n in _merge(self.blocks.get((file_id, provider), []))
        )

    def _promote(self, file_id: str, provider: str) -> None:
        """Move the pending blocks whose lag has run out into the block map."""
        waiting = self.pending.get((file_id, provider), [])
        due = [p for p in waiting if p[2] <= 0]
        self.pending[(file_id, provider)] = [p for p in waiting if p[2] > 0]
        if due:
            held = self.blocks.setdefault((file_id, provider), [])
            held.extend([o, n] for o, n, _ in due)
            self.blocks[(file_id, provider)] = _merge(held)

    def _distribution(self, node: Node) -> dict:
        per: dict = {}
        for provider in self.providers:
            for waiting in self.pending.get((node.file_id, provider), []):
                waiting[2] -= 1
            self._promote(node.file_id, provider)
            held = self.blocks.get((node.file_id, provider), [])
            backend: dict = {"physicalSize": sum(n for _, n in held)}
            if not self.omit_blocks:
                backend["blocks"] = [list(b) for b in held]
            if provider not in self.omit_providers:
                per[provider] = {
                    "virtualSize": len(node.content),
                    "distributionPerStorageBackend": {"storage-1": backend},
                }
        return {"type": node.kind, "distributionPerProvider": per}

    def _schedule(self, request: httpx.Request, via: str) -> httpx.Response:
        """Schedule a job through the provider `via`. A file that provider answers as deleted, one whose delete is under
        way or that landed, is accepted, as seen live (`DELETE_FACTS`): the job ends at once with no file processed and
        places or removes nothing. A file id the fake never made is refused as an entry that is not there."""
        sent = json.loads(request.content or b"{}")
        kind = sent.get("type")
        provider = sent.get("evictingProviderId" if kind == "eviction" else "replicatingProviderId")
        source = sent.get("evictingProviderId") if kind == "migration" else None
        node = self.nodes.get(sent.get("fileId"))
        gone = (node is None and sent.get("fileId") in self.deleted_parents) or (
            node is not None and self._hidden(node, via)
        )
        if (
            kind not in ("replication", "eviction", "migration")
            or (node is None and not gone)
            or provider is None
            or (kind == "migration" and source is None)
        ):
            return self._error(400, "badValueNotAllowed")
        tid = f"tid-{next(self._ids):04d}"
        now = int(self.clock())
        self.transfers[tid] = Transfer(
            kind, sent["fileId"], provider, source_id=source, scheduled=now, started=now, deleted=gone
        )
        if gone:
            self.end_transfer(tid)
        return httpx.Response(201, json={"transferId": tid})

    def _list_transfers(self, space: str, query: httpx.QueryParams, provider: str) -> httpx.Response:
        if space != self.SPACE_ID:
            return self._error(404, "notFound")
        if query.get("state") not in ("waiting", "ongoing", "ended"):
            return self._error(400, "badValueNotAllowed")
        self.listings += 1
        if self.transfer_list_body is not None:
            return httpx.Response(200, json=self.transfer_list_body)
        ids = [
            tid
            for tid, t in self.transfers.items()
            if self._view(tid, provider).get("state", t.state) == query["state"]
            and self.listings > self.listing_lag.get(tid, 0)
        ]
        start = int(query.get("page_token") or "0")
        end = start + int(query.get("limit", "100"))
        token = str(end) if end <= len(ids) else None  # a full page gets a token, so an empty last page can follow
        return httpx.Response(200, json={"transfers": ids[start:end], "nextPageToken": token})

    def _transfer(self, tid: str, provider: str) -> httpx.Response:
        transfer = self.transfers.get(tid)
        if transfer is None:
            return self._error(404, "notFound")
        transfer.asked += 1
        if transfer.asked <= self.transfer_visible_after:
            return self._answer(self.transfer_unseen_answer)
        if not transfer.ended and transfer.asked > self.transfer_visible_after + self.transfer_flip_after:
            self.end_transfer(tid)
        state = "completed" if transfer.ended else "scheduled" if transfer.waiting else "active"
        size = len(self.nodes[transfer.file_id].content) if transfer.file_id in self.nodes else 0
        moved = transfer.bytes_replicated if transfer.bytes_replicated is not None else size
        files = 0 if transfer.deleted else 1  # a job for a file that was deleted processed none
        answer = {
            "evictionStatus" if transfer.kind == "eviction" else "replicationStatus": state,
            "type": transfer.kind,
            "fileId": transfer.file_id,
            "replicatingProviderId": transfer.provider_id if transfer.replicates else None,
            "evictingProviderId": transfer.provider_id if transfer.kind == "eviction" else transfer.source_id,
            "scheduleTime": transfer.scheduled,
            "startTime": transfer.started,
            "finishTime": transfer.finished,
            "bytesReplicated": moved if transfer.ended and transfer.replicates and files else 0,
            "filesReplicated": files if transfer.ended and transfer.replicates else 0,
            "filesEvicted": files if transfer.ended and transfer.kind != "replication" and not transfer.noop else 0,
            "filesFailed": 0,
        }
        view = self._view(tid, provider)
        if "state" in view:  # this provider lists the job in another state, and its record says so
            status = "evictionStatus" if transfer.kind == "eviction" else "replicationStatus"
            answer[status] = {"ended": "completed", "ongoing": "active", "waiting": "scheduled"}[view["state"]]
        answer.update({k: v for k, v in view.items() if k != "state"})
        return httpx.Response(200, json={k: v for k, v in answer.items() if k not in self.transfer_omit})

    def _view(self, tid: str, provider: str) -> dict:
        """What one provider says of a job where it differs from the job itself: its listed state, record fields."""
        return self.provider_view.get((tid, provider), {})

    def _finish(self, transfer: Transfer) -> None:
        if transfer.deleted:  # nothing to place or remove, and no eviction counted as a no-op
            return
        key = (transfer.file_id, transfer.provider_id)
        if transfer.replicates and self.transfers_place_blocks and transfer.file_id in self.nodes:
            size = len(self.nodes[transfer.file_id].content)
            self.pending.setdefault(key, []).append([0, size, self.fetch_lag])
            self._promote(*key)
        evicted = (transfer.file_id, transfer.source_id) if transfer.kind == "migration" else key
        if transfer.kind == "eviction" and self.evictions_noop_first > 0:  # completed, and nothing evicted
            self.evictions_noop_first -= 1
            transfer.noop = True
            return
        if transfer.kind != "replication" and self.evictions_remove_blocks:
            self.blocks.pop(evicted, None)
            self.pending.pop(evicted, None)


__all__ = ["ERRORS_25_0", "ERRORS_LEGACY", "ERROR_SHAPES", "PROVIDER", "ROOT_ID", "FakeData", "Node", "Transfer"]

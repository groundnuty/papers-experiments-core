# Provenance: new
"""The fake federation with its providers' data API: files, block maps, range reads, transfers and their delays."""

from __future__ import annotations

import itertools
import json
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.utils import formatdate

import httpx

from expcore_onedata.testing.fake_federation import FakeFederation
from expcore_onedata.testing.fake_onedata import Request
from expcore_onedata.testing.fake_qos import FakeQos

PROVIDER = "/api/v3/oneprovider"
ZONE_TIME = "/api/v3/onezone/provider/public/get_current_time"  # Onezone's reading of the global clock, public
ROOT_ID = "root-0001"
EPOCH_INFINITY = 9_999_999_999  # the transfer listing's keys count down from it (`expcore_onedata.transferorder`)
ZONE_CLOCK = "<the zone's clock>"  # `FakeData.zone_date`'s default: Onezone dates an answer by its clock

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
# a delete about 5 s later (the second), a directory delete finishing after the call returns and, it said, taking the
# files of a directory made again under its name (the third, a module docstring and not a dated fact; #29), and the
# 12 Nov check of the whole model (the fourth), which held, failed or left untested each claim: it reproduced no
# loss, so the third now backs only the opt-in `lose_recreated`.
DELETE_FACTS = (
    "deploy/compose-federation/day0/2026-11-10-federation-facts-parent-walk.md",
    "deploy/compose-federation/day0/2026-11-11-federation-facts-posix-and-fuse.md",
    "deploy/compose-federation/fedcontent.py",
    "deploy/compose-federation/day0/2026-11-12-federation-facts-pending-deletes.md",
)

# What a deleted entry answers when it is read by id (`FakeData`, "Tombstones"): the facts file that records it with its
# source references, and the live check of 12 Nov (merged separately), cited by path. The tombstone lives 14 days by
# default on every provider (cluster-worker's `document_expiry`, 1 209 600 s, from the source and not measured), so no
# test of this fake waits it out.
TOMBSTONE_FACTS = (
    "deploy/compose-federation/day0/2026-11-13-federation-facts-deleted-entries-by-id.md",
    "deploy/compose-federation/day0/2026-11-12-federation-facts-pending-deletes.md",
)
TOMBSTONE_S = 14 * 24 * 3600.0

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
    processed: int | None = None  # the files the record counts processed, when not the one file (a directory's)
    evicted: int | None = None  # the files an eviction record counts evicted, when not decided by `noop`
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


@dataclass
class _Tombstone:
    """What a deleted entry still answers to a lenient read: its attributes as they were when its delete landed, and
    when that was (on `clock`)."""

    attributes: dict
    deleted: float


# The attributes that, asked for alone, a deleted entry keeps answering (`FakeData`, "Tombstones"): the ones the
# platform resolves without reading the entry's document strictly. Any other, `name` and `type` included, is enoent.
# The id attribute is `file_id` on 25.0. `size` is traced for a directory only; for a file it is assumed to answer so.
LENIENT_ATTRIBUTES = frozenset({"size", "parent_id", "file_id"})

# Names the `attribute` parameter does not take on 25.0, refused before any lookup: `guid` is the internal name of the
# id, which the REST API calls `file_id` (ctool `onedata_file.erl`, no clause for it).
UNKNOWN_ATTRIBUTES = frozenset({"guid"})


def _merge(blocks: list[list[int]]) -> list[list[int]]:
    """Merge overlapping or adjacent [offset, length] blocks."""
    merged: list[list[int]] = []
    for off, length in sorted(blocks):
        if merged and off <= merged[-1][0] + merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], off + length - merged[-1][0])
        else:
            merged.append([off, length])
    return merged


class FakeData(FakeQos, FakeFederation):
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
                                                    newest first and ending as 25.0's do
      POST   /qos_requirements                      a requirement on a file or a directory (body fileId, expression)
      GET    /qos_requirements/{qid}                the requirement: qosRequirementId, fileId, expression, status
      DELETE /qos_requirements/{qid}                remove it (204; once gone, answered as `missing_answer`)
      GET    /data/{id}/qos/summary                 the requirements on the entry and its ancestors, keyed by id
      GET    /data/{id}/files                       the recursive listing (`attributes`, `include_directories`,
                                                    `limit`, `token`), with `effQosInheritancePath`

    and, from Onezone, `GET /spaces/{space}` (the live name). Time is counted in calls, and where a delay is in
    seconds it is seconds of the fake's own `clock`, never of the wall, so a test never waits. A space besides the
    study space has a tree of its own when it is added with `add_data_space`: every provider answers its root, and its
    entries are made and read by id like the study space's.

    **QoS requirements** (#206) are the minimum the reset's QoS stages need, as the 25.0 build's source answers them
    (`expcore_onedata.resetqos` cites it): a requirement is added (`add_qos`, or the POST) and kept until removed; a
    summary holds the requirements on the entry and its ancestors; the recursive listing gives each entry's
    `effQosInheritancePath` (`direct`, `ancestor`, both, or `none`), ends on `nextPageToken` null and has no
    `isLast`. A QoS replication creates no transfer document: with `qos_replicates_to` set, a new requirement's files
    land whole on that provider from `qos_land_after_s` seconds of `clock` on, until its removal reaches that provider.
    How a requirement reaches each provider (dbsync), the status it reports and how its replication ends are
    `fake_qos.FakeQos`'s, whose docstring says what is modelled and what is not. Assumed, not seen live: an expression
    is not evaluated, and only `qos_replicates_to` decides where a replication lands.

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
    the last, so a full page is followed by an empty one with a null token. A state's jobs are listed as the 25.0
    build lists them (`expcore_onedata.transferorder`): newest first, by the finish time of an ended job and the
    schedule time of a waiting or an ongoing one, each as the listing provider's record gives it, and a full page's
    token is the key of its last job, after which the next page resumes. The key is `9999999999 - T` and the job's
    whole id, where the build takes six characters of it: the fake's ids are not random, and six would tie.

    **Per-reader views.** A file or a directory is made on one provider, and every other provider shows it only
    after `visibility_delay_s` seconds of `clock` (`visibility_delay_by_provider` gives a reader its own; the default is
    0, at once). Until then that reader's attributes, children, content and block-map reads of it answer
    `unseen_answer`, and the listing of its parent leaves it out. About 6 s was seen live on Onedata 25.0. A test can
    also hold a file back from one reader for a number of reads instead of seconds (`unseen`, `unseen_after_create`):
    each read of the file through that provider answers `unseen_answer` until they are used up; the listing is not
    counted and shows the file.

    **Pending deletes** (`delete_delay_s`; the default 0 finishes every delete at once, everywhere, so a test that does
    not set it sees one change: a transfer of a deleted file's id is accepted, where it was refused 400). With a
    delay, a DELETE of a file or a directory is answered 204 and takes that many seconds of `clock` to land
    (`land_deletes()` lands them without waiting), so a study can test that its code waits before it uses the name
    again. In between, by what was recorded (`DELETE_FACTS`; the 12 Nov check, on a 25.0 development build, is
    `deploy/compose-federation/day0/2026-11-12-federation-facts-pending-deletes.md`):

    - Recorded. The provider the DELETE went to answers a deleted file, and what is under a deleted directory, as a
      deleted entry at once (`missing_answer`), except a read of its `parent_id` alone, which kept answering live
      (12 Nov facts); by the source, not live, the same holds for `file_id` alone, and for `size` alone of a
      directory ("Tombstones"). The providers the DELETE did not go to keep showing the entry, its files and its
      listing entry until it lands, and all of them stop in the same second. A listing through the DELETE's provider
      already leaves out a directory still going. A delete finishes after the call returns, a file's like a
      directory's. The create of a name made again meanwhile is accepted there, and it **loses nothing**: no file of
      a directory made again was lost or changed when the old delete landed (0 of 21, in four trials watched for
      120 s on every provider, by hash, and on the owner's storage in one), nor was a file made again. A transfer
      scheduled through the DELETE's provider for a file it answers as deleted is accepted, a transfer id and no
      error, and completes at once with 0 files processed, replicated or failed (a replication was tried; the fake
      does the same for an eviction and a migration, an assumption, and places or removes no block). The earlier
      assumptions (a) in reads and listings, (b) at once, (c), (e) and (g) held.
    - Corrected by that check; the fake had them wrong, from an earlier record and an assumption. A directory made
      again under the same name did not lose its files when the old delete landed: `lose_recreated=True` keeps that
      loss, for testing code that waits defensively, and it was not reproduced live. The check did not cover a file
      over 64 KiB, or a delete still working through many files when the name is made again, so a loss there is not
      ruled out. (h) A transfer for a deleted file was accepted, where the fake answered 400. (f) Corrected: a
      lagging provider shows only the old entry until the delete lands (12 Nov facts). No tick listed the old and the
      new entry side by side, and the mirrors switched from the old entry to the new one within one tick; the fake
      listed both, so a study that resolved a path through a mirror without waiting got the new entry, which live it
      did not. A mirror now lists, and resolves, only the old entry; the new one, and what is under it, shows in the
      step the old one goes. The provider the DELETE went to shows the new one at once.
    - Assumed, not seen live; a live check can confirm or correct each. (a) The providers the DELETE did not go to
      refuse the entry's name (409, as for any taken name), and a second DELETE through them changes nothing: no
      mutation was sent through them. (d) With `lose_recreated`, the loss takes files at any depth and no directory:
      nothing was lost live. (i) A provider that holds the new entry back (f) answers its id, and what is under it,
      as an entry it does not show yet (`unseen_answer`), as it does not list it: no read of a new entry's id through
      a mirror was made before the old one went.
    - Simplified. One `delete_delay_s` serves every provider; live, the mirrors stopped showing an entry after 3 s in
      four trials and 8 s in two. The owner answered a deleted directory's files as gone at once and the
      directory's own attributes for up to a second, under another parent and partly emptied, where the fake
      answers the directory gone at once. A new file showed on a mirror late, at size 0 or with only
      some of its directory's files, before about 8 s (lag, not loss): not modelled, since `visibility_delay_s` hides
      a new entry whole. In two trials a mirror showed neither entry of the name for some seconds between the old one
      and the new one; the fake switches in one step. Directory sizes followed a delete, and an addition, 5 s to 13 s
      late: not modelled.

    **Tombstones** (`tombstone_s`, `keep_deleted_parents`; `TOMBSTONE_FACTS`). On Onedata 25.0, `GET /data/{id}`
    resolves only the attributes it is asked for, and a deleted entry's document is read strictly for some and
    leniently for others. A read with no `attribute` parameter, or with `name` or `type` among the attributes, answers
    the entry as gone (`missing_answer`, 400 `posix` `enoent`); a read that asks for `size`, `parent_id` or `file_id`
    and nothing else (`LENIENT_ATTRIBUTES`) keeps answering 200 from the entry's last state, only the attributes asked
    for, on every provider, for `tombstone_s` seconds of `clock` counted from when the delete landed. Entries under a
    deleted directory have one each; a read of its children, content or block map is the entry as gone.

    Source, at op-worker f118ed9 (ctool 8ae7baf, cluster-worker 0e9cb40): strict `name` and the `file_meta`
    attributes, `src/modules/fslogic/operations/utils/file_attr.erl:170-189,203-213,577-578,620-628` through
    `src/modules/fslogic/context/file_ctx.erl:355-358` and
    `src/modules/datastore/models/file/metadata/file_meta.erl:219-227`; lenient `parent_id`, a directory's `size` and
    the permission check, `file_attr.erl:193-199,234-242,505-514`,
    `src/modules/fslogic/file_tree/file_tree.erl:236-240`, `file_ctx.erl:425-431`; the tombstone keeps its value and
    expires after `document_expiry`, `cluster-worker/src/cluster_worker.app.src:277-278`, 14 days by default and not
    measured, and replicates to every provider, `src/modules/datastore/datastore_doc.erl:211-235`,
    `src/modules/dbsync/dbsync_changes.erl:83-110` (all in
    `deploy/compose-federation/day0/2026-11-13-federation-facts-deleted-entries-by-id.md`). Live, on 12 Nov: a deleted
    directory read with `name`, `size` and `parent_id` together answered `enoent` from about 1 s, while `parent_id`
    alone still answered at 2.0 s (`deploy/compose-federation/day0/2026-11-12-federation-facts-pending-deletes.md`,
    merged with #192); another study's Day-0 reported a directory's `size` alone answering on every provider for 120 s
    (its report, not recorded in this repository).

    The default `tombstone_s` is `TOMBSTONE_S`, the platform's 14 days, so no test that advances the fake's clock
    reaches the expiry: a check of presence by `size`, `parent_id` or `file_id` alone never sees the entry go, and
    fails in a test, as it does live for `parent_id`, `file_id` and a directory's `size`. A test of the expiry itself
    sets a short `tombstone_s`. `ERRORS_LEGACY` keeps no tombstone.

    The id attribute is `file_id` on 25.0 and `file_id` alone answers the one key (a live read of `parent_id` and
    `file_id` answered those two keys and nothing else); `guid` is no name the parameter takes, so `?attribute=guid`
    answers 400 `badValueNotAllowed`, with no `errno`, for every entry, deleted or not, before any lookup
    (`UNKNOWN_ATTRIBUTES`; ctool `src/onedata_file.erl:253-270` has no clause for it, and `sanitize_attr_names`,
    `:134-153`, refuses it).

    Assumed, not recorded: that a deleted **file's** `size` alone answers from its tombstone as a directory's does, for
    `tombstone_s`. The source trace covered a directory's `size` (`resolve_location_attrs_for_dir`); the regular-file
    branch (`file_attr.erl:458-459`, `file_ctx:get_file_size` through the file location) was not read, and
    [inference] it may answer from the file location while that exists. The fake models a file as it does a directory,
    and a live check can correct that. Also assumed: that `size` is the entry's size when it was deleted.

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
      there yet, answer 400 with error id `posix` and `errno` `enoent`, while a deleted entry's tombstone still
      answers a read of its `size`, `parent_id` or `file_id` alone ("Tombstones"); a write made with a token that has
      the read-only caveat answers 400 `posix`/`eacces`; an
      upload (`PUT` of content) without `Content-Type: application/octet-stream` answers 415. The statuses and the
      `posix` errors are what was seen. A 415 was seen for an upload with no Content-Type at all; that this fake
      refuses any other type too, and the error id of its 415, `unsupportedMediaType`, are its own.
    - `ERRORS_LEGACY`: the shape this fake had before those observations: 404 `notFound` for an entry that is gone
      or not shown yet (404 `posix`/`enoent` for the space root), no tombstone for a deleted entry, 403 `forbidden`
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
    it), partial replication (a replication places the whole file, as Onedata's does), QoS beyond the above, file
    metadata other than the id, name, type, size and parent, storage backends beyond one per provider, concurrent
    writers, quotas, symbolic links, a token's space or interface caveats (only the read-only caveat is enforced, and
    `enforce_readonly=False` turns that off), a delete that other providers show late (unless `delete_delay_s` is set),
    a new file's lag on a mirror at size 0 or with only some of its siblings, directory sizes, the visibility delay for
    scheduling a transfer of a file the provider does not show yet (the job is accepted), eviction of part of a file
    (it removes all of a provider's blocks), other errors than those above, and any provider call that is not listed
    above.

    A test bends the fake through plain attributes:

    - `error_shape` (also a constructor argument): `ERRORS_25_0` or `ERRORS_LEGACY`; any other value is refused;
    - `visibility_delay_s` (also a constructor argument) and `visibility_delay_by_provider`: seconds of `clock` before
      a new file shows on a provider it was not made on, for all readers and for one;
    - `fetch_lag`: polls of the file's block map before bytes fetched by a range read or a replication show in it;
    - `onthefly_places`: False makes a range read answer without ever adding a block (a provider that does not cache);
    - `transfers_place_blocks`: False makes a replication end as `completed` with no block placed;
    - `evictions_remove_blocks`: False makes an eviction end as `completed` and leave the blocks;
      `evictions_noop_first` does that, with `filesEvicted` 0, for the next N evictions only;
    - `qos_covered`: `(file id, provider id)` pairs whose eviction is cancelled per file, as op-worker's is for a file
      a QoS requirement still covers on that provider: the job completes, the file keeps its blocks, and the record
      counts it in `filesProcessed` and not in `filesEvicted`, with `filesFailed` 0, as the platform does;
    - `onthefly_unit`: a range read through a provider lacking the bytes places at least this many from its offset;
    - `links`: `(reading provider id, holding provider id)` to `"hang"` or `"fail"`, a link between two providers that
      is stuck (#221): a range read through the reader of bytes it lacks, when every other provider holding them has
      its link to the reader so marked, either hangs, `hang_hook` given the read's timeout (a `Ticker`'s `sleep`, so the
      wait runs out on the test's clock) before it raises `httpx.ReadTimeout`, or answers `link_fail_answer` (500 by
      default; how a provider answers a failed fetch was not measured). One working link to a holder serves the read.
      Seen live: a stuck link left a read through the reader hanging past its bound with nothing logged, while every
      other pair transferred; the hang is that, the failure an assumption;
    - `joins_stuck_jobs`: True makes a read that hung a stuck fetch job of its reader (`stuck_jobs`, which a test
      empties to model the synchronizer's restart): every later read of that file through that reader hangs too, even
      once its link works, as op-worker's does (a later read of the same range joins the job, until its 1,800 s
      restart); a read of another file is unaffected;
    - `distribution_entries`: `(file id, provider id)` to what the block map holds for that provider instead of its
      own entry, such as a malformed one;
    - `transfer_flip_after`: polls of a transfer before it ends; `transfer_visible_after`: polls before it is found at
      all, each answered `transfer_unseen_answer`; a fresh space root answers `root_enoent_answer` for
      `root_enoent_polls` reads;
    - `resets`: a list of `[substring, count]` or `[substring, count, provider id]`: the next `count` provider calls
      whose route contains the substring (to that provider, when named) fail with a connection reset;
    - `statuses`: a list of `[substring, count, status]` likewise answering with that HTTP status;
    - `fail_deletes`, `ignore_deletes`: every DELETE answers 500, or answers 204 and removes nothing;
    - `delete_delay_s` (also a constructor argument): seconds of `clock` a delete stays under way after it is answered
      (0: none); `land_deletes()` ends every one now (see "Pending deletes");
    - `lose_recreated` (also a constructor argument): True makes the landing of a directory's delete take the files of a
      directory made again under its name meanwhile; off by default, since it was not reproduced live (12 Nov facts)
      and is kept for testing defensive waits (see "Pending deletes");
    - `enforce_readonly`: False lets a token with the read-only caveat write (a broken enforcement);
      `readonly_answer` is how such a write is refused;
    - `unseen` (`(provider id, file id)` to reads) and `unseen_after_create` (provider id to reads, for every file made
      from then on): a provider that sees a new file only after so many reads of it, each answered `unseen_answer`;
    - `missing_answer`: how a file or directory that is not there is answered; `keep_deleted_parents`: a deleted
      entry still answers a read of its `size`, `parent_id` or `file_id` alone from its tombstone (the name is kept from
      when only the parent link did); `tombstone_s` (also a constructor argument): how long, in seconds of `clock`
      from the delete landing (default `TOMBSTONE_S`, 14 days); `answer_missing_as_25(answer)` sets the first two, as
      25.0 has them;
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
    - `listing_time`: job id to the time its listing key is built from in place of its record's, which lists it out
      of the newest-first order while its record keeps its own times (a broken order, for a reader's check);
    - `qos` (requirement id to its record), `ignore_qos_deletes` (a removal answers 204 and the requirement stays),
      `qos_hidden_on` (requirement id to the provider ids that have not seen it: their summaries, listings and reads
      leave it out), `qos_replicates_to` and `qos_land_after_s` (where and when a new requirement's files land), and
      `FakeQos`'s: `qos_sync_s` and `qos_sync_by_provider` (an addition's or a removal's dbsync to another provider),
      `qos_land_every_s`, `qos_missed_cancel`, `qos_status_of`, and what happened, `qos_removed` and `qos_landed`;
    - every data-API answer has a `Date` header of `clock` moved by `provider_skew` (provider id to seconds ahead,
      negative when behind); `date_override` (provider id to a header value, or None for none) replaces it. A job's
      times are `clock`'s, the platform's global clock, so a provider's skew moves its Date headers alone, as a
      provider's system clock does;
    - every answer of Onezone's has a `Date` header of `clock` moved by `zone_skew` (seconds ahead, negative when
      behind; 0 by default, Onezone's system clock agreeing with the global one), and `zone_date` replaces it as
      `date_override` does a provider's (`ZONE_CLOCK`, the default, sends the clock's). The header is Onezone's
      answering node's system clock: `zone_skew` 8 with the global clock agreeing with this host's is a multi-node
      Onezone whose node runs 8 s ahead with a bias of -8 s;
    - `GET /provider/public/get_current_time`, Onezone's reading of the global clock, answers with no credential (and
      with any) `{"timeMillis": N}`, `clock` in epoch milliseconds: `clock` is the global clock, the one a job's times
      are stamped on, so a test that wants this host to disagree with it gives the code under test a clock of its own.
      `time_status` makes that call alone fail with that status, `time_hook` replaces its answer's body (a shape that
      cannot be read), and its `Date` header is Onezone's like any other's.

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
        tombstone_s: float = TOMBSTONE_S,
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
        self.tombstone_s = tombstone_s  # seconds of `clock` a tombstone answers, from its delete landing
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
        self.qos_covered: set[tuple[str, str]] = set()  # (file id, provider id): its eviction there is cancelled
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
        self.tombstones: dict[str, _Tombstone] = {}  # a deleted entry's last state, read if `keep_deleted_parents`
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
        self.listing_time: dict[str, float] = {}  # job id -> the time its listing key is built from (a broken order)
        self.provider_skew: dict[str, float] = {}  # provider id -> seconds its clock runs ahead (negative: behind)
        self.date_override: dict[str, str | None] = {}  # provider id -> the Date header it sends (None: none)
        self.zone_skew = 0.0  # seconds Onezone's Date headers run ahead of `clock` (negative: behind)
        self.zone_date: str | None = ZONE_CLOCK  # the Date header Onezone sends (None: none; ZONE_CLOCK: its clock's)
        self.time_status: int | None = None  # makes Onezone's current-time read alone fail with this status
        self.time_hook: Callable[[dict], object] | None = None  # replaces the body of Onezone's current-time answer
        self.listings = 0  # the listing answers given
        self.qos: dict[str, dict] = {}  # requirement id -> its record (`add_qos`, `POST /qos_requirements`)
        self.ignore_qos_deletes = False  # a requirement's removal answers 204 and the requirement stays
        self.qos_hidden_on: dict[str, set[str]] = {}  # requirement id -> provider ids that have not seen it yet
        self.qos_replicates_to: str | None = None  # a new requirement replicates its files into this provider id
        self.qos_land_after_s = 0.0  # seconds of `clock` before that replication's bytes land, with no transfer
        self.space_roots: dict[str, str] = {self.SPACE_ID: ROOT_ID}  # space id -> its root's id (`add_data_space`)
        # (reading provider id, holding provider id) -> "hang" or "fail": a link between two providers that is stuck
        self.links: dict[tuple[str, str], str] = {}
        self.hang_hook: Callable[[float], None] | None = None  # given the read's timeout before a stuck read times out
        # With `joins_stuck_jobs`, a read that hung is a stuck fetch job of its reader: (reader id, file id) is kept
        # here, and any later read of that file through that reader hangs too, whatever `links` says, until `clear`.
        self.joins_stuck_jobs = False
        self.stuck_jobs: set[tuple[str, str]] = set()
        # (file id, provider id) -> what the block map holds for that provider instead of its entry (a malformed one)
        self.distribution_entries: dict[tuple[str, str], object] = {}
        self.link_fail_answer: Answer = (500, "internalServerError", None)  # a read whose only link failed
        self._init_qos_model()  # dbsync, status and replication of a requirement (`fake_qos.py`)
        self._qos_ids = itertools.count(1)
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
        helpers ask), and is found by the others, which still show it, and which do not show a name made again beside
        it (`_held_back`)."""
        with self._lock:
            self._settle_deletes()
            held = self._held_back(provider)
            found = (
                n
                for n in self.nodes.values()
                if n.parent == parent and n.name == name and not self._left(n, provider) and n.file_id not in held
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

    def _lingers(self, node: Node, provider: str) -> bool:
        """Tell whether `provider` still shows an entry whose delete was sent through another provider and has not
        landed: it answers the entry as it was, and lists it."""
        pending = self._deleting.get(node.file_id)
        return pending is not None and pending.via != provider and self._visible(node, provider)

    def _held_back(self, provider: str | None) -> set[str]:
        """The ids of the entries `provider` does not show yet, because it still shows an entry of the same name,
        made before them, whose delete has not landed there: a name deleted through one provider and made again at
        once is listed by another as the old entry alone until the delete lands, and then as the new entry alone, in
        one step (seen live, `DELETE_FACTS`: old and new were never listed side by side). The provider the DELETE went
        to answers the old entry as deleted, so it holds nothing back, and a test's helpers (`None`) hold nothing back.
        Entries are visited in the order they were made, which is the order of `nodes`."""
        if provider is None or not self._deleting:
            return set()
        shown_old: set[tuple[str | None, str]] = set()
        held: set[str] = set()
        for node in self.nodes.values():
            key = (node.parent, node.name)
            if key in shown_old:
                held.add(node.file_id)
            elif self._lingers(node, provider):
                shown_old.add(key)
        return held

    def _held_above(self, node: Node, provider: str) -> bool:
        """Tell whether an entry, or a directory above it, is held back from `provider` (`_held_back`)."""
        held = self._held_back(provider)
        current: Node | None = node
        while held and current is not None:
            if current.file_id in held:
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
        self.tombstones[file_id] = _Tombstone(self._attributes(self.nodes[file_id]), self.clock())
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
        """Answer what is not there as Onedata 25.0 does: 400 posix enoent instead of a 404, while a deleted entry still
        answers a read of its size, parent link or `file_id` alone from its tombstone (the parent link was read on the
        Compose federation).
        `answer` replaces the 400."""
        self.missing_answer = answer
        self.keep_deleted_parents = True

    def _missing(self, file_id: str, query: httpx.QueryParams, method: str, part: str) -> httpx.Response:
        """A file or directory that is not there: `missing_answer`, except a read of the bare entry that asks for
        `LENIENT_ATTRIBUTES` only, which its tombstone answers (`FakeData`, "Tombstones")."""
        kept = self._tombstone_read(file_id, query) if method == "GET" and part == "" else None
        return self._answer(self.missing_answer) if kept is None else httpx.Response(200, json=kept)

    def _tombstone_read(self, file_id: str, query: httpx.QueryParams) -> dict | None:
        """What a deleted entry's tombstone answers to this read, or None when it answers the entry as gone: the read
        names no attribute or one that is not lenient, the shape keeps no tombstone, or `tombstone_s` is up. Only the
        attributes asked for are answered, as the platform resolves only those."""
        asked = query.get_list("attribute")
        if not self.keep_deleted_parents or not asked or not set(asked) <= LENIENT_ATTRIBUTES:
            return None
        live = self.nodes.get(file_id)  # a delete under way: the entry has not been tombstoned yet and reads as it is
        if live is not None:
            attributes = self._attributes(live)
        else:
            tombstone = self.tombstones.get(file_id)
            if tombstone is None or self.clock() >= tombstone.deleted + self.tombstone_s:
                return None
            attributes = tombstone.attributes
        return {name: attributes[name] for name in ("size", "parent_id", "file_id") if name in asked}

    def _answer(self, answer: Answer) -> httpx.Response:
        """An error answer from (status, error id, errno); an error id of None is a body that is no error object."""
        status, error_id, errno = answer
        if error_id is None:
            return httpx.Response(status, text="<html><body>Bad Request</body></html>")
        return self._error(status, error_id, errno)

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer a provider call here and anything else as the federation does, dated by Onezone's clock."""
        if request.method == "GET" and request.url.path == ZONE_TIME:
            return self._zone_dated(self._current_time(request))
        if not request.url.path.startswith(PROVIDER):
            return self._zone_dated(super().respond(request))
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

    def _current_time(self, request: httpx.Request) -> httpx.Response:
        """Onezone's reading of the global clock, `clock`, in epoch milliseconds, which anyone may ask for: the request
        is recorded, and the answer is the one `time_status` or `time_hook` gives instead when a test sets either."""
        with self._lock:
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
        if self.status_override is not None:
            return self._error(self.status_override, "internalServerError")
        if self.time_status is not None:
            return self._error(self.time_status, "internalServerError")
        answer = {"timeMillis": int(self.clock() * 1000)}
        return httpx.Response(200, json=self.time_hook(answer) if self.time_hook else answer)

    def _dated(self, response: httpx.Response, provider: str) -> httpx.Response:
        """Give an answer the Date header of the provider's clock: the fake's clock moved by the provider's skew, in
        whole seconds as HTTP has it, or the override a test set (None: no header at all)."""
        if provider in self.date_override:
            if self.date_override[provider] is not None:
                response.headers["Date"] = self.date_override[provider]
            return response
        response.headers["Date"] = formatdate(self.clock() + self.provider_skew.get(provider, 0.0), usegmt=True)
        return response

    def _zone_dated(self, response: httpx.Response) -> httpx.Response:
        """Give an answer of Onezone's the Date header of its clock (`clock` moved by `zone_skew`), or `zone_date`."""
        if self.zone_date == ZONE_CLOCK:
            response.headers["Date"] = formatdate(self.clock() + self.zone_skew, usegmt=True)
        elif self.zone_date is not None:
            response.headers["Date"] = self.zone_date
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
        self._advance_qos()
        found = re.fullmatch(r"/spaces/([^/]+)", route)
        if found and method == "GET":
            return self._space_root(found.group(1))
        qos = self._qos_route(request, provider, route)
        if qos is not None:
            return qos
        found = re.fullmatch(r"/data/([^/]+)(/children|/content|/distribution)?", route)
        if found:
            if method == "GET" and not found.group(2) and UNKNOWN_ATTRIBUTES & set(query.get_list("attribute")):
                return self._error(400, "badValueNotAllowed")  # the names are checked before the entry is looked up
            node = self.nodes.get(found.group(1))
            if node is None or self._hidden(node, provider):  # a delete through this provider that is under way: gone
                return self._missing(found.group(1), query, method, found.group(2) or "")
            if self._held_above(node, provider) or not self._shown(node, provider):  # it does not see the new entry yet
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
                return self._read(node, provider, request.headers.get("Range"), request)
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

    # -- QoS requirements and the recursive listing -------------------------------
    def add_qos(self, path: str, *, expression: str = "anyStorage", through: str | None = None) -> str:
        """Put a requirement on an entry, as an agent's QoS tool would, and return its id; `through` is the provider
        id it is added through, which the others see `qos_sync_s` later (None: everywhere at once)."""
        return self._new_qos(self.id_of(path), expression, through)

    def _new_qos(self, file_id: str, expression: str, through: str | None = None) -> str:
        """Record a requirement; with `qos_replicates_to`, its files land there from `qos_land_after_s` on, as a QoS
        replication's do: with no transfer document, until its removal reaches that provider (`fake_qos.py`)."""
        qid = f"qos-{next(self._qos_ids):04d}"
        self.qos[qid] = {"qosRequirementId": qid, "fileId": file_id, "expression": expression, "replicasNum": 1}
        self._qos_added_at(qid, through)
        if self.qos_replicates_to is not None:
            self._qos_schedule(qid, file_id, self.qos_replicates_to)
        return qid

    def _regular_under(self, file_id: str) -> list[str]:
        """The regular files at or under an entry."""
        node = self.nodes.get(file_id)
        if node is None or node.kind != "DIR":
            return [file_id] if node is not None else []
        return [
            f for child in self.nodes.values() if child.parent == file_id for f in self._regular_under(child.file_id)
        ]

    def _visible_qos(self, provider: str) -> dict[str, dict]:
        """The requirements a provider has: not hidden from it, reached by their addition and not by their removal."""
        seen = [q for q in self.qos if provider not in self.qos_hidden_on.get(q, ()) and self._qos_reaches(q, provider)]
        return {q: self.qos[q] for q in seen}

    def _qos_route(self, request: httpx.Request, provider: str, route: str) -> httpx.Response | None:
        """The QoS calls and the recursive listing, or None for another call."""
        method = request.method
        if route == "/qos_requirements" and method == "POST":
            sent = json.loads(request.content or b"{}")
            if sent.get("fileId") not in self.nodes or not sent.get("expression"):
                return self._error(400, "badValueNotAllowed")
            qid = self._new_qos(sent["fileId"], sent["expression"], provider)
            return httpx.Response(201, json={"qosRequirementId": qid})
        found = re.fullmatch(r"/qos_requirements/([^/]+)", route)
        if found and method in ("GET", "DELETE"):
            record = self._visible_qos(provider).get(found.group(1))
            if record is None:
                return self._answer(self.missing_answer)  # a 404, or 25.0's 400 posix enoent
            if method == "DELETE" and not self.ignore_qos_deletes:
                self._qos_remove(found.group(1), provider)
            return (
                httpx.Response(204)
                if method == "DELETE"
                else httpx.Response(200, json={**record, "status": self._qos_status(found.group(1))})
            )
        found = re.fullmatch(r"/data/([^/]+)/(qos/summary|files)", route)
        if not found or method != "GET":
            return None
        node = self.nodes.get(found.group(1))
        if node is None or self._hidden(node, provider):
            return self._answer(self.missing_answer)
        if found.group(2) == "files":
            return self._list_recursive(node, request.url.params, provider)
        chain = set(self._ancestry(node.file_id))
        requirements = {q: self._qos_status(q) for q, r in self._visible_qos(provider).items() if r["fileId"] in chain}
        status = next((s for s in ("impossible", "pending") if s in requirements.values()), "fulfilled")
        return httpx.Response(200, json={"requirements": requirements, "status": status})

    def _ancestry(self, file_id: str) -> list[str]:
        """An entry's id and its ancestors' ids, up to its root."""
        chain, node = [], self.nodes.get(file_id)
        while node is not None:
            chain.append(node.file_id)
            node = self.nodes.get(node.parent) if node.parent else None
        return chain

    def _inheritance(self, file_id: str, provider: str) -> str:
        """An entry's `effQosInheritancePath`: whether a requirement is on it, on an ancestor, on both, or none."""
        on = {r["fileId"] for r in self._visible_qos(provider).values()}
        direct, above = file_id in on, bool(on & set(self._ancestry(file_id)[1:]))
        return {(True, True): "directAndAncestor", (True, False): "direct", (False, True): "ancestor"}.get(
            (direct, above), "none"
        )

    def _list_recursive(self, top: Node, query: httpx.QueryParams, provider: str) -> httpx.Response:
        """A page of the recursive listing under an entry, as the 25.0 build answers it (`resetqos.py` cites the
        source): in path order, its regular files, and with `include_directories` its directories and the entry itself
        as "."; each with the `attributes` asked for (`fileId` and `path` by default); `nextPageToken` null on the last
        page, no `isLast`, and `inaccessiblePaths` empty. A null `token` is refused, as the build reads it as "null"."""
        if query.get("token") == "null":
            return self._error(400, "badValueNotAllowed")
        directories = query.get("include_directories") == "true"
        entries: list[tuple[str, Node]] = [(".", top)] if directories else []
        pending = [(top, "")]
        while pending:
            parent, prefix = pending.pop()
            for child in sorted((n for n in self.nodes.values() if n.parent == parent.file_id), key=lambda n: n.name):
                if self._hidden(child, provider) or not self._shown(child, provider):
                    continue
                path = f"{prefix}{child.name}"
                if child.kind == "DIR":
                    pending.append((child, f"{path}/"))
                if child.kind != "DIR" or directories:
                    entries.append((path, child))
        entries.sort(key=lambda item: item[0])
        start, limit = int(query.get("token") or "0"), int(query.get("limit", "1000"))
        page = entries[start : start + limit]
        asked = query.get_list("attributes") or ["fileId", "path"]
        files = []
        for path, node in page:
            known = {"fileId": node.file_id, "path": path, "type": node.kind, "name": node.name}
            known["effQosInheritancePath"] = self._inheritance(node.file_id, provider)
            files.append({k: known[k] for k in asked if k in known})
        token = str(start + limit) if start + limit < len(entries) else None
        return httpx.Response(200, json={"files": files, "inaccessiblePaths": [], "nextPageToken": token})

    def add_data_space(self, space_id: str, name: str, supports: Iterable[str]) -> None:
        """Add a space with a tree of its own: Onezone knows it and a token's scope shows it supported by these provider
        ids (`add_space`), and every provider answers its root (`space_roots`). Its entries are made and read through
        the data API, by id, as the study space's are; `add_file` and `id_of` stay the study space's. It stands for a
        space no study code touches, such as the link check's probe space."""
        self.add_space(space_id, name, list(supports))
        root_id = f"root-{space_id}"
        with self._lock:
            self.nodes[root_id] = Node(root_id, "", "DIR", None)
            self.space_roots[space_id] = root_id

    def _space_root(self, space: str) -> httpx.Response:
        if space in self.space_roots and space != self.SPACE_ID:
            return httpx.Response(200, json={"fileId": self.space_roots[space], "name": self.other_spaces[space]})
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
        held = self._held_back(provider)
        shown = (
            n
            for n in self.nodes.values()
            if n.parent == node.file_id
            and self._visible(n, provider)
            and not self._left(n, provider)
            and n.file_id not in held
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

    def _read(self, node: Node, provider: str, header: str | None, request: httpx.Request) -> httpx.Response:
        found = re.fullmatch(r"bytes=(\d+)-(\d+)", header or "")
        typed = {} if self.content_type is None else {"Content-Type": self.content_type}
        if found is None:
            return httpx.Response(200, content=bytes(node.content), headers=typed)
        first, last = int(found.group(1)), min(int(found.group(2)), len(node.content) - 1)
        if first > last:
            return self._error(416, "rangeNotSatisfiable")
        length = last - first + 1
        if not self._holds(node.file_id, provider, first, length):
            stuck = self._stuck_link(node.file_id, provider, first, length)
            if stuck == "hang":
                if self.joins_stuck_jobs:
                    self.stuck_jobs.add((provider, node.file_id))
                wait = (request.extensions.get("timeout") or {}).get("read")
                if self.hang_hook is not None:
                    self.hang_hook(float(wait) if wait is not None else 0.0)  # the wait runs out, then the read
                raise httpx.ReadTimeout("the link to the provider holding the bytes is stuck", request=request)
            if stuck == "fail":
                return self._answer(self.link_fail_answer)
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

    def _stuck_link(self, file_id: str, reader: str, offset: int, length: int) -> str | None:
        """How a read through `reader` that lacks these bytes fares by `links`: "hang" or "fail" when every other
        provider holding them has its link to the reader so marked ("hang" if any does), None when one link works or
        no other provider holds them."""
        if (reader, file_id) in self.stuck_jobs:  # the fetch of this file through this reader is still outstanding
            return "hang"
        holders = [p for p in self.providers if p != reader and self._holds(file_id, p, offset, length)]
        marks = [self.links.get((reader, holder)) for holder in holders]
        if not marks or None in marks:
            return None
        return "hang" if "hang" in marks else "fail"

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
                per[provider] = self.distribution_entries.get(
                    (node.file_id, provider),
                    {"virtualSize": len(node.content), "distributionPerStorageBackend": {"storage-1": backend}},
                )
        return {"type": node.kind, "distributionPerProvider": per}

    def _schedule(self, request: httpx.Request, via: str) -> httpx.Response:
        """Schedule a job through the provider `via`. A file that provider answers as deleted is accepted, and the job
        ends at once with no file processed and places or removes nothing. Seen live (`DELETE_FACTS`): a replication
        scheduled through the deleting provider for a file whose delete was under way. Extrapolated from that, not
        seen: a file whose delete has landed, a directory's id, an eviction or a migration, and a job scheduled through
        another provider after the delete landed there. A file id the fake never made is refused as an entry that is
        not there."""
        sent = json.loads(request.content or b"{}")
        kind = sent.get("type")
        provider = sent.get("evictingProviderId" if kind == "eviction" else "replicatingProviderId")
        source = sent.get("evictingProviderId") if kind == "migration" else None
        node = self.nodes.get(sent.get("fileId"))
        gone = (node is None and sent.get("fileId") in self.tombstones) or (
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
        return self._transfer_page(query, provider, lambda tid, transfer: True)

    def _listing_key(self, tid: str, transfer: Transfer, provider: str, state: str) -> str:
        """A job's key in a provider's listing: `9999999999 - T`, ten digits, then its id, where T is the finish time
        of an ended job and the schedule time of another, as that provider's record gives them (`listing_time` in
        their place)."""
        view = self._view(tid, provider)
        if tid in self.listing_time:
            moment = self.listing_time[tid]
        elif state == "ended":
            moment = view.get("finishTime", transfer.finished)
        else:
            moment = view.get("scheduleTime", transfer.scheduled)
        return f"{EPOCH_INFINITY - int(moment or 0):010d}{tid}"

    def _transfer_page(
        self, query: httpx.QueryParams, provider: str, include: Callable[[str, Transfer], bool]
    ) -> httpx.Response:
        """One page of a provider's listing of the jobs `include` admits in the state asked for: newest first, in the
        keys' order, from just after the `page_token`'s key; a full page's token is its last job's key."""
        state = query["state"]
        keyed = sorted(
            (self._listing_key(tid, t, provider, state), tid)
            for tid, t in self.transfers.items()
            if include(tid, t)
            and self._view(tid, provider).get("state", t.state) == state
            and self.listings > self.listing_lag.get(tid, 0)
        )
        after = query.get("page_token")
        page = [(key, tid) for key, tid in keyed if not after or key > after][: int(query.get("limit", "100"))]
        token = page[-1][0] if page and len(page) == int(query.get("limit", "100")) else None  # an empty last page
        return httpx.Response(200, json={"transfers": [tid for _, tid in page], "nextPageToken": token})

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
            "filesProcessed": (files if transfer.processed is None else transfer.processed) if transfer.ended else 0,
            "filesEvicted": self._files_evicted(transfer, files),
            "filesFailed": 0,
        }
        view = self._view(tid, provider)
        if "state" in view:  # this provider lists the job in another state, and its record says so
            status = "evictionStatus" if transfer.kind == "eviction" else "replicationStatus"
            answer[status] = {"ended": "completed", "ongoing": "active", "waiting": "scheduled"}[view["state"]]
        answer.update({k: v for k, v in view.items() if k != "state"})
        return httpx.Response(200, json={k: v for k, v in answer.items() if k not in self.transfer_omit})

    @staticmethod
    def _files_evicted(transfer: Transfer, files: int) -> int:
        """The record's `filesEvicted`: the job's own count, else all its files for an ended eviction but a no-op."""
        if not transfer.ended or transfer.kind == "replication":
            return 0
        if transfer.evicted is not None:
            return transfer.evicted
        return 0 if transfer.noop else files

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
        if transfer.kind == "eviction" and key in self.qos_covered:  # cancelled per file: processed, not evicted
            transfer.evicted = 0
            return
        if transfer.kind != "replication" and self.evictions_remove_blocks:
            self.blocks.pop(evicted, None)
            self.pending.pop(evicted, None)


__all__ = ["ERRORS_25_0", "ERRORS_LEGACY", "ERROR_SHAPES", "PROVIDER", "ROOT_ID", "FakeData", "Node", "Transfer"]

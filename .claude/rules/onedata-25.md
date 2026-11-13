---
paths:
  - "src/expcore_onedata/**"
  - "deploy/compose-federation/**"
  - "tests/onedata/**"
  - "tests/live/**"
---

# Onedata 25.0 as measured, and the live federation

The images are a 25.0 development build (op-worker `f118ed9`), not the released 25.0 tag: a dated facts file names it as "25.0 development build (op-worker f118ed9)", never just "25.0" (`day0/2026-11-12-federation-facts-build.md`).

Behaviours measured live on 25.0 (dated facts in `deploy/compose-federation/day0/`), which the platform layer must handle:
- A deleted or not-yet-visible entry answers 400 with error id `posix`/`enoent`, not 404. Treat exactly that id, or 404, as absent; any other 400 fails closed.
- A write with a read-only token answers 400 `posix`/`eacces`: a refusal, like 401 and 403.
- An upload without a `Content-Type` answers 415; send `application/octet-stream`.
- `GET /data/{id}?attribute=path` is refused: find a file's space by walking `parent_id` to the space root.
- A new file is invisible on another provider for about 6 s: wait, bounded, before reading or transferring through that provider.
- An eviction scheduled within about a second of a placement settling can complete with `filesEvicted` 0: judge eviction only by the block map, and retry, bounded.
- A range read places a 1 MiB window from the read's own offset, clipped to the file's end, aligned to nothing. Record held bytes from the block map, never the request.
- Deleting a directory is asynchronous: never reuse a name at once.
- A deleted entry is gone only when a read with no attribute selection (or with `name` or `type`) answers `enoent`; a read of `parent_id` or `file_id` alone, or of `size` alone (a directory's by the source, a file's seen live by another study), keeps answering from a tombstone on every provider, by default (`document_expiry`, source) for 14 days, not measured; a file's `size` alone was not traced and may answer while its file location exists. Never test absence with any of those. The id attribute is `file_id`, and `?attribute=guid` is a 400 `badValueNotAllowed` (facts: `2026-11-13-federation-facts-deleted-entries-by-id.md`).
- A dated facts file in `deploy/compose-federation/day0/` is **sealed once its PR merges**: studies pin their evidence by each file's SHA-256. A later finding or correction goes in a new dated file that names the file it amends; never edit a merged one.
- A children listing ends only on `isLast: true` (a JSON boolean, false on every earlier page); `nextPageToken` is a string on every page, the last included, and an exactly full last page is followed by an empty one. A transfers listing has no `isLast`: it ends on `nextPageToken: null`. Never treat a missing marker as the end (facts: `2026-11-12-federation-facts-listing-end.md`).
- Ended transfers never expire and cannot be removed (`DELETE /transfers/{id}` is a cancel), so a space's ended listing grows with every run and reset. The transfers listing is newest first, by `finishTime` for `ended` and `scheduleTime` for `waiting`/`ongoing`, and a page token resumes after the last job its page gave (source, `transfer_links.erl:160-164`; not in the REST description). Read a run's transfers as `transferorder.newest_first` does, down to the window and checking the order; never read the whole history, and never build a page token by hand (#207, O32).
- There is no listing of a space's QoS requirements, and a QoS replication creates no transfer document: no transfer listing, cancel or wait reaches it, so wait it out on the block maps (`resetqos.py`). A directory's physical size lags a placement by up to 10 s plus 5 s per directory level above the file's parent (source: `blocks_flush_delay`, `dir_stats_collector` flush), and an eviction only by the per-level part; never take an occupancy read right after a placement, or a zero, as proof (#209, O33).
- Through the mount, oneclient 25.0 names no file in its logs, and `--io-trace-log` crashes it on the first read: per-file served bytes exist only on the REST path (the gate).
- A token with any data-access caveat (`data.path`, `data.objectid`, `data.readonly`, or interface `oneclient`) is usable for data access alone: `POST /transfers` and its own `POST /tokens/examine` answer `tokenCaveatUnverified` (#177). A token that must write carries none.

The live federation (`deploy/compose-federation/`, Docker Compose):
- One live federation session at a time on this host. Stay within the project's own containers and networks, never prune, publish ports on 127.0.0.1 only, keep data under the federation's root, never print a secret, and leave everything down and verified at the end.
- If an agent stops mid-pass, check `ps` for a `federation.py` process and `docker ps -a` for the project before anything else. `up` is idempotent; let an orphaned `up` finish, then run `down`.

---
paths:
  - "src/expcore_onedata/**"
  - "deploy/compose-federation/**"
  - "tests/onedata/**"
  - "tests/live/**"
---

# Onedata 25.0 as measured, and the live federation

Behaviours measured live on 25.0 (dated facts in `deploy/compose-federation/day0/`), which the platform layer must handle:
- A deleted or not-yet-visible entry answers 400 with error id `posix`/`enoent`, not 404. Treat exactly that id, or 404, as absent; any other 400 fails closed.
- A write with a read-only token answers 400 `posix`/`eacces`: a refusal, like 401 and 403.
- An upload without a `Content-Type` answers 415; send `application/octet-stream`.
- `GET /data/{id}?attribute=path` is refused: find a file's space by walking `parent_id` to the space root.
- A new file is invisible on another provider for about 6 s: wait, bounded, before reading or transferring through that provider.
- An eviction scheduled within about a second of a placement settling can complete with `filesEvicted` 0: judge eviction only by the block map, and retry, bounded.
- A range read places a 1 MiB window from the read's own offset, clipped to the file's end, aligned to nothing. Record held bytes from the block map, never the request.
- Deleting a directory is asynchronous: never reuse a name at once.
- A children listing ends only on `isLast: true` (a JSON boolean, false on every earlier page); `nextPageToken` is a string on every page, the last included, and an exactly full last page is followed by an empty one. A transfers listing has no `isLast`: it ends on `nextPageToken: null`. Never treat a missing marker as the end (facts: `2026-11-12-federation-facts-listing-end.md`).
- Through the mount, oneclient 25.0 names no file in its logs, and `--io-trace-log` crashes it on the first read: per-file served bytes exist only on the REST path (the gate).

The live federation (`deploy/compose-federation/`, Docker Compose):
- One live federation session at a time on this host. Stay within the project's own containers and networks, never prune, publish ports on 127.0.0.1 only, keep data under the federation's root, never print a secret, and leave everything down and verified at the end.
- If an agent stops mid-pass, check `ps` for a `federation.py` process and `docker ps -a` for the project before anything else. `up` is idempotent; let an orphaned `up` finish, then run `down`.

# The Compose federation's facts, 2026-11-12: the provider-outage check, rerun on the writing token

The second live run of the day-0 `provider_outage` check (#108, O28) **passed** (exit status 0). The data provider was
stopped through the fault injector, seen down and restored. While it was down, the agent's four tools all answered,
a file with a replica at the mirror read whole through the local provider over REST and through the mount, and a file
held by the stopped provider alone neither errored nor timed out on its own: both paths were still waiting at the 60 s
bound, and the mount read outlived its kill. The first run (`2026-11-12-federation-facts-provider-outage.md`) came
out unknown because `schedule_file_replication` was refused on a data-only token; on the writing token of #180 it was
accepted with everything up and during the outage.

Every value below is a field of the check's own report (`facts` of `provider_outage`) unless a section says otherwise.
Providers appear in the report by position; the roles are the report's `roles`, and the names are the layout's own
invented ones.

## The slot

| Fact | Value |
|---|---|
| Step start (UTC) | 17:38 |
| Check run (UTC) | 17:54:24 to 18:00:35; report `day0-20261112T180034Z`, 367.4 s, exit status 0 |
| Branch | `feat/108-day0-outage` at `0f614aa` (main `85bce7c`, with #180's writing token and #188's read retry) |
| Build | 25.0 development build (op-worker f118ed9) |
| Federation | the bundled slot's layout, already up: the study space owned by its data provider and mirrored by two; the local provider owns no space; a fourth provider owns two other spaces, both mirrored by the data provider |
| Roles (`roles`) | target `providers[0]` = `pine` (data provider, `[placement] holder`), mirror `providers[1]` = `birch`, local `providers[2]` = `cedar` (`[support] local`) |
| Admission | the federation held exclusively (`admission.exclusive`: true) |
| Sandbox | started in mount mode and stopped (`agent.sandbox`) |
| Tool server | started and stopped (`agent.tool_server`), condition `knowledge`, allow-list the four knowledge tools; nothing missing (`knowledge.missing_tools`: none) |
| Token ceiling | 17678 s (`agent.tokens.ceiling_s`) |
| Bound per answer | 60 s (`bound_s`); scratch files of 16384 bytes (`bytes`) |
| Scratch removed | yes (`cleanup_verified`: true) |

How the derived platform file was made, none of it committed (assumptions of this slot, not code changes):

- A copy of the federation root's rendered `platform.toml`, written into the root and removed after the run. The
  rendered file already had the slot's sanctioned `[faults]` (the journal in the root, the layout's generated Compose
  file), two `[slots]`, `[support] local = "cedar"` and `[placement] holder = "pine"`.
- The copy narrowed `[faults.services]` to `pine`, the only service the injector could stop, and `[slots]` to the
  writing slot. The launcher requires the leased tool-server user's privileges to equal the condition's rights, and
  the read-only slot's user lacks `space_schedule_replication`.
- `[toolserver]`: the real onedata-mcp server at the OMCP pin of `docs/inventory.md` (`0cc17ce`), from an existing
  local checkout with its own environment (`clone` and `commit` set, the pin verified clean), nothing installed; one
  condition allowing the four knowledge tools, with the writing slot user's six rights (`space_view`,
  `space_read_data`, `space_view_transfers`, `space_schedule_replication`, `space_view_qos`, `space_manage_qos`). The
  first run declared four rights; this layout has no user with exactly those four. The manifest is the first run's,
  frozen from the same server with the same four tools.
- `[sandbox]` as `tests/support/mount_live.py` derives one: the sidecar on the project network dialling `cedar`;
  `python:3.12-slim` and the `onedata/oneclient` image on the host, both pinned by id, nothing pulled; the pool a
  private range the host routes nowhere.

## Verdict

**Passed.** The check's summary: "providers[0] was taken down, seen down and restored; through the local provider a
file held only there was blocked over REST and blocked through the mount, a replicated file served, the listing
served; get_file_distribution served; the scratch was removed".

For a study's design: reads of a file with a replica at a provider that stays up fail over without the agent doing
anything, on both data paths, at all-up speed. A file held only by the stopped provider gives the agent no error to
act on within 60 s on either path, so a read timeout has to come from the study (a client timeout over REST); through
the mount, the reading process did not end within 10 s of being killed while the provider was down.

## The agent's tools during the outage (`during.knowledge`)

Each tool was called through the tool server while the target was down; every call took one attempt and none timed
out. Answers and errors are kept as digests (a SHA-256 prefix with every known name and id-like token replaced), their
length and the error terms they hold.

| Tool | File | Outcome | Seconds (all up) | Answer (digest, length, error terms) | What it shows |
|---|---|---|---|---|---|
| `get_file_distribution` | `knowledge/replicated` | served | 3.065 (0.351) | `1dbc738da87db71d`, 1514 chars, none | target not listed; mirror 16384 bytes, local 0 |
| `get_file_distribution` | `knowledge/owner_only` | served | 3.059 (0.351) | `830973cee3bc2f00`, 1413 chars, none | target not listed; mirror 0, local 0 |
| `list_space_providers` | | served | 0.041 (0.034) | `efbc2b4d3237de9f`, 1189 chars, none (the all-up digest) | names all three providers, the stopped target included |
| `schedule_file_replication`, to local | `knowledge/replicated` | **accepted** (served) | 0.038 (0.035) | `7b345d2d24e49987`, 56 chars, none (the all-up digest) | |
| `get_transfer` of that job | | served, ended | 2.074 over 2 calls | last call `5a3655e61532eae0`, 1644 chars, none | `enqueued` at 0.037 s, `completed` at 2.074 s |
| `schedule_file_replication`, to local | `knowledge/owner_only` | **accepted** (served) | 0.041 (0.035) | `7b345d2d24e49987`, 56 chars, none | |
| `get_transfer` of that job | | **blocked**: not ended within the bound | 61.029 over 31 calls | last call `1eb8498b60f18cb0`, 1027 chars, none; each call served, the last in 0.029 s | `enqueued` at 0.029 s and nothing after |

The all-up times are `baseline.knowledge`, where `get_file_distribution` was called on `baseline/owner_only`. Every
`get_file_distribution` with the target down took 3.054 to 3.065 s (six calls, including the four below), against
0.351 s with everything up. Both jobs scheduled during the outage were `completed` after the restore
(`after.transfers`).

## Where each read was served from (`during.knowledge.from_where`)

The agent's `get_file_distribution` of the very file read, called while the target was down, in bytes:

| File read | Target | Mirror | Local |
|---|---|---|---|
| `rest/replicated` | not listed | 16384 | 16384 |
| `rest/owner_only` | not listed | 0 | 0 |
| `mount/replicated` | not listed | 16384 | 16384 |
| `mount/owner_only` | not listed | 0 | 0 |

Before the stop the local provider held none of these files (`held_before`, below), so each replicated read placed the
whole file at the local provider, and with the target down the mirror was the only other holder. The owner-only reads
placed nothing.

## The mount during the outage (`during.mount`)

The sandbox's read-only oneclient mount through the local provider:

| Cell | Outcome | Bytes | Hash as written | Blocked | Killed | Errno | Seconds (all up) |
|---|---|---|---|---|---|---|---|
| `replicated` | served | 16384 of 16384 | yes | no | | | 0.288 (0.397) |
| `owner_only` | **blocked** | 0 of 16384 | | **yes** | **no** | none recorded | 70.067 (0.334) |
| `listing` | served: 10 of 10 entries, none missing or unexpected | | | no | | | 0.243 (0.273) |

The owner-only read was still running at its 60 s deadline. The cell then killed it and waited 10 s more (its kill
wait, hence 70.067 s); the read had still not ended (`killed`: false). It returned no bytes and no errno.

## REST during the outage (`during.rest`, `outage.rest`)

Through the local provider, with the data path's token:

| Cell | Outcome | Bytes | Hash as written | Status, error id, errno | Seconds (all up, cold) |
|---|---|---|---|---|---|
| `replicated` | served | 16384 of 16384 | yes | | 0.065 (0.255) |
| `owner_only` | **blocked**: no answer at the bound | 0 of 16384 | | none: no status, no error id, no errno; a transport-level end (`transport`: true) | 60.048 (0.285) |
| `listing` | served: 10 of 10 entries, none missing, unexpected or of another size | | | | 0.024 (0.013) |

The target itself answered no REST call while down (`outage.rest`: no answer after 0.002 s, a transport error, no
status; `rest_answers_while_down`: false).

## The outage and the restore (`outage`, `restore`, `after`)

| Fact | Value |
|---|---|
| Stop | sent through the injector (`stop_sent`), the target seen down (`seen_down`), its identity recorded before as a digest |
| Restore | the injection cleared and the restore verified; `how`: "restored"; the container's identity changed; journal steps `sanctioned`, `injected`, `cleared`, `verified` |
| Time to ready | **48.9 s** (`ready_after_s`: 48.93) until the injector found the target serving; then REST answered on the first poll, after 0.027 s |
| Re-read, owner-only file, REST | served, 16384 of 16384, hash as written, 4.121 s, 1 attempt |
| Re-read, replicated file, REST | served, 16384 of 16384, hash as written, 0.023 s, 1 attempt |
| Re-read, listing, REST | served, 10 of 10 entries, none missing, unexpected or of another size, 0.017 s |
| Re-read, owner-only file, mount | served, 16384 of 16384, hash as written, not blocked, 0.293 s |
| Jobs scheduled during the outage | both `completed` (`after.transfers`) |
| Scratch | removed (`cleanup_verified`: true) |

## All providers up (`baseline`)

REST, `baseline.rest`:

| Cell | Through | Outcome | Bytes | Hash as written | Seconds |
|---|---|---|---|---|---|
| `target_owner_only` | target | served | 16384 of 16384 | yes | 0.020 |
| `mirror_replicated` | mirror | served | 16384 of 16384 | yes | 0.116 |
| `local_replicated` | local, cold | served | 16384 of 16384 | yes | 0.255 |
| `local_owner_only` | local, cold | served | 16384 of 16384 | yes | 0.285 |
| `listing` | local | served: 10 of 10 entries, none missing, unexpected or of another size | | | 0.013 |

Mount, `baseline.mount`:

| Cell | Outcome | Bytes | Hash as written | Blocked | Seconds |
|---|---|---|---|---|---|
| `replicated` | served | 16384 of 16384 | yes | no | 0.397 |
| `owner_only` | served | 16384 of 16384 | yes | no | 0.334 |
| `listing` | served: 10 of 10 entries, none missing or unexpected | | | no | 0.273 |

The agent's tools, `baseline.knowledge`, each called once, none timed out, no error terms:

| Tool | File | Outcome | Seconds | Answer (digest, length) |
|---|---|---|---|---|
| `get_file_distribution` | `baseline/owner_only` | served; all three providers listed: target 16384, mirror 0, local 16384 (after the local cold read) | 0.351 | `a35bb63b34b81dc6`, 1213 chars |
| `list_space_providers` | | served; names all three | 0.034 | `efbc2b4d3237de9f`, 1189 chars |
| `schedule_file_replication`, to local | `baseline/owner_only` | **accepted** (served) | 0.035 | `7b345d2d24e49987`, 56 chars |
| `get_transfer` of that job | | served: `enqueued` at 0.032 s, `completed` at 2.061 s, 2 calls | 2.061 | last call `16220a93a743cf5a`, 1038 chars |

Who held what before the stop (`held_before`, from the local provider's own block map, bytes):

| File | Target | Mirror | Local |
|---|---|---|---|
| each `replicated` file (`rest`, `mount`, `knowledge`, `baseline`, `baseline_mount`) | 16384 | 16384 | 0 |
| each `owner_only` file (the same five) | 16384 | 0 | 0 |

The scenario held as designed: the target held every file whole, the mirror the replicated ones, the local provider
nothing.

## Checked outside the check, after the run

Not fields of the report:

- All five services of the federation healthy; the target's container started again at 17:59:14 UTC, through the
  injector; every provider answered REST.
- A CPU-only test run of another project on the same host, meant to pause during this step, started at 17:59:24 UTC:
  10 s after the target's container was started, before the injector found it serving. The time to ready, the
  re-reads and the scratch's removal overlapped it; every read and tool call during the outage came before it.
- The fault journal's last entry for the injection is `cleared`, `restored`; no injection open.
- The sandbox's own teardown record: the mount unmounted on the first attempt, no reader left and no process in
  uninterruptible sleep, the sidecar and the agent removed, 1.4 s in all. By then the blocked mount read had ended.
- No `fedsbx` container or network and no `fuse.oneclient` mount left; the study space's root on all three providers
  held only the federation's own content; no day-0, tool-server or oneclient process left.
- The derived platform file and the sandbox's work directory removed from the root. The federation stays up.

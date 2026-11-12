# The Compose federation's facts, 2026-11-12: a writing tool-server token on the real tool server (#177)

The live run of #177's fix (PR #180) on the pinned onedata-mcp server. **A writing condition writes, within its
boundary:** its replication returned a transfer that completed and its QoS requirement was accepted, in its space; the
same tools were refused with 403 `forbidden` for a file of a space it does not hold, where nothing appeared; and a
membership of an outside space or of a provider's cluster stopped both the launch and a new mint. **A read-only
condition is refused every write, but also three of its five read tools:** the transfer views and the QoS requirement
view answer 401 `unauthorized` with `tokenCaveatUnverified`, while the file distribution and the file QoS summary
serve. That contradicts O05's live item (c), which expects every read tool to answer. The data path is unchanged.

Every name below is the layout's own invented one; no id, token, address or host is recorded.

## The slot

| Fact | Value |
|---|---|
| Slot start, end (UTC) | 09:23, 09:48 |
| Branch | `fix/177-tool-server-write-token` at `fe1ff0c` |
| Federation | the default layout plus two lease slots' four users, and `pine` as a third supporter of `beta-space` (below), brought up with `--layout` |
| Tool server | the real onedata-mcp server at the OMCP pin of `docs/inventory.md`, from an existing local checkout with its own environment (`clone` and `commit` set, the pin verified clean); nothing installed |
| Launch | the core's `ToolServerLauncher.launch`, with the slot's tool-server user as `principal`; tokens minted by `TokenIssuer.mint_run_tokens` for the slot's two users |
| Token ceiling | 900 s per mint |
| Allow-list, both conditions | `schedule_file_replication`, `add_file_qos_requirement`, `get_file_distribution`, `get_transfer`, `list_space_transfers`, `get_file_qos_summary`, `get_qos_requirement`; the manifest frozen from the server with these seven |

How the derived platform files were made, none of it committed (assumptions of this slot, not code changes):

- **The layout** is the default one with four more principals, all in `alpha-space`: per slot a data-path user
  (`space_view`, `space_read_data`) and a tool-server user. The writing slot's holds exactly the writing condition's
  rights (`space_view`, `space_read_data`, `space_view_transfers`, `space_schedule_replication`, `space_view_qos`,
  `space_manage_qos`), the read-only slot's exactly the read-only condition's (`space_view`, `space_read_data`,
  `space_view_transfers`, `space_view_qos`). Neither is a member of `beta-space`.
- **`beta-space` gained `pine` as a mirror,** so the tool server's local provider supports the space the writing
  condition does not hold, and a refusal there is about rights, not support. The first `up` made the federation as
  declared; a second `up` with the edited layout added the support.
- **Three platform files** beside the rendered one, in the root: one with `[toolserver]` (both conditions) and
  `[slots]` (both slots) for checks (a) to (e); and one per condition, without `[slots]`, whose
  `[principals.toolserver]` is that condition's slot user and whose `[toolserver]` declares that condition alone, for
  the day-0 checks and the live tests (a fixed user stands for one condition's rights only).
- **The pinned server has no eviction tool** (its write tools are file, QoS and replication ones), so (c)'s eviction
  was sent as a direct `POST /transfers` of type eviction with the read-only condition's token, not through the tool
  server.
- **The server's own examination** of its token is recorded twice: the request it makes at its start (`POST
  /tokens/examine` on Onezone, sent as the token itself) made directly, and whether the server's log holds its
  fail-open warning ("Could not examine Oneprovider token via Onezone").

## Verdicts

| Check | Verdict | Key statuses and error ids |
|---|---|---|
| (a) the writing condition writes in its space | **pass** | replication returned a transfer id, the transfer `completed`; QoS requirement accepted; own examination 200, no fail-open warning |
| (b) the same tools on a space it does not hold | **pass** | both 403 `forbidden`; no transfer, no QoS requirement there |
| (c) the read-only condition | **fail** | writes refused (401 `unauthorized`, `tokenCaveatUnverified`); distribution and QoS summary served; `get_transfer`, `list_space_transfers` and `get_qos_requirement` refused with the same 401 `tokenCaveatUnverified` |
| (d) the new reads | **pass** | `infer_access_token_scope` 200, exactly the plane's one space; admin `effective_privileges` 200, empty; the token's own `effective_clusters` 200, empty |
| (e) an outside space, a provider's cluster | **pass** | launch refused (`tool_plane_integrity`) and new mint refused, each time; both mint again after removal; no private name in any message |
| (f) the data path | **pass** | day-0 `tokens` and `read_only` pass for both conditions; the live tests of the new endpoints and of the data gate pass; one live test outside #177 failed (directory eviction, below) |

## (a) The writing condition, in its space

The writing slot's tokens minted in 0.12 s; the tool-server token's caveats are its expiry and the `rest` interface.
The server started in 2.96 s and showed the seven tools. File: `alpha-space/samples/sample-1.bin`, held by `pine`.

| Call | Outcome | Seconds |
|---|---|---|
| `schedule_file_replication` to `birch` | served: a transfer id | 0.195 |
| `get_transfer`, polled every 2 s | `completed`, 65536 bytes, 1 file | ended within 22.5 |
| `add_file_qos_requirement`, `providerId` of `cedar`, 1 replica | served: a requirement id | 0.133 |
| `get_qos_requirement` of it | served: `pending` at once | 0.116 |
| `get_file_distribution` | served | 0.103 |

The server's own examination: the direct `POST /tokens/examine` as the token answered 200, and the server's log has no
fail-open warning. The tool server stopped, and the launcher's check at teardown passed (no event, no error).

## (b) The same tools on a space the condition does not hold

File: `beta-space/beta.txt`, owned by `birch`, supported by `pine`, the tool server's provider.

| Call | Outcome | Seconds |
|---|---|---|
| `schedule_file_replication` to `cedar` | refused: `POST /transfers` 403 `forbidden` | 0.268 |
| `add_file_qos_requirement` | refused: `POST /qos_requirements` 403 `forbidden` | 0.123 |

Read with the admin credential through `birch`, before the calls and 3 s after: `beta-space` lists 0 transfers in
every state (`waiting`, `ongoing`, `ended`) both times, and the file's QoS summary (200) holds no requirement both
times.

## (c) The read-only condition

The read-only slot's tokens minted in 0.14 s; the tool-server token carries the four caveats of before (`time`,
`interface`, `data.path`, `data.readonly`). The server started in 3.06 s and showed the seven tools: its own
examination failed, so its token policy kept the write tools (fail-open, as O05 says). File:
`alpha-space/samples/sample-2.bin`.

| Call | Kind | Outcome | Seconds |
|---|---|---|---|
| `schedule_file_replication` to `birch` | write | refused: `POST /transfers` 401 `unauthorized`, `tokenCaveatUnverified` | 0.369 |
| `add_file_qos_requirement` | write | refused: `POST /qos_requirements` 401 `unauthorized`, `tokenCaveatUnverified` | 0.162 |
| eviction, direct `POST /transfers` (no tool) | write | refused: 401 `unauthorized`, `tokenCaveatUnverified` | |
| `get_file_distribution` | read | served | 0.165 |
| `get_file_qos_summary` | read | served | 0.031 |
| `get_transfer` (the transfer of (a)) | read | **refused**: `GET /transfers/{id}` 401 `unauthorized`, `tokenCaveatUnverified` | 0.148 |
| `list_space_transfers`, `ended` | read | **refused**: `GET /spaces/{id}/transfers` 401 `unauthorized`, `tokenCaveatUnverified` | 0.122 |
| `get_qos_requirement` (the requirement of (a)) | read | **refused**: `GET /qos_requirements/{id}` 401 `unauthorized`, `tokenCaveatUnverified` | 0.150 |

The read tools that serve are those under `/data/{id}/`; the transfer and QoS requirement views are not data access,
and a token with a data-access caveat is refused there as it is for a write. The server's own examination answered
401 `unauthorized` with `tokenCaveatUnverified`, directly and in the server's log, which holds the fail-open warning
once. Read with the admin credential through `pine`, 3 s after the calls: no new transfer in `alpha-space`, and the
file's QoS summary holds no requirement, as before.

## (d) The new reads, as the writing condition's token and the admin

| Read | Sent as | Status | Answer |
|---|---|---|---|
| `POST /tokens/infer_access_token_scope` | the token | 200 | exactly the plane's one space (`tokenscope.scope_spaces`) |
| `GET /users/{id}/effective_privileges` | the admin credential | 200 | empty (`tokenscope.admin_privileges`) |
| `GET /user/effective_clusters` | the token | 200 | empty (`tokenscope.effective_clusters`) |

## (e) The writing slot's user made a member of an outside space, and of a provider's cluster

A writing token was minted first; then, with the admin credential, the user was added to `beta-space` with
`space_view` (201), and Onezone's scope of that token showed the space at the first read.

| Step | Outcome |
|---|---|
| launch with the token minted before | refused before the start, `InfraError` `tool_plane_integrity`: "conditions[0]: the tool plane failed its check before the start: the token's scope differs from the plane's spaces: has also 1 space the configuration does not declare" (0.07 s) |
| a new mint | refused, `DroppedToken`: "the token's scope differs from the plane's spaces: has also 1 space the configuration does not declare" |
| membership removed (204), scope back to the one space at the first read | a new mint succeeds |
| the user added to `cedar`'s cluster with `cluster_view` (201), the token's clusters read non-empty at the first read | launch refused, `tool_plane_integrity`: "... the principal is an effective member of 1 cluster" (0.05 s); a new mint refused, `DroppedToken`, the same wording |
| cluster membership removed (204) | a new mint succeeds |

No message names a space, a provider, a user or an id: each was checked against every name, id and address the
federation's facts hold.

## (f) The data path

Day-0, `--only tokens --only read_only` with `EXPCORE_LIVE_MUTATE=1`, once per condition's platform file:

| Platform file | `tokens` | `read_only` |
|---|---|---|
| the writing condition's | pass: the data path's four caveats (`time`, `interface`, `data.path`, `data.readonly`), the writing token's two (`time`, `interface`) | pass: the write refused on all 3 supporting providers |
| the read-only condition's | pass: the data path's four, the read-only token's four | pass: the write refused on all 3 supporting providers |

Live contract tests, `tests/live/test_contract.py`, on the writing condition's platform file with
`EXPCORE_LIVE_CONTENT_PATH=hello.txt`, `EXPCORE_LIVE_MUTATE=1` and `EXPCORE_LIVE_SLOW=1`: 21 passed, 1 failed, 2
skipped (the reset and the fault tests, whose gates were not set), in 146 s. Passed among them: every test marked as
covering the new endpoints (`POST /tokens/examine`, `POST /tokens/infer_access_token_scope`, `GET
/users/{id}/effective_privileges`, `GET /user/effective_clusters`), each condition's tool-server mint, and the data
gate's content test (`GET /data/{file}/content`, whole and by range).

**The one failure is outside #177:** `test_a_directory_eviction_covers_every_file_under_it` found the directory
emptied, both files evicted, after 2 eviction transfers where it asserts 1 (`(2, 2, True) == (2, 1, True)`).
`evict_tree` schedules the directory's eviction again when an attempt leaves bytes, which is 25.0's measured early
eviction (`.claude/rules/onedata-25.md`), so the test's exact count is stricter than the layer it tests. PR #180
changes neither `eviction.py` nor that test. It was not rerun.

On the read-only condition's platform file, the condition's mint and the admin-privileges read (`[]`) passed; the
clusters test skipped, as it needs a condition with a write right. 2 passed, 1 skipped, in 1 s.

## Timings

| Command | Seconds |
|---|---|
| first `up --layout` | 203 (prepare 2, zone 56, providers 74, spaces 6, baseline 66) |
| second `up --layout` (already up; added the support) | 3 |
| (a) and (b), mint to the server's stop | 32 wall; server start 2.96, up to its stop 26.86 |
| (c), mint to the server's stop | 11 wall; server start 3.06, up to its stop 4.66 |
| (e), all steps | 4 wall |
| day-0, each platform file | 1 |
| live contract tests, the writing condition's file | 146 |
| `down --layout` | 37 (containers 35, data 2) |

## Teardown

`federation.py down --layout` (the same layout) took 37 s and read each service's peak memory: `zone` 0.99 GiB,
`pine` 1.19 GiB, `birch` 1.13 GiB, `cedar` 1.05 GiB. Afterwards, checked outside the sandbox: no container, network
or volume whose name holds `expcore` or `fedsbx`, none labelled with the Compose project, the root gone, no
`fuse.oneclient` mount or mount of the project, and no `federation.py`, day-0 or tool-server process. No sandbox was
started in this run.

## What this run does not show

- **An eviction through the tool server:** the pinned server has none; only the direct REST call was refused.
- **Whether a read-only condition can be given its transfer and QoS views at all:** with a data-access caveat
  Onedata 25.0 refuses them, and without one the token is no longer read-only. #177's fix leaves the read-only
  token's caveats byte-identical, so this is not a regression of it, but O05's live item (c) cannot pass as written.

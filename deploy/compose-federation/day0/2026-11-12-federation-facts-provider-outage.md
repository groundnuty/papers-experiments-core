# The Compose federation's facts, 2026-11-12: the provider-outage check, first live run

The first live run of the day-0 `provider_outage` check (#108, O28) came back **unknown** (exit status 3): nothing was
taken down. With every provider up, the agent's `schedule_file_replication` was refused, so the check could not prove
its scenario and stopped before the injector sent a stop. The REST reads, the mount reads, the listing and the other
two tools served. Every value below is a field of the check's own report (`facts` of `provider_outage`); "not
measured" means the check never reached that step. Every name is the layout's own invented one.

## The slot

| Fact | Value |
|---|---|
| Slot start, end (UTC) | 07:38, 07:54 |
| Branch | `feat/108-day0-outage` at `0c5cab6` |
| Check run (UTC) | report `day0-20261112T075115Z`, 59.8 s, exit status 3 |
| Federation | the default layout, plus one lease slot's two users (below), brought up with `--layout` |
| Roles | target `pine` (data provider, `[placement] holder`), local `birch` (`[support] local`), mirror `cedar` |
| Admission | the federation held exclusively (`admission.exclusive`: true) |
| Sandbox | started in mount mode at the level `mounted`, and stopped (`agent.sandbox`) |
| Tool server | started and stopped (`agent.tool_server`), condition `knowledge`, allow-list the four knowledge tools |
| Token ceiling | 17678 s (`agent.tokens.ceiling_s`) |
| Bound per answer | 60 s (`bound_s`); scratch files of 16384 bytes (`bytes`) |
| Scratch removed | yes (`cleanup_verified`: true) |

How the derived platform file was made, none of it committed (assumptions of this slot, not code changes):

- The default layout copied with two more principals, a data-path user with `space_view` and `space_read_data` and a
  tool-server user with exactly the condition's rights (`space_view`, `space_read_data`, `space_view_transfers`,
  `space_schedule_replication`), so `up` made their credentials and `[slots]` could name them. No layout declares
  slot users, and the check leases them.
- The root's `platform.toml` with `[support] local = "birch"`, `[placement] holder = "pine"`, the README's sanctioned
  `[faults]` table (journal and Compose file beside it in the root, `pine` the only service the injector may stop), a
  `[sandbox]` table as `tests/support/mount_live.py` derives one (the sidecar on the project network, dialling
  `birch`; `python:3.12-slim` and the `onedata/oneclient` image present by digest, both pinned by id, nothing pulled),
  one `[slots]` slot and a `[toolserver]` running the real onedata-mcp server at the OMCP pin of `docs/inventory.md`
  (`clone` and `commit` set, the pin verified), its manifest frozen from that server with the four tools.
- The first `up` stopped at `pine`'s panel answering 500 on its storages list right after the providers came online;
  the second `up`, which is idempotent, finished in 69 s.

## Verdict

**Unknown.** The check's summary: "nothing was taken down: the scenario could not be set up or proved (the knowledge
tools did not serve with everything up (schedule_file_replication))". No outage was measured, so this run gives no
read-timeout setting and no failover result.

## All providers up (`baseline`)

REST, `baseline.rest`:

| Cell | Through | Outcome | Bytes | Hash as written | Seconds |
|---|---|---|---|---|---|
| `target_owner_only` | `pine` | served | 16384 of 16384 | yes | 0.026 |
| `mirror_replicated` | `cedar` | served | 16384 of 16384 | yes | 0.132 |
| `local_replicated` | `birch`, cold | served | 16384 of 16384 | yes | 0.196 |
| `local_owner_only` | `birch`, cold | served | 16384 of 16384 | yes | 0.069 |
| `listing` | `birch` | served: 10 of 10 entries, none missing, unexpected or of another size | | | 0.013 |

Mount, `baseline.mount` (the sandbox's read-only oneclient mount through `birch`):

| Cell | Outcome | Bytes | Hash as written | Blocked | Seconds |
|---|---|---|---|---|---|
| `replicated` | served | 16384 of 16384 | yes | no | 0.335 |
| `owner_only` | served | 16384 of 16384 | yes | no | 0.289 |
| `listing` | served: 10 of 10 entries, none missing or unexpected | | | no | 0.242 |

The agent's tools through the tool server, `baseline.knowledge`, each called once (`attempts` 1), none timed out:

| Tool | File | Outcome | Seconds | Error class, terms | Answer or error (digest, length) |
|---|---|---|---|---|---|
| `get_file_distribution` | `baseline/owner_only` | served; lists all three providers, two holding 16384 bytes, the third 0 | 0.284 | | answer `d610cf184cc0587a`, 1213 chars, no error terms |
| `list_space_providers` | | served; names all three providers | 0.035 | | answer `efbc2b4d3237de9f`, 1189 chars, no error terms |
| `schedule_file_replication`, to `birch` | `baseline/owner_only` | **refused** | 0.29 | `ToolError`, `unauthorized` | error `af1412b96db3a70e`, 1719 chars |
| `get_transfer` | | not called: no job was accepted | | | |

The distribution's providers are listed by position (`providers[0..2]`); the report does not say which position is
which provider, so the holders are not named here.

Who held what before anything would have been stopped (`held_before`, from `birch`'s own block map, bytes):

| File | `pine` | `birch` | `cedar` |
|---|---|---|---|
| each `replicated` file (`rest`, `mount`, `knowledge`, `baseline`, `baseline_mount`) | 16384 | 0 | 16384 |
| each `owner_only` file (the same five) | 16384 | 0 | 0 |

The scenario held as designed: the target held every file whole, the mirror the replicated ones, the local provider
nothing.

## During the outage, and the restore

Not measured: nothing was stopped (no `outage`, `during`, `restore` or `after` in the report).

| Step of #108 | Result |
|---|---|
| `pine` stopped (`outage`) | not measured: no stop was sent |
| Mirrored file over REST and the mount (`during.*.replicated`), served from where | not measured |
| Owner-only file over REST and the mount (`during.*.owner_only`): error id, errno, seconds, blocked | not measured |
| Listing over REST and the mount (`during.*.listing`) | not measured |
| The four tools during the outage (`during.knowledge`) | not measured |
| Restore (`restore`): how, `ready_after_s`, seconds until `pine` answers REST | not measured: nothing to restore |
| Re-reads after the restore (`after`) | not measured |

`pine`'s container ran through the whole check without a restart, and the fault journal held no injection afterwards.

## Teardown

`federation.py down --layout` (the same layout) took 36 s (containers 34 s, data 2 s) and read each service's peak
memory: `zone` 1.15 GiB, `pine` 1.16 GiB, `birch` 1.03 GiB, `cedar` 1.06 GiB. Afterwards, checked outside the
sandbox: no container, network or volume whose name holds `expcore` or `fedsbx`, none labelled with the Compose
project or the sandbox's lease, the root gone, no `fuse.oneclient` mount and no mount of the project or the sandbox,
and no `federation.py`, day-0 or tool-server process.

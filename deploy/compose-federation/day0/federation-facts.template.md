# The Compose federation's facts, <date>

A template: copy it to `<YYYY-MM-DD>-federation-facts.md` for a live pass, beside the day-0 checks' own report
(`day0-<UTC>.md` and `.json`), and replace each "unknown (not measured)" the pass measured. "Unknown" means not
measured; it is never a pass. Every name below is the layout's own invented one.

## The deployment

One table per layout run. The rows down to "Memory limits together" are what
`federation.py --layout <layout> describe` prints, pasted unchanged; it records the layout file and its digest, the
root's allowed parent and which setting gave it, and each service's memory limit.

| Fact | Value |
|---|---|
| Layout | `<file>`, SHA-256 `<first 16 hex digits>` |
| Compose project | `<project>`, domain `<domain>` |
| Root parent | `<path>` (default, or EXPCORE_FED_ROOT_PARENT) |
| Root | `<path>` |
| Onezone | `zone` on 127.0.0.1:`<port>`, memory limit `<limit>` |
| Oneprovider `<name>` | 127.0.0.1:`<port>`, memory limit `<limit>` (one row per provider) |
| Space `<name>` | owner `<provider>`, mirrors `<providers>`, support `<size>` each (one row per space) |
| Principal `<user>` | `<privileges>` in `<spaces>` (one row per user) |
| Memory limits together | `<sum>` for `<n>` services |
| Onezone and Oneprovider releases | unknown (not measured) |
| Host memory before up (`free -g`: total, available) | unknown (not measured) |

## Memory per service

The peak since the container started, from the container's own cgroup (`memory.peak`; `memory.max_usage_in_bytes`
under cgroup v1): read by `federation.py memory` after each step, and by down just before it stops anything (its
`peak memory:` line). The cgroup charges the page cache too, which the kernel reclaims before the limit is reached,
so a peak near the limit is not by itself a shortage. One row per service of the layout:

| Layout | Service | Declared limit | Cgroup limit | Peak after up | Peak after the resets | Peak at down |
|---|---|---|---|---|---|---|
| `<file>` | `zone` | `<limit>` | unknown (not measured) | unknown (not measured) | unknown (not measured) | unknown (not measured) |
| `<file>` | `<provider>` | `<limit>` | unknown (not measured) | unknown (not measured) | unknown (not measured) | unknown (not measured) |

The largest peak per service across the runs, and the limit it suggests for a study: unknown (not measured).

## Up, the two resets and down

Each sequence from clean, twice per layout; a layout whose limits together exceed the host's available memory is
refused by up and recorded here as refused, not run. Seconds and phases are the commands' own summary lines.

| Step | Layout | Command | Seconds | Phases and result |
|---|---|---|---|---|
| 1a | `<file>` | up from clean | unknown (not measured) | |
| 1b | `<file>` | reset --replicas | unknown (not measured) | per space: files, what the mirrors held, sweeps |
| 1c | `<file>` | reset --clean | unknown (not measured) | entries removed and written again |
| 1d | `<file>` | down | unknown (not measured) | containers, data; `peak memory:` |
| 2a-2d | `<file>` | the same again, from clean | unknown (not measured) | |

After each `reset --replicas`: every mirror's block maps empty of the space's files and every owner's whole, on two
sweeps in a row (its own verdict), and what the mirrors held before. After each down: no container, network or file of
the project left, and no listener on its ports.

## Call shapes the core's reset depends on

From `uv run python deploy/compose-federation/measure_transfer_shapes.py`, run once per layout on a federation that is
up: each call's request and answer as field names and JSON types, never a value. The core's per-run reset (#118) and
its transfer log rely on these.

| Call | Request (query, body) | Status | Answer |
|---|---|---|---|
| `POST /transfers`, an eviction of a file, on the mirror | unknown (not measured) | unknown | unknown (not measured) |
| `POST /transfers`, an eviction of a directory, on the mirror | unknown (not measured) | unknown | unknown (not measured) |
| `POST /transfers`, a replication, on the data provider | unknown (not measured) | unknown | unknown (not measured) |
| `GET /transfers/{transfer}`, an eviction's record once ended | none | unknown | unknown (not measured) |
| `GET /spaces/{space}/transfers?state=waiting` | unknown (not measured) | unknown | unknown (not measured) |
| `GET /spaces/{space}/transfers?state=ongoing` | unknown (not measured) | unknown | unknown (not measured) |
| `GET /spaces/{space}/transfers?state=ended` | unknown (not measured) | unknown | unknown (not measured) |
| the same with `limit=1`, then its `page_token` | unknown (not measured) | unknown | unknown (not measured) |

Whether the listing answers carry a `Date` header, on the data provider and on the mirror: unknown (not measured).
Whether evicting a directory empties what the mirror holds of the files in it, as `reset --replicas` assumes, and how
long its block map took to show it: unknown (not measured).

## What listens on the host

The layout's loopback ports only, each held by one `docker-proxy` of the project: unknown (not measured).

## Day-0 checks and live contract tests

Their results, when run against this deployment: unknown (not measured).

## What could not be measured

Each fact above still unknown, and why.

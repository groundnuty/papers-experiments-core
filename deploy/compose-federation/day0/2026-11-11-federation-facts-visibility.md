# The Compose federation after the wait for a provider to see a new file, 11 Nov 2026

A fifth fresh `up`, after provisioning learned to wait until a provider sees a file written through another
(`consistency.wait_visible`, bounded by `VISIBLE_TIMEOUT_S`, 120 s). The earlier facts files stay as they were. The
day-0 checks' own report is beside this file (`day0-20261111T133619Z.md` and `.json`, mutation allowed). "Unknown" means
not measured; it is never a pass. Every name below is the federation's own invented one.

## Day-0 checks (`day0-20261111T133619Z.md`)

5 pass, 1 fail, 1 unknown, exit status 1, in 322 s.

- **reachability, health, tokens, listing, read_only: pass**, as on the run before.
- **content_type: unknown**: no `[sandbox]` is configured, so nothing was measured.
- **round_trip: fail, three steps further than before.** The scratch file is made through `pine` (`created`); the
  placement now waits until `birch` sees it, reads half of it through `birch` and confirms it by the block map
  (`placed`); the bytes read back match (`read back`). The check then fails at its eviction (`NotPlaced`): `birch`'s
  block map is not empty within the settle bound (300 s). The scratch was removed, and that verified.

## Two more answers of 25.0, read directly

On the same federation, as the admin, with the provisioner: a scratch file of 8192 bytes made through `pine`, half of it
read through `birch` by `place_ranges`, then one eviction from `birch` scheduled through each provider in turn:

| Step | What 25.0 did |
|---|---|
| A range read of bytes 0-4095 through `birch` | `birch` held all 8192 bytes afterwards: it fetched the whole file, not the range |
| `POST /transfers` (eviction from `birch`) through `birch` itself | `completed` after 11 s with `filesEvicted: 0`; `birch` still held 8192 bytes over the next 10 s |
| The same eviction through `pine`, the data provider | `completed` after 30 s with `filesEvicted: 1`; `birch` held 0 bytes at once |

So the round trip meets two more behaviours, both outside this fix and left for a decision: the provisioner schedules
an eviction through the evicting provider (`Provisioner._schedule`), which 25.0 reports completed while it removes
nothing (the shape O01 describes), and a range read through the receiver of a file this small places the whole file,
so the check's `held_bytes == range_bytes` would fail even once the eviction works.

## Live contract tests

`EXPCORE_LIVE_PLATFORM=<root>/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt EXPCORE_LIVE_MUTATE=1
EXPCORE_LIVE_SLOW=1 uv run pytest tests/live/test_contract.py`: 13 passed, 1 failed, 1 skipped, in 333 s. The failure is
the round-trip day-0 check, run as a test, at the eviction above; the skip is the tool-server test, with no tool server
installed. After the run the federation was taken down: no container or network of the project, no data directory and
no listener on its four ports.

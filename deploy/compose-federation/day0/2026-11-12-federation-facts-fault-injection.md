# The Compose federation: the fault injector against real providers, 12 Nov 2026

One slot, 05:10:11Z to 05:20:40Z, on the default layout, run from the fault-injector branch (#27, tip `4eed81a`). `up`
took 202 s (prepare 2, zone 56, providers 73, spaces 6, baseline 65) and `down` 36 s. Every name below is the
federation's own invented one: `pine` (`providers[0]`) is the agent's local provider, `birch` (`providers[1]`) and
`cedar` (`providers[2]`) the mirrors. Identities are short SHA-256 prefixes: of the container id where it says "id", and
the injector's own digest of `id@StartedAt` where it says "identity". No token was read or recorded.

The platform file was a copy of the rendered `platform.toml` in the root, beside the CA and the secrets, with this
`[faults]` added: `sanctioned = true`, `journal = "fault-journal"`, `project = "expcore-fed"`, `compose_file` the
branch's committed `compose.yaml` (the default layout's output), and `[faults.services]` naming `pine`, `birch` and
`cedar`. Every other key kept its default: `settle_timeout_s` 180, `command_timeout_s` 120, `poll_s` 2,
`clear_attempts` 3. The Compose driver needs `EXPCORE_FED_ROOT` in its environment, because the Compose file
interpolates it with `:?`.

The branch has one gated live test. The other claims below come from a scratch driver (not committed) that built the
same `FaultInjector` with the same `compose_driver(settings)` against the same file, with the driver's commands timed
and the journal read just before each stop, and the injector's clock wrapped to keep every reading.

## Results

| Claim | Result |
|---|---|
| Sanction needs both halves, otherwise `Unsanctioned` (O10) | pass |
| The target is journalled before the stop and seen down within the bound | pass |
| It comes back with a changed identity, the snapshot `ok`, injections all true, no open journal entry (O11) | pass; the container id did not change, see below |
| Every step is on the injector's clock | pass |
| An outage on the declared target after a model turn while injected is treatment; another provider's failure, or one before the first turn, is set aside | pass |
| After a crash between inject and clear, the next start leaves no provider down | pass |

## The live test

`EXPCORE_LIVE_PLATFORM=<the copy> EXPCORE_LIVE_FAULTS=1 uv run pytest tests/live/test_contract.py -k
provider_taken_down`: `test_a_provider_taken_down_is_seen_down_and_comes_back_with_a_new_identity` passed in 31.28 s.
With no `EXPCORE_LIVE_FAULT_TARGET` it took `providers[1]` (`birch`, the first provider that is not the agent's own).
The journal then held one `injecting`, `injected`, `cleared` (`how` `restored`) for `providers[1]`.

## Sanction (O10)

`FaultInjector` was built against the live file in three forms, `sanctioned = true`, `sanctioned = false` and the key
absent, each with the caller's `sanctioned` given as `True`, `False`, omitted, `1` and `"yes"`. Only `true` in the file
with `True` from the caller built an injector. The other 14 combinations raised `Unsanctioned` with the message naming
both halves. The driver handed to every construction counted its calls: 0, so no command reached Docker on any path.

## One measured injection of `birch` (O11)

Started once `birch` was healthy again, 48 s after the live test's restart (its `StartedAt`), after a health wait of
34.7 s.

- **Journal before the stop:** when the driver's `stop` was called, the journal held exactly one open `injecting`
  entry for the service. The journal ended with `injecting`, `injected`, `cleared` for it and 0 open entries.
- **Stop and seen down:** `docker compose stop` took 30.79 s (the provider's graceful shutdown; its
  `stop_grace_period` is 120 s). The first reading after it returned, 0.24 s later, showed the service not running,
  against a bound of 180 s. The driver ran `observe`, `stop`, `observe`, `observe`, `start`, `observe`.
- **While injected:** a request to `birch`'s REST configuration endpoint failed with `ConnectError` (0.04 s); `pine`
  answered 200.
- **Restore:** `start` took 0.62 s and the next reading, 0.26 s later, showed it running. From the stop command to the
  start's return: 31.97 s.
- **Identity:** id `bbd34dcd204e` before and after; `StartedAt` 05:15:02.043Z before, 05:16:22.501Z after. Identity
  `c8b2573167fb` before, `906c415e85f0` after. `pine` and `cedar` kept their id and start time.
- **Snapshot:** `ok` true, `injections` `[{target: providers[1], injected: true, cleared: true, verified: true}]`.
- **Steps:** `sanctioned` at 0.022 s, `injected` at 31.376 s (with the identity before), `cleared` at 32.573 s (`how`
  `restored`), `verified` at 32.573 s (`identity_changed` true, with the identity after). Every `at_s` equals a reading
  of the clock the injector was given minus its reading at `start` (to 1e-6 s), over the 8 readings it took.

## Treatment over the live snapshot

Three records were made through the real loop (`record_run`, scripted model) while `birch` was down, each under a
condition declaring `provider_outage` with target `providers[1]`, and each finished with `finish_instruments(record,
[injector])`, so the flag comes from the injector's real snapshot (`injected_targets` gave `{providers[1]}`).

| Record | The failure | `infra_target` | treatment | set aside |
|---|---|---|---|---|
| A tool in the first model turn contacts `birch` | real: `ConnectError`, raised as `provider_outage(birch)` | `providers[1]` | true | false |
| A tool in the first model turn reports `pine` failed | raised by the scripted tool; `pine` was up | `providers[0]` | false | true |
| The model fails before its first turn | real: `ConnectError` on `birch`, raised as `provider_outage(birch)` | `providers[1]` | false | true |

`Outcome.is_treatment` on the same injected set agreed: the target after one turn true, the target with 0 turns
false, `providers[0]` after one turn false.

## A crash between inject and clear

A child process built an injector for `birch`, ran `start` and ended with `os._exit(0)`, with no clear; it ran 30.6 s
in all. Afterwards `birch` was not running and the journal held one open `injecting` entry for `providers[1]`.

A new injector for `cedar` (`providers[2]`, attempt 2) then started in 32.4 s, with no error:

- `sanctioned` at 0.010 s; `recovered` at 1.090 s for `providers[1]`, `how` `restored`, identity after `d34b0469776c`
  (`birch`'s `StartedAt` 05:16:22.501Z before the crash, 05:18:05.088Z after the recovery, id unchanged);
- `injected` at 32.441 s for `providers[2]`; then, at `finish`, `cleared` (`restored`) and `verified` at 34.577 s,
  identity `4b88375438bc` before, `d64c2b68fc2c` after.

Right after that start the only provider not running was `cedar`, the new injection's own target; `birch` and `pine`
were running. After `finish` all three were running, the snapshot was `ok` with injections all true, the journal held
0 open entries (four injections, each `injecting`, `injected`, `cleared`), and every `at_s` was on the injector's clock.

## Memory

Sampled every 5 s with `docker stats --no-stream` over the project's containers (114 samples, 05:10:27Z to 05:19:52Z).

The peak of the slot was 3.69 GiB (4 containers) at 05:13:37Z, during up's baseline, before any injection. The
largest sum while a provider was down or being stopped was 3.29 GiB, in the live test's window. In each window, from a
stop command's start to the start's return, the stopping provider's own use fell as it shut down. The live test's and
the crashing child's windows are inferred from their restart times less their stops of about 31 s.

| Window | Largest sum | At |
|---|---|---|
| The live test, `birch` (about 05:14:31Z to 05:15:02Z) | 3.29 GiB (4 containers) | 05:14:32Z |
| The measured injection, `birch` (05:15:50.9Z to 05:16:22.9Z) | 3.12 GiB (4 containers) | 05:15:52Z |
| The crashing child's `birch`, to its recovery (about 05:17:33Z to 05:18:05Z) | 3.15 GiB (4 containers) | 05:17:37Z |
| The next start's `cedar` (05:18:06Z to 05:18:39Z) | 2.58 GiB (4 containers) | 05:18:22Z |

The only sample with a provider stopped, and so absent from `docker stats`, held 2.09 GiB (3 containers) at 05:18:37Z,
with `cedar` stopped. `down`'s cgroup peaks: `pine` 1.15 GiB, `zone` 1.09 GiB, `cedar` 0.72 GiB, `birch` 0.70 GiB.

## Unexpected

- **The container id never changed.** `docker compose stop` and `start` keep the container, so the restored identity
  differs only in `StartedAt`, as `compose.py` says. Every restore here was verified on that alone.
- **A stop takes about 31 s.** The measured `docker compose stop` ran 30.79 s, and the two stops inside a `start`
  (the crashing child's, `cedar`'s) put `injected` about 30 to 31 s after the step before it, so "seen down" comes about
  31 s into `start`.
- **A verified restore is not a ready provider.** Within about a second of the restore, `birch`'s REST endpoint still
  refused the connection. The Compose health check was healthy about 48 s (`birch`, after the live test), 49 s
  (`birch`, after the measured injection) and 52 s (`cedar`) after the restarted container's `StartedAt`, polled every
  3 s. The injector verifies the container's identity, not readiness,
  so a run that follows an outage run at once meets a provider still starting.
- **The sampler barely saw an outage.** A container counted stopped only from its exit to the start, about 1 to 2 s
  here, and `docker stats` omits a stopped container: one sample of 114 held 3 containers.
- **`down`'s peaks for `birch` and `cedar` cover only their last start.** The sampler saw `birch` at 1.00 GiB and
  `cedar` at 0.97 GiB before their restarts, more than `down` reported for either.

## Teardown

`down` exited 0. Afterwards `docker ps -a`, `docker network ls` and `docker volume ls`, filtered to `expcore` and
`fedsbx`, listed nothing, no container carried the root label, and `/mnt/volume2/expcore-federation` no longer
existed.

# Provenance:
#   source: icsoc-2026-code-agent @ c1e76c2e1b7cea56a88ad70efca2d5048aa9a4ea, harness/provider_restart.py, blob 9c1013c507a9bfae7e318ba6c69bc80f68c6097b
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/sweep_driver.py:130-131 (the fault is cleared in finally)
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/netem.py:85-86,337-348,362-392,401-416 (verified inject, bounded verified clear, fail-closed)
#   changes: see git history of this file from its import commit
"""Taking one provider offline for a run and bringing it back, as an instrument (O10, O11, O12).

A condition whose manipulation is a provider outage declares the kind `provider_outage` with the provider's position
label as its target, in study.toml: `infrastructure_is_treatment = [{ kind = "provider_outage", target = "providers[1]"
}]`. The run's `FaultInjector`, built for that label, takes the provider down in `start`, before the agent's loop starts,
and brings it back in `finish`, after the loop ends; neither happens in the agent's timed window, so neither is a wait
kind. Its snapshot (namespace `fault_injector`, `expcore.records.injections`) records the injection, and the core scores
the attempt as treatment only if the failure that ended it names the same label (`provider_outage` builds such a
failure) and the snapshot shows that label injected in this attempt. Another provider's failure, the agent's own
included, is set aside. Both kinds of this module are registered by `expcore_onedata.sandbox.kinds`, which the study
lists in `[study] platforms` (`platforms = ["expcore_onedata.sandbox.kinds"]`), so `provider_outage` is known when its
conditions are validated and `check` knows both kinds when a record names one.

**Sanction (O10).** Taking a provider down is destructive, so nothing here acts on two halves of a sanction short of
both: `sanctioned = true` in `[faults]` of platform.toml, and `sanctioned=True` passed by the code that builds the
injector. Anything else raises `Unsanctioned` when the injector is built, and again before it recovers or injects.

**Inject (O11).** The provider's identity is read first, the injection is journalled durably before the stop command
runs (`expcore_onedata.fault_journal`), and the provider must then be seen down within a bounded wait and the injection
journalled as done. Whatever breaks after the stop command (the wait, the journal, the driver, anything else) brings
the provider back at once and fails `start` as `injector_failure`, a kind no condition may declare, so the attempt is
set aside and run again; the reading shows the failed injection and the clear that followed.

**An owner stop needs a mirror asked first (O98).** Op-worker asks the holders of a file's blocks in the order of their
provider ids and does not retry another after a disconnected one, so taking down the provider that holds a space's data
fails its reads over to a mirror only when some mirror's id sorts below the owner's (`expcore_onedata.failover`). Before
anything else, before the federation is admitted, an earlier run's injection is recovered or anything is locked or
journalled, `start` (and the entry of `prepared`) checks that from the platform's ids and holders, as the injector was
built with them, for every space the target holds and another provider supports; when it does not hold, or an id is not
known, nothing is touched and `start` fails as `injector_failure`, naming spaces and providers by label. A target that
holds no space with a mirror is unchanged, and reads nothing the injector did not already hold.

**Clear (O11).** `clear` brings the provider back and counts a restore only when the provider runs with an identity
other than the one read before the injection (`ComposeDriver`: the container's id and start time). Readiness alone is
not a restore, and a provider back with the same identity is a failed restore. The start is tried a bounded number of
times; a service that cannot be read is started all the same, and its restore is `restore_unverified`.

**A restore is verified only when the provider serves (O11).** A changed identity is not yet a provider that serves: in
the live run of 12 Nov 2026 (deploy/compose-federation/day0) the restarted provider's REST endpoint still refused
connections about a second after the restart, and its container's health check passed 48 to 52 s after the container
started. Releasing the federation then lets the next run, a control perhaps, start on a provider that is still starting,
whose refusals and delays its agent can absorb without any recorded infrastructure error. So once the identity is
verified, `clear` waits for the driver to say the provider serves (`FaultDriver.serves`: `ComposeDriver` asks that its
container, if it has a health check, is healthy and that the platform's authenticated REST read through that provider
succeeds, `expcore_onedata.fault_serves`), every `poll_s`, for at most `ready_timeout_s` (180 s by default), and only
then journals the injection cleared and lets the federation go. A provider that does not serve within the bound is a
restore `not_serving`: unverified, the injection open and journalled `clear_failed`, the target's lock and the
federation's exclusive admission held, the fault gate failing for it, exactly as for a failed restore; the start command
is not issued again, and a later `clear`, `finish` or `start` finds the provider restored and waits again. The snapshot
records the wait as `ready_after_s`, in the `verified` step and in the injection once it served: the seconds from the
verified identity change to the first reading that showed it serving (0 for a provider that already served). A provider
the stop never took down is not waited for. The recovery of a crashed run's injection, by `start`, `recover` or the
fault gate, waits the same way, and its `recovered` reading carries `ready_after_s` too. **On an HPC federation (O58)** a
restart waits, inside that wait, for op_worker's domain (null until its Onezone connection is up) before its identity is
checked, and the restore records how long as `domain_wait_s`, beside `ready_after_s` and in the same places: the
seconds as the node measured them (0 when no wait was needed). It is present only when the driver reported one: a
driver with no such wait (Compose) and a status without the figure leave it out, never as a 0 nobody measured.

**The links after a restore (O35).** A provider that serves is not yet a federation whose links transfer: an outage
can leave a pair between two other providers stuck. Before it stops anything, `start` makes the session gate's probe
file and journals it with the injection (`fault_sessions`); the restore then passes the session gate (`gate_timeout_s`,
else `sessions_not_ready`) and checks every ordered pair (`links_stuck`), and with `[faults] heal` declared heals a stuck
link, bounded and failing closed (`fault_heal`: `all_links` restarts every provider's link at once, then a stuck
reader's container). Only the injector's own clear and start heal, while an exclusive admission is held; the snapshot
gains the `gate` and `heal` steps. Stuck pairs no restore heals are healed by the fault gate's health check, under an
exclusive admission of its own, before any run's tokens exist (`fault_gate_heal`).

**A stop of unknown outcome (O11).** When the stop command raises, typically at its timeout (`command_timeout_s`), the
CLI is killed but the Docker daemon goes on stopping the container for up to the provider's `stop_grace_period`; the
federation's is 120 s, the default command timeout. The injection is then journalled `stop_unknown`, and the clear
does not start the service or read its identity at once, because a `start` of a container still running does nothing
and the old identity then read is the container on its way down, not one that never went down. It waits, for at most
`settle_timeout_s`, for the service to show not running, and only then starts it and judges the restore by the changed
identity. A service not seen down within that bound leaves the restore `stop_unsettled`: unverified, the injection
open and pending, never cleared. A process killed while the stop command blocks cannot journal `stop_unknown`, so the
journal says it before the stop instead: a `stopping` line, written durably right before the command (a journal that
fails there stops the start before anything is taken down). Recovery treats an open injection that was never seen down
as of unknown outcome, `stop_unknown` for one that has the `stopping` line or the `stop_unknown` line, and
`stop_unrecorded` for one that has neither (killed before the command, or journalled before the `stopping` line
existed): it waits,
for at most `settle_timeout_s`, for the service to show not running or running with a changed identity, and then
restores it and requires it to serve, as for any restore. Only a service that every reading of the whole bound showed
running with the identity it had, after a journal that never says the stop was asked for, is `never_down`; after a
`stopping` line it is `stop_unsettled` instead. `settle_timeout_s` must therefore exceed the providers' `stop_grace_period` with a
margin; its default, 180 s, does for the federation's 120 s, and platform.toml does not know the grace, so a federation
that raises it raises this too (`expcore_onedata.fault_settings`). `clear` never raises: a failed restore is in the
snapshot (`verified: false`), stays open in the journal, journalled `clear_failed`, and stays pending, with the target's
lock held, so a later `clear`, `finish` or `start` tries again, and the health gate and every other injector's start
refuse the provider until then. A later try runs only while no step of the run that uses its tokens runs; while one
does, the injection is handed to recovery instead, open (see "The token's window").

**A restore that is not verified is never passed silently (O11).** `clear` and `finish` answer it as data (`False`, the
snapshot's `ok` false), which a caller that handles only a raising exit never reads, so every other way out either
raises or leaves the failure in the record, which the campaign refuses to score:

- `session` clears however the start and the body end, and raises `RestoreNotVerified` at its exit when the restore is
  not verified. It is an `InfraError` of kind `injector_failure`, so a caller keyed on that kind sees what it saw for a
  failed start, and it names the provider by its label, how the restore ended (`how`, a word such as `not_serving`), and
  what was checked (whether the identity changed to a running one, and whether the provider then served), never a
  service, a path or a token. When the start or the body raised too, it is raised from that exception (`__cause__`), so
  neither is lost; an interrupt, an exit or a cancellation (anything but an `Exception`) is raised as it was, with the
  restore failure in its notes.
- `running` clears the same way and **returns** when its body returned, so the executor that runs the agent inside it
  (`record = await record_run(...)`) keeps its record. The record carries the failure itself: the snapshot, which is
  the record's instrument reading, holds `restore_verified` (`true` once a restore of a target this injector took down
  is verified, `false` with `how`, `identity_changed` and `serving` when it is not, absent when nothing was taken down).
  The core never scores a record that carries it as anything but `true` (`expcore.records.injections.restore_refusal`):
  the record is set aside, and the scheduler sets its attempt aside as `injector_failure` whatever the post-run health
  check decides. When the body raised, no record exists to carry it, and `running` raises as `session` does.
- `check`, the instrument's post-run check, raises the same failure, so an executor that starts the injector with
  `start_instruments` and clears it itself gets the refusal from `finish_instruments`: the record keeps its snapshot
  and holds the refusal (`instrument_refusal`), and the attempt is set aside as infrastructure and run again.
- `start` already raises `injector_failure` whenever the clear it makes before or after a failure does not verify.

`finish` returns the snapshot and never raises for it, as `finish_instruments` would replace a raised snapshot with a
bare failure that loses the injections; its `check` refuses. `finish` calls `clear`, `session` and `running` call it
around both `start` and the agent's run, and `start` calls it before failing.

**Recovery (O11).** Before it injects, `start` (like `recover`) clears and verifies every injection the journal holds
open whose target no live injector holds: each injector holds an exclusive lock on its target's lock file for as long
as it may have the target down, the kernel drops the lock when its holder dies, and a forked child never keeps it.
A crash between inject and clear therefore leaves no provider down after the next start, and an open injection that
cannot be cleared and verified fails that start as `injector_failure`, so a campaign never runs on with a provider it
left down. The journal names each injection's holder (pid and boot id): a lock held while that holder is gone is
stuck, and fails the start the same way instead of being skipped as a live run's.

**Baseline (O11).** While an injection proceeds or is unsettled the federation is not in its baseline state, so no
other run may be on it. An outage run holds the federation's lock exclusively for its whole run, inject and clear
included (`expcore_onedata.admission`): the injector uses its run's exclusive admission, takes one itself when the run
holds none, refuses a shared one, and keeps it until the restore is verified. Its own wait that runs out while another
run holds the federation, or while it stays closed for an outage run's session drain, is `federation_busy`, a hold that
costs the run no attempt, never `injector_failure`. Every other run holds it shared, so the
two never overlap; a turnstile in front of the lock keeps a stream of shared holders from starving an outage run. An
injector also refuses to start, as `injector_failure`, while the journal holds an injection recovery could not
settle, and the platform's standard health check (`PlatformHealth`), whenever platform.toml has a `[faults]` table,
recovers what a crashed run left, holds the federation shared for the whole check, is deferred (charging no run)
while an outage run holds it, and fails only for an injection that stays open.

**The session drain (O11).** A restored federation is not yet one the next run can read from: an agent's reads of a file
only the stopped provider holds, during the outage, leave on-demand sync jobs on the local provider that a later read of
the same file and range would join and hang on, until the run's callers end (`expcore_onedata.fault_drain`). So once a
restore of a provider that went down is verified, the federation admits no run for `[faults] session_drain_s` (320 s)
after the later of the run's mount disconnecting and its REST gate stopping, and a recovery of a crashed run's injection
owes the full drain from its own start. An injector that holds the federation by an admission of its own waits it out in
`clear` and records a `drain` step (the times used, the wait, why); with a run's admission, whose sandbox is still up at
the restore, the step is `deferred` and the admission closes the federation for what is left when it ends.

**Readings.** Every step (`sanctioned`, `recovered`, `injected`, `cleared`, `verified`) is a reading in the snapshot,
labelled with the seconds on the injector's clock since `start`, so replay serves it and no scorer asks the platform.
The injection's own entry holds `target`, `injected`, `cleared`, `verified` and, once its provider served, `ready_after_s` (and, on an HPC federation that reported it, `domain_wait_s`, the part of that wait spent on the provider's domain).

**A link check whose record was not written says so (O35, `linkrecord`).** The restore's link check and each read of the
pairs a heal makes write `link-check.json` (`fault_serves.recorded`), and one that could not be serialized or whose
record could not be written leaves no record. The snapshot then holds `link_record: "not_written"` and
`link_record_reason` (`lock_not_taken` or `write_failed`) at its top level, a `link_record` step with the same two keys,
and the heal's matrices (`first`, `before`, `after`) say it of their own check; a `recovered` step holds them for a
recovery's. All of it is absent when every record was written, so a reading is what it was. The restore is verified or
not as ever: the record is advisory.

**Use.** The study's plugin passes the platform's run admission to the campaign (`admission=run_admission(config,
schema)`, with the study's factor schema), which admits each run to the federation before anything starts (exclusive
when its condition declares `provider_outage` or `exclusive = true`) and keeps it until after the post-run check, and
the platform's half of the fingerprint (`platform=config.fingerprint()`), which holds the injector's sanction and
targets. It passes the platform's health check too (`health=PlatformHealth(...)`): the admission declares that it needs
one, and a campaign without one is refused when it is built. The executor, already admitted, starts the sandbox and
the tool server, builds the injector for the target its condition declares and runs the agent inside `running`:

    injector = FaultInjector(config, settings, request.treatment_targets[PROVIDER_OUTAGE], sanctioned=True,
                             driver=compose_driver(settings, probe=provider_serves(config, settings, client)))
    async with injector.running(dataclasses.asdict(request.identity)):
        record = await record_run(request, ...)
    record = await asyncio.to_thread(finish_instruments, record, [injector])

**The token's window (O35, O06).** In that order the restore runs at `running`'s exit, while the run's sandbox and tool
server still run, and the tool server's stop after it re-verifies the tool plane and examines the run's token, failing
the run as `tool_plane_integrity` once it has expired (`toolserver._recheck`). The token outlives the run's ceiling by
`[tokens] margin_s` only. The restore always runs, the provider brought back and its session gate passed, since none
of it uses a run token, and `load_faults` refuses a margin it could outlast, with the session drain a clear that takes
the federation itself may wait (`fault_settings.restore_token_margin_s`, `restore_drain_s`). A
heal may take `heal_timeout_s` more, so at a heal's start, the restore's and the start's recovery's alike, the
injector reads the steps of the run that use its tokens: the tool server's launch (to the end of that
re-verification) and the sandbox's (from its mint to the end of its stop), which each launch counts on the run's
admission while it runs (`admission.token_step`), its worker threads included. With a heal declared, the federation
held exclusively, a step still running and no long margin declared, the heal is skipped: the reading records it (the
`heal_skipped` step, or `heal_skipped` in the start's `recovered` step: the steps that ran, `margin_s` and
`long_margin_s`), and a pair that stays stuck leaves the restore unverified (`links_stuck`, the record kept and never
scored) or fails the start closed, the provider up and its injection open. A later start with no step of its run
running heals it, under its own exclusive admission. **In this order that is no outage run's start**, whose launches
run before it too, and the health gate, whose recovery never heals, holds the campaign meanwhile: a stuck pair is healed
only by a start with no step running (an executor that starts the injector before its launches, or a bare injector
start), by the long margin, or by hand. The long margin, `[tokens] margin_s` of at least
`fault_settings.heal_token_margin_s` (10,854 s with the defaults), heals as before whatever still runs. An executor
that stops its sandbox and tool server before `running`'s exit heals with the default margin. A tool server or sandbox
the study starts itself, which no launch counts, is not seen.

**One restore inside the window, and every token step under the run's admission.** The least margin counts one restore,
so a restore that failed is tried again (by `finish`, a later `clear`, or a reused injector's next `start`) only when no
step of the run that uses its tokens still runs, decided at once and never waited for (`_retry_now`): in an executor
that stops its sandbox and tool server before `running`'s exit, `finish` retries at once, the window closed. While one
runs, as at `finish` in the order above, the injection is handed to recovery (`_hand_over`): it stays open in the
journal, the record keeps `restore_verified` false with the injection open and is never scored, a later `clear`
reports it still down, and its target's lock goes with the run's admission, so nothing of the run restores it inside
the window (a recovery finds it held, and a second start fails closed); once the run's admission has ended, the next
start's recovery, or the health gate's, restores it. A
`restore_retry` step records the path (`after_steps` or `left_open`), why, and the steps that ran. The steps are
counted where the injector reads them because, on a platform with `[faults]`, each worker that uses a token (the tool
server's two checks, the sandbox's mint and start) starts only under the run's admission held then, and is refused
before it starts otherwise (`admission.begin_token_step`, `TokenStepRefused`, naming it). So a token is minted only
once the run's admission and its waits are behind it, and an injector that takes an admission of its own, and waits
for the lock, does so while no run's admission is held, which no token step can run without.

`running` starts the injector in a worker thread and keeps that thread's future: a cancellation cannot stop the thread,
so on cancellation it waits for it (shielded, bounded), clears however the block ends and raises the cancellation again.
A restore that is not verified does not raise at the block's exit: the record, built inside the block, is kept, and
`finish_instruments` puts the injector's reading into it, with `restore_verified` false, which the campaign refuses to
score (it sets the attempt aside as `injector_failure` to run again), and `check` refuses it as well. The journal keeps
the provider unhealthy for the gate. An executor that drives the injector through `start_instruments` and clears it
itself gets the same from `finish_instruments`, with its record kept. Never
await `asyncio.to_thread(injector.start, ...)` bare: cancelled, it leaves the thread stopping the provider while the
code meant to clear it never runs. `start` and `clear` hold the injector's own lock, so a clear waits for a start still
running in its thread (a `prepared` block holds it only while it prepares and injects: see "A baseline before the
stop"). `finish_instruments` puts the snapshot into the record and derives its treatment flag again from it.

**Order (C31).** `finish` is the restore, and `finish_instruments` finishes the instruments in the reverse of their
list. Probes listed after the injector therefore finish before the restore and read the state the outage left; probes
listed before it finish after the restore. `running` clears when its block exits, so a `finish_instruments` called
after the block runs after the restore the block's exit made, whatever the order (a restore that was not verified is
tried again by `finish` once no step of the run that uses its tokens runs, and then the order decides again; while one
runs it is handed to recovery, see "The token's window"): call it inside the block for the probes to read first.

**A baseline before the stop (`prepared`, O11).** `start` admits the run to the federation, recovers what an earlier
run left open, waits out the drain that recovery owes and the federation's drain record, and only then takes the
target's lock, journals the injection, stops the target and sees it down. A probe whose `start` reads a baseline must
read it between the two: after the first, so it reads the federation admitted, recovered and drained, and before the
second, so it reads the target up. `prepared` splits `start` there:

    with injector.prepared(identity) as prepared:
        start_instruments(probes, identity)  # the baseline; the injector is not in this list
        prepared.inject()
    ...  # the agent's run
    record = finish_instruments(record, [injector, *probes])  # the probes finish first, then the restore

`start` is that block with nothing in its body but `inject`. The target's lock and the journal entry belong to
`inject`, so a block left without it has one thing to undo, its admission, and its exit keeps `start`'s rule: the
admission goes only when nothing is down (an `Exception` or any other `BaseException` in the body, `inject` never
called, an `inject` that failed before its stop), and nothing goes after a stop that took effect, the admission held
until the restore. The block never restores: after `inject`, the caller's `finish` or `clear` does, as after `start`
(it is no `session`). Run from async code, the whole block runs in one worker thread (`asyncio.to_thread` carries the
run's context, so its admission), and the caller keeps that thread's future as `running` keeps its start's.

- **The locks across the body.** The injector's lock, a reentrant `RLock`, is held while the block prepares and while
  `inject` runs, never across the body, whose length the injector does not know; a share of the run's admission
  (`retain_admission`) is held for the whole block, so the federation stays held exclusively through the baseline
  whatever the run's task does. While a block is open, from any thread, `start` (so `session` and `running`), a second
  `prepared` and `recover` are refused at once as a defect of the caller (`RuntimeError`, a `prepared` block when it is
  entered), before they wait for the lock, which the block's `inject` may hold; `start`'s own block is waited for, as a start always was. `check` and
  `snapshot` answer as usual. A `clear` or `finish` from any thread during the body returns at once (True: nothing is
  down) and makes the block's `inject` refuse as `injector_failure`, with nothing locked, journalled or stopped; one
  that comes while `inject` runs waits for it, as for a start, and restores what it took down. Either way nothing is
  stopped after a clear has returned, which is what the lock gives `start`. A clear that returned before the block was
  entered does not stop a later one, so a caller that runs the block in a worker thread waits for that thread before
  its last clear, as `running` does. `running`'s cancellation (its event, `cancel_wait_s`) knows no block: a block's
  caller cancels it with a `clear`, or leaves it without `inject`. `inject` is refused (`RuntimeError`) outside its
  block, after the block ended and a second time.
- **The window is unbounded, so a caller's block's `inject` re-checks** what the preparation found, before it
  touches anything, with no reading of the injector's clock and no step; `start`'s own block, with no body between its
  halves, reads nothing again, so `start` is unchanged. The exclusive admission the block was prepared under must be
  the one held where `inject` is called (a thread that does not carry the run's context holds none; `injector_failure`);
  the journal must still hold no open injection (the recovery found none; one journalled since is `injector_failure`,
  naming its label); the drain record must still have passed; and the link epoch must not have moved. The preparation
  reads the epoch, making the record when there is none (`LinkEpoch.ensure`, as the fault gate's check does), and
  `inject` reads it again once every link check under way has ended (`LinkEpoch.settled`, within
  `admission_timeout_s`, as a run's admission reads it). Only the same `id` and count at both ends counts as unmoved:
  a record raised, missing (a raise that could not be written spoils it), made anew or unreadable at either end
  refuses, so no target is stopped after a heal began or a link check found a pair failing in the window (the hold
  `federation_busy`, for the drain record, the epoch and a link check that outlasts the wait).
- **Who can move them while the exclusive admission is held.** No other run, and no health check or heal of the gate
  holding the federation: the gate's link check needs a shared hold, and its heal and its recoveries an exclusive one of
  their own (`RECOVERY`). What remains: a health check in the run's own context, whose link check can raise the epoch;
  a second injector under the same run's admission, outside the one-injector-per-run use (its injection, its restore's
  heal, and the drain its restore arms on the run's admission, which the run publishes as it ends); and the health
  gate's stuck-holder path, which decides from a holder record it read when its own wait ran out, which may predate
  this block's admission (a race of `fault_gate._busy` that `start` has too), and may then raise the drain record and
  run a recovery without the lock, which finds nothing open (this block's recovery settled it) and leaves alone a
  target whose lock is held. The re-reads above refuse each of these that lands before `inject` reads; the identity
  read before the stop, the session gate's probe and the holder line are `inject`'s own.
- **The fault relay on the HPC backend** (`fault_relay`), which the run's admission requires live (`relay_refusal`, the
  hold `federation_busy`), can stop answering in the window. It is not read again here: `inject`'s first request of
  the driver, the read of the target's identity, fails when it does. With the heartbeat already missing or stale it is
  never sent; a relay that died within `RELAY_STALE_S` of its last beat never takes it, and it is withdrawn when its
  wait ends; one that took it and never answers leaves a reading that failed. Either way `_journal_injection` lets the
  target's lock go and `inject` fails as `injector_failure`, with nothing journalled and nothing stopped, the admission
  let go at the block's exit. Unlike the admission's hold, that failure costs the attempt, as a failed start's does.
- **The token's window and the deferral bound: the window documented.** In the documented order the run's tokens are
  minted before the injector starts, so a baseline spends token life, and the least `[tokens] margin_s`
  (`fault_settings.restore_token_margin_s`) counts no baseline: the injector's start after the mint falls inside
  `TOKEN_SLACK_S` (300 s, shared with the rest of the slack), apart from, on the HPC backend, the start's stop, a term
  of its own (`settle_timeout_s` and `relay_wait_s`). A study whose baseline may take longer than the slack leaves
  declares a larger margin (the least is a floor), or reads its baseline before its launches mint a token. An overrun is
  never silent: the tool server's stop re-verifies the token after the restore and fails the run as
  `tool_plane_integrity`, which is never scored. A heal's skip is decided at the heal's start from the steps that then
  run, which a baseline does not change. A longer exclusive hold lengthens the deferral of other runs' health checks,
  which no setting bounds already (a deferral begun while an outage run holds the federation counts from its first
  answer, `fault_settings`), so a campaign whose `max_defer_s` it outlasts meets a blocking hold, never a wrong result.
- **The snapshot** is `start`'s: the `injected` step is labelled with its own time, so the gap after the preparation's
  last step shows the baseline.

**Names (O12).** Outside the driver and the journal the provider is known only by its label: in the snapshot, in every
message and in every failure. Identities are recorded as SHA-256 digests. The settings are `[faults]` of
platform.toml (`expcore_onedata.fault_settings`).

**An HPC federation (O58).** With `[faults] backend = "hpc"` the driver is `hpc_driver(settings, probe=...)`
(`expcore_onedata.fault_hpc`): the same injector, journal, restore, settle, recovery and heal, its requests run by the
federation's fault relay on the login node (`expcore_onedata.fault_relay`), and its stop's evidence journalled with the
injection (`injected`: how the stop ended and its seconds).
"""

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

from expcore.campaign import liveness
from expcore.config.loading import ConfigError
from expcore.errors import InfraError, uncharged_kinds
from expcore.records.injections import FAULT_NAMESPACE
from expcore_onedata import failover, fault_owner
from expcore_onedata.admission import (
    INJECTOR,
    Admission,
    AdmissionRefused,
    FederationBusy,
    FederationLock,
    current_admission,
    require_same_federation,
    retain_admission,
)
from expcore_onedata.compose import DriverError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_drain import SessionDrain
from expcore_onedata.fault_epoch import LinkEpoch
from expcore_onedata.fault_journal import HeldLock, SettledInjections, holder
from expcore_onedata.fault_recovery import RESTORED, FaultDriver, Recovery, RestoreNotVerified, digest
from expcore_onedata.fault_restore import stop_record
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import FaultSettings, compose_driver, heal_token_margin_s, hpc_driver, load_faults
from expcore_onedata.linkrecord import NOT_WRITTEN, fact
from expcore_onedata.sandbox.kinds import INJECTOR_FAILURE, PROVIDER_OUTAGE

__all__ = [
    "INJECTOR_FAILURE",
    "PROVIDER_OUTAGE",
    "FaultDriver",
    "FaultInjector",
    "FaultRefused",
    "FaultSettings",
    "PreparedInjection",
    "RestoreNotVerified",
    "SettledInjections",
    "Unsanctioned",
    "compose_driver",
    "hpc_driver",
    "load_faults",
    "provider_outage",
    "provider_serves",
]


class Unsanctioned(ConfigError):
    """The injector was built or run without both halves of the sanction (O10)."""


class FaultRefused(ConfigError):
    """The injector was asked to act on a target platform.toml does not let it take down, or for no run."""


class _NotSeenDown(Exception):
    """The stop command returned, but the service was never seen down within the bounded wait."""


def provider_outage(config: PlatformConfig, name: str, detail: str) -> InfraError:
    """The failure of a provider the platform attributes to it, naming it by its label (`providers[i]`), never by name."""
    return InfraError(PROVIDER_OUTAGE, detail, target=config.provider_label(name))


def heal_skipped(margin_s: float, long_margin_s: float) -> Callable[[], dict[str, Any] | None]:
    """The question a recovery asks at a heal's start, the restore's or the start's recovery's (`LinkSettling`): why
    the heal is skipped, the steps of the run that use its tokens and still run, with `margin_s` and `long_margin_s`,
    when the study has not declared the long margin; None when it may run (see "The token's window"). The run's
    admission counts the steps, and on a platform with `[faults]` none starts without it (`admission.begin_token_step`),
    so none starts while an injector holds an admission of its own."""

    def skipped() -> dict[str, Any] | None:
        if margin_s >= long_margin_s:
            return None
        held = current_admission()
        running = held.open_steps() if held is not None else ()
        if not running:
            return None
        return {"steps": list(running), "margin_s": margin_s, "long_margin_s": long_margin_s}

    return skipped


_INSIDE_A_BLOCK = (
    "a prepared block of this fault injector is open: {what} is refused until the block ends, never waited for "
    "(see 'A baseline before the stop')"
)


class PreparedInjection:
    """What `FaultInjector.prepared` yields: the injector admitted, recovered and drained, its target not yet touched.
    `inject()` takes the target down, once, inside the block (see "A baseline before the stop")."""

    def __init__(self, injector: "FaultInjector", *, own: bool) -> None:
        self._injector = injector
        self._own = own  # `start`'s own block, whose body is `inject` alone: waited for, never refused
        self._admission: Admission | None = None  # the exclusive admission the block was prepared under
        self._epoch: tuple[str, int] | str | None = None  # a caller's block: the link epoch it was prepared at
        self._open = False  # the block's body runs: set once it is prepared, cleared as it ends
        self._used = False  # `inject` was called
        self._cleared = False  # a `clear` came while the body ran: `inject` takes nothing down

    def inject(self) -> None:
        """The second half of `start`: lock the target, journal the injection, stop the target and see it down (O11),
        or raise InfraError of kind `injector_failure` with the target brought back. Refused as a defect of the caller
        (`RuntimeError`) outside its block, after the block ended and when called again; refused with nothing touched
        when a `clear` came before it (`injector_failure`), or when what the block's preparation found no longer
        holds: the run's admission where it is called and an empty journal (`injector_failure`), the drain record
        passed and the link epoch unmoved (`federation_busy`)."""
        self._injector._inject_prepared(self)


class FaultInjector:
    """The instrument that takes one provider down for a run and brings it back (see the module docstring)."""

    namespace = FAULT_NAMESPACE

    def __init__(
        self,
        config: PlatformConfig,
        settings: FaultSettings,
        target: str,
        *,
        sanctioned: bool = False,
        driver: FaultDriver,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """Check the sanction and the target; act on nothing yet. `target` is a provider's label (`providers[1]`);
        `wall` is the clock the federation's drain record is on (`FederationLock`)."""
        self._settings, self._sanctioned = settings, sanctioned
        self._require_sanction()
        names = {config.provider_label(name): name for name in config.providers}
        if target not in names:  # what it is may be a configured name, so it is never echoed (O12)
            raise FaultRefused(f"{settings.file}: [faults]: the target is not the label of a provider in [providers]")
        service = settings.services.get(names[target])
        if service is None:
            raise FaultRefused(f"{settings.file}: [faults.services]: no service is declared for {target}")
        self.target, self._service, self._driver = target, service, driver
        self._config, self._owner_name = config, names[target]  # its ids and holders as loaded: an owner stop's check
        self._clock, self._sleep = clock, sleep
        labels = {config.provider_label(name): service for name, service in settings.services.items()}
        # holds no reference to the injector: a dropped injector's locks go when its last reference does (no cycle)
        heal_skip = heal_skipped(config.tokens.margin_s, heal_token_margin_s(settings))
        self._recovery = Recovery(settings, driver, clock=clock, sleep=sleep, labels=labels, heal_skip=heal_skip)
        self._journal = self._recovery.journal
        self._gate_probe: str | None = None  # the open injection's session gate probe file (`fault_sessions`)
        self._lock: HeldLock | None = None
        self._federation = FederationLock(settings, clock=clock, sleep=sleep, wall=wall)
        self._admission: Admission | None = None  # the exclusive admission it took itself, when its run held none
        self._cancel = threading.Event()  # set when `running` is cancelled: an admission still awaited is given up
        self._mutex = threading.RLock()  # a start and a clear never run at once, in whichever threads
        self._block: PreparedInjection | None = None  # the open `prepared` block, from its entry to its end
        rounds = settings.clear_attempts + 2
        heal_s = settings.heal_timeout_s if settings.heal != "off" else 0.0
        self.cancel_wait_s = (
            (settings.settle_timeout_s + settings.command_timeout_s) * rounds
            + 2 * (settings.ready_timeout_s + settings.gate_timeout_s + heal_s)  # a recovered provider's, and its own
            + settings.session_drain_s  # and the drain a clear waits out before it lets its own admission go
        )  # about a start's longest
        self._pending: tuple[str, str] | None = None  # the open injection's journal id and identity digest
        self._tried = 0  # restores of the pending injection tried so far: a later one is a retry (`_retry_now`)
        self._left_open = False  # this run's injection, handed to recovery open (`_hand_over`): never verified here
        self._run: dict[str, Any] | None = None
        self._attempt = None  # the attempt the run's hold is keyed by (`fault_owner.attempt_key`), taken at the start
        self._steps: list[dict[str, Any]] = []
        self._injection: dict[str, Any] | None = None
        self._went_down = False  # the target was seen down during the open injection
        self._stop_unknown = False  # the open injection's stop command raised: what it did is not known
        self._origin = clock()

    # --- the sanction and the readings ------------------------------------------------------------------------

    def _require_sanction(self) -> None:
        """Refuse unless platform.toml and the caller both sanction the injector, each with exactly True (O10)."""
        if self._settings.sanctioned is not True or self._sanctioned is not True:
            raise Unsanctioned(
                f"{self._settings.file}: [faults]: a fault injector takes a provider down and needs both "
                "'sanctioned = true' in platform.toml and sanctioned=True from its caller"
            )

    def _step(self, step: str, ok: bool, target: str | None = None, **extra: Any) -> None:
        self._steps.append(
            {"step": step, "at_s": self._clock() - self._origin, "target": target or self.target, "ok": ok, **extra}
        )

    def _fail(self, detail: str) -> InfraError:
        return InfraError(INJECTOR_FAILURE, detail, target=self.target)

    # --- the instrument -----------------------------------------------------------------------------------------

    def start(self, identity: Mapping[str, Any]) -> None:
        """Recover what an earlier run left open, then take the target down and see it down; raise InfraError of kind
        `injector_failure` when either fails, or while an injection stays unsettled. Every path after the stop command
        brings the target back first (O11).

        `identity` is the run's identity as a plain mapping with at least its `run_key` and `attempt`, which the snapshot
        names so the treatment rule counts the injection for this attempt only.

        It is its own `prepared` block with nothing in its body but `inject`, under the injector's lock for the whole
        of it, so a clear waits for a start still running, and with no window between its halves it reads nothing again:
        what a caller's block re-checks in `inject` guards a caller's body (see "A baseline before the stop").
        """
        self._refuse_inside_a_block("start")
        try:
            with self._mutex, self._block_for(identity, own=True) as block:
                block.inject()
        except BaseException:
            self._release_held()  # a start that failed after it registered its hold holds nothing (O97)
            raise

    @contextlib.contextmanager
    def prepared(self, identity: Mapping[str, Any]) -> Iterator[PreparedInjection]:
        """`start` in two halves, for a baseline between them (see "A baseline before the stop"): the entry admits,
        recovers what an earlier run left open and waits out the drain that recovery owes and the federation's drain
        record, raising as `start` does; the body runs with the federation held exclusively and nothing of the target
        touched; `inject()` on what it yields locks, journals and stops the target. The exit lets the admission go only
        when nothing is down, however the body ends, and never restores: after `inject` the caller's `finish` or `clear`
        does, as after `start`. While the block is open, `start`, `prepared` and `recover` are refused at once
        (`RuntimeError`), and a `clear` makes `inject` refuse. Entering the block, not building it, is what is refused.
        """
        self._refuse_inside_a_block("prepared")
        with self._block_for(identity, own=False) as block:
            yield block

    def _refuse_inside_a_block(self, what: str) -> None:
        """Refuse `what` at once while a caller's block is open, without waiting for the injector's lock, which that
        block's `inject` may hold; `start`'s own block is waited for, as a start always was. Read again under the lock
        (`_block_for`, `recover`), so a block entered meanwhile is refused too."""
        block = self._block
        if block is not None and not block._own:
            raise RuntimeError(_INSIDE_A_BLOCK.format(what=what))

    @contextlib.contextmanager
    def _block_for(self, identity: Mapping[str, Any], *, own: bool) -> Iterator[PreparedInjection]:
        with self._mutex:
            if self._block is not None:
                raise RuntimeError(_INSIDE_A_BLOCK.format(what="prepared (and so start)"))
            block = self._block = PreparedInjection(self, own=own)
            give_up = retain_admission()  # the run's admission stays held until this block ends, whatever its task does
        try:
            with self._mutex:
                self._prepare(identity)
                block._admission = self._exclusive_admission()
                if not own:  # made when there is none, so that a raise spoiled in the body reads as moved
                    block._epoch = self._epoch_note(make=True)
                block._open = True
            yield block
        finally:
            with self._mutex:
                block._open, self._block = False, None
                if self._pending is None:  # nothing is down: the federation goes back to the other runs
                    self._release_admission()
            give_up()

    def _prepare(self, identity: Mapping[str, Any]) -> None:
        """The first half of `start`: everything before the target's lock. A stop that is known to be invalid is refused
        first (`_require_failover_order`), so nothing is admitted, released, recovered, journalled or locked for it."""
        self._require_sanction()
        self._require_failover_order()
        self._release_held()  # a hold of an earlier run of this injector never outlives that run's injection
        if self._pending is not None and not self._clear():  # an earlier start of this injector was never cleared
            raise self._fail(f"an earlier injection of {self.target} by this injector is still not cleared")
        run_key, attempt = identity.get("run_key"), identity.get("attempt")
        if not isinstance(run_key, str) or isinstance(attempt, bool) or not isinstance(attempt, int):
            raise FaultRefused("a fault injector is started with the run's identity: its run_key and attempt")
        self._origin, self._steps, self._injection = self._clock(), [], None
        self._run = {"run_key": run_key, "attempt": attempt}
        self._attempt = fault_owner.attempt_key()  # the start runs inside the attempt's context; later threads do not
        self._admit()
        self._step("sanctioned", True)
        unsettled = self._recover(heal=self._holds_exclusive())  # under the exclusive admission just taken
        if unsettled:  # held down by a live injector: the federation is not in its baseline state
            raise self._fail(f"an injection of {', '.join(unsettled)} is unsettled, so no other run may start")
        self._left_open = False  # one this injector handed to recovery is settled now: the recovery found none open
        self._wait_owed()  # a recovery's drain is satisfied before this run takes anything down
        self._await_drain()  # the record is read after the journal was: see `_await_drain`

    def _inject_prepared(self, block: PreparedInjection) -> None:
        """`inject` of `block`: refuse a call outside it or a second one, and one that a clear came before, then, for a
        caller's block, re-check what the preparation found (`_recheck`), and take the target down (`_inject`)."""
        with self._mutex:
            if self._block is not block or not block._open:
                raise RuntimeError("inject() is called inside its own prepared block, before that block ends")
            if block._used:
                raise RuntimeError("inject() is called once per prepared block")
            block._used = True
            if block._cleared:
                raise self._fail(f"a clear of {self.target} came before its injection, so nothing is taken down")
            if not block._own:  # `start`'s own block has no body between its halves: it reads nothing again
                self._recheck(block)
            self._inject()

    def _epoch_note(self, *, make: bool = False) -> tuple[str, int] | str | None:
        """The federation's link epoch as it stands (`fault_epoch`): its `id` and count, None when there is no record,
        or a word for a record that cannot be read or made. With `make`, a record is made when there is none
        (`LinkEpoch.ensure`, as the fault gate's check makes it). Never raised: `inject` refuses on what it says."""
        epochs = LinkEpoch(self._settings.journal)
        try:
            record = epochs.ensure() if make else epochs.read()
        except (OSError, ValueError) as error:
            return f"unreadable ({type(error).__name__})"
        return None if record is None else (record["id"], record["epoch"])

    def _recheck(self, block: PreparedInjection) -> None:
        """Before the target's lock: what the preparation found and a baseline may have outlived, read again with no
        reading of the injector's clock and no step (see "A baseline before the stop"). The federation must still be
        held exclusively by the block's admission, where `inject` is called; the journal must still hold no open
        injection; the drain record must still have passed, and the link epoch must not have moved, read once every
        link check under way has ended (`LinkEpoch.settled`, as a run's admission reads it), else the hold
        `federation_busy`. The epoch counts as unmoved only when both readings are the same `id` and count: a record
        missing (a raise spoiled it), made anew or unreadable at either end is no proof that nothing was raised."""
        admission = block._admission
        if admission is None or self._exclusive_admission() is not admission:
            raise self._fail(
                "the federation is not held exclusively for this injection where inject() is called: call it in the "
                "context the block was prepared in"
            )
        opened = self._recovery.entries(self.target)
        if opened:
            targets = ", ".join(sorted({str(entry.get("target")) for entry in opened}))
            raise self._fail(f"an injection of {targets} was journalled after this start's recovery, so none may start")
        if self._federation.drain_left() > 0:
            raise FederationBusy("the federation was closed for a drain after this start waited for it: no injection")
        epochs = LinkEpoch(self._settings.journal)
        with epochs.settled(self._settings.admission_timeout_s, self._cancel) as settled:
            if not settled:
                raise FederationBusy("a link check of the federation outlasted the wait for it: no injection")
            now = self._epoch_note()
        if not isinstance(block._epoch, tuple):
            noted = block._epoch or "missing"
            raise FederationBusy(f"the federation's link epoch was {noted} when this block was prepared: no injection")
        if now != block._epoch:
            raise FederationBusy(
                "the federation's link epoch moved since this start's preparation (a heal began, or a link check found "
                "a pair failing): no injection"
            )

    def _require_failover_order(self) -> None:
        """Refuse an owner stop whose mirrors would not be asked first (O98), before the federation is admitted, an
        earlier injection is recovered or anything is locked, journalled or stopped: for every space whose data provider
        is the target and that has a mirror, a mirror's provider id must sort below the owner's, from the ids and
        holders this injector was built with (the configuration it was given, read nowhere again), or op-worker asks the
        stopped owner first and every read waits on it (`expcore_onedata.failover`). A target that owns no space with a
        mirror is no owner stop, and this reads nothing new of it and records nothing. A platform whose ids are not
        known for such a space is refused as well: the order is never assumed. A pass changes no step and no snapshot."""
        try:
            failover.admit(self._config, (self._owner_name,))
        except failover.FailoverError as error:
            raise self._fail(f"{error}; nothing is taken down") from None

    def _inject(self) -> None:
        """The second half of `start`: lock the target, journal the injection, stop it and see it down."""
        self._lock = self._journal.try_lock(self.target)
        if self._lock is None:
            raise self._fail(f"{self.target} is held by another run's fault injector")
        digest, entry = self._journal_injection()
        self._pending, self._went_down, self._tried = (entry, digest), False, 0
        self._injection = {"target": self.target, "injected": False, "cleared": False, "verified": False}
        try:
            # durable before the command, so a process killed while it blocks leaves the line (a failing journal stops here)
            self._journal.append({"event": "stopping", "id": entry})
            self._stop_unknown = True  # until the command returns, the daemon may be stopping the target all the same
            self._driver.stop(self._service)
            self._stop_unknown = False
            self._went_down = self._recovery.wait(lambda seen: not seen.running, self._service) is not None
            if not self._went_down:
                raise _NotSeenDown()
            self._journal.append({"event": "injected", "id": entry, **stop_record(self._driver)})
        except BaseException as error:  # whatever broke after the stop, the target is brought back before it is raised
            why = type(error).__name__
            self._step(
                "injected",
                False,
                identity_before_sha256=digest,
                seen_down=self._went_down,
                error=why,
                **stop_record(self._driver),
            )
            if self._stop_unknown:
                self._journal_stop_unknown(entry)
            self._clear()
            if not isinstance(error, Exception):
                raise
            raise self._fail(f"{self.target} could not be taken down, seen down and journalled ({why})") from None
        self._injection["injected"] = True
        self._step("injected", True, identity_before_sha256=digest, **stop_record(self._driver))
        if self._run is not None:  # the target is seen down: a cell blocked on its data is this run's own outage (O97)
            fault_owner.hold(self._run["run_key"], self.target, self, attempt=self._attempt)

    def _release_held(self) -> None:
        """The run no longer holds the target down, from the moment a restore is asked for, or an exit of `start` or
        `running` is taken (`fault_owner.release`, under the attempt's token taken when the injection began)."""
        if self._run is not None:
            fault_owner.release(self._run["run_key"], self.target, attempt=self._attempt)

    def _wait_owed(self) -> None:
        """When this start's recovery restored a provider an earlier run left down, wait out the drain it owes before
        the start goes on: that run's sessions may still hold on-demand jobs on the local provider, and this run's reads
        must not meet them. Recorded as a `recovery_drain` step. The wait is the admission's own plan (`SessionDrain.wait`),
        so it counts what the recovery already took."""
        admission = self._exclusive_admission()
        drain = None if admission is None else admission.drain
        if drain is None or not drain.owing:
            return
        record = drain.wait()
        self._step("recovery_drain", True, drain_s=record["drain_s"], waited_s=round(record["waited_s"], 3))

    def _await_drain(self) -> None:
        """Wait, inside `admission_timeout_s` and until cancelled, for the federation's drain record to pass. It is read
        after the journal was (`_recover` read it): a restore that cleared an injection without holding this admission
        (the health gate's recovery of a stuck holder's injection) wrote its record before it cleared, so a start that
        finds the journal settled finds the record too, whatever it read before. A start that cannot wait it out is the
        hold `federation_busy` (`FederationBusy`), as a wait for the lock that runs out is, with nothing taken down; a
        cancelled wait fails as `injector_failure`."""
        deadline = self._clock() + self._settings.admission_timeout_s
        while (left := self._federation.drain_left()) > 0:
            if self._cancel.is_set():
                raise self._fail("the wait for the federation was cancelled")
            if self._clock() >= deadline:
                raise FederationBusy(
                    "the federation stayed closed past the bound of the wait: an outage run's sessions drain"
                )
            self._sleep(min(left, 0.5))

    def _journal_stop_unknown(self, entry: str) -> None:
        """Journal that the stop command raised, so that a later process settling the injection waits for the target
        to show down too. The journal may be what failed: the injection stays open all the same, and this injector's
        own clear still knows. A stop that answered unconfirmed carries its evidence (`stop_record`), as `injected` does."""
        with contextlib.suppress(OSError):
            self._journal.append({"event": "stop_unknown", "id": entry, **stop_record(self._driver)})

    def _admit(self) -> None:
        """Hold the federation exclusively for the whole injection: the run's own admission, which must then be
        exclusive, or one taken here and kept until the restore is verified. The wait for the lock in the second case
        is in no token's window: on a platform with `[faults]` no step that uses a token runs without the run's
        admission (`admission.begin_token_step`), so this context has minted none, and no run's step can start while
        the admission taken here is held (O35)."""
        held = current_admission()
        if held is not None:
            require_same_federation(held, self._federation.federation)  # another federation's admission is none
            if not held.exclusive:
                raise AdmissionRefused(
                    "an outage run holds the federation exclusively for its whole run and never shared: no upgrade"
                )
            held.mark(self.target)  # the run's holder record names the target it takes down
        elif self._admission is None or not self._admission.held:
            timeout_s, cancel = self._settings.admission_timeout_s, self._cancel
            try:
                self._admission = self._federation.acquire(
                    exclusive=True, timeout_s=timeout_s, cancel=cancel, purpose=INJECTOR, target=self.target
                )
            except FederationBusy:  # another run holds the federation: a hold that costs no attempt, never a failure
                raise
            except InfraError as error:
                raise self._fail(error.detail) from None

    def _holds_exclusive(self) -> bool:
        """Whether the federation is held exclusively for this injector now: the admission it took itself, or its run's
        own, still held and for this federation. Only then may a restore heal a link (O35): a run's admission that has
        ended, or none, heals nothing."""
        return self._exclusive_admission() is not None

    def _exclusive_admission(self) -> Admission | None:
        """The exclusive admission that holds the federation for this injector now: the one it took itself, else its
        run's, still held and for this federation; None when there is none."""
        if self._admission is not None and self._admission.held and self._admission.exclusive:
            return self._admission
        held = current_admission()
        if held is None or not held.exclusive:
            return None
        try:
            require_same_federation(held, self._federation.federation)
        except AdmissionRefused:
            return None
        return held

    def _release_admission(self) -> None:
        if self._admission is not None:
            self._admission.close()
            self._admission = None

    def _journal_injection(self) -> tuple[str, str]:
        """Read the target's identity and journal the injection durably, before anything stops it; on any failure
        release the lock and raise InfraError of kind `injector_failure`. Return the identity's digest and the line's id."""
        try:
            before = self._driver.observe(self._service)
            if not before.running or before.identity is None:
                raise self._fail(f"{self.target} is not running before the injection, so no restore could be confirmed")
            self._gate_probe = self._arm_gate()
            before_sha256, entry = digest(before.identity), uuid.uuid4().hex
            line = {
                "target": self.target,
                "service": self._service,
                "identity_sha256": before_sha256,
                "holder": holder(),
                "owner": liveness.owner(),  # the attempt this context runs: the verdicts charge by it (O80)
                **({} if self._gate_probe is None else {"gate_probe": self._gate_probe}),
            }
            self._journal.append({"event": "injecting", "id": entry, **line})
        except InfraError:
            self._unlock()
            raise
        except DriverError as error:  # its message names the step and the exit code, never the service
            self._unlock()
            raise self._fail(f"{self.target} could not be read before the injection: {error}") from None
        except Exception as error:  # nothing was stopped: the lock goes, and only the error's type is told (O12)
            self._unlock()
            raise self._fail(f"{self.target} could not be read and journalled ({type(error).__name__})") from None
        return before_sha256, entry

    def _arm_gate(self) -> str | None:
        """The session gate's probe file, made and seen by every provider of the probe space before anything stops
        (`fault_sessions`), so a restore's gate reads a file its provider knew; None when the driver has no gate. A
        probe that cannot be made fails the start with nothing stopped (its error's type only, O12)."""
        arm = getattr(self._driver, "arm_gate", None)
        if arm is None:
            return None
        try:
            return arm(self._service)
        except Exception as error:
            why = type(error).__name__
            raise self._fail(
                f"the session gate's probe could not be made before {self.target}'s outage ({why})"
            ) from None

    def _record_links(self, record: Mapping[str, Any]) -> None:
        """The session gate's reading and the heal's record of the latest restore, as steps of the snapshot (O35). A
        driver with no session gate (`how` none) records no gate step, so its readings stay as they were."""
        gate = record.get("gate")
        if isinstance(gate, Mapping) and gate.get("how") != "none":
            self._step("gate", gate.get("ok") is True, **{k: v for k, v in gate.items() if k != "ok"})
        skipped = record.get("heal_skipped")
        if isinstance(skipped, Mapping):
            self._step("heal_skipped", False, **skipped)
        heal = record.get("heal")
        if isinstance(heal, Mapping):
            self._step("heal", heal.get("ok") is True, **{k: v for k, v in heal.items() if k != "ok"})
        if record.get("link_record") == NOT_WRITTEN:  # a check's record was not written: the run's record says so
            self._step("link_record", False, **fact(str(record["link_record_reason"])))

    @contextlib.asynccontextmanager
    async def running(
        self, identity: Mapping[str, Any], *, cancel_wait_s: float | None = None
    ) -> AsyncIterator["FaultInjector"]:
        """Start in a worker thread for the body of an `async with`, and clear however it ends (O11).

        The start's future is kept: when the body's task is cancelled while the start runs, the thread runs on, so the
        cancellation waits for it, shielded and at most `cancel_wait_s` (by default `cancel_wait_s` of the injector,
        about the longest a start takes), then the clear runs and the cancellation is raised again. The clear, in its
        own thread, waits for a start that is still running, through the injector's lock.

        An exit that follows the body returning returns too, whether or not the restore is verified: the executor that
        runs the agent inside the block (`record = await record_run(...)`) keeps its record, which carries the failure
        itself (`snapshot`: `restore_verified` false, with `how`, `identity_changed` and `serving`) for the campaign to
        refuse to score, and `check` refuses it as well. When the body raised, no record exists to carry it, so the
        restore failure is raised from the body's exception as `session` does; a cancellation or any other
        `BaseException` is raised as it was, with the failure in its notes.
        """
        bound = self.cancel_wait_s if cancel_wait_s is None else cancel_wait_s
        self._cancel = threading.Event()
        starting = asyncio.ensure_future(asyncio.to_thread(self.start, identity))
        try:
            try:
                await asyncio.shield(starting)
            except asyncio.CancelledError:
                self._cancel.set()  # a start still waiting for the federation gives up instead of injecting
                await _outlast(starting, bound)
                raise
            yield self
        except BaseException as error:
            self._release_held()  # at once, in this thread: a cancellation must not leave the hold to the clear's thread
            await self._clear_for(bound)
            self._raise_unrestored(error)
            raise
        self._release_held()
        await self._clear_for(bound)

    async def _clear_for(self, bound: float) -> None:
        """Clear in a worker thread, shielded: a cancellation waits for it for at most `bound` and is raised again, with
        in its notes what the clear found (a restore not verified), or that it was still running when the wait ended."""
        clearing = asyncio.ensure_future(asyncio.to_thread(self.clear))
        try:
            await asyncio.shield(clearing)
        except asyncio.CancelledError as cancelled:
            await _outlast(clearing, bound)
            self._note_clear(cancelled, clearing)
            raise

    def _note_clear(self, error: BaseException, clearing: "asyncio.Future[Any]") -> None:
        """Add to `error`, a cancellation that interrupted the clear's wait, the restore failure the clear left, or that
        the clear had not finished. The injector's lock is taken only for a clear that did: a cancelled task (the event
        loop's shutdown cancels every one) says nothing of its worker thread, which may still hold the lock, and waiting
        for it would block the loop and defeat the bound."""
        if not clearing.done() or clearing.cancelled():
            error.add_note(f"the clear of {self.target} had not finished when the wait for it ended: not verified")
            return
        with self._mutex:
            failure = self._unrestored()
        if failure is not None:
            error.add_note(str(failure))

    def finish(self) -> dict[str, Any]:
        """Bring the target back and verify it (`clear`), then return the snapshot of every step. It never raises for
        a restore that is not verified, so the snapshot reaches the record whole: the instrument's `check` refuses it."""
        self.clear()
        return self.snapshot()

    @contextlib.contextmanager
    def session(self, identity: Mapping[str, Any]) -> Iterator["FaultInjector"]:
        """Inject for the body of a `with`, and clear however it ends, whatever the start or the body raises (O11).

        The exit raises `RestoreNotVerified` when the restore is not verified: the exit that returns normally is a
        verified restore. When the start or the body raised too, the restore failure is raised from that exception
        (`__cause__`) so neither is lost; an interrupt or an exit (anything but an `Exception`) is raised as it was,
        with the restore failure in its notes. The injection stays open in the journal, so the health gate and the
        next start refuse the provider, until a later `clear`, `finish` or `start` verifies the restore.
        """
        try:
            self.start(identity)
            yield self
        except BaseException as error:
            self.clear()
            self._raise_unrestored(error)
            raise
        self.clear()
        self.check()

    def check(self) -> None:
        """The post-run check (`finish_instruments` calls it once the snapshot is in): return when nothing this
        injector took down is left unrestored; raise `RestoreNotVerified`, an `InfraError` of kind `injector_failure`,
        when an injection it made is not verified restored or is still open. A start that took nothing down is not one.
        It does not clear: `finish` or `clear` did, and a record whose restore failed keeps its snapshot and holds this
        refusal (`instrument_refusal`), so the attempt is set aside and run again with its usage known."""
        with self._mutex:
            failure = self._unrestored()
        if failure is not None:
            raise failure

    def _unrestored(self) -> RestoreNotVerified | None:
        """The failure of a restore that is not verified, read from the injector's own steps; None when there is none."""
        injection = self._injection
        open_ = self._pending is not None or self._left_open  # handed to recovery, it is still open in the journal
        if not open_ and not (injection is not None and injection["injected"] and not injection["verified"]):
            return None
        cleared = [step for step in self._steps if step["step"] == "cleared"]
        verified = [step for step in self._steps if step["step"] == "verified"]
        healed = [step for step in self._steps if step["step"] == "heal"]
        return RestoreNotVerified(
            self.target,
            str(cleared[-1]["how"]) if cleared else "not_cleared",
            identity_changed=bool(verified and verified[-1]["identity_changed"]),
            serving=bool(verified and verified[-1]["ready_after_s"] is not None),
            still_open=open_,
            tried=str(healed[-1]["tried"]) if healed else None,
        )

    def _raise_unrestored(self, error: BaseException) -> None:
        """Called after a clear, while `error` is being raised: raise the failure of a restore not verified from
        `error`, or, when `error` is no `Exception` or is a platform's hold (an `InfraError` of a kind that costs no
        attempt, C33: an ingest's start its baseline's mark held back), attach it as a note and return so that `error`
        is raised as it was. A held start costs its run no attempt whatever failed after it; the restore's failure is
        still in the journal (`clear_failed`), so the health gate fails for it and no run starts on it."""
        with self._mutex:
            failure = self._unrestored()
        if failure is None:
            return
        held = isinstance(error, InfraError) and error.kind in uncharged_kinds()
        if isinstance(error, Exception) and not held:
            raise failure from error
        error.add_note(str(failure))

    def snapshot(self) -> dict[str, Any]:
        """The reading the record keeps: the run, the target, the injection and every step; `ok` only when the target
        was injected and its restore verified."""
        injection = None if self._injection is None else dict(self._injection)
        return {
            "ok": bool(injection and injection["injected"] and injection["verified"]),
            **(self._run or {"run_key": None, "attempt": None}),
            "kind": PROVIDER_OUTAGE,
            "target": self.target,
            "injections": [] if injection is None else [injection],
            "steps": [dict(step) for step in self._steps],
            **self._restore_reading(injection),
            **self._link_record_reading(),
        }

    def _link_record_reading(self) -> dict[str, Any]:
        """The optional fact that a link check's record was not written (`linkrecord.fact`), from the latest step that
        holds it, a `link_record` step or a `recovered` one; nothing when every record was written."""
        for step in reversed(self._steps):
            if step.get("link_record") == NOT_WRITTEN:
                return fact(str(step["link_record_reason"]))
        return {}

    def _restore_reading(self, injection: Mapping[str, Any] | None) -> dict[str, Any]:
        """What the snapshot says of the restore, so that the record carries a failure itself (O11): `restore_verified`
        true once a target this injector took down is verified restored; false, with `how` the restore ended, whether the
        provider's `identity_changed` and whether it was `serving`, when an injection is open or was not verified. The
        core reads it (`expcore.records.injections.restore_refusal`) and never scores the record. Nothing, when the
        injector took nothing down and has nothing open: no restore was needed, so none is claimed verified."""
        failure = self._unrestored()
        if failure is not None:
            return {
                "restore_verified": False,
                "how": failure.how,
                "identity_changed": failure.identity_changed,
                "serving": failure.serving,
            }
        return {"restore_verified": True} if injection and injection["injected"] else {}

    # --- clearing and recovery --------------------------------------------------------------------------------

    def clear(self) -> bool:
        """Bring the target back and verify its identity changed; record the outcome and never raise for it.

        Returns True when this injector leaves nothing down. Until a restore is verified the injection stays pending,
        with the target's lock held, so a later `clear`, `finish` or `start` tries again, but only while no step of the
        run that uses its tokens runs: with one running, the injection is handed to recovery open, its lock kept until
        the run's admission ends, and this returns False, now and later (`_retry_now`); a verified one makes calling it
        again harmless. A target the injection never
        took down is found running with its old identity, unless the stop's outcome is unknown: then the clear first
        waits for the target to show not running. An injector discarded with its restore still unverified lets the lock
        go when it is collected, so recovery can settle it. A clear while a `prepared` block's body runs returns at
        once and makes that block's `inject` take nothing down, so nothing is stopped after a clear has returned.

        The run's hold on the target (`fault_owner.hold`) is released first, before the wait for a start and before the
        restore, so that no cell is called the agent's for a target a clear is already bringing back (O97).
        """
        self._release_held()
        with self._mutex:
            if self._block is not None:  # the caller's way to cancel a block: never waits for its baseline
                self._block._cleared = True
            # a share of the run's admission for the whole clear: a cancelled run whose context lets its admission go
            # while this worker still restores or heals keeps the federation held until the clear has ended (O35)
            give_up = retain_admission()
            try:
                return self._clear()
            finally:
                give_up()

    def _clear(self) -> bool:
        self._release_held()  # also for a restore reached by another way than `clear` (a failed start, a new start)
        if self._pending is None:
            return not self._left_open  # one handed to recovery is still open, whoever restores it later
        if self._tried and not self._retry_now():  # a retry inside the token's window: handed to recovery instead
            return False
        self._tried += 1
        with self._restoring() as (admission, own, refused):
            if refused is not None:
                # the federation could not be taken for the restore, so nothing was restored and nothing is journalled
                # (settle never ran): the injection stays open and pending, which fails closed
                self._step("cleared", False, how="federation_unavailable")
                return False
            return self._settle_pending(admission, own)

    def _retry_now(self) -> bool:
        """Before the pending injection's restore is tried again: whether it may be now, decided at once, never waited
        for (O35, O11). It may when no step of the run that uses its tokens still runs (the run's admission counts
        them, `admission.token_step`): the token's window has then closed, as in an executor that stops its sandbox and
        tool server before `running`'s exit. While one runs, a second restore inside the window would outlast the
        least margin, which counts one: the injection is handed to recovery instead (`_hand_over`). Either way a
        `restore_retry` step records the path taken, why, and the steps that ran."""
        held = current_admission()
        running = list(held.open_steps()) if held is not None else []
        if held is None or not running:  # a step that uses a token runs only under the run's admission
            why = "no step of the run that uses its tokens still runs, so the restore is tried again outside its window"
            self._step("restore_retry", True, path="after_steps", why=why, steps=running)
            return True
        why = (
            "a step of the run that uses its tokens still runs, so a second restore would fall inside the token's "
            "window: the injection is left open, the run set aside, and the next start's recovery restores it"
        )
        self._step("restore_retry", False, path="left_open", why=why, steps=running)
        self._hand_over(held)
        return False

    def _hand_over(self, admission: Admission) -> None:
        """Leave the pending injection open in the journal (`clear_failed` already says how), for the recovery of the
        next start or of the health gate, and touch the target no more. Its lock goes with the run's admission, one of
        the admission's `endings`, so nothing restores it inside the run's token window: until then a recovery, this
        injector's own included, finds it held by a live injector and leaves it (a start fails closed), and once the
        admission's last share is let go, after the run's post-run check, recovery may take it. The closure holds the
        lock alone, never the injector. The snapshot keeps the restore unverified and the injection open
        (`_unrestored`), so the record is never scored (#245), and a later `clear` reports it still down."""
        lock, self._lock = self._lock, None
        self._pending, self._stop_unknown, self._left_open = None, False, True
        if lock is not None:
            admission.endings.append(lock.close)

    def _settle_pending(self, admission: Admission | None, own: bool) -> bool:
        """Settle the pending injection under `admission`, which the restore keeps held: the drain is armed before the
        provider is brought back, and an injector that holds the federation by an admission of its own waits it out
        before it lets go (`_drain`)."""
        assert self._pending is not None
        entry, digest = self._pending
        injection = self._injection if self._injection is not None else {"target": self.target, "injected": False}
        began = self._clock()  # the restore's start, from which an unknown end of the run's sessions is drained in full
        drain, fresh = self._arm(admission, began)  # before the provider is brought back
        try:
            done, how, after, ready_after_s = self._recovery.settle(
                {
                    "id": entry,
                    "target": self.target,
                    "service": self._service,
                    "identity_sha256": digest,
                    "injected": self._went_down,
                    "stop_unknown": self._stop_unknown,
                    "gate_probe": self._gate_probe,
                },
                heal=self._holds_exclusive(),  # a heal only under an exclusive admission held now, between runs
            )
        except Exception as error:  # never lose the reading: a clear that failed stays pending and open
            done, how, after, ready_after_s = False, type(error).__name__, None, None
        if drain is not None and fresh and done and how == "never_down":
            drain.disarm()  # the provider never went down: nothing was left to drain
        verified = done and how in ("restored", "found_restored")
        timed = {} if ready_after_s is None else {"ready_after_s": ready_after_s}
        waited = self._recovery.restore_record.get("domain_wait_s") if ready_after_s is not None else None
        if waited is not None:  # only a driver that reported it: never a 0 nobody measured (O58)
            timed["domain_wait_s"] = waited
        start_timing = self._recovery.restore_record.get("start_timing")  # where the start's time went, served or not
        if start_timing is not None:
            timed["start_timing"] = start_timing
        self._injection = {**injection, "cleared": done, "verified": verified, **timed}
        self._record_links(self._recovery.restore_record)
        self._step("cleared", done, how=how)
        self._step(
            "verified",
            verified,
            identity_changed=after is not None,
            identity_after_sha256=after,
            ready_after_s=ready_after_s,
            **({} if waited is None else {"domain_wait_s": waited}),
            **({} if start_timing is None else {"start_timing": start_timing}),
        )
        if done:
            self._drain(admission, own, how, began)  # before the federation goes back to the other runs
            self._pending, self._stop_unknown = None, False
            self._unlock()
            self._release_admission()
        return done

    def recover(self) -> list[str]:
        """Clear and verify every open injection of the journal that no live injector holds; raise InfraError of kind
        `injector_failure` when one cannot be. Return the targets of the injections left open, which live injectors
        hold. Refused at once (`RuntimeError`) while a `prepared` block is open."""
        self._refuse_inside_a_block("recover")
        with self._mutex:
            if self._block is not None:
                raise RuntimeError(_INSIDE_A_BLOCK.format(what="recover"))
            return self._recover()

    def _recover(self, *, heal: bool = False) -> list[str]:
        """Recovery, healing a stuck link only when `heal`: the caller holds the federation exclusively (`_start`). A
        provider it restored went down in a run this injector knows nothing of, so its sessions are unknown whatever the
        current run's sandbox says: the exclusive admission it restores under owes the full drain from the start of the
        recovery (`SessionDrain.owe`), decided against the injections the recovery has just read and before it settles
        one (`fault_drain`), including one a live injector holds, which makes the drain longer than needed and no worse."""
        self._require_sanction()
        with self._restoring() as (admission, _, refused):
            if refused is not None:
                raise refused
            drain = None if admission is None else admission.drain
            owe = (lambda entries: drain.owe(self._clock(), clock=self._clock, sleep=self._sleep)) if drain else None
            return self._recovery.recover(self._step, heal=heal, before=owe)

    @contextlib.contextmanager
    def _restoring(self) -> Iterator[tuple[Admission | None, bool, InfraError | None]]:
        """The exclusive admission a restore runs under, for the restore's whole duration, so that nothing is admitted
        while it restores and the drain it arms is published when it ends (`fault_drain`). Yield `(admission, own,
        refused)`:

        - the exclusive admission already held, a share of it kept for the restore (`Admission.try_retain`: a cancelled
          owner cannot let the lock go under it; one whose last share is already given up is not held for this purpose);
        - else an exclusive admission this injector takes for the restore and gives up when it ends (`own` true: no
          sandbox of the run is under it), which waits for the federation inside `[faults] admission_timeout_s`; one it
          cannot get is `refused`, and nothing is restored: the hold `federation_busy` (`FederationBusy`) when the wait
          ran out, else an InfraError of kind `injector_failure`;
        - with `session_drain_s` 0, nothing: there is no drain to keep.

        Declared limits. A restore that holds no admission waits for the federation (and for a drain still standing)
        inside `admission_timeout_s`, which `cancel_wait_s` does not count: inside `running` the run's or the injector's
        own admission is always held, so only a later retry of `clear` or `finish` takes that branch. The least token
        margin counts the drain record that branch waits out before it restores, and the drain `_drain` waits after it
        (`fault_settings.restore_drain_s`); the wait for the lock itself it does not, since no exclusive admission is
        then held for this context and no step that uses a token runs without the run's (`admission.begin_token_step`;
        a retry while one runs is handed to recovery first, `_retry_now`). The holder record of an admission
        taken for a restore names this injector's own target, not the one recovered.
        """
        if self._settings.session_drain_s <= 0:
            yield None, False, None
            return
        held = self._exclusive_admission()
        give_up = held.try_retain() if held is not None else None
        if held is not None and give_up is not None:
            try:
                yield held, held is self._admission, None
            finally:
                give_up()
            return
        try:
            taken = self._federation.acquire(
                exclusive=True,
                timeout_s=self._settings.admission_timeout_s,
                cancel=self._cancel,
                purpose=INJECTOR,
                target=self.target,
            )
        except FederationBusy as busy:  # another run holds the federation, or it drains: a hold, never a failure (O11)
            yield None, False, busy
            return
        except InfraError as error:
            yield None, False, self._fail(error.detail)
            return
        try:
            yield taken, True, None
        finally:
            taken.close()

    def _arm(self, admission: Admission | None, began: float) -> tuple[SessionDrain | None, bool]:
        """Before a restore of a provider that may have gone down: arm the drain of the admission it runs under. Return
        the drain, and whether this call armed it (for a restore that finds nothing to restore, `SessionDrain.disarm`)."""
        if admission is None or admission.drain is None:
            return None, False
        drain = admission.drain
        fresh = not drain.armed
        drain.arm(began, clock=self._clock, sleep=self._sleep)
        return drain, fresh

    def _drain(self, admission: Admission | None, own: bool, how: str, began: float) -> None:
        """After a verified restore of a provider that went down, record a `drain` step. An injector that holds the
        federation by an admission of its own (`own`) waits the drain out here, before it lets go: it has no sandbox of
        its own run. A run's admission is waited out when the run ends, with the sandbox's mount and gate ends known; the
        restore comes before them, so the step records the plan as it stands (`deferred`), with the mount unknown if it
        is still up."""
        if how not in RESTORED or self._settings.session_drain_s <= 0:
            return
        if admission is None or admission.drain is None:
            self._step("drain", False, why="no exclusive admission holds the federation: nothing keeps it closed")
            return
        admission.drain.arm(began, clock=self._clock, sleep=self._sleep)
        record = admission.drain.wait() if own else {**admission.drain.plan().record(), "waited_s": 0.0}

        def since_start(moment: float | None) -> float | None:
            return None if moment is None else round(moment - self._origin, 3)

        self._step(
            "drain",
            True,
            deferred=not own,
            drain_s=record["drain_s"],
            basis=record["basis"],
            why=record["why"],
            mount_end_s=since_start(record["mount_end"]),
            gate_stop_s=since_start(record["gate_stop"]),
            restore_start_s=since_start(record["restore_started"]),
            until_s=since_start(record["until"]),
            waited_s=round(record["waited_s"], 3),
        )

    def _unlock(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None


async def _outlast(future: "asyncio.Future[Any]", bound: float) -> None:
    """Wait, shielded and at most `bound` seconds, for a worker thread's future that a cancellation left running."""
    with contextlib.suppress(asyncio.CancelledError):  # cancelled again: the thread still runs, the bound still holds
        await asyncio.wait({future}, timeout=bound)
    if future.done() and not future.cancelled():
        future.exception()  # read here: what the thread raised is the clear's to answer, never an unread error

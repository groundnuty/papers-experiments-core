# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/scheduler.py, blob ab7e21172a589eaee0f70b47eb5f285fc21abeca
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The campaign loop (#16): dispatch the manifest in order with per-provider
caps, endpoint pauses, health checks and the infra-failure rule."""
from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import inspect
import json
import math
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from dataset_76 import opwrestart, paths
from dataset_76.records import RunId, RunRecord

from .config import CampaignConfig, _zoned, group_of
from .endpoints import API_SLOT, backend_of, fresh_endpoints, provider_i_slot, pick_slot, read_endpoints, usable_slots
from .errors import KEY_REJECTED, ONEDATA_OUTAGE, PRE_CHECK_FAILED, is_infra_error
from .hostmem import HostMemoryWatchdog
from .manifest import ManifestRow, build_manifest
from . import healthgate as HG
from . import livecheck as LC
from . import release
from . import endpoint_caps
from . import harness_stop as HS
from .store import Store, harness_error_kind, unstarted_streaks

Executor = Callable[[RunId, int, Path, str, Any], RunRecord]
HealthCheck = Callable[[], tuple[bool, str]]
LEFTOVER_RETRY_S = 60.0     # a dead holder's sandbox that could not be shown gone, or a sweep that could not read the pool, is tried again this often


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.isoformat()


class CampaignLocked(RuntimeError):
    pass


class Campaign:
    hosts: Any = None                           # hosts.HostBook with two run hosts (set in open()); a class default, so a bare instance has one host

    def __init__(self, cfg: CampaignConfig, rows: list[ManifestRow], execute_run: Executor,
                 health_check: HealthCheck, root: Path | None = None,
                 clock: Callable[[], dt.datetime] = utc_now,
                 resolve_image: Callable[[str], str] | None = None,
                 mem_reader: Callable[[], int | None] | None = None,
                 slot_probe: Callable[[dict], Any] | None = None, slot_pins: dict[str, dict] | None = None):
        self.resolve_image = resolve_image
        from .slot_equality import SlotGate, http_probe, pins_from_models
        nofp = frozenset(cfg.slot_no_fingerprint_models)
        base_probe = slot_probe or (lambda e: http_probe(e, no_fp_models=nofp))

        def routed_probe(e: dict):                  # two hosts: a slot is read from the host whose models use it (its own supervisor)
            h = e.get("_host")
            if h and self.hosts is not None:
                return self.hosts.slot_probe(h, e, nofp) if slot_probe is None else slot_probe(e)
            return base_probe(e)
        if cfg.slot_equality_check and slot_pins is None:      # the registered pins: every slot, the first too, must serve them
            from dataset_76.harness.config import load_config
            slot_pins = pins_from_models(load_config().models)
        self.slot_gate = SlotGate(routed_probe, cfg.slot_unknown_blocks, no_fingerprint_models=nofp,
                                  pins=slot_pins, peer_blocks=cfg.slot_peer_blocks) if cfg.slot_equality_check else None
        self._gate_restored = False
        from .hostmem import read_mem_available
        self.hostmem = HostMemoryWatchdog(cfg.host_mem_pause_below_gib, cfg.host_mem_resume_at_gib, mem_reader or read_mem_available)
        self._hostmem_saved = 0.0
        self._hostmem_pauses = 0
        self._hostmem_paused_since: float | None = None
        self._hostmem_paused_s = 0.0
        self._b1_wait_since: float | None = None
        self._b1_waits, self._b1_wait_s, self._b1_loaded, self._b1_hb, self._b1_start_wall = 0, 0.0, False, 0.0, 0.0
        self.cfg, self.rows, self.execute_run, self.health_check = cfg, rows, execute_run, health_check
        self.root = Path(root) if root else paths.RUNS / "campaign"
        self.clock = clock
        self.store: Store | None = None
        self.stop = threading.Event()
        self._cv = threading.Condition()
        order: dict[str, int] = {}
        for r in rows:
            order.setdefault(r.stage or r.group, len(order))        # seed-first: a stage is the priority unit; the group still decides caps and the read check
        self._group_idx: dict[str, int] = {r.run_key: order[r.stage or r.group] for r in rows}
        self._stage_name: dict[str, str] = {r.run_key: r.stage or r.group for r in rows}
        self._staged = any(r.stage for r in rows)            # seed-first rows: the stage is the priority unit, and the seed boundary and quiet window apply
        self._seed_of: dict[str, int] = {r.run_key: r.run.seed for r in rows if r.stage}      # the seed-first runs only: the pilot and calibration have no boundary
        self._crossed: set[tuple[str, int]] | None = None        # (held model, seed) pairs whose boundary crossing is already on the record
        self._cross_counts: dict[str, dict[str, int]] | None = None     # held model -> seed -> runs of that seed that started ahead of its unstarted earlier seed (kept in the store)
        self._lost: dict[str, dict] | None = None                # model -> {since_utc, why, held_utc}: no usable backend since; held once the launch config's time has passed (kept in the store)
        self._seed_models = sorted({r.run.model for r in rows if r.stage})
        self._quiet: dict | None = None; self._quiet_at: dt.datetime | None = None; self._quiet_check_at = -1e9     # the quiet window of 29(g), loaded from the store on first use
        self._group_name: dict[str, str] = {r.run_key: r.group for r in rows}
        # groups of the blocks that run together (pilot, calibration) share ONE read check: it runs between blocks, with nothing in flight, so a check of its own
        # before calibration would hold calibration until the pilot's last run ended, which is the order amendment 29(g) replaces
        together = list(dict.fromkeys(r.group for r in rows if r.block in cfg.concurrent_barrier_blocks))
        self._probe_alias: dict[str, str] = {g: together[0] for g in together}
        # Amendment 39: the groups of one stage share ONE read check too (the manifest puts seed 1's block 1, tier A and tier B in one stage), for the same reason: the check runs only with
        # nothing in flight, so a check of its own before tier A's first run would hold dispatch until every block-1 run in flight had ended, and undo the merge. Each group maps to the
        # first group of a stage it shares; a group already mapped (pilot, calibration) keeps its alias. The check is per group, so a later seed's groups, probed already, need no more.
        by_stage: dict[str, list[str]] = {}
        for r in rows:
            if r.stage and r.group not in by_stage.setdefault(r.stage, []):
                by_stage[r.stage].append(r.group)
        for gs in by_stage.values():
            for g in gs[1:]:
                self._probe_alias.setdefault(g, gs[0])
        self._option_of: dict[str, str] = {r.run_key: getattr(r.run.option, "value", str(r.run.option)) for r in rows}
        self._layout_of: dict[str, str] = {r.run_key: getattr(r.run.layout, "value", str(r.run.layout)) for r in rows}
        self._model_of: dict[str, str] = {r.run_key: r.run.model for r in rows}
        # Endpoint health per backend unit (an HPC slot, an LLM Lab model, a Claude model): consecutive
        # endpoint failures, the monotonic time dispatch to it is paused until, and whether it is
        # considered down (set at the pause, cleared only by a success, so a probe after the pause
        # re-pauses on one failure and an outage window stays open until real recovery).
        self._unit_fails: dict[str, int] = {}
        self._unit_pause: dict[str, float] = {}
        self._unit_down: set[str] = set()
        self._unit_lock = threading.Lock()  # update + persist of a unit's health is one atomic step
        self._unit_parent: dict[str, str] = {}   # unit -> its model, for alarm de-duplication
        self._unit_seen: set[str] = set()   # non-Claude units that ever failed: their windows are tracked
        self._key_held: dict[str, dict] = {}   # unit -> its key rejection (kept in the store, meta key_rejected): held until a probe run is accepted
        try:
            ps = inspect.signature(execute_run).parameters
            self._pass_cap = "provider_cap" in ps or any(p.kind is p.VAR_KEYWORD for p in ps.values())
            self._pass_conc = "concurrency_at_start" in ps      # the host router: one process counts both hosts' runs of a cap's scope
        except (TypeError, ValueError):
            self._pass_cap = self._pass_conc = False
        self._served_stop: dict[str, dict] = {}; self._served_loaded = False; self._served_unreported: set[str] = set()
        self._provider_i_fp: dict[str, dict[str, dict]] | None = None   # model -> LLM Lab build -> where it was first seen (kept in the store)
        self._ep_unreadable = False          # endpoints.json unreadable at the last look: its changes are events
        self._alive_at = -1e9                # monotonic time the dispatch loop last recorded that it is looking
        self._since_noted = False
        self._hard: dt.datetime | None = None; self._cohort: dt.datetime | None = None; self._hard_logged = False
        self._fc_at: float | None = None
        self._frm_at: float | None = None       # monotonic time of fr's memory check (progress.update_fr_memory); an alarm only, nothing in dispatch reads it
        self._rc: dict | None = None; self._rc_at = -1e9; self._probed: set[str] | None = None      # the pre-block read check (provider_l.py)
        self._unstarted_streaks: dict[str, int] = {}; self._unstarted_loaded = False     # per option: consecutive refusals to start a sandbox (no slot user)
        self._he: dict | None = None; self._he_stopped_models: set[str] = set()          # the harness-error stops in force (harness_stop.py): loaded once, kept in the store
        self._canary_run: str | None = None; self._canary_epoch: float | None = None; self._canary_cache: dict | None = None     # the unstarted stop's canary (_canary_options)
        self._lg: dict | None = None; self._lg_at = -1e9     # the kept-log check's last answer
        self._fr_run = subprocess.run   # tests replace it
        self._fr_at_dispatch: dict[str, str | None] = {}
        self._fr: dict | None = None; self._fr_at = -1e9; self._fr_state: str | None = None   # fr's settings hash (dataset_76/provider_m.py)
        self._gate: dict | None = None; self._gate_at = 0.0; self._gate_sig: tuple | None = None   # the model_66 gate (gate.py)
        self._provider_g_cap: int | None = None   # derived from the up slots at each dispatch pass (_provider_g_pool_cap)
        self._ep_wait: dict[str, dict[str, Any]] = {}   # per model, in memory: the dispatch passes that found its next run waiting for endpoint caps (amendment 41(b); _note_endpoint_wait)
        self._caps_raw: str | None = None     # the text of the endpoint-caps override last looked at (applied or rejected), re-read every dispatch round (_caps_sync)
        self._caps_launch: tuple[dict, dict] | None = None   # the launch file's (provider_i_caps, provider_g_slot_caps), taken when an override is first applied: what a record is merged over
        self._inflight: dict[str, tuple[str, str, str]] = {}  # run_key -> (model, provider, slot)
        self._not_before: dict[str, float] = {}               # run_key -> monotonic time
        self._opw_cache: tuple[tuple[int, int] | None, list] = (None, [])
        self._opw_held: str | None = None
        self._pause_until = 0.0                                # Onedata pre-check pause (no longer set by a failed pre-run check: the health gate below holds instead)
        # The Onedata health gate (healthgate.py): closed by the first failed pre-run check, opened by the dispatcher's own check passing. `_hg` is the open outage's record
        # (None: open gate), also kept in the outage list the store holds; `_hg_lock` makes "who found it" and the record's writes one step each.
        self._hg: dict | None = None
        self._hg_outages: list[dict] = []
        self._hg_lock = threading.Lock()
        self._hg_wake = threading.Event()                      # set when the gate closes: the gate thread stops waiting for one
        self._hg_idle = False                                  # set by the dispatch pass: nothing may start (past the hard stop), so there is nothing to check for
        self._hg_check_now = False                             # a gate restored by a restart is checked at once, not after a period
        self._hg_call: dict | None = None                      # the gate's check in flight: one at a time
        self._gate_thread: threading.Thread | None = None
        self._lockfile: Any = None
        self._workers: list[threading.Thread] = []
        self._keys = {r.run_key for r in rows}
        self._fatal: BaseException | None = None
        self.hosts: Any = None                  # hosts.HostBook when the config names two hosts (open()); None: one host, as before
        self._host_transports: dict | None = None   # tests hand in stand-ins; otherwise ssh from the config
        self._prober: threading.Thread | None = None
        self._leftover_sweep: threading.Thread | None = None
        self._held_back: list[str] = []         # slot users whose dead holder's sandbox could not be shown gone (_sweep_leftovers): each counts as a sandbox in flight in
                                                # _within_every_cap (the global and block caps) and in _host_holds (this host's max_sandboxes); NOT in _option_step's option
                                                # caps, the fr read check's wait for a gap, or the D disk reserve (they count only what is in flight or an orphan)
        self._held_back_unknown: str | None = None   # why the last sweep could not read the pool (None: it could): the status alarms until one does
        self._slot_pool: Any = None             # tests hand in a stand-in; otherwise this host's own pool
        self._cap_held: dict[str, tuple] = {}   # run_key -> (model, host, option, group, seed, since): runs held by their host's option cap
        self._dispatched_at: dict[str, float] = {}   # run_key -> monotonic time of its latest dispatch: a measure older than that is another attempt's
        self._use_cache: dict[str, tuple] = {}  # run_key -> (monotonic, diskuse) of a local run's sandbox
        self._live = LC.LiveState()             # the level in force and the live check (amendment 25(e)): the steps, the streaks, the probe history; the store's record, re-read every dispatch round
        self._live_raw: str | None = None       # the text of the record `_live` was made from
        self._live_lock = threading.Lock()      # update + persist of the live state is one atomic step (never taken while holding it: _cv)
        self._probe_places = 0                  # 1 while a live-check probe is waiting for its place in the level or its sandbox is up: it counts in every total
        self._live_stop = threading.Event()     # set at the end of run() and at close(): the live-check thread ends and a probe in flight is asked to end (its sandbox with it)
        self._live_thread: threading.Thread | None = None
        self._probe_proc: subprocess.Popen | None = None      # the probe process in flight, so that a stop can reach it
        self._live_run: Callable | None = None  # tests replace it; None: the real, stoppable process (`_probe_process`)
        self._rise_noted: tuple | None = None   # the (state, why) of the rise check last recorded as a hold: said once, not at every check
        self._rise_at = -1e9                    # monotonic time of the last rise check
        self._rise_errors: Callable | None = None       # tests replace it: (t0, t1) -> (reading or None, why); None: the records (`livecheck.attempt_errors`)
        self._attempt_errors_cache: dict = {}
        self._res_dir: Path | None = None       # the process reservations (dataset_76/harness/reservation.py): the directory of their locks, the names held at the last look,
        self._res_held: set[str] = set()        # and the ones whose lock could not be read (name -> why; each also counts as held)
        self._res_bad: dict[str, str] = {}
        self._res_env: dict[str, str | None] | None = None   # the environment the local pool reads, as this launch found it

    # --- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self.cfg.require_known_code:                  # a campaign records the code version on every run: none known, none starts
            from dataset_76 import codestamp
            codestamp.require_known("campaign", Path(__file__).resolve().parents[2])
        self.root.mkdir(parents=True, exist_ok=True)
        self._lockfile = open(self.root / "campaign.lock", "w")
        try:
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CampaignLocked(f"another campaign process holds {self.root / 'campaign.lock'}")
        try:
            self.store = Store(self.root / "state.sqlite")
            self._hard, self._cohort = self.cfg.hard_stop(), self.cfg.cohort_cut()      # one that cannot be read stops the start: a campaign must know when to stop
            cut = iso(self._cohort) if self._cohort is not None else "none"
            was = self.store.get_meta("cohort_cut_utc")
            barrier = list(self.cfg.barrier_blocks)
            if was is not None and was != cut and self.store.q(      # amendment 29(c): the cut is fixed before any main run and cannot move after one has started
                    "SELECT 1 FROM attempts a JOIN runs r USING(run_key) WHERE a.state != 'unstarted' AND r.block NOT IN (%s) LIMIT 1" % ",".join("?" * len(barrier)), barrier):
                raise ValueError(f"the cohort cut is stamped {was} and this launch's config gives {cut}: it cannot move after a main run has started (amendment 29(c)). "
                                 "Launch with the snapshot_utc and cohort_cut_utc it was started with")
            self.store.put_meta("cohort_cut_utc", cut, iso(self.clock()))    # the export tells a cohort run from a later one by it
            if self._hard is None and self.cfg.hard_stop_required:
                raise ValueError("no hard_stop_utc in the campaign config: a campaign must know when to stop. Set it, or set hard_stop_required false, which the event log says")
            if self.cfg.coder_spend_required:
                from .progress import coder_config_problem
                why = coder_config_problem(self.cfg)
                if why:
                    raise ValueError(f"{why}: the spend alarm could not see the coders' spend and would read UNKNOWN all campaign. Set coder_work_dir to the "
                                     "coders' runner's --work directory (its answers/ created empty if no coder has run) and coder_packets_dir or "
                                     "coder_expected_packets, or set coder_spend_required false, which the event log says")
            self._open_launch_times()
            off = [k for k in ("read_check_required", "fr_settings_required", "hard_stop_required", "coder_spend_required", "launch_times_required", "live_check_required") if not getattr(self.cfg, k)]
            off += [k for k in ("slot_equality_check", "slot_unknown_blocks", "slot_peer_blocks") if not getattr(self.cfg, k)]      # the provider_g identity gate, or the part of it that holds an unread slot out, or the peer comparison's block (amendment 41)
            if self.cfg.live_check_required and not self.cfg.live_check_cmds and self.cfg.sandbox_image:      # a real launch with no live-check commands: the guard of 25(e) is not there
                off.append("live_check_cmds (none given in a real launch)")
            if not self.cfg.fr_memory_file:                  # the memory alarm reads a file: with none set it is off (required false) or reads unknown from its first check
                off.append("fr_memory_file (not set)" if self.cfg.fr_memory_required else "fr_memory_required")
            if off:                                      # on unless set off, and when set off it is on the record
                self.store.event(iso(self.clock()), "safety_checks_off", None, ", ".join(off) + " set off in this launch's config")
            self._caps_sync(at_start=True)               # amendment 41(b): the caps an earlier process was given live stay in force over a restart
            self._pin_sandbox_image()
            self._open_live_check()
            self._open_hosts()
            self._open_reservations()
            self._require_block_caps()
            self.store.sync_manifest(self.rows)
            self._reconcile_remote()
            self._reconcile()
            self.store.recover(iso(self.clock()))
            self._requeue_harness_errors()               # after the reconcile, which may commit a harness error from its marker
            self._load_units()
            self._load_gate()
            self._sweep_leftovers()
        except BaseException:
            self.close()
            raise

    def _open_launch_times(self) -> None:
        """Amendment 29(c) and (g): the snapshot's t (the cohort cut is t minus the time cap) and the quiet window's latest time come from the launch config, never
        from the code. A launch without them would stamp every run into the cohort and never pause for the window, silently, so it refuses to start unless
        `launch_times_required` is set off, which the event log says. A time that cannot be read stops the start. Amendment 30(g): the window opens "at the fixed
        snapshot's cut", which is the cohort cut, so a launch that requires its times refuses unless `quiet_window_utc` is that cut to the second: two keys that could
        differ silently (the final time cap moves the cut and not the other) now cannot."""
        if self.cfg.launch_times_required and (not self.cfg.snapshot_utc or not self.cfg.quiet_window_utc):
            raise ValueError("no snapshot_utc or no quiet_window_utc in the campaign config: the cohort cut (amendment 29(c)) and the quiet window (29(g)) need them. "
                             "Set both, or set launch_times_required false, which the event log says")
        self._quiet_at = _zoned(self.cfg.quiet_window_utc) if self.cfg.quiet_window_utc else None
        if self.cfg.launch_times_required and self._quiet_at is not None and (self._cohort is None or int(self._quiet_at.timestamp()) != int(self._cohort.timestamp())):
            cut = "none" if self._cohort is None else iso(self._cohort)
            how = (f"cohort_cut_utc {self.cfg.cohort_cut_utc}" if self.cfg.cohort_cut_utc
                   else f"snapshot_utc {self.cfg.snapshot_utc} less the time cap of {self.cfg.run_time_cap_s():g} s")
            raise ValueError(f"quiet_window_utc {self.cfg.quiet_window_utc} is not the cohort cut {cut} ({how}): amendment 30(g) opens the quiet window at the fixed snapshot's cut, "
                             f"so the two must agree to the second. Set quiet_window_utc to {cut}, or set launch_times_required false, which the event log says")

    def _sweep_leftovers(self) -> None:
        """Real runs (the lead, 2 Nov): a restart is the likeliest recovery in a long campaign, and a dead dispatcher leaves its sandboxes running, under slot users
        the kernel has already freed, and their loop mounts for good (leaked ones have frozen a VM here). They go now, logged first (slot_lease, dataset_76.sandbox.reap).
        A slot user whose sandbox cannot go stays held back and counts as a sandbox in flight (on the caps `_held_back` names), and the status alarms;
        `_leftover_loop` keeps trying."""
        if not (self.cfg.reap_at_open and self.cfg.sandbox_image):
            return
        assert self.store
        was, was_unknown = list(self._held_back), self._held_back_unknown
        try:
            if self._slot_pool is None:
                from dataset_76.harness.slot_lease import SlotPool
                self._slot_pool = SlotPool(reap_log_dir=self._attempt_dir_of)
            self._held_back = list(self._slot_pool.sweep_dead_holders())
        except Exception as e:  # noqa: BLE001 - what it held back before stays held back; whether more is left over is unknown, and the status alarms until a sweep reads
            why = f"{type(e).__name__}: {e}"[:300]
            self._held_back_unknown = why
            now = iso(self.clock())
            self.store.put_meta("held_back_unknown", why, now)
            if not was_unknown:
                self.store.event(now, "dead_holders_unknown", None, f"the sweep for a dead run's leftover sandboxes could not read the slot pool: {why}")
            return
        now = iso(self.clock())
        self._held_back_unknown = None
        self.store.put_meta("held_back_unknown", "", now)
        if was_unknown:
            self.store.event(now, "dead_holders_known", None, "the sweep for a dead run's leftover sandboxes read the slot pool again")
        self.store.put_meta("held_back_slot_users", json.dumps(self._held_back), now)
        if set(self._held_back) != set(was):
            self.store.event(now, "dead_holders_left" if self._held_back else "dead_holders_cleared", None,
                             f"slot users whose dead holder's sandbox is not shown gone: {self._held_back}" if self._held_back else f"cleared: {was}")

    def _attempt_dir_of(self, run_id_key: str) -> Path | None:
        """The run dir of a run's latest attempt: a dead attempt's record goes with it."""
        assert self.store
        keys = [r.run_key for r in self.rows if r.run.key() == run_id_key]
        if not keys:
            return None
        got = self.store.q(f"SELECT run_dir FROM attempts WHERE run_key IN ({','.join('?' * len(keys))}) ORDER BY id DESC LIMIT 1", keys)
        return Path(got[0][0]) if got and got[0][0] else None

    def _leftover_loop(self) -> None:
        """Each minute while a slot user is held back, or a sweep could not read the pool: dispatch goes on meanwhile (a key with a dead holder's sandbox still up is
        given to nobody by the lease itself), holding only the keys known to be stuck."""
        while (self._held_back or self._held_back_unknown) and not self._prober_stop.wait(LEFTOVER_RETRY_S):
            self._sweep_leftovers()

    def _pin_sandbox_image(self) -> None:
        """Real runs: resolve the sandbox image tag once, keep its id in the campaign state, refuse a resume that
        resolves to a different one (PinMismatch) unless cfg.repin_image."""
        if not self.cfg.sandbox_image:
            return
        resolve = self.resolve_image
        if resolve is None:
            from dataset_76.sandbox import pin_image as resolve
        assert self.store
        self.cfg.pinned_image_id = self.store.pin("sandbox_image_id", resolve(self.cfg.sandbox_image),
                                                  iso(self.clock()), self.cfg.repin_image)

    def _open_hosts(self) -> None:
        """Two hosts (hosts.py): refuse a plan that breaks a rule, pin the assignment at the first start (a resume with another one
        is refused, like the image), and keep this host's harness to its own share of the slot users."""
        from . import hosts as H
        assert self.store
        if not H.multi_host(self.cfg):
            if self.store.get_meta("host_assignment") and not self.cfg.repin_hosts:     # a resume must not drop the pinned split silently
                raise H.HostConfigError("this campaign's state pins a two-host assignment, but this launch names no hosts: its remote models "
                                        "would run here, and runs left on the other host would go unreconciled. Launch with the same hosts, "
                                        "or set repin_hosts (an operator decision, on the record)")
            self._keep_runs_off_probe_keys()
            return
        from dataset_76.faults.matcher import load_slot_map
        from dataset_76.harness.slot_lease import SLOT_MAP
        slot_map = {k: s.option for k, s in load_slot_map(SLOT_MAP).items()}
        opts: dict[str, set[str]] = {}
        for r in self.rows:
            opts.setdefault(r.run.model, set()).add(getattr(r.run.option, "value", str(r.run.option)))
        H.validate(self.cfg, opts.keys(), opts, slot_map)
        plan = H.load_plan(self.cfg)
        now = iso(self.clock())
        self.store.pin("host_assignment", H.assignment_digest(self.cfg, plan), now, self.cfg.repin_hosts)
        self.store.put_meta("host_plan", json.dumps({"model_host": self.cfg.model_host, "hosts": self.cfg.hosts, "source": plan,
                                                     "statement": "Model and host are confounded: each model runs on one host only."}, sort_keys=True), now)
        from dataset_76 import codestamp
        tree = codestamp.tree_hash(Path(__file__).resolve().parents[2])
        self.hosts = H.HostBook(self.cfg, self.store.event, lambda: iso(self.clock()), transports=self._host_transports,
                                local_tree=tree, image_id=self.cfg.pinned_image_id or None, persist=self._persist_orphans, on_orphan_end=self._orphan_ended)
        try:
            self.hosts.orphans = dict(json.loads(self.store.get_meta("host_orphans") or "{}"))
        except ValueError:
            self.hosts.orphans = {}
        self._env_slot_keys = os.environ.get("STUDY_VAR_95")
        os.environ["STUDY_VAR_95"] = ",".join(sorted(self.hosts.specs[self.hosts.local].slot_keys))   # the local harness's SlotPool reads it

    def _open_live_check(self) -> None:
        """The live check and the level: refuse a setting that could not work, read back the level in force, and take the launch's start step. Whatever the launch says, steps
        taken before a restart bind again: an unreadable record, or steps with no ladder to compute them from, stop the start (the level would silently come back)."""
        assert self.store
        problems = LC.config_problems(self.cfg)
        if problems:
            raise ValueError("the live check is refused: " + "; ".join(problems))
        if self.cfg.sandbox_image and self.cfg.live_check_cmds and self.cfg.live_check_required:       # a real launch (30(b)): the probe's fixed subset, not the whole cell
            why = LC.probe_files_problem(self.cfg)
            if why:
                raise ValueError("the live check is refused: " + why)
        self._live_raw = self.store.get_meta(LC.META)
        self._live = LC.LiveState.from_json(self._live_raw)
        if self._live.steps and self._live.steps > LC.max_steps(self.cfg.level_ladder):
            raise ValueError(f"the campaign's state records {self._live.steps} step(s) of the level (live check, start, restart), but this launch's level_ladder {self.cfg.level_ladder} "
                             f"has {LC.max_steps(self.cfg.level_ladder)} at most: the level in force cannot be computed. Launch with the ladder it was lowered on")
        if self.cfg.live_check_cmds or self.cfg.level_start_steps or self._live.steps:
            unbound = sorted({g for g in map(self._capped_group, self._group_name) if self._raw_cap(g) is None})
            if unbound and (self.cfg.live_check_cmds or self.cfg.level_start_steps):
                raise ValueError(f"the live check is refused: the groups {unbound} have no cap (no max_sandboxes_by_block entry and no max_sandboxes), so a lowered level could not bind "
                                 "them. Give the cap of each group, or a global max_sandboxes")
        if self.cfg.level_start_steps:
            now = iso(self.clock())
            self._live_update(lambda st: (None, LC.start_level(st, self.cfg, now)), now)

    def _raw_cap(self, group: str) -> int | None:
        cg = self._cap_group(group)
        return self.cfg.max_sandboxes_by_block[cg] if cg is not None else self.cfg.max_sandboxes

    def _keep_runs_off_probe_keys(self) -> None:
        """One host with a live check: the probe's slot users are its own, but with no `hosts` plan nothing else keeps a run from leasing them, so the dispatcher's
        harness is limited to every other user of the slot map."""
        keys = set((self.cfg.live_check_slot_keys or {}).values()) if self.cfg.live_check_cmds else set()
        if not keys:
            return
        from dataset_76.faults.matcher import load_slot_map
        from dataset_76.harness.slot_lease import SLOT_MAP
        self._env_slot_keys = os.environ.get("STUDY_VAR_95")
        have = [k for k in (self._env_slot_keys or "").split(",") if k] or list(load_slot_map(SLOT_MAP))
        os.environ["STUDY_VAR_95"] = ",".join(sorted(k for k in have if k not in keys))

    def _require_block_caps(self) -> None:
        """A real launch (one with a sandbox image) must hold the cap of block 1 and, if its plan has S2 runs, of block 2 in `max_sandboxes_by_block`: without them
        those runs would fall back to `max_sandboxes`, None by default, which is no cap (the lead, 3 Nov: a refusal, not a fallback; amendment 30(i): a launch with no
        main cap must not proceed quietly). Block 1 is main S1 with the pagination cell, whose key is `condition_e` (the pilot and calibration run at its cap, and a key of
        their own stands for them); block 2's key is `condition_f`, and tier B shares it unless it has `condition_g` of its own. A calibration or pilot run of layout S2 is
        under its model's S2 cap (`_capped_group`), so it needs that key too, whether or not the plan has a main S2 group. A stub (no image) is as before."""
        if not self.cfg.sandbox_image:
            return
        lacking = sorted({g for g in map(self._capped_group, self._group_name) if (self._is_s2(g) or self._is_block1(g)) and self._cap_group(g) is None})
        if lacking:
            want = {"condition_f": "max_sandboxes_by_block['condition_f']", "condition_g": "max_sandboxes_by_block['condition_g'] (or ['condition_f'], which tier B shares)"}
            keys = sorted({want.get(g, f"max_sandboxes_by_block[{g!r}]") if self._is_s2(g) else "max_sandboxes_by_block['condition_e']" for g in lacking})
            raise ValueError("no sandbox cap for the runs of this plan: set " + "; ".join(keys)
                             + ". Without it they would fall back to max_sandboxes, which is no cap by default")

    def _open_reservations(self) -> None:
        """Process reservations (dataset_76/harness/reservation.py): refuse a launch that names them wrongly, then give the local pool what it needs to hold back the
        keys of a reservation that is held (the env its SlotPool reads, put back by close())."""
        specs = self.cfg.process_reservations
        if not specs:
            return
        from dataset_76.faults.matcher import load_slot_map
        from dataset_76.harness import reservation as R
        from dataset_76.harness.slot_lease import SLOT_MAP
        slot_map = {k: s.option for k, s in load_slot_map(SLOT_MAP).items()}
        if self.hosts is None:                           # two hosts: hosts.validate has checked them against the local partition
            probs = R.problems(specs, slot_map, None)
            if probs:
                raise ValueError("the process reservations are refused: " + "; ".join(probs))
        try:
            d = R.resolve_dir(self.cfg.reservation_dir)
        except R.ReservationError as e:
            raise ValueError(f"the process reservations are refused: {e}") from e
        if d is None:
            raise ValueError("process_reservations are set but no reservation_dir (or STUDY_VAR_84): the campaign could not see whether the processes are up")
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"the reservation directory {d} could not be made: {type(e).__name__}: {e}") from e
        self._res_dir = d
        self._res_env = {k: os.environ.get(k) for k in (R.ENV_DIR, R.ENV_KEYS)}
        os.environ[R.ENV_DIR] = str(d)
        os.environ[R.ENV_KEYS] = json.dumps({n: sorted(sp["slot_keys"]) for n, sp in specs.items()}, sort_keys=True)

    def _look_reservations(self) -> None:
        """At each look of the dispatch loop: which reservations are held now. A lock that cannot be read counts as held and is an event and a status alarm."""
        if self._res_dir is None or not self.cfg.process_reservations:
            return
        assert self.store
        from dataset_76.harness import reservation as R
        held, bad = R.look(self._res_dir, self.cfg.process_reservations)
        if held == self._res_held and bad == self._res_bad:
            return
        now = iso(self.clock())
        for n in sorted(held - self._res_held):
            total = sum(int(self.cfg.process_reservations[x]["sandboxes"]) for x in held if x in self.cfg.process_reservations)
            self.store.event(now, "reservation_held", None, f"{n}: its process is up; its {self.cfg.process_reservations[n]['sandboxes']} sandbox(es) count in every cap (the held processes' add up: {total} in all) and "
                                                           f"its slot keys {sorted(self.cfg.process_reservations[n]['slot_keys'])} are out of the local pool")
        for n in sorted(self._res_held - held):
            self.store.event(now, "reservation_released", None, f"{n}: its process is not up; its sandboxes and slot keys are back")
        for n in sorted(set(bad) - set(self._res_bad)):
            self.store.event(now, "reservation_unreadable", None, f"{n}: the lock could not be read ({bad[n]}): counted as held")
        for n in sorted(set(self._res_bad) - set(bad)):
            self.store.event(now, "reservation_readable", None, f"{n}: the lock can be read again")
        self._res_held, self._res_bad = held, bad
        with self._cv:                                   # a live-check probe waiting for its place re-reads the figure (_live_has_room); a release is a place
            self._cv.notify_all()
        self.store.put_meta("process_reservations", json.dumps({"held": sorted(held), "unreadable": bad}, sort_keys=True), now)

    def _reserved_n(self) -> int:
        """Sandboxes the held reservations count for, in every cap and in the local host's own."""
        return sum(int(self.cfg.process_reservations[n]["sandboxes"]) for n in self._res_held if n in self.cfg.process_reservations)

    def _orphan_ended(self, key: str, o: dict) -> None:
        """A lost run's host shows it gone. Its worker may have finished and billed meanwhile, so its dir comes here, its usage goes on the attempt, and its copy
        leaves the host. If it cannot be pulled the attempt stays marked incomplete: unknown, not zero."""
        from . import hosts as H
        assert self.hosts is not None and self.store is not None
        t = self.hosts.transports[o["host"]]
        run_dir = Path(o["run_dir"])
        rc, out, _ = t.call(["pack", str(run_dir)], timeout=600)
        if rc != 0:
            self.store.event(iso(self.clock()), "orphan_not_pulled", o.get("model"), f"{o['run_key']}: pack exit {rc}: its usage stays unknown")
            return
        facts = H._safe_extract(out, run_dir)
        u = _usage_from_file(run_dir / "run_record.json")
        u.pop("termination", None)
        u.pop("wall_time_s", None)
        if u:
            self.store.set_usage(int(key.rsplit("#", 1)[1]), **u)
            self.store.event(iso(self.clock()), "orphan_usage_read", o.get("model"), f"{o['run_key']}: {u.get('input_tokens')} input tokens, from the record on its host")
        t.call(["clean", str(run_dir), "--files", str(facts[0]), "--bytes", str(facts[1])], timeout=120)

    def _persist_orphans(self, orphans: dict) -> None:
        assert self.store
        self.store.put_meta("host_orphans", json.dumps(orphans, sort_keys=True), iso(self.clock()))

    def _reconcile_remote(self) -> None:
        """Before recovery: a remote run left 'running' by a dead dispatcher may still be executing on its host. It is stopped there
        and its run dir pulled; then the usual reconcile marks it interrupted and re-runs it, as for a local run. A host that cannot be
        reached keeps it as an orphan, counted as in flight until the host shows it gone."""
        if self.hosts is None:
            return
        assert self.store
        from . import hosts as H
        for r in self.store.running_attempts():
            host = H.host_of(self.cfg, r["model"])
            if not self.hosts.specs[host].remote:
                continue
            t = self.hosts.transports[host]
            try:
                rc, _, err = t.call(["kill", r["run_dir"]], timeout=180)
                if rc != 0:
                    raise H.TransportError(f"kill exit {rc}: {(err or b'').decode(errors='replace')[-200:]}")
                rc, out, _ = t.call(["pack", r["run_dir"]], timeout=600)
                if rc == 0:
                    H._safe_extract(out, Path(r["run_dir"]))
                self.store.event(iso(self.clock()), "remote_reconciled", r["model"], f"{r['run_key']} on {host}: stopped there and pulled")
                if r["run_dir"] in self.hosts.probe(host).live:   # it could only be killed, or was dead already: its sandbox is not shown gone yet
                    raise H.TransportError("its worker is gone, its sandbox not shown gone yet")
            except (H.TransportError, H.RemoteHarnessError, OSError) as e:
                self.hosts.add_orphan(f"{r['run_key']}#{r['id']}", {"run_key": r["run_key"], "model": r["model"], "host": host,
                                                                     "run_dir": r["run_dir"], "slot": r["slot"], "option": self._option_of.get(r["run_key"], ""),
                                                                     "group": self._group_name.get(r["run_key"], ""), "why": str(e)})

    def close(self) -> None:
        self._stop_live_check()             # before the store goes: the thread's last write, or a probe's end, must not meet a closed store
        stop = getattr(self, "_prober_stop", None)
        if stop is not None:
            stop.set()
        for th in (self._prober, self._leftover_sweep, self._gate_thread):     # either may be inside a store call; a probe stuck past this still cannot crash: Store.close takes the store's lock
            if th is not None:
                th.join(timeout=30.0)
        if self._res_env is not None:
            for k, v in self._res_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            self._res_env = None
        if hasattr(self, "_env_slot_keys"):         # set with two hosts, or by one host's live check keeping runs off the probe's slot users
            if self._env_slot_keys is None:
                os.environ.pop("STUDY_VAR_95", None)
            else:
                os.environ["STUDY_VAR_95"] = self._env_slot_keys
        if self.store:
            self.store.close()
        if self._lockfile:
            self._lockfile.close()  # releases the flock; the OS also does so on kill

    def __enter__(self): self.open(); return self
    def __exit__(self, *a): self.close()

    # --- helpers -----------------------------------------------------------
    def _candidate_blocks(self) -> list[str] | None:
        if self.cfg.past_pilot:
            return None
        # the go/no-go on the pilot is the science agent's call; the blocks that run together with the pilot (calibration, amendment 29(g)) do not wait for it
        return ["pilot", *[b for b in self.cfg.concurrent_barrier_blocks if b != "pilot"]]

    def _pending(self) -> list[Any]:
        """Pending rows of the CURRENT manifest only (state of dropped blocks stays for reporting)."""
        assert self.store
        rows = [r for r in self.store.pending(self._candidate_blocks()) if r["run_key"] in self._keys]
        # barrier: a run waits while an earlier run of a DIFFERENT barrier block is still pending; inside a block the order is free. Barrier blocks that run
        # together (amendment 29(g): pilot and calibration) do not hold each other, only every block after them.
        together = set(self.cfg.concurrent_barrier_blocks)
        first: dict[str, int] = {}
        for r in rows:
            if r["block"] in self.cfg.barrier_blocks:
                first.setdefault(r["block"], r["seq"])  # rows are in seq order
        rows = [r for r in rows
                if not any(blk != r["block"] and seq < r["seq"] and not (blk in together and r["block"] in together) for blk, seq in first.items())]
        return rows

    def _provider_cap(self, model: str, slot: str) -> int:
        b = backend_of(slot)
        if b == "provider_i":
            return self.cfg.provider_i_caps.get(model, 0)
        if b == "provider_g" and self._provider_g_cap is not None:
            return self._provider_g_cap
        return self.cfg.api_cap(model) if b == "anthropic" else self.cfg.cap(b)

    def _read_check_holds(self, pending: list, mono: float) -> bool:
        """The short read before each block: when the next pending run belongs to a group not yet checked, and nothing is in flight (between
        blocks, never during one), run the configured read. ok lets the group start; low or unknown holds dispatch, retried every retry_s."""
        if not self.cfg.read_check_required or not pending:
            return False
        assert self.store
        from dataset_76 import provider_l
        if self._probed is None:
            try:
                self._probed = set(json.loads(self.store.get_meta("read_check_groups") or "[]"))
            except ValueError:
                self._probed = set()
        g = self._group_name.get(min(pending, key=lambda r: self._group_idx[r["run_key"]])["run_key"], "")
        g = self._probe_alias.get(g, g)
        if g in self._probed:
            return False
        if self._inflight or (self.hosts is not None and self.hosts.n_orphans()):     # a block is still running (or a lost remote run may be): the read waits for the gap
            return True
        if self._rc and self._rc.get("group") == g and self._rc["state"] not in ("ok", "off") and mono - self._rc_at < self.cfg.read_check_retry_s:
            return True
        r = provider_l.check_configured(timeout_s=self.cfg.read_check_timeout_s, run=self._fr_run)
        now = iso(self.clock())
        r.update(group=g, checked_utc=now, op_worker_rss_mb=((self._fr or {}).get("unhashed") or {}).get("op_worker_rss_mb"))
        self._rc, self._rc_at = r, mono
        self.store.put_meta("read_check", json.dumps(r), now)
        self.store.event(now, "read_check", None, f"before {g}: {r['state']} ({r['mbit_s']} Mbit/s, threshold {r['threshold_mbit_s']}, port {r['port']}) {r['why'] or ''}")
        if r["state"] in ("ok", "off"):
            self._probed.add(g)
            self.store.put_meta("read_check_groups", json.dumps(sorted(self._probed)), now)
            return False
        return True

    def _fr_settings(self, mono: float) -> dict:
        """fr's settings hash, re-read at most every `fr_settings_every_s`: logged on every change of state and kept in the store for the
        status. mismatch stops dispatch and unknown holds it (the caller); the hash is stamped on every run record."""
        assert self.store
        if self._fr is None or mono - self._fr_at >= self.cfg.fr_settings_every_s:
            from dataset_76 import provider_m
            r = provider_m.check(self.cfg.fr_settings_cmd, self.cfg.fr_settings_frozen_file, required=self.cfg.fr_settings_required,
                                 timeout_s=self.cfg.fr_settings_timeout_s, run=self._fr_run,
                                 restart_log=self.cfg.fr_restart_log, host=self.cfg.fr_restart_host)
            r["checked_utc"] = iso(self.clock())
            d = provider_m.check_disk(self.cfg.fr_disk_cmd, min_gib=self.cfg.fr_disk_min_gib, required=self.cfg.fr_settings_required,
                                      timeout_s=self.cfg.fr_settings_timeout_s, run=self._fr_run)
            d["checked_utc"] = r["checked_utc"]
            r["disk"] = d
            if self._lg is None or mono - self._lg_at >= self.cfg.log_check_every_s:      # its own, slower cadence
                lg = provider_m.check_exit(self.cfg.log_check_cmd, stop_code=self.cfg.log_check_stop_code, required=self.cfg.log_check_required,
                                           timeout_s=self.cfg.fr_settings_timeout_s * 3, run=self._fr_run)
                lg["checked_utc"] = r["checked_utc"]
                self._lg, self._lg_at = lg, mono
            lg = self._lg
            r["logs"] = lg
            self.store.put_meta("fr_logcheck", json.dumps(lg), lg["checked_utc"])
            if r["state"] in ("match", "off") and d["state"] in ("low", "unknown"):
                r = {**r, "state": "disk_" + d["state"]}                      # the settings are fine; the disk is not
            if r["state"] in ("match", "off") and lg["state"] in ("alarm", "unknown"):
                r = {**r, "state": "logs_" + lg["state"]}                     # nor are the kept logs
            self._fr, self._fr_at = r, mono
            self.store.put_meta("fr_disk", json.dumps(d), d["checked_utc"])
            self.store.put_meta("fr_settings", json.dumps(r), r["checked_utc"])
            if r["state"] != self._fr_state:
                self._fr_state = r["state"]
                self.store.event(r["checked_utc"], "fr_settings", None,
                                 f"{r['state']}: hash {r['hash']}, frozen {r['frozen']}{' (' + r['why'] + ')' if r['why'] else ''}")
        return self._fr

    def _model_66_gate(self, mono: float) -> dict:
        """The model_66 gate (gate.py), re-evaluated at most every 5 s; a change of state is logged once."""
        assert self.store
        if self._gate is None or mono - self._gate_at >= 5.0:
            from . import gate
            self._gate, self._gate_at = gate.evaluate(self.store, self.cfg, self._keys), mono
            sig = (self._gate["state"], self._gate["held"], self._gate.get("hold_after_seed"))
            if sig != self._gate_sig:
                self._gate_sig = sig
                self.store.event(iso(self.clock()), "model_66_gate_state", self.cfg.gate_model,
                                 f"{sig[0]}, model_66 {'HELD' + ('' if sig[2] is None else f' (seeds after {sig[2]})') if sig[1] else 'dispatching'}, projected {self._gate['projected_usd']}"
                                 + (f" ({'; '.join(self._gate['why'])})" if self._gate.get("why") else "")
                                 + (f", released by {self._gate['released_by']}" if self._gate.get("released_by") else ""))
        return self._gate

    def _provider_g_pool_cap(self, slots: list[dict] | None) -> int | None:
        """The provider_g provider cap derived from the up slots (config.provider_g_cap), not the placeholder `provider_caps["provider_g"]`."""
        return self.cfg.provider_g_cap(slots)

    def _unit_of(self, model: str, slot: str) -> str:
        b = backend_of(slot)
        return f"anthropic:{model}" if b == "anthropic" else slot

    def _unit_ok(self, unit: str, for_window: bool) -> bool:
        if for_window:  # an outage window stays open until a success clears the unit
            return unit not in self._unit_down
        if unit in self._key_held and any(self._unit_of(m, s) == unit for (m, _, s) in self._inflight.values()):
            return False    # a refused key is probed by ONE run at a time, and a run in flight on the unit may still be that probe
        return time.monotonic() >= self._unit_pause.get(unit, 0)  # dispatch may probe after the pause

    def _model_slots(self, model: str, slots: list[dict] | None, now: dt.datetime,
                     for_window: bool = False, block: str = "main", margin_s: float | None = None) -> list[str]:
        """The model's slots a run of `block` could start on. `margin_s` replaces the block's margin (the seconds a stack must still run): -inf asks for every up slot, whatever its window."""
        if model in self.cfg.api_models:
            cands = [API_SLOT]
        else:
            key = self.cfg.open_models.get(model)
            cands = usable_slots(slots, key, now, self.cfg.slot_margin(block) if margin_s is None else margin_s) if key else []
            if self.cfg.provider_i_caps.get(model, 0) > 0:
                cands.append(provider_i_slot(model))  # a model with no HPC slot but LLM Lab is not paused
        return [c for c in cands if self._unit_ok(self._unit_of(model, c), for_window)
                and (self.slot_gate is None or backend_of(c) != "provider_g" or self.slot_gate.allowed(c))]

    def _is_unstarted(self, infra: tuple[str, str]) -> bool:
        """The run never started its sandbox: no slot user could be had, or the start was refused before any container existed, as for a moved image tag (the
        harness wraps the start error as `start failed: <Type>: ...`). The names come from the classes, so a rename cannot leave a mark that matches nothing, and
        only the type counts, not a word in the message."""
        from dataset_76.harness.slot_lease import NO_SLOT_USER
        from dataset_76.sandbox.sandbox import StartRefused
        prefix = "start failed: "
        if self._host_refused(infra):
            return True
        return infra[0] == "sandbox" and infra[1].startswith(prefix) and infra[1][len(prefix):].split(":", 1)[0] in {c.__name__ for c in (*NO_SLOT_USER, StartRefused)}

    def _host_refused(self, infra: tuple[str, str]) -> bool:
        """No attempt used: its host did not start it, or may have and could not say (then it is also kept as an orphan)."""
        from .hosts import HOST_KIND, NOT_STARTED, UNCERTAIN
        return self.hosts is not None and infra[0] == HOST_KIND and infra[1].startswith((NOT_STARTED, UNCERTAIN))

    def _host_setup_failure(self, infra: tuple[str, str], model: str, run_key: str, ended: str) -> str | None:
        """How a failure before the agent started counts toward its HOST (hosts.py `note_setup_failure`: science #34, 12:11Z), or None when it is not one: one host (no HostBook), a
        stage after the agent began, or a start the unstarted rule already owns (no slot user, a refused start), which charges nothing and says nothing of the host."""
        from .hosts import setup_failure_kind
        if self.hosts is None or self._is_unstarted(infra):
            return None
        kind = setup_failure_kind(infra[0], infra[1])
        if kind is None:
            return None
        return self.hosts.note_setup_failure(self._host_of(model), kind, run_key, dt.datetime.fromisoformat(ended).timestamp())

    def _host_uncertain(self, infra: tuple[str, str]) -> bool:
        from .hosts import HOST_KIND, UNCERTAIN
        return self.hosts is not None and infra[0] == HOST_KIND and infra[1].startswith(UNCERTAIN)

    def _host_lost(self, infra: tuple[str, str]) -> bool:
        from .hosts import HOST_KIND, LOST, WORKER_DIED
        return self.hosts is not None and infra[0] == HOST_KIND and infra[1].startswith((LOST, WORKER_DIED))

    def _worker_died(self, infra: tuple[str, str]) -> bool:
        from .hosts import HOST_KIND, WORKER_DIED
        return self.hosts is not None and infra[0] == HOST_KIND and infra[1].startswith(WORKER_DIED)

    def _unit_concurrency(self, model: str, slot: str) -> int:
        """The runs in flight in this run's cap scope (dataset_76.harness.run.concurrency_unit), on both hosts, this one included: with two hosts
        no harness process sees them all."""
        def unit(m: str, s: str) -> tuple:
            b = backend_of(s)
            if b == "provider_i":
                return (b, m)
            if b == "anthropic":
                return (b, m if m in self.cfg.anthropic_caps else None)
            return (b, None)
        u = unit(model, slot)
        with self._cv:
            return sum(1 for (m, _, s) in self._inflight.values() if unit(m, s) == u)

    def _load_unstarted(self) -> None:
        assert self.store
        if not self._unstarted_loaded:
            self._unstarted_loaded = True
            self._unstarted_streaks = unstarted_streaks(self.store, self.cfg.unstarted_stop_after)

    def _canary(self) -> dict:
        """The canary's record (meta unstarted_canary): when a canary last ended the stop, how many times in a row the stop latched again within the window, and whether it
        is now latched for a person."""
        assert self.store
        if self._canary_cache is None:
            try:
                d = json.loads(self.store.get_meta("unstarted_canary") or "{}")
                self._canary_cache = {"last_end_epoch": d.get("last_end_epoch"), "chain": int(d.get("chain", 0)), "person": bool(d.get("person")), "person_utc": d.get("person_utc")}
            except (ValueError, TypeError, AttributeError):
                self._canary_cache = {"last_end_epoch": None, "chain": 0, "person": True, "person_utc": None}      # unreadable: no canary, a person decides
        return self._canary_cache

    def _canary_options(self) -> set[str] | None:
        """The options a canary may go for now, or None: the options at their limit, one run, every `unstarted_canary_every_s` from the stop or the last canary, never two at
        once and none once the stop is latched for a person. (The caller has ruled out the hard stop.)"""
        if self._canary()["person"] or (self._canary_run is not None and self._canary_run in self._inflight):
            return None
        stop_epoch = self._stop_epoch("unstarted_stopped_epoch", "unstarted", iso(self.clock()))
        if time.time() < max(self._canary_epoch or 0.0, stop_epoch) + self.cfg.unstarted_canary_every_s:
            return None
        return {o for o, n in self._unstarted_streaks.items() if n >= self.cfg.unstarted_stop_after and o != "*"} or None

    def _note_unstarted(self, refused: bool, now: str, run_key: str, option: str) -> None:
        """The count is per option, so a run of another option does not reset it (the lead, 2 Nov): one option refused while others run would break the
        balance within a model that the analysis assumes, and must not go on quietly. While the stop holds, the canary run (science, 3 Nov) that starts a sandbox
        ends it; a refused one changes nothing (it used no attempt)."""
        assert self.store
        with self._cv:
            self._load_unstarted()
            if refused:
                streaks = dict(self._unstarted_streaks)
                n = streaks[option] = streaks.get(option, 0) + 1
                facts, events = {"unstarted_streaks": json.dumps(streaks)}, []
                if n == self.cfg.unstarted_stop_after:
                    facts["unstarted_stopped_epoch"] = str(time.time())      # a release must be newer than this (release.py); written WITH the count, in one transaction
                    events.append(("unstarted_stop", None, f"{n} runs of option {option} in a row could not start a sandbox (last: {run_key}); "
                                                           "dispatch of all options is STOPPED"))
                    cs, t = dict(self._canary()), time.time()
                    cs["chain"] = cs["chain"] + 1 if cs["last_end_epoch"] is not None and t - cs["last_end_epoch"] <= self.cfg.unstarted_relatch_window_s else 0
                    if cs["chain"] >= self.cfg.unstarted_canary_chain and not cs["person"]:
                        cs["person"], cs["person_utc"] = True, now
                        events.append(("unstarted_latched_for_a_person", None, f"the stop latched again within {self.cfg.unstarted_relatch_window_s / 60:g} min of a canary ending it, "
                                                                              f"{cs['chain']} times in a row: no more canary, only the release file ends it"))
                    facts["unstarted_canary"] = json.dumps(cs)
                    self._canary_cache = cs
                self.store.put_meta_many(facts, now, events)
                self._unstarted_streaks = streaks                            # in memory only once it is kept
            elif run_key == self._canary_run and any(v >= self.cfg.unstarted_stop_after for v in self._unstarted_streaks.values()):
                self._canary_run = None                                      # the canary started a sandbox: the stop ends itself
                cs = dict(self._canary())
                cs["last_end_epoch"] = time.time()
                self.store.put_meta_many({"unstarted_streaks": "{}", "unstarted_canary": json.dumps(cs)}, now,
                                         [("unstarted_canary_ended", None, f"the canary {run_key} (option {option}) started a sandbox: the unstarted stop ends and dispatch resumes")])
                self._unstarted_streaks, self._canary_cache = {}, cs
            elif 0 < self._unstarted_streaks.get(option, 0) < self.cfg.unstarted_stop_after:     # a stop reached is ended only by a release or the canary: a run still in flight when it came changes nothing
                self._unstarted_streaks.pop(option)
                self.store.put_meta("unstarted_streaks", json.dumps(self._unstarted_streaks), now)

    def _stop_epoch(self, key: str, kind: str, now: str) -> float:
        """When the stop now in force began, from the store. A stop with no readable time (a store from before it was kept, or a write lost) began NOW: reading
        it as time zero would accept a release written before the stop, which says nothing about it (release.py). The time given is kept, with an event."""
        assert self.store
        raw = self.store.get_meta(key)
        try:
            since = float(raw) if raw is not None else math.nan
        except ValueError:
            since = math.nan
        if math.isfinite(since):
            return since
        since = time.time()
        self.store.put_meta_many({key: str(since)}, now, [(f"{kind}_stop_time_missing", None, "the stop in force had no readable time; taken as now, so only "
                                                                                             "a release written after this counts")])
        return since

    def _unstarted_stopped(self) -> bool:
        assert self.store
        self._load_unstarted()
        at_limit = [o for o, n in self._unstarted_streaks.items() if n >= self.cfg.unstarted_stop_after]
        if at_limit:
            since = self._stop_epoch("unstarted_stopped_epoch", "unstarted", iso(self.clock()))
            said = release.take(self.cfg.unstarted_release_file, since)
            if said:
                self._unstarted_streaks, self._canary_cache = {}, {"last_end_epoch": None, "chain": 0, "person": False, "person_utc": None}
                self.store.put_meta_many({"unstarted_streaks": "{}", "unstarted_canary": json.dumps(self._canary_cache)}, iso(self.clock()))
                self.store.event(iso(self.clock()), "unstarted_released", None, f"by {said} (stopped by {', '.join(sorted(at_limit))})")
                return False
        return bool(at_limit)

    def _requeue_harness_errors(self) -> None:
        """A run lost to our own bug is not an outcome (science, #34, 2 Nov 18:35Z): when the campaign opens, which after a fix is a restart with the new code,
        every harness-error run of the manifest is pending again, from the same row and seed (so its place in the order is its own and it is sent at the next
        opportunity), its attempts kept. It used no infra attempt (`infra_attempts` counts only infra failures), so `max_attempts` cannot stop it. The count that
        stops dispatch is not touched: a stop reached stays until it is released."""
        assert self.store
        self.store.requeue_harness_errors(self._keys, iso(self.clock()))

    def _he_state(self) -> dict:
        assert self.store
        if self._he is None:
            self._he = HS.load(self.store)
            if self._he.get("unreadable"):
                self._he["all"] = {"kinds": ["*"], "epoch": time.time(), "utc": iso(self.clock()), "why": "the stored harness-error record is unreadable"}
        return self._he

    def _note_harness_error(self, now: str, run_key: str, kind: str, model: str, option: str) -> None:
        """A harness error (a bug of ours) of this kind on this model and option, at `now`. Counted from the store's own attempts plus this one, which is not yet in
        them: called BEFORE the attempt is finalized, as `_check_served` is, so a crash between the two replays the run and can only count it twice, never skip a stop.
        `harness_error_stop_after` of a kind on a model within the window stop that model; the same kind on `harness_error_models_stop` models, or
        `harness_error_option_stop` on one option, stop ALL dispatch. A stop is written with its event in one transaction and is kept until a release."""
        assert self.store
        with self._cv:
            st = self._he_state()
            t = HS._epoch(now)
            errs = HS.stored_errors(self.store) + [{"epoch": t, "kind": kind, "model": model, "option": option, "run_key": run_key}]
            inwin = HS.counted(errs, st.get("cleared", {}), t, self.cfg.harness_error_window_s)
            mstop, astop = HS.fired(inwin, kind, model, option, self.cfg)
            new, events = json.loads(json.dumps(st)), []
            if astop and (new["all"] is None or kind not in new["all"]["kinds"]):
                prev = (new["all"] or {}).get("kinds", [])
                new["all"] = {"kinds": sorted({*prev, kind}), "epoch": (new["all"] or {}).get("epoch", t), "utc": now, "why": astop + f" (last: {run_key})"}
                events.append(("harness_error_stop", None, f"{astop} (last: {run_key}); dispatch of ALL options and models is STOPPED"))
            if mstop and model not in new["models"]:
                new["models"][model] = {"kind": kind, "epoch": t, "utc": now, "why": mstop + f" (last: {run_key})"}
                events.append(("harness_error_stop", model, f"{mstop} (last: {run_key}); dispatch of {model} is STOPPED, the other models go on"))
            if events:
                self.store.put_meta_many({HS.KEY: json.dumps(new, sort_keys=True)}, now, events)
                self._he = new                                                   # in memory only once it is kept

    def _harness_error_stopped(self) -> bool:
        """Whether ALL dispatch is stopped by harness errors; the models stopped alone are left in `_he_stopped_models`. The release file lists names, one per line:
        a model's name ends that model's stop, the word ALL the all-stop; each only if written after its stop (release.take_listed). A release also clears the errors of
        the kinds it ended, so they stop counting; the restart with the fix comes first (the runs are pending again then)."""
        assert self.store
        st = self._he_state()
        stops = {m: float(s.get("epoch") or 0.0) for m, s in st["models"].items()}
        if st["all"] is not None:
            stops[HS.ALL] = float(st["all"].get("epoch") or 0.0)
        said = release.take_listed(self.cfg.harness_error_release_file, stops) if stops else {}
        if said:
            new, now, t, events = json.loads(json.dumps(st)), iso(self.clock()), time.time(), []
            new.pop("unreadable", None)                                      # a release ends the fail-closed stop on an unreadable record too
            for name, who in sorted(said.items()):
                if name == HS.ALL:
                    kinds = new["all"]["kinds"]
                    for k in kinds:
                        new["cleared"][f"{k}|*"] = t
                    for m in [m for m, s in new["models"].items() if s.get("kind") in kinds]:
                        new["models"].pop(m)                                      # the same kind's model stops go with it: the release says that bug is fixed
                    if "*" in kinds:
                        new["cleared"]["*|*"] = t
                    new["all"] = None
                    events.append(("harness_error_released", None, f"by {who} (ALL dispatch, stopped by {', '.join(kinds)})"))
                elif name in new["models"]:
                    kind = new["models"].pop(name)["kind"]
                    new["cleared"][f"{kind}|{name}"] = t
                    events.append(("harness_error_released", name, f"by {who} ({name} only, stopped by {kind})"))
            self.store.put_meta_many({HS.KEY: json.dumps(new, sort_keys=True)}, now, events)
            self._he = st = new
        self._he_stopped_models = set(st["models"])
        return st["all"] is not None

    def _served_state(self) -> dict[str, dict]:
        """model -> {state, reasons, run_key, utc} of the models stopped by what their server reported (kept in the store, so a restart keeps
        the stop). A model listed in the release file after its stop is let go, and its line is removed (release.py)."""
        assert self.store
        if not self._served_loaded:
            self._served_loaded = True
            try:
                self._served_stop = json.loads(self.store.get_meta("served_settings") or "{}")
                if not isinstance(self._served_stop, dict):
                    raise ValueError
            except ValueError:       # an unreadable record of which models are stopped must not release them: stop all (fail closed)
                self._served_stop = {"*": {"state": "unreadable", "reasons": ["the stored served-settings record is unreadable"],
                                           "run_key": None, "utc": iso(self.clock())}}
        for m, said in release.take_listed(self.cfg.served_release_file, {m: release.epoch_of(d.get("utc")) for m, d in self._served_stop.items()}).items():
            self._served_stop.pop(m)
            self.store.put_meta("served_settings", json.dumps(self._served_stop), iso(self.clock()))
            self.store.event(iso(self.clock()), "served_settings_released", m, f"listed in the release file by {said}")
        return self._served_stop

    def _check_served(self, model: str, run_key: str, rec: RunRecord | None, now: str) -> None:
        """After a run: what the server reported (served model id, settings) against what was requested. A mismatch stops this model's
        dispatch and alarms; nothing reported at all is alarmed (unverified) and stops nothing."""
        if not self.cfg.served_check or rec is None or self.store is None:
            return
        from dataset_76 import servedsettings
        ex = rec.extras or {}
        requested = servedsettings.requested_settings(ex.get("sampling"), ex.get("request_extra"))
        j = servedsettings.judge(ex.get("served_model_ids") or [], ex.get("served_settings"), rec.model_api_id, requested,
                                 self.cfg.served_id_aliases.get(model, ()))
        with self._cv:
            st = self._served_state()
            if j["state"] == "mismatch" and model not in st:
                st[model] = {"state": "mismatch", "reasons": j["reasons"], "run_key": run_key, "utc": now}
                self.store.put_meta("served_settings", json.dumps(st), now)
                self.store.event(now, "served_settings_mismatch", model, f"{run_key}: " + "; ".join(j["reasons"]) + " (dispatch of this model STOPPED)")
            elif j["state"] == "unreported" and model not in self._served_unreported:
                self._served_unreported.add(model)
                self.store.event(now, "served_settings_unreported", model, f"{run_key}: the server reported no model id: unverified")

    def _note_provider_i_fingerprint(self, slot: str, model: str, run_key: str, rec: RunRecord | None, now: str) -> None:
        """After a run on LLM Lab: the builds (`system_fingerprint`) its replies reported, against the builds seen for this model on
        LLM Lab so far in the campaign. A build not seen before, after the first (or two builds in the model's first run), is a change:
        recorded as an event and alarmed, and it stops nothing (science, #34, 17:49Z: model_61 alternates two builds there by design).
        A return to a build already seen is not a new change. The builds seen are kept in the store, so a restart keeps the reference."""
        if rec is None or self.store is None or backend_of(slot) != "provider_i":
            return
        fps = sorted({str(f) for f in ((rec.extras or {}).get("system_fingerprints") or []) if f})
        if not fps:
            return                                   # no build reported: nothing to compare (the per-run record says so)
        with self._cv:
            if self._provider_i_fp is None:
                try:
                    self._provider_i_fp = json.loads(self.store.get_meta("provider_i_fingerprints") or "{}")
                    if not isinstance(self._provider_i_fp, dict):
                        raise ValueError
                except ValueError:
                    self._provider_i_fp = {}
                    self.store.event(now, "provider_i_fingerprint_record_unreadable", None,
                                     "the stored record of LLM Lab builds is unreadable: builds are compared from here on only")
            seen = self._provider_i_fp.setdefault(model, {})
            new = [f for f in fps if f not in seen]
            if not new:
                return
            before = sorted(seen)
            for f in new:
                seen[f] = {"run_key": run_key, "utc": now}
            self.store.put_meta("provider_i_fingerprints", json.dumps(self._provider_i_fp), now)
            if before or len(fps) > 1:
                self.store.event(now, "provider_i_fingerprint_changed", model,
                                 f"{run_key}: LLM Lab reported build(s) {', '.join(new)} not seen before for this model "
                                 f"(seen: {', '.join(before) or 'none'}; this run: {', '.join(fps)}); recorded, dispatch continues")

    def _check_run_identity(self, slot: str, run_key: str, model: str, rec: RunRecord | None, now: str,
                            ident: dict | None = None) -> str | None:
        """After a run on a provider_g slot: its recorded system fingerprint against the slot's checked one. A change (a slot restarted
        with other weights mid-campaign) is an event and an alarm, blocks the slot, and is returned so the run is retried. With
        `slot_peer_blocks` false (amendment 41) it is the event and the alarm only: the slot stays in dispatch and None is returned, so the run's outcome stands."""
        if self.slot_gate is None or rec is None or backend_of(slot) != "provider_g":
            return None
        from .slot_equality import run_fingerprints
        kw = {} if ident is None else {"expected": ident.get("fingerprint")}   # a stored None = unknown at the start: not checked
        msg = self.slot_gate.note_run(slot, run_key, run_fingerprints(getattr(rec, "model_version", None)), **kw)
        if msg:
            assert self.store
            if self.cfg.slot_peer_blocks:
                self.store.event(now, "slot_identity_changed", model, msg + "; the slot is out of dispatch and the run is retried")
            else:
                self.store.event(now, "slot_identity_changed", model, msg + "; recorded and alarmed: the slot stays in dispatch (it is held to the registered pin only, amendment 41) and the run's outcome stands (no retry)")
            self.store.put_meta("slot_equality", json.dumps(self.slot_gate.state()), now)
            if not self.cfg.slot_peer_blocks:
                return None
        return msg

    def _update_slot_gate(self, slots: list[dict] | None, now: dt.datetime) -> None:
        """Compare each provider_g slot not yet checked for its current job with its model's other slots (read-only GET /v1/models);
        record the state for the status process and log every change."""
        if self.slot_gate is None or slots is None:
            return
        assert self.store
        if not self._gate_restored:
            self._gate_restored = True               # a restart keeps the earlier verdicts (who is the reference, who is blocked)
            try:
                raw = self.store.get_meta("slot_equality")
                self.slot_gate.restore(json.loads(raw) if raw else None)
            except (ValueError, TypeError, AttributeError):
                pass                                 # unreadable record: judged from scratch, the alarm side reports it
        ran = {r["slot"] for r in self.store.q("SELECT DISTINCT slot FROM attempts WHERE slot IS NOT NULL")}
        before = {k: dict(v) for k, v in self.slot_gate.status.items()}
        if self.slot_gate.update(slots, ran):
            for k, v in self.slot_gate.status.items():
                if before.get(k) != v:
                    self.store.event(iso(now), "slot_equality", v["model"],
                                     f"{k}: {v['verdict']}{' (OUT OF DISPATCH)' if v['blocked'] else ' (stays in dispatch: it passes the registered pin)' if v['verdict'] == 'different' else ''}: " + "; ".join(v["reasons"]))
            self.store.put_meta("slot_equality", json.dumps(self.slot_gate.state()), iso(now))

    def _refresh_windows(self, slots: list[dict] | None, now: dt.datetime) -> None:
        assert self.store
        for m in [*self.cfg.open_models, *self.cfg.api_models]:
            self.store.set_model_up(m, bool(self._model_slots(m, slots, now, for_window=True)), iso(now))
        # Per-unit windows: every HPC slot of endpoints.json (and every unit that ever failed or has an
        # open window) is tracked. A slot is down when endpoints.json says so ("slot_down": down, ended,
        # ending soon, or gone from the file) or when its requests fail ("request_failures"); either way the
        # window opens even while the model stays up through another backend. Claude units are the model
        # window itself. With an unreadable endpoints file availability is unknown: an open slot window stays open (it closes only after
        # the slot is observed usable) and no new one opens; only request failures count then.
        key_to_model = {v: k for k, v in self.cfg.open_models.items()}
        file_slots = {str(s["slot"]): key_to_model.get(s.get("model")) for s in slots or [] if "slot" in s}
        usable: set[str] = set()
        if slots is not None:
            for key in self.cfg.open_models.values():
                usable |= set(usable_slots(slots, key, now, self.cfg.slot_margin("main")))
        with self._unit_lock:
            for u, m in file_slots.items():
                if m:
                    self._unit_parent.setdefault(u, m)
            self._unit_seen |= {u for u, m in file_slots.items() if m}   # retained: a slot that later vanishes is down
            units = sorted(self._unit_seen)
            down, parent = set(self._unit_down), dict(self._unit_parent)
        for u in units:
            if u.startswith("provider_i:"):
                healthy, reason, par = u not in down, "request_failures", u[len("provider_i:"):]
            else:
                if slots is None and u not in down:
                    continue   # unreadable file: availability unknown, keep the window state as it is
                healthy = (slots is None or u in usable) and u not in down
                reason, par = ("request_failures" if u in down else "slot_down"), parent.get(u)
            self.store.set_model_up(u, healthy, iso(now), reason, par)

    def _active_by_slot(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for _, (_, _, slot) in self._inflight.items():
            c[slot] = c.get(slot, 0) + 1
        return c

    def _active_by_model(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for _, (model, prov, _) in self._inflight.items():
            if prov == "anthropic":
                c[model] = c.get(model, 0) + 1
        return c

    def _active_by_provider(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for _, (_, prov, _) in self._inflight.items():
            c[prov] = c.get(prov, 0) + 1
        return c

    PILOT_GROUPS = ("pilot", "condition_h", "calibration")       # calibration's rows carry the group "calibration" (default_blocks' interleave), "condition_h" is the status's name for it

    def _cap_group(self, group: str) -> str | None:
        """The key of `max_sandboxes_by_block` that caps runs of this priority group, or None (the global cap alone applies)."""
        by = self.cfg.max_sandboxes_by_block
        if group in by:
            return group
        if group in self.PILOT_GROUPS and "condition_e" in by:
            return "condition_e"
        if group.startswith("main_s2") and "condition_f" in by:
            return "condition_f"          # tier B's S2 runs share tier A's cap: one S2 mix, one measured cap
        return None

    def _is_block1(self, group: str) -> bool:
        """Block 1 = main S1 + pagination; pilot and calibration run at its cap."""
        return group in self.PILOT_GROUPS or group == "condition_e"

    @staticmethod
    def _is_s2(group: str) -> bool:
        """S2 = tier A or B, whether or not a cap entry exists for the group (one without falls back to `max_sandboxes`)."""
        return group.startswith("main_s2")

    def _run_cap(self, key: str) -> int | None:
        """The cap of the block this run belongs to (`max_sandboxes_by_block`), else the global `max_sandboxes`, lowered by the live check's steps. A calibration or pilot run of layout S2 is an
        S2 run, starting under and contributing the S2 cap of its model's tier, exactly as main's (science, #34, 3 Nov: "as main does")."""
        cap = self._raw_cap(self._capped_group(key))
        return LC.lowered_cap(cap, self.cfg.level_ladder, self._live.steps)       # the live check's steps (amendment 25(e)): every cap, global and per block, here and nowhere else

    def _capped_group(self, key: str) -> str:
        """The group whose cap this run is under: its own, but a calibration or pilot run of layout S2 is under the S2 cap of its model's tier."""
        g = self._group_name.get(key, "")
        if g in self.PILOT_GROUPS and self._layout_of.get(key) == "state_b":
            return group_of("main", "state_b", self._model_of.get(key, ""))
        return g

    def _is_s2_run(self, key: str) -> bool:
        """An S2 run for the caps: main's S2 groups, and also the pilot's and calibration's runs of layout S2 (science, #34, 3 Nov)."""
        g = self._group_name.get(key, "")
        return self._is_s2(g) or (g in self.PILOT_GROUPS and self._layout_of.get(key) == "state_b")

    def _inflight_cap(self, key: str) -> int | None:
        """What a run in flight contributes to the cap on everything else: its own cap, as a calibration or pilot run of layout S2 is an S2 run for the caps throughout (`_run_cap`)."""
        return self._run_cap(key)

    def _within_every_cap(self, key: str) -> bool:
        """Science, #9 (1 Nov): every run in flight stays within its OWN block's measured cap, counting everything in flight. Starting
        `key` is allowed when the total after it is at most its own cap and at most the cap of every run already in flight (so block 1
        stops at cap 2 while an S2 run is in flight)."""
        held = list(self._inflight) + ([o["run_key"] for o in self.hosts.orphans.values()] if self.hosts is not None else [])   # a lost remote run may still be reading fr
        caps = [c for c in [self._run_cap(key)] + [self._inflight_cap(k) for k in held] if c is not None]
        return not caps or len(held) + len(self._held_back) + self._probe_places + self._reserved_n() + 1 <= min(caps)         # (a dead dispatcher's sandbox that could not go, a live-check probe's place, and the sandboxes of the processes beside the campaign that are up)

    def _next_dispatch(self) -> tuple[Any, str] | None:
        """The first pending run, in manifest order, that can start now. Also logs every wait of a ready block-1 run for an S2 run."""
        res, waits_for_s2 = self._next_dispatch_inner()
        self._track_block1_wait(waits_for_s2)
        if res is not None and self._cap_held:
            self._note_cap_dispatch(res[0])
        self._report_endpoint_waits(self.clock())       # every pass, whatever stopped it early: the passes of a wait that has ended still go out
        return res

    def _note_endpoint_wait(self, model: str, now: dt.datetime, snap: dict[str, Any]) -> None:
        """One dispatch pass in which `model`'s next run was ready (eligible, within the level and every sandbox cap) and every up, admitted, unpaused endpoint of the model was at its
        cap (amendment 41(b); science, #34, 4 Nov 01:49Z). Kept in memory; `_report_endpoint_waits` writes it out. `snap`: the caps in force and the sessions in flight, at this pass."""
        w = self._ep_wait.setdefault(model, {"unreported": 0, "unflushed": 0, "first": None, "last": None, "first_ever": None, "event_at": None, "flush_at": None, "since": None, "snap": None})
        w["unreported"] += 1
        w["unflushed"] += 1
        w["first"] = w["first"] or iso(now)
        w["first_ever"] = w["first_ever"] or iso(now)
        w["last"], w["snap"] = iso(now), snap

    def _report_endpoint_waits(self, now: dt.datetime) -> None:
        """At most one `endpoint_wait` event per model per `endpoint_wait_event_s`, carrying the passes since the model's last one (the first wait of a model goes out at once), and the
        cumulative count of every model with passes not yet in the store (meta `endpoint_waits`: {model: {passes, since_utc, last_utc}}, merged in ONE transaction, so a restart goes on
        from it) at the same cadence. Called on every pass, so the passes of a wait that has ended go out with the first pass a period after the last event: no separate event for
        the end of a wait."""
        assert self.store
        period = self.cfg.endpoint_wait_event_s
        due = lambda at: at is None or (now - at).total_seconds() >= period      # noqa: E731
        for m, w in sorted(self._ep_wait.items()):
            if w["unreported"] and due(w["event_at"]):
                self.store.event(iso(now), "endpoint_wait", m, self._endpoint_wait_text(m, w))
                w.update(unreported=0, first=None, since=iso(now), event_at=now)
        flush = {m: w for m, w in self._ep_wait.items() if w["unflushed"] and due(w["flush_at"])}
        if not flush:
            return

        def go(raw):
            try:
                cur = json.loads(raw) if raw else {}
            except ValueError:
                cur = {}
            cur = cur if isinstance(cur, dict) else {}
            for m, w in flush.items():
                old = cur.get(m) if isinstance(cur.get(m), dict) else {}
                n = old.get("passes") if isinstance(old.get("passes"), int) and not isinstance(old.get("passes"), bool) else 0
                cur[m] = {"passes": n + w["unflushed"], "since_utc": old.get("since_utc") or w["first_ever"], "last_utc": w["last"]}
            return json.dumps(cur, sort_keys=True), [], None
        self.store.update_meta("endpoint_waits", go, iso(now))
        for w in flush.values():
            w.update(unflushed=0, flush_at=now)

    def _endpoint_wait_text(self, model: str, w: dict[str, Any]) -> str:
        snap = w["snap"] or {}
        ll, slots, pool = snap.get("provider_i"), snap.get("slots") or {}, snap.get("pool")
        n = w["unreported"]
        return (f"{model}: {n} dispatch pass{'' if n == 1 else 'es'} found its next run waiting for endpoint caps since {w['since'] or 'this process started'}"
                f" (first wait {w['first']}, last wait {w['last']}); every endpoint of the model was at its cap; caps in force: "
                f"LLM Lab cap {ll[1] if ll else 'none'}; provider_g per slot " + (", ".join(f"{sl} {c[1]}" for sl, c in sorted(slots.items())) or "none")
                + (f" (pool {pool[1]})" if pool else "") + "; in flight: LLM Lab " + (str(ll[0]) if ll else "none") + ", provider_g "
                + (", ".join(f"{sl} {c[0]}" for sl, c in sorted(slots.items())) or "none") + (f" (pool {pool[0]})" if pool else ""))


    def _cohort_start(self, run_key: str, started_at: dt.datetime) -> dt.datetime:
        """When this run counts as started, this attempt being begun included (it is the first if none came before): the first start sets cohort membership,
        since a retry or a re-queued harness_error run is the same run restarting (science, #34, 21:33Z); a deferral by M's or D's cap counts as a start
        (amendment 24)."""
        assert self.store
        c = self.store.counted_starts(run_key).get(run_key)
        if c:
            t = dt.datetime.fromisoformat(c.replace("Z", "+00:00"))
            if t < started_at:
                return t
        return started_at

    def _deferred_in_cohort(self) -> set[str]:
        """Pending runs deferred by their cap before the cohort cut and not yet started: past the hard stop they still start when their cap allows
        (amendment 24). One that has started since is as any other: a retry after the hard stop waits for the operator's word."""
        assert self.store
        if self._cohort is None:
            return set()
        return {r["run_key"] for r in self.store.q(
            "SELECT run_key, deferred_utc FROM runs r WHERE status='pending' AND deferred_utc IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.run_key=r.run_key AND a.state != 'unstarted')")
            if dt.datetime.fromisoformat(r["deferred_utc"]) < self._cohort}

    def _note_cap_hold(self, row: Any, why: tuple[str, str]) -> None:
        """An event when a run is first held by its host's option cap (lane-dress, 2 Nov: holds auditable from the records, not from timing)."""
        k = row["run_key"]
        if k in self._cap_held:
            return
        h = self._host_of(row["model"])
        g = self._group_name.get(k, "")
        self._cap_held[k] = (row["model"], h, row["option"], g, row["seed"], time.time())
        assert self.store
        now = iso(self.clock())
        self.store.event(now, "option_cap_hold", row["model"], f"{k}: held, {why[1]}; stage {g}, seed {row['seed']}")
        if row["option"] in ("M", "D"):        # amendment 24: its deferral is when it counts as started
            self.store.set_deferred(k, now, why[0])

    def _note_cap_dispatch(self, row: Any) -> None:
        """At dispatch: the held run itself starting is its release; another run of a model with a held run is a skip-ahead past it."""
        assert self.store
        k, m = row["run_key"], row["model"]
        if k in self._cap_held:
            _, h, o, g, sd, since = self._cap_held.pop(k)
            self.store.event(iso(self.clock()), "option_cap_released", m, f"{k}: starts after {time.time() - since:.0f} s held ({o} on {h}, stage {g}, seed {sd})")
            return
        for hk, (hm, h, o, g, sd, _) in self._cap_held.items():
            if hm == m:
                now = iso(self.clock())
                self.store.event(now, "option_cap_skip_ahead", m, f"{k} ({row['option']}) starts ahead of the held {hk} ({o} on {h}); "
                                                                f"stage {g}, seed {sd}")
                for k2, seq2, why2 in getattr(self, "_stage_capped", {}).get(m, []):    # every capped M/D run it passes is deferred now (amendment 24)
                    if seq2 < row["seq"]:
                        self.store.set_deferred(k2, now, why2)
                break

    def _save_b1(self, open_wait: dict | None) -> None:
        """One meta value holds the cumulative waits AND the open wait, so closing a wait and counting it is a single write."""
        assert self.store
        self.store.put_meta("block1_waits", json.dumps({"count": self._b1_waits, "total_s": round(self._b1_wait_s, 1), "open": open_wait}),
                            iso(self.clock()))

    def _track_block1_wait(self, waiting: bool) -> None:
        assert self.store
        mono = time.monotonic()
        if not self._b1_loaded:                      # cumulative counts survive a restart: continue from the store
            self._b1_loaded = True
            prev = self.store.get_meta("block1_waits")
            if prev:
                d = json.loads(prev)
                self._b1_waits, self._b1_wait_s = int(d.get("count", 0)), float(d.get("total_s", 0.0))
                o = d.get("open")
                if o:                                # a wait the previous process did not live to end: counted up to its last heartbeat
                    length = max(0.0, float(o.get("last_seen_s", 0.0)) - float(o.get("start_s", 0.0)))
                    self._b1_waits += 1
                    self._b1_wait_s += length
                    self._save_b1(None)              # one write: the count includes it and the open marker is gone
                    self.store.event(iso(self.clock()), "block1_wait_interrupted", None,
                                     f"a block-1 wait for an S2 run was cut by a restart after at least {length:.1f} s (counted up to its last heartbeat)")
        if waiting and self._b1_wait_since is None:
            self._b1_wait_since, self._b1_hb, self._b1_start_wall = mono, mono, time.time()
            self._save_b1({"start_s": self._b1_start_wall, "last_seen_s": self._b1_start_wall})
            self.store.event(iso(self.clock()), "block1_wait_started", None,
                             "a ready block-1 run is held back only by the S2 cap (an S2 run is in flight)")
        elif waiting and mono - self._b1_hb >= 10.0:        # a heartbeat, so a wait cut by a restart is still counted
            self._b1_hb = mono
            self._save_b1({"start_s": self._b1_start_wall, "last_seen_s": time.time()})
        elif not waiting and self._b1_wait_since is not None:
            length = mono - self._b1_wait_since
            self._b1_wait_since = None
            self._b1_waits += 1
            self._b1_wait_s += length
            self._save_b1(None)                      # one write, as above
            self.store.event(iso(self.clock()), "block1_waited_for_s2", None,
                             f"a ready block-1 run waited {length:.1f} s for an S2 run to finish (the S2 cap bounds everything in flight)")

    def _next_dispatch_inner(self) -> tuple[tuple[Any, str] | None, bool]:
        assert self.store
        now = self.clock()
        mono = time.monotonic()
        if mono - self._alive_at >= 60.0:            # the loop is looking: the status alarms when it stops, or when it starts nothing for long
            if not self._since_noted:
                self._since_noted = True
                self.store.put_meta("dispatch_since", iso(now), iso(now))      # this process's first look: a restart starts the grace anew
            self._alive_at = mono
            self.store.put_meta("dispatch_alive", iso(now), iso(now))
        self._caps_sync()                            # before anything reads a cap (the windows, the slot gate and the caps below): one set of caps for the whole round
        self._look_reservations()                    # which processes beside the campaign are up (their sandboxes count in the caps below)
        slots = fresh_endpoints(self._read_slots(), now, self.cfg.endpoints_max_age_s) if self.cfg.open_models else None
        if self.cfg.open_models and (slots is None) != self._ep_unreadable:      # unreadable: no provider_g slot is a candidate (usable_slots of None)
            self._ep_unreadable = slots is None
            self.store.event(iso(now), "endpoints_unreadable" if slots is None else "endpoints_readable", None,
                             f"{self.cfg.endpoints_file}: " + ("unreadable: slot availability is unknown, no provider_g slot is dispatched"
                                                              if slots is None else "readable again"))
        self._update_slot_gate(slots, now)  # before any slot is offered: a new slot is compared with its model's others first
        self._refresh_windows(slots, now)  # endpoint windows are tracked even while dispatch is held
        local_mem_paused = self._host_memory_paused(now, mono)      # read once a tick, here: the backend clock below needs them, whatever stops dispatch further down
        local_disk_held = self._local_disk_held(now, mono)
        self._update_backend_loss(slots, now, local_mem_paused, local_disk_held)      # seed-first: a model without a usable backend, or on a host held, for the launch config's time counts as held at the boundary
        self._fincore(mono, now)
        self._fr_memory(mono, now)
        self._note_m_backlog(mono)
        self._live_raise(now)
        past_hard = self._past_hard_stop(now)
        exempt = self._deferred_in_cohort() if past_hard else set()
        self._hg_idle = bool(past_hard and not exempt)      # the health gate has nothing to learn then: it makes no check, and no run starts
        if past_hard and not exempt:        # amendment 24: past the hard stop only a run deferred by its cap before the cohort cut may still start
            return None, False
        if mono < self._pause_until:
            return None, False
        opw_busy = [w for h in sorted({e["host"] for e in self._opw_entries()}) if (w := opwrestart.in_progress(self._opw_entries(), h))]
        if opw_busy:        # an op_worker restart is under way or aborted: nothing starts, for all options, at once (not on the settings check's cadence)
            if opw_busy[0] != self._opw_held:
                self._opw_held = opw_busy[0]
                self.store.event(iso(now), "opw_restart_hold", None, opw_busy[0])
            return None, False
        if self._opw_held:
            self._opw_held = None
            self.store.event(iso(now), "opw_restart_resume", None, "the restart log has no open restart: dispatch resumes")
        if local_mem_paused and self.hosts is None:
            return None, False
        if local_disk_held and self.hosts is None:
            return None, False
        stopped_u, stopped_h = self._unstarted_stopped(), self._harness_error_stopped()     # both asked, so each release is used at its own stop
        canary_opts = None
        if stopped_u and not stopped_h and not past_hard:      # the unstarted stop lets ONE canary run of its option through now and then, and nothing else (none past the hard stop)
            canary_opts = self._canary_options()
            if not canary_opts:
                return None, False
        elif stopped_u or stopped_h:                  # repeated refusals to start a sandbox, or harness errors of one kind: nothing new is sent until someone looks
            return None, False
        if self._fr_settings(mono)["state"] in ("mismatch", "unknown", "restart", "unplanned", "disk_low", "disk_unknown", "logs_alarm", "logs_unknown"):   # a revert or an unreadable state: nothing new starts
            return None, False
        by_prov, by_slot = self._active_by_provider(), self._active_by_slot()
        self._provider_g_cap = self._provider_g_pool_cap(slots)
        by_model = self._active_by_model()
        shared_api = sum(n for m, n in by_model.items() if m not in self.cfg.anthropic_caps)
        pending = self._pending()
        if self._quiet_window_holds(now, mono):      # amendment 29(g): dispatch is paused for all options until a file says who resumes it and why
            return None, False
        every_pending = pending
        if past_hard:
            pending = [r for r in pending if r["run_key"] in exempt]
        gate = self._model_66_gate(mono)
        if self.cfg.served_check and self._served_state():       # a model whose server reported other settings than were requested
            pending = [] if "*" in self._served_stop else [r for r in pending if r["model"] not in self._served_stop]
        if canary_opts is not None:
            pending = [r for r in pending if r["option"] in canary_opts]
        if self._he_stopped_models:       # a model stopped by harness errors: its runs wait (they stay pending); the other models go on
            pending = [r for r in pending if r["model"] not in self._he_stopped_models]
        if gate["held"] or gate.get("authorised_seed") is not None:      # model_66's main-block runs of the seeds the gate holds wait (they stay pending, in full): every seed before
            # pricing, the seeds after the highest AUTHORISED one on an over-limit or unknown projection (amendment 29(d); a seed that became the one in progress is not thereby
            # authorised), and while it is not holding, the seeds above the authorised one until their own decision; everything else, the barriers and the authorised seeds go on
            from .gate import blocked_by_gate
            pending = [r for r in pending if r["model"] != self.cfg.gate_model or r["block"] in self.cfg.barrier_blocks or not blocked_by_gate(gate, r["seed"])]
        if self._hg is not None:            # the Onedata health gate (healthgate.py): a pre-run check failed, so no run starts on any host until the dispatcher's own check passes. It sits
            return None, False              # after every hold that keeps state of its own (the hard stop, the fr settings and disk checks, the quiet window, the model_66 gate, a served-settings
                                            # release), so those go on at their own times through an outage, and before what only a start reads (the read check between blocks, the cap
                                            # holds that stamp a deferral, the seed boundary), which an outage must not advance. Opening the gate removes only this hold.
        if self._read_check_holds([r for r in pending if r["run_key"] not in self._inflight], mono):
            return None, False          # between blocks only: the read check of the next block failed or is unreadable, or a block is still running
        host_hold, orphan_keys = self._host_holds(local_mem_paused, local_disk_held)
        gi = self._group_idx
        # earliest group still to dispatch, per model (pending, not in flight) and overall (in flight counts)
        min_model: dict[str, int] = {}
        min_all = min((gi[r["run_key"]] for r in pending), default=0)
        for r in pending:
            if r["run_key"] not in self._inflight:
                min_model[r["model"]] = min(min_model.get(r["model"], 1 << 30), gi[r["run_key"]])
        seed_cap, held_unstarted = self._seed_boundary(every_pending, gate)       # amendment 29(b): no seed's run starts until every run of the seed before has started
        def eligible(row) -> bool:
            k = row["run_key"]
            if k in self._inflight or self._not_before.get(k, 0) > mono:
                return False
            if host_hold and (host_hold.get(self._host_of(row["model"])) or k in orphan_keys):
                return False                   # its host takes nothing new now (down, unprobed, its own cap, memory, disk), or its lost attempt may still run
            if self.cfg.priority_mode == "strict":
                return gi[k] <= min_all
            if seed_cap is not None and self._seed_of.get(k, 0) > seed_cap:
                return False                   # a later seed waits for the one before it, until each of its runs (a held model's apart) has started
            return gi[k] <= min_model[row["model"]]

        def slot_full(model: str, sl: str) -> bool:
            """Is this slot at a cap of its backend right now?"""
            b = backend_of(sl)
            if b == "provider_i":
                return by_slot.get(sl, 0) >= self.cfg.provider_i_caps.get(model, 0)
            if b == "anthropic":
                if model in self.cfg.anthropic_caps:   # its own cap: model_67's never blocks model_66's
                    return by_model.get(model, 0) >= self.cfg.anthropic_caps[model]
                return shared_api >= self.cfg.cap("anthropic")     # a Claude model with no entry shares the fallback cap
            if by_prov.get(b, 0) >= (self._provider_g_cap if b == "provider_g" and self._provider_g_cap is not None else self.cfg.cap(b)):
                return True
            slot_cap = self.cfg.provider_g_slot_caps.get(self.cfg.open_models.get(model, "")) if b == "provider_g" else None
            return slot_cap is not None and by_slot.get(sl, 0) >= slot_cap     # this slot holds as many sessions as it can

        def endpoint_slot(row) -> str | None:
            """The slot this run could use right now as far as its model's endpoint goes (the sandbox cap is not looked at)."""
            model = row["model"]
            cands = [sl for sl in self._model_slots(model, slots, now, block=row["block"]) if not slot_full(model, sl)]
            return self._choose_slot(row, cands, by_slot)

        def endpoint_wait_snapshot(row) -> dict[str, Any] | None:
            """Called for a ready run with no slot: None unless the model is an open model with at least one up, admitted, unpaused endpoint, and EVERY one of them is at its cap
            (a model with no such endpoint has no backend, which `_update_backend_loss` records). Else the caps in force and the sessions in flight, for the event."""
            model = row["model"]
            if model in self.cfg.api_models:
                return None
            every = self._model_slots(model, slots, now, block=row["block"], margin_s=float("-inf"))      # every up, admitted, unpaused slot, whatever its window: a slot with room that
            if not every or not all(slot_full(model, sl) for sl in every):                                 # ends too soon for this run holds it by its window, not by the caps
                return None                                                                                  # (so the answer depends on the model alone, and `ep_seen` may keep it)
            hel = [sl for sl in every if backend_of(sl) == "provider_g"]
            cap_h = self._provider_g_cap if self._provider_g_cap is not None else self.cfg.cap("provider_g")
            return {"provider_i": [by_slot.get(provider_i_slot(model), 0), self.cfg.provider_i_caps.get(model, 0)] if provider_i_slot(model) in every else None,
                    "slots": {sl: [by_slot.get(sl, 0), self.cfg.provider_g_slot_caps.get(self.cfg.open_models.get(model, ""))] for sl in hel},
                    "pool": [by_prov.get("provider_g", 0), cap_h] if hel else None}

        deferred = self._deferred_unstarted() if self._staged else {}
        if deferred:        # science, #34, 3 Nov 02:45Z: a deferred run keeps first call on its option's freed slots, and no later seed's run of that option starts on its host while one is deferred there
            lowest: dict[tuple[str, str], int] = {}
            for dk, (dh, do, dsd) in deferred.items():
                lowest[(dh, do)] = min(lowest.get((dh, do), dsd), dsd)
            by_key = {r["run_key"]: r for r in pending}
            base_deferral = eligible

            def eligible(row, _b=base_deferral) -> bool:      # noqa: F811
                k = row["run_key"]
                if not _b(row):
                    return False
                lo = lowest.get((self._host_of(row["model"]), row["option"]))
                if lo is None or k in deferred:
                    return True
                if row["seed"] > lo:
                    return False                   # a later seed waits while a seed-1 run of its option is deferred on its host
                me = (self._host_of(row["model"]), row["option"])
                return not any((dh, do) == me and dk in by_key and dsd <= row["seed"] and _b(by_key[dk]) and endpoint_slot(by_key[dk]) is not None
                               for dk, (dh, do, dsd) in deferred.items())      # the deferred run, if it could start now but for the caps, goes before this one
        if self.hosts is None and self._local_disk_floor() is not None:      # one host: a D run's own peak must fit too (delta pass on d90cb55, finding 3)
            room1 = self._d_room_gib("")
            base_eligible = eligible
            eligible = lambda row, _b=base_eligible: _b(row) and not (row["option"] == "D" and (room1 != room1 or room1 < self._d_peak_gib(
                self._layout_of.get(row["run_key"], "state_b"))))   # noqa: E731 - nan: free space unknown
        def only_its_bound_holds(row, stepped) -> bool:
            """Would this run start now, but for its M or D bound? Only then is it deferred by that bound (final pass S3 #1: a stamp on a run that had no endpoint
            or sandbox anyway would put it in the cohort, and let it start past the hard stop, for a wait the bound did not cause)."""
            k = row["run_key"]
            if not self._within_every_cap(k) or endpoint_slot(row) is None:
                return False
            if self._staged:                         # seed-first: a run yields to a ready run of an EARLIER stage, not to block 1 as such
                return not any(gi[r["run_key"]] < gi[k] and stepped(r) and endpoint_slot(r) is not None for r in pending)
            if self.cfg.max_sandboxes_by_block and self._is_s2(self._group_name.get(k, "")):
                return not any(self._is_block1(self._group_name.get(r["run_key"], "")) and stepped(r) and endpoint_slot(r) is not None for r in pending)
            return True
        if host_hold:                      # a per-host option cap (e.g. D on a small disk): a model whose next run is held starts its next runs of
            eligible = self._option_step(pending, eligible, only_its_bound_holds)    # other options from the SAME stage and seed only; the held run goes first when space frees
        # A block-1 run is "waiting for a sandbox" when it is eligible and its own model's endpoint is free: only the sandbox is
        # missing. A block-1 run blocked on its model's endpoint does not count, so it never holds back S2 (science, #9, 18:47Z).
        capped = bool(self.cfg.max_sandboxes_by_block)
        b1_ready = capped and any(self._is_block1(self._group_name.get(r["run_key"], "")) and eligible(r) and endpoint_slot(r) is not None
                                  for r in pending)
        s2_inflight = capped and any(self._is_s2_run(k) for k in self._inflight)
        waits_for_s2 = False
        staged = self._staged
        ready_stage: int | None = None         # seed-first: the earliest stage with a run that is ready and held only by the caps
        ep_seen: dict[str, Any] = {}           # the models whose endpoints were looked at for a wait in this pass
        for row in pending:
            if not eligible(row):
                continue
            k = row["run_key"]
            gname = self._group_name.get(k, "")
            is_s2 = capped and self._is_s2(gname)
            if staged:
                if ready_stage is not None and gi[k] > ready_stage:
                    continue                   # amendment 27(b) supersedes 10 and 18(e): the order is the stages', so a later stage never takes the sandbox a ready earlier one waits for
            elif is_s2 and b1_ready:
                continue                       # S2 uses only spare capacity: a ready block-1 run has first claim on the next sandbox
            if not self._within_every_cap(k):
                if capped and self._is_block1(gname) and s2_inflight and endpoint_slot(row) is not None:
                    waits_for_s2 = True        # ready, and held only by the S2 cap
                if staged and ready_stage is None and endpoint_slot(row) is not None:
                    ready_stage = gi[k]
                continue                       # a run of another block with a higher cap may still start
            slot = endpoint_slot(row)
            if slot is None:
                if row["model"] not in ep_seen:      # model paused, with no backend, or at its endpoints' caps: only the last is a wait (amendment 41(b)), once per model and pass
                    ep_seen[row["model"]] = snap = endpoint_wait_snapshot(row)
                    if snap is not None:
                        self._note_endpoint_wait(row["model"], now, snap)
                continue
            if canary_opts is not None:
                self._canary_run, self._canary_epoch = k, time.time()      # the canary goes; the next one in `unstarted_canary_every_s`
            if held_unstarted:
                self._note_seed_crossing(row, held_unstarted)
            return (row, slot), False
        return None, waits_for_s2

    def _held_models(self, no_backend: bool = True) -> set[str]:
        """Models the scheduler withholds as a whole: those stopped on what their server reported ("*" is every model) and, with `no_backend`, those held for no usable
        backend or host. The seed boundary takes both; the quiet window takes neither (amendment 30(g): an automatically held model's seed-1 runs count as not started
        there). The model_66 gate (18(e)) holds runs, not the model: see `_gate_holds`."""
        held: set[str] = set()
        if self.cfg.served_check and self._served_state():
            held |= set(self._served_stop)
        if no_backend:
            held |= {m for m, rec in (self._lost or {}).items() if rec.get("held_utc")}       # no usable backend for the launch config's time (science, #34, 3 Nov 02:45Z)
        return held

    def _gate_holds(self, gate: dict, model: str, seed: int) -> bool:
        """Does the model_66 gate hold this model's runs of this seed? It holds the seeds after the highest AUTHORISED one (or every seed before pricing), never an authorised
        seed (`gate.held_by_gate`, `hold_after_seed`): "the seed in progress is never held, so its unstarted model_66 runs count at the boundary and for the window" (science,
        #34, 3 Nov 04:34Z). "In progress" there is the authorised seed: the lowest unstarted seed can be a later one the gate holds (no release for it), and that one does not count."""
        from .gate import held_by_gate
        return bool(self.cfg.gate_model) and model == self.cfg.gate_model and held_by_gate(gate, seed)

    def _backend_lost(self, model: str, slots: list[dict] | None, now: dt.datetime, host_held: dict[str, str | None] | None = None) -> str | None:
        """Why this model has no usable backend now, or None: every endpoint it could use is down (the outage window, which stays open until a success clears it), unreadable,
        or not shown to serve it, or every one is a key the provider refused; or its host is unreachable (hosts.unreachable) or held (`host_held`, from _host_held: disk, memory).
        Pauses of a minute do not count."""
        cands = self._model_slots(model, slots, now, for_window=True)
        if not cands:
            return "no endpoint of it is up, readable and shown to serve it"
        if all(self._unit_of(model, c) in self._key_held for c in cands):
            return "the provider refused its key"
        if self.hosts is not None:
            why = self.hosts.unreachable(self._host_of(model))
            if why:
                return f"its host {self._host_of(model)} is unreachable ({why})"
            why = (host_held or {}).get(self._host_of(model))
            if why:
                return f"its host {self._host_of(model)} is held ({why})"
        return None

    def _host_held(self, host: str, local_mem_paused: bool, local_disk_held: bool) -> str | None:
        """Science, #34, 3 Nov 03:27Z: why `host` takes no new run for a reason beyond an ordinary wait, or None. As _host_holds (the host's own holds: down, probe, code, image, memory,
        disk; and for the local host its memory watchdog and disk floor), but asked with nothing on the host, so its own cap, which passes as runs end and says nothing of the host,
        never reads as a hold. With one host there is none to hold: the watchdog and the disk floor stop all dispatch at once, and nothing starts."""
        if self.hosts is None:
            return None
        why = self.hosts.hold(host, 0, disk_reserve_gib=self._d_reserve_gib(host))
        if why is None and host == self.hosts.local:
            if local_mem_paused:
                why = "this host's memory watchdog"
            elif local_disk_held:
                why = "this host's disk below its hold"
        return why

    def _update_backend_loss(self, slots: list[dict] | None, now: dt.datetime, local_mem_paused: bool = False, local_disk_held: bool = False) -> None:
        """Science, #34, 3 Nov 02:45Z: "A model with no usable backend for 30 minutes counts as held, automatically, recorded with an alarm; when it returns, its seed-1 runs go
        first." The time is `no_backend_hold_s` of the launch config. The record (kept in the store, so a restart keeps the clock running) is `since_utc` of the loss and, once
        held, `held_utc`; `_held_models` reads it, and the status alarms while it stands. Only seed-first has a boundary; only a model with unstarted runs is looked at. On its return the
        model is no longer held, so its unstarted seed-1 runs hold the boundary again: nothing of a later seed starts until they have. Science, 3 Nov 03:27Z: a host held for that time
        counts its models the same way (_host_held), one clock and one record, and the clock starts over when the hold clears."""
        hold_s = self.cfg.no_backend_hold_s
        if not self._staged or not hold_s or hold_s <= 0:
            return
        assert self.store
        if self._lost is None:
            try:
                self._lost = {m: dict(r) for m, r in json.loads(self.store.get_meta("backend_lost") or "{}").items() if isinstance(r, dict)}
            except (ValueError, TypeError, AttributeError):
                self._lost = {}
        now_s = iso(now)
        host_held = {h: self._host_held(h, local_mem_paused, local_disk_held) for h in {self._host_of(m) for m in self._seed_models}} if self.hosts is not None else {}
        lost_now = {m: w for m in self._seed_models if (w := self._backend_lost(m, slots, now, host_held))}
        if not lost_now and not self._lost:
            return
        todo = {m for m, _ in self._unstarted_seed_first().values()}       # a model with nothing left to start has nothing to hold
        changed = False
        for m, why in lost_now.items():
            if m not in todo:
                continue
            rec = self._lost.get(m)
            if rec is None:
                rec = self._lost[m] = {"since_utc": now_s, "why": why, "held_utc": None}
                self.store.event(now_s, "model_backend_lost", m, f"{m} has no usable backend ({why}); it counts as held at the seed boundary if that lasts {hold_s:g} s")
                changed = True
            if rec["why"] != why:
                rec["why"], changed = why, True
            if not rec.get("held_utc") and (now - dt.datetime.fromisoformat(rec["since_utc"])).total_seconds() >= hold_s:
                rec["held_utc"], changed = now_s, True
                self.store.event(now_s, "model_held_no_backend", m, f"ALARM: {m} has had no usable backend since {rec['since_utc']} ({why}) for {hold_s:g} s: it counts as HELD at the seed boundary "
                                                                   "(amendment 29(b), science's ruling of 3 Nov), so a later seed's runs start ahead of its earlier ones, each counted per model in the status; "
                                                                   "when it returns its seed-1 runs go first")
        for m in [m for m in self._lost if m not in lost_now or m not in todo]:
            rec = self._lost.pop(m)
            changed = True
            self.store.event(now_s, "model_backend_back", m, (f"{m} has a usable backend again" if m in todo else f"{m} has nothing left to start") +
                             f" (no usable backend since {rec['since_utc']}" + (f", held since {rec['held_utc']}" if rec.get("held_utc") else ", never held") + "); "
                             "its unstarted runs of the earliest seed hold the seed boundary again and go first")
        if changed:
            self.store.put_meta("backend_lost", json.dumps(self._lost, sort_keys=True), now_s)

    def _unstarted_seed_first(self, deferred_as_started: bool = False) -> dict[str, tuple[str, int]]:
        """run key -> (model, seed) of the seed-first runs with no started attempt: a run that started once and waits for a retry has started (26(b): the first start
        counts), one refused before its sandbox (state `unstarted`) has not. With `deferred_as_started` a run deferred by M's or D's cap counts as started too, as for
        the cohort (science, #34, 3 Nov 02:45Z): the seed boundary asks it so; the quiet window does not (a deferred run has not run)."""
        assert self.store
        started = {r[0] for r in self.store.q("SELECT DISTINCT run_key FROM attempts WHERE state != 'unstarted'")} | set(self._inflight)
        if deferred_as_started:
            started |= set(self._deferred_unstarted())
        return {r.run_key: (r.run.model, r.run.seed) for r in self.rows if r.stage and r.run_key not in started}

    def _deferred_unstarted(self) -> dict[str, tuple[str, str, int]]:
        """run key -> (host, option, seed) of the pending runs deferred by their host's M or D cap that have not started. Deferral exists only with two hosts."""
        assert self.store
        if self.hosts is None:
            return {}
        return {r["run_key"]: (self._host_of(r["model"]), r["option"], r["seed"]) for r in self.store.q(
            "SELECT run_key, model, option, seed FROM runs r WHERE status='pending' AND deferred_utc IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.run_key=r.run_key AND a.state != 'unstarted')")}

    def _seed_boundary(self, every_pending: list, gate: dict) -> tuple[int | None, dict[str, tuple[int, int]]]:
        """Amendment 29(b): "No seed's run starts until every run of the seed before has started, except a held model's, recorded." Returns the highest seed whose
        runs may start (the lowest seed with a run not yet started, that of a held model apart; None: no boundary) and, per held model, (its lowest unstarted held seed,
        how many of its held runs are unstarted) when that seed is below the cap, for the record of each crossing. A run is held when its model is (`_held_models`) or the
        model_66 gate holds its seed (`_gate_holds`): the gate's model keeps the boundary for the seed in progress, and a later seed it holds is crossed and recorded."""
        if not self._staged:
            return None, {}
        held = self._held_models()
        every = "*" in held
        pend = {r["run_key"] for r in every_pending}
        rest = {k: ms for k, ms in self._unstarted_seed_first(deferred_as_started=True).items() if k in pend}      # a run the pilot's barrier still hides cannot start anyway; a deferred one has started
        exempt = lambda m, sd: m in held or self._gate_holds(gate, m, sd)      # noqa: E731
        mine = [sd for m, sd in rest.values() if not every and not exempt(m, sd)]
        cap = min(mine, default=None)
        crossed: dict[str, tuple[int, int]] = {}
        if cap is not None and not every:
            for m, sd in rest.values():
                if sd < cap and exempt(m, sd):      # the runs the boundary would have waited for
                    lo, n = crossed.get(m, (sd, 0))
                    crossed[m] = (min(lo, sd), n + 1)
        return cap, crossed

    def _note_seed_crossing(self, row: Any, held_unstarted: dict[str, tuple[int, int]]) -> None:
        """A run starts in a seed past a held model's unstarted runs of an earlier one: once per held model and seed, on the record."""
        assert self.store
        sd = self._seed_of.get(row["run_key"])
        if sd is None:
            return
        ahead = [m for m, (lo, _) in held_unstarted.items() if lo < sd]
        if ahead:
            if self._cross_counts is None:
                try:
                    self._cross_counts = {m: {str(x): int(n) for x, n in v.items()} for m, v in json.loads(self.store.get_meta("seed_crossing_counts") or "{}").items()}
                except (ValueError, TypeError, AttributeError):
                    self._cross_counts = {}
            for m in ahead:
                self._cross_counts.setdefault(m, {})[str(sd)] = self._cross_counts.get(m, {}).get(str(sd), 0) + 1       # every run that starts ahead is counted, per held model and seed
            self.store.put_meta("seed_crossing_counts", json.dumps(self._cross_counts, sort_keys=True), iso(self.clock()))
        if self._crossed is None:
            try:
                self._crossed = {(m, int(x)) for m, x in json.loads(self.store.get_meta("seed_crossings") or "[]")}
            except (ValueError, TypeError):
                self._crossed = set()
        new = [(m, lo, n) for m, (lo, n) in held_unstarted.items() if lo < sd and (m, sd) not in self._crossed]
        if not new:
            return
        now = iso(self.clock())
        for m, lo, n in new:
            self._crossed.add((m, sd))
            self.store.event(now, "seed_boundary_crossed", m, f"{row['run_key']} (seed {sd}) starts while held model {m} has {n} unstarted run(s) of an earlier seed, the lowest of seed {lo}: "
                                                              "a held model's runs do not hold the seed boundary (amendment 29(b))")
        self.store.put_meta("seed_crossings", json.dumps(sorted([m, x] for m, x in self._crossed)), now)

    def _quiet_window_holds(self, now: dt.datetime, mono: float) -> bool:
        """Amendment 29(g): "M+'s timed measurements and the registered curve run in one quiet window, when every seed-1 run has started or at the
        time the launch config gives (`quiet_window_utc`), whichever comes first; dispatch pauses for all options, and runs in flight finish first." The window opens once and stays open until a file written
        after it at `quiet_window_release_file` says who resumes and why; the file is used once. Nothing is stopped or killed. Amendment 30(g), which corrects
        29(g): "A deferred run and an automatically held model's seed-1 runs count as not started for this; a model_66-gate hold doesn't." So only a run the model_66 gate
        holds is apart here (the gate never holds the seed in progress: science, #34, 3 Nov 04:34Z); a model held for no usable backend, for its host or on its served
        settings (30(f)'s automatic holds), and a run deferred by its cap, are unstarted (science, #34, 3 Nov 03:14Z, 03:27Z)."""
        assert self.store
        if self._quiet is None:
            try:
                self._quiet = json.loads(self.store.get_meta("quiet_window") or "{}")
            except ValueError:
                self._quiet = {}
        q = self._quiet
        if q.get("released_by"):
            return False
        if not q.get("opened_utc"):
            why = self._quiet_cause(now, mono)
            if why is None:
                return False
            q.update(opened_utc=iso(now), cause=why)
            self.store.put_meta("quiet_window", json.dumps(q, sort_keys=True), iso(now))
            self.store.event(iso(now), "quiet_window_open", None, f"the quiet window is OPEN ({why}): dispatch is paused for all options, {len(self._inflight)} run(s) in flight "
                                                                  f"finish first; it resumes only on a file at {self.cfg.quiet_window_release_file} naming who and why")
        if mono - self._quiet_check_at >= 1.0:       # the file is looked at once a second at most
            self._quiet_check_at = mono
            said = self._quiet_release(q["opened_utc"])
            if said:
                q["released_by"], q["released_utc"] = said, iso(now)
                self.store.put_meta("quiet_window", json.dumps(q, sort_keys=True), iso(now))
                self.store.event(iso(now), "quiet_window_released", None, f"dispatch resumes: {said}")
                return False
        return True

    def _quiet_cause(self, now: dt.datetime, mono: float) -> str | None:
        if self._quiet_at is not None and now >= self._quiet_at:
            return f"the latest time {self.cfg.quiet_window_utc} has come"
        seed1 = {k: ms for k, ms in self._unstarted_seed_first().items()}
        first = min((r.run.seed for r in self.rows if r.stage), default=None)
        if first is None:
            return None
        gate = self._model_66_gate(mono)
        # Amendment 30(g): only a run the model_66 gate holds is apart. A model held for no backend, for its host or on its served settings keeps the window closed (it opens at
        # the latest time if the model is not back), and so does the gate's model while its seed in progress has an unstarted run: the gate never holds that seed.
        if any(sd == first and not self._gate_holds(gate, m, sd) for m, sd in seed1.values()):
            return None
        apart = sorted({m for m, sd in seed1.values() if sd == first and self._gate_holds(gate, m, sd)})
        return f"every seed-{first} run has started" + (f", a held model's ({', '.join(apart)}) apart" if apart else "")

    def _quiet_release(self, opened: str) -> str | None:
        """The release file counts if written after the window opened and its first line says who and why (the file is the word, and an empty one says nothing)."""
        p = Path(self.cfg.quiet_window_release_file)
        try:
            first = next(iter(p.read_text(errors="replace").splitlines()), "").strip()
        except OSError:
            return None
        if not first:
            return None
        return release.take(p, release.epoch_of(opened))

    def _read_slots(self) -> list[dict] | None:
        """The endpoint slots: this host's endpoints.json, and with two hosts each remote host's own file from its last probe, each model's
        slots from its own host (hosts.merge_endpoints)."""
        local = read_endpoints(self.cfg.endpoints_file)
        if self.hosts is None:
            return local
        from .hosts import merge_endpoints
        remote = {h: self.hosts.endpoints_of(h) for h, sp in self.hosts.specs.items() if sp.remote}
        seen = getattr(self, "_rep_unreadable", {})
        for h, eps in remote.items():                    # a remote host's own file unreadable: its models get no provider_g slot; each change is an event
            if (eps is None) != seen.get(h, False):
                seen[h] = eps is None
                assert self.store
                self.store.event(iso(self.clock()), "endpoints_unreadable" if eps is None else "endpoints_readable", None,
                                 f"run host {h}'s endpoints.json: " + ("unreadable or not probed: no provider_g slot of its models is dispatched"
                                                                         if eps is None else "readable again"))
        self._rep_unreadable = seen
        return merge_endpoints(self.cfg, local, remote)

    def _host_of(self, model: str) -> str:
        from .hosts import host_of
        return host_of(self.cfg, model)

    def _host_holds(self, local_mem_paused: bool, local_disk_held: bool = False) -> tuple[dict[str, str | None], set[str]]:
        """Two hosts: per host, why nothing new may start there now (None: it may), and the run keys whose lost attempt may still run.
        One host: nothing ({}), and the memory watchdog above holds all dispatch, as before."""
        if self.hosts is None:
            return {}, set()
        n: dict[str, int] = {}
        for _, (m, _, _) in self._inflight.items():
            h = self._host_of(m)
            n[h] = n.get(h, 0) + 1
        holds = {}
        for h in self.hosts.specs:
            left = (len(self._held_back) + self._probe_places + self._reserved_n()) if h == self.hosts.local else 0       # the probe's sandbox is on the dispatcher's host
            why = self.hosts.hold(h, n.get(h, 0) + self.hosts.n_orphans(h) + left, disk_reserve_gib=self._d_reserve_gib(h))
            if why is None and h == self.hosts.local and local_mem_paused:
                why = "this host's memory watchdog"
            if why is None and h == self.hosts.local and local_disk_held:
                why = "this host's disk below its hold"
            holds[h] = why
        return holds, self.hosts.orphan_run_keys()

    def _option_step(self, pending: list, eligible: Callable[[Any], bool], ready: Callable[[Any, Callable], bool] | None = None) -> Callable[[Any], bool]:
        """Bounded skip-ahead (science, #34, 15:39Z): no run starts of an option at its host's cap for it; a model whose first eligible run (manifest
        order) is so held may start its later runs of OTHER options from the same stage (priority group; seed 1's merged stage, amendment 40, holds three) and seed only, never across a stage or a
        seed; the held run is still its earliest pending run, so it starts first once the option has room."""
        assert self.hosts is not None
        n: dict[tuple[str, str], int] = {}
        for k, (m, _, _) in self._inflight.items():
            key = (self._host_of(m), self._option_of.get(k, ""))
            n[key] = n.get(key, 0) + 1
        for o in self.hosts.orphans.values():
            key = (o["host"], o.get("option", ""))
            n[key] = n.get(key, 0) + 1
        room = {h: self._d_room_gib(h) for h in self.hosts.specs}     # per host: free - floor - spools in flight (None: no floor; nan: unknown)

        def at_cap(row) -> tuple[str, str] | None:
            """(the reason's name, reported apart; what the event says), or None."""
            h = self._host_of(row["model"])
            cap = self.hosts.option_cap(h, row["option"])
            if cap is not None and n.get((h, row["option"]), 0) >= cap:
                return f"{row['option']}_cap", f"{row['option']} at host {h}'s cap {cap} ({n.get((h, row['option']), 0)} in flight)"
            if row["option"] == "D" and room[h] is not None:          # its own peak must fit: the sandbox and its spool (science, #2, 20:20Z)
                need = self._d_peak_gib(self._layout_of.get(row["run_key"], "state_b"))
                if room[h] != room[h] or room[h] < need:
                    return "D_disk", (f"D on host {h}: its disk cannot hold this run's peak of {need:.2f} GiB "
                                       + ("(free space unknown)" if room[h] != room[h] else f"({room[h]:.1f} GiB above the floor after the spools in flight)"))
            return None
        held: dict[str, tuple] = {}            # model -> (stage, seed, option) of its first eligible run, held by its host's option cap
        seen: set[str] = set()
        heads: list[tuple] = []                # the held runs, noted below if nothing else stops them (`ready`)
        for r in pending:
            m = r["model"]
            if m in seen or not eligible(r):
                continue
            seen.add(m)
            why = at_cap(r)
            if why:
                held[m] = (self._stage_name.get(r["run_key"], ""), r["seed"], r["option"])      # the STAGE, not the group: seed 1's merged stage holds three groups (amendments 39 and 40)
                heads.append((r, why))
        # amendment 24: the M and D runs of a held model's stage and seed that its cap also stops, which a skip-ahead may pass (then deferred too)
        self._stage_capped = {m: [(r["run_key"], r["seq"], w[0]) for r in pending if r["model"] == m and r["option"] in ("M", "D")
                                  and self._stage_name.get(r["run_key"], "") == g and r["seed"] == sd and r["run_key"] not in self._inflight and (w := at_cap(r))]
                              for m, (g, sd, _) in held.items()}
        if not held and not any(sp.option_caps or sp.disk_min_free_gib is not None for sp in self.hosts.specs.values()):
            return eligible

        def ok(row) -> bool:
            if at_cap(row):
                return False
            hm = held.get(row["model"])
            if hm is not None and (self._stage_name.get(row["run_key"], "") != hm[0] or row["seed"] != hm[1]):
                return False                   # skip ahead only within the held run's own stage and seed (science, #34, 15:39Z). A stage is a group within a seed, except seed 1's merged stage (amendment 40)
            return eligible(row)
        for r, why in heads:
            if r["run_key"] not in self._cap_held and (ready is None or ready(r, ok)):
                self._note_cap_hold(r, why)
        return ok

    def _d_peak_gib(self, layout: str) -> float:
        return float(self.cfg.d_spool_gib.get(layout, max(self.cfg.d_spool_gib.values()))) + float(self.cfg.d_sandbox_gib.get(layout, max(self.cfg.d_sandbox_gib.values())))

    def _d_reserve_gib(self, host: str) -> float:
        """What the D runs in flight on `host` may still add to its disk (the lead, 2 Nov: reserve conservatively): each run's peak (its spool and its
        sandbox) less what its sandbox holds now, measured (diskuse: here directly, on a remote host by its probe); the whole peak while unmeasured, or
        measured before this attempt began; nothing once this attempt's download is known complete (_download_done). A lost run: the whole peak."""
        big = max(self._d_peak_gib(lay) for lay in self.cfg.d_spool_gib)
        local = self.hosts is None or host == self.hosts.local
        probe = None if local else self.hosts.probes.get(host)
        use_of = {} if local else (probe.d_use if probe is not None and probe.ok else {})
        tot = 0.0
        for k, (m, _, _) in list(self._inflight.items()):
            if self._option_of.get(k) != "D" or not (self.hosts is None or self._host_of(m) == host):
                continue
            peak = self._d_peak_gib(self._layout_of.get(k, "state_b"))
            u = self._local_use(k) if local else use_of.get(k)
            if u is None or (probe is not None and probe.at < self._dispatched_at.get(k, 0.0)):
                tot += peak
                continue
            if self._download_done(k, u):
                continue
            tot += max(0.0, peak - u.get("bytes", 0) / 1024 ** 3)
        if self.hosts is not None:
            tot += sum(big for o in self.hosts.orphans.values() if o.get("option") == "D" and o["host"] == host)
        return tot

    def _download_done(self, k: str, u: dict) -> bool:
        """This attempt's sandbox has no spool up and its gate logged a whole-file transfer the size of the layout's spool. A spool that is merely gone proves
        nothing: it also goes between requests and after a failed transfer (final pass S3 #2). S1's small daily files never reach it, so S1 keeps its small
        reserve. The figure is taken in decimal GB, the smaller reading of science's 3.79, at 90%: an underestimate here only keeps a reserve longer."""
        spool = float(self.cfg.d_spool_gib.get(self._layout_of.get(k, "state_b"), max(self.cfg.d_spool_gib.values())))
        return u.get("spool_bytes", 0) == 0 and u.get("largest_transfer_bytes", 0) >= 0.9 * spool * 1e9

    def _local_use(self, k: str) -> dict | None:
        """This host's measure of a run's sandbox (diskuse), at most every 10 s per run."""
        now = time.monotonic()
        hit = self._use_cache.get(k)
        if hit is not None and now - hit[0] < 10.0:
            return hit[1]
        from dataset_76.sandbox import reap
        from .diskuse import sandbox_use
        u = sandbox_use(reap.work_root(), k)
        self._use_cache[k] = (now, u)
        return u

    def _d_room_gib(self, host: str) -> float | None:
        """Free space above the host's floor after the spools in flight; None: the host has no floor; nan: its free space is unknown."""
        floor = self._local_disk_floor() if self.hosts is None or host == self.hosts.local else self.hosts.specs[host].disk_min_free_gib
        if floor is None:
            return None
        if self.hosts is None or host == self.hosts.local:
            free = self._local_disk_free_gib()
        else:
            p = self.hosts.probes.get(host)
            free = p.disk_free_gib if p is not None and p.ok else None
        return float("nan") if free is None else free - self._d_reserve_gib(host) - floor

    def _local_disk_floor(self) -> float | None:
        if self.hosts is not None:
            return self.hosts.specs[self.hosts.local].disk_min_free_gib
        return self.cfg.local_disk_min_free_gib

    def _local_disk_free_gib(self) -> float | None:
        """The smaller free space of the campaign root and the docker root (run dirs and sandboxes); None: unreadable (then held)."""
        import shutil
        try:
            return min(shutil.disk_usage(p).free for p in (self.root, Path(os.environ.get("STUDY_VAR_45", "/var/lib/docker")))) / 1024 ** 3
        except OSError:
            return None

    def _local_disk_held(self, now: dt.datetime, mono: float) -> bool:
        """The run host's own disk (alarm runbook, 2 Nov): below its floor, or unreadable, nothing new starts on this host; runs in flight go on.
        Each change is an event, and the state is kept for the status."""
        floor = self._local_disk_floor()
        if floor is None:
            return False
        assert self.store
        free = self._local_disk_free_gib()
        reserve = self._d_reserve_gib(self.hosts.local if self.hosts is not None else "")
        held = free is None or free - reserve < floor            # less what the D transfers in flight here may still spool
        was = getattr(self, "_ld_held", None)
        if held != was or mono - getattr(self, "_ld_at", -1e9) >= 60.0:
            self._ld_at = mono
            self.store.put_meta("local_disk", json.dumps({"held": held, "free_gib": None if free is None else round(free, 2), "floor_gib": floor,
                                                          "checked_utc": iso(now)}), iso(now))
        if held != was:
            self._ld_held = held
            if held or was is not None:
                self.store.event(iso(now), "local_disk_hold" if held else "local_disk_resumed", None,
                                 f"{'unreadable' if free is None else f'{free:.1f} GiB free'} against a floor of {floor:g} GiB: "
                                 + ("nothing new starts on this host (runs in flight go on)" if held else "dispatch here resumes"))
        return held

    def _host_memory_paused(self, now: dt.datetime, mono: float) -> bool:
        """The watchdog (dataset_76.campaign.hostmem): True while the host's available memory is below the pause threshold. Logs each
        transition once and keeps the state where the status process can read it. Holds back new runs only; kills nothing."""
        assert self.store
        w = self.hostmem
        paused, tr = w.check()
        if tr == "pause":
            self._hostmem_pauses += 1
            self._hostmem_paused_since = mono
            self.store.event(iso(now), "host_memory_pause", None,
                             f"available {w.available / 1024 ** 3:.1f} GiB < {w.pause_below / 1024 ** 3:g} GiB: no new run starts (runs in flight are untouched)")
        elif tr == "resume":
            if self._hostmem_paused_since is not None:
                self._hostmem_paused_s += mono - self._hostmem_paused_since
            self._hostmem_paused_since = None
            self.store.event(iso(now), "host_memory_resume", None,
                             f"available {w.available / 1024 ** 3:.1f} GiB >= {w.resume_at / 1024 ** 3:g} GiB: dispatch resumes")
        if w.enabled and (tr or paused or mono - self._hostmem_saved >= 60.0):
            self._hostmem_saved = mono
            cur = self._hostmem_paused_s + (mono - self._hostmem_paused_since if self._hostmem_paused_since is not None else 0.0)
            self.store.put_meta("host_memory", json.dumps({
                "enabled": True, "paused": paused, "available_gib": None if w.available is None else round(w.available / 1024 ** 3, 2),
                "readable": w.available is not None, "pause_below_gib": w.pause_below / 1024 ** 3, "resume_at_gib": w.resume_at / 1024 ** 3,
                "pauses_this_process": self._hostmem_pauses, "paused_s_this_process": round(cur, 1)}), iso(now))
        return paused

    def _choose_slot(self, row: Any, cands: list[str], by_slot: dict[str, int]) -> str | None:
        """Only one backend free: use it. Both free: the backend that keeps this run's
        (model, option, layout) cell closest to the model's overall LLM Lab share; a tie is broken
        by an RNG seeded from the campaign seed, the run and its attempt count. Within HPC, the
        least-loaded slot."""
        hel = [c for c in cands if backend_of(c) == "provider_g"]
        ll = [c for c in cands if backend_of(c) == "provider_i"]
        if not (hel and ll):
            return pick_slot(cands, by_slot)
        assert self.store
        model, k = row["model"], row["run_key"]
        cell = (row["model"], row["option"], row["layout"])
        c_l = c_n = m_l = m_n = 0
        by_run = self.store.backend_slots()
        for k2, (m2, _, sl2) in list(self._inflight.items()):  # dispatched, attempt row maybe not yet written
            by_run[k2] = (*self.store.run_cell(k2), sl2)
        by_run.pop(k, None)  # the run being dispatched is re-assigned
        for m2, o2, l2, sl in by_run.values():
            if m2 != model:
                continue
            is_l = backend_of(sl) == "provider_i"
            m_n += 1
            m_l += is_l
            if (m2, o2, l2) == cell:
                c_n += 1
                c_l += is_l
        target = m_l / m_n if m_n else 0.5
        d_l = abs((c_l + 1) / (c_n + 1) - target)   # assign LLM Lab
        d_h = abs(c_l / (c_n + 1) - target)         # assign HPC
        if abs(d_l - d_h) < 1e-12:
            n_prev = len(self.store.q("SELECT 1 FROM attempts WHERE run_key=?", (k,)))
            use_l = random.Random(f"{self.cfg.rng_seed}:{k}:{n_prev}").random() < 0.5
        else:
            use_l = d_l < d_h
        return pick_slot(ll if use_l else hel, by_slot)

    def _health(self) -> tuple[bool, str]:
        """A health check that raises, or answers something other than (ok, detail), counts as a failed check, never as a crash."""
        try:
            r = self.health_check()
        except Exception as e:  # noqa: BLE001
            return False, f"health check raised {type(e).__name__}: {e}"
        if isinstance(r, (tuple, list)) and len(r) == 2:
            return bool(r[0]), str(r[1])
        return False, f"health check answered {str(r)[:80]!r}, not (ok, detail)"

    def _health_at_gate(self) -> tuple[bool, str, dict | None]:
        """A run's own health check (pre-run or post-run) with the gate's state taken the instant it returns: (ok, detail, the open outage's record or None). The check runs outside
        the gate's lock, since it may take as long as its timeouts. The instant it "returns" is the one at which `_hg_lock` (the lock that closes and opens the gate) is held with
        the answer in hand (science, #34, 04:24Z): the gate is read there, and the run is classified from that read, not from one made after other statements (D7 finding 2). A
        close or a pass in the gap between the check's return and taking the lock falls on the other side of that instant. A run admitted at it (pre-run: gate open) is in flight
        like any other when the gate closes after (D7 finding 1)."""
        ok, detail = self._health()
        with self._hg_lock:
            return ok, detail, self._hg

    # --- the Onedata health gate (healthgate.py) ------------------------------------------------------------
    GATE_MIN_PERIOD_S = 0.01             # a floor under `onedata_pause_s`, so a test's 0 does not spin

    def _load_gate(self) -> None:
        """open(): the outage list, and a gate that was closed when the last process ended is closed again (restarts += 1) and checked at once. A record that cannot be read
        reads as closed too: whether fr is down is then unknown, and the first passing check ends it. Nothing is charged for a restart."""
        assert self.store
        now = iso(self.clock())
        raw = self.store.get_meta(HG.META)
        outs = HG.parse(raw)
        if outs is None:
            self.store.event(now, "onedata_gate_record_unreadable", None, f"{HG.META} cannot be read ({(raw or '')[:120]!r}): the gate is closed until a check passes")
            outs = [{"start_utc": now, "end_utc": None, "how": None, "checks": 0, "restarts": 0, "last_check_utc": None,
                     "first_detail": "the stored outage record was unreadable", "last_detail": "the stored outage record was unreadable"}]
        self._hg_outages = outs
        open_ = next((o for o in reversed(outs) if o.get("end_utc") is None), None)
        if open_ is not None:
            open_["restarts"] = int(open_.get("restarts", 0)) + 1
            self._hg, self._hg_check_now = open_, True
            self.store.put_meta(HG.META, HG.dump(self._hg_outages), now)
            self.store.event(now, "onedata_gate_restored", None, f"the gate was closed (since {open_['start_utc']}) when the last process ended: it stays closed, and the dispatcher "
                                                                f"checks at once; no attempt is charged for the restart")

    def _gate_close(self, run_key: str, detail: str) -> bool:
        """The first failed pre-run check closes the gate and is the one attempt of the outage (True: this run found it). Any other check that fails while it is closed
        returns False: that run is not charged and not recorded."""
        assert self.store
        with self._hg_lock:
            if self._hg is not None:
                return False
            now = iso(self.clock())
            rec = {"start_utc": now, "end_utc": None, "how": None, "checks": 0, "restarts": 0, "last_check_utc": None,
                   "first_detail": detail[:200], "last_detail": detail[:200]}
            self._hg = rec
            self._hg_outages.append(rec)
            self.store.put_meta(HG.META, HG.dump(self._hg_outages), now)
            self.store.event(now, "onedata_gate_closed", None, f"{run_key}: the pre-run health check failed ({detail[:200]}): no run starts on any host until the dispatcher's own check passes "
                                                              f"(every {self.cfg.onedata_pause_s:g} s); this run is charged one infra attempt, no other is")
        self._hg_wake.set()
        return True

    def _gate_note_discard(self, outage: dict, cols: dict) -> None:
        """The outage's record counts the post-run attempts it discarded and their tokens (None where an attempt's usage is unknown: counted apart, never as zero)."""
        assert self.store
        with self._hg_lock:
            outage["discarded"] = int(outage.get("discarded", 0)) + 1
            if cols.get("input_tokens") is None and cols.get("output_tokens") is None:
                outage["discarded_usage_unknown"] = int(outage.get("discarded_usage_unknown", 0)) + 1
            outage["discarded_input_tokens"] = int(outage.get("discarded_input_tokens", 0)) + int(cols.get("input_tokens") or 0)
            outage["discarded_output_tokens"] = int(outage.get("discarded_output_tokens", 0)) + int(cols.get("output_tokens") or 0)
            self.store.put_meta(HG.META, HG.dump(self._hg_outages), iso(self.clock()))

    def _gate_result(self, ok: bool, detail: str) -> None:
        assert self.store
        with self._hg_lock:
            rec = self._hg
            if rec is None:
                return
            now = iso(self.clock())
            rec["checks"] = int(rec.get("checks", 0)) + 1
            rec["last_check_utc"], rec["last_detail"] = now, detail[:200]
            if ok:
                rec["end_utc"], rec["how"] = now, HG.HOW_PASSED
                self._hg = None
            self.store.put_meta(HG.META, HG.dump(self._hg_outages), now)
            if ok:
                survived = f" and survived {rec['restarts']} restart(s)" if rec.get("restarts") else ""
                self.store.event(now, "onedata_gate_opened", None, f"the dispatcher's own health check passed ({detail[:200]}) after {rec['checks']} check(s); "
                                                                   f"the outage began {rec['start_utc']}{survived}: dispatch resumes")
        if ok:
            with self._cv:
                self._cv.notify_all()

    def _timed_health(self, stop: threading.Event) -> tuple[bool, str]:
        """One gate check, outside the dispatch lock, with a timeout: one that does not answer in `onedata_gate_check_timeout_s` reads as down and its late answer is thrown
        away (never a pass); while it is still out no second one starts. A check that raises or answers nonsense is down too (`_health`)."""
        prev = self._hg_call
        if prev is not None and not prev["done"].is_set():
            return False, "the previous health check has still not answered"
        box: dict = {"done": threading.Event()}

        def go() -> None:
            try:
                box["r"] = self._health()
            except BaseException as e:  # noqa: BLE001 - whatever it was, the check did not pass
                box["r"] = (False, f"health check raised {type(e).__name__}")
            finally:
                box["done"].set()
        self._hg_call = box
        threading.Thread(target=go, daemon=True, name="health-gate-check").start()
        limit = time.monotonic() + self.cfg.onedata_gate_check_timeout_s
        while not box["done"].wait(0.02):                    # in slices, so the end of the run is seen at once
            if stop.is_set():
                return False, "the campaign is ending"
            if time.monotonic() >= limit:
                return False, f"the health check did not answer within {self.cfg.onedata_gate_check_timeout_s:g} s: counted as down"
        return box["r"]

    def _gate_poll(self, stop: threading.Event) -> None:
        ok, detail = self._timed_health(stop)
        if not stop.is_set():
            self._gate_result(ok, detail)

    def _gate_loop(self, stop: threading.Event) -> None:
        """While the gate is closed: every `onedata_pause_s` (at once after a restart) the dispatcher runs the health check itself; the first pass opens the gate. Nothing
        here takes the dispatch lock while a check runs. Past the hard stop (nothing may start) it makes no check. An error in one round is an event, never the end of the loop."""
        while not stop.is_set():
            try:
                if self._hg is None:
                    self._hg_wake.wait(1.0)
                    self._hg_wake.clear()
                    continue
                wait = 0.0 if self._hg_check_now else max(self.cfg.onedata_pause_s, self.GATE_MIN_PERIOD_S)
                self._hg_check_now = False
                if stop.wait(wait):
                    break
                if self._hg is not None and not self._hg_idle:
                    self._gate_poll(stop)
            except Exception as e:  # noqa: BLE001
                try:
                    assert self.store
                    self.store.event(iso(self.clock()), "onedata_gate_error", None, f"{type(e).__name__}: {e}"[:300] + "; the gate stays as it was and is tried again")
                except Exception:  # noqa: BLE001
                    pass
                stop.wait(1.0)

    def _start_gate(self) -> None:
        self._hg_wake.clear()
        self._gate_thread = threading.Thread(target=self._gate_loop, args=(self._prober_stop,), daemon=True, name="health-gate")
        self._gate_thread.start()

    # --- one run attempt ---------------------------------------------------
    def _work(self, row: Any, slot: str) -> None:
        try:
            self._work_inner(row, slot)
        except Exception as e:  # noqa: BLE001
            # Unexpected (disk full, DB error): stop dispatching loudly. A 'running' attempt is
            # left as is; the next start turns it into 'interrupted' and re-runs it.
            self._fatal = e
            self.stop.set()
            with self._cv:
                self._cv.notify_all()

    def _work_inner(self, row: Any, slot: str) -> None:
        assert self.store
        k, model = row["run_key"], row["model"]
        try:
            if self._hg is not None:         # the gate closed after this run was picked: nothing is tried, charged or recorded, and the run stays pending
                return
            pre_ok, pre_detail, closed = self._health_at_gate()
            if pre_ok and closed is not None:
                return                       # another run's failed check closed the gate while this run's check was out (30(d)): it passed, but no run starts in the hold. As above: nothing is tried, charged or recorded, and the run stays pending
            if not pre_ok and closed is not None:
                return                       # this failed check returned while the gate was already closed (an outage in progress): it is not charged a second outage's attempt, even if the gate has opened since (D8). As above: nothing is tried, charged or recorded, and the run stays pending
            if not pre_ok and not self._gate_close(k, pre_detail):
                return                       # another run's failed check closed the gate first: that run is the outage's one attempt, this one is not charged
            attempt = self.store.infra_attempts(k) + 1
            run = RunId(row["instance_id"], _opt(row["option"]), _lay(row["layout"]), model, row["seed"])
            n_try = len(self.store.q("SELECT 1 FROM attempts WHERE run_key=?", (k,))) + 1
            run_dir = self.root / "runs" / k / f"attempt{attempt}.try{n_try}"
            run_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(run_dir / "fr_settings_sha256", (self._fr_at_dispatch.get(k) or "") + "\n")   # survives a crash: recovery reads it
            _atomic_write(run_dir / "opw_restart_no", f"{opwrestart.completed(self._opw_entries())}\n")     # restarts finished before this run began (#34, 3 Nov ruling)
            started_at = self.clock()
            counted_from = self._cohort_start(k, started_at)       # a run's FIRST start, or its deferral, sets its cohort (science, #34, 21:33Z; amendment 24)
            _atomic_write(run_dir / "out_of_cohort", "1\n" if self._cohort is not None and counted_from >= self._cohort else "0\n")
            if counted_from != started_at:
                _atomic_write(run_dir / "cohort_start_utc", iso(counted_from) + "\n")
            aid = self.store.begin_attempt(k, attempt, slot, iso(started_at), os.getpid(), str(run_dir))
            if self.hosts is not None:
                self.store.set_usage(aid, exec_host=self._host_of(model))
            # the slot's identity as checked when the attempt starts, kept with the attempt even if it is interrupted
            ident = self.slot_gate.identity(slot) if self.slot_gate is not None and backend_of(slot) == "provider_g" else None
            if ident is not None:
                self.store.set_usage(aid, slot_identity=json.dumps(ident, sort_keys=True))
            mono0 = time.monotonic()
            rec: RunRecord | None = None
            infra: tuple[str, str] | None = None
            outage: dict | None = None          # the open outage record when a failed post-run check is its own (below)
            harness_err: str | None = None
            started = pre_ok
            if not pre_ok:
                # The issue: a failed Onedata check before a run is an infra failure. The run never
                # started, so there is no executor call and no post-run check. This run found the outage
                # (`_gate_close` above): it is charged this one attempt, and the gate holds all dispatch
                # until the dispatcher's own check passes, so an outage does not burn attempts.
                infra = ("onedata_health", f"{PRE_CHECK_FAILED}: {pre_detail}")
            else:
                try:
                    if self._pass_cap and self.hosts is not None and self._pass_conc:
                        rec = self.execute_run(run, attempt, run_dir, slot, self.cfg, provider_cap=self._provider_cap(model, slot),
                                               concurrency_at_start=self._unit_concurrency(model, slot))
                    elif self._pass_cap:
                        rec = self.execute_run(run, attempt, run_dir, slot, self.cfg,
                                               provider_cap=self._provider_cap(model, slot))
                    else:
                        rec = self.execute_run(run, attempt, run_dir, slot, self.cfg)
                except Exception as e:  # noqa: BLE001 - classified below
                    if is_infra_error(e):
                        infra = (e.kind, e.detail)  # type: ignore[attr-defined]
                    else:
                        harness_err = f"{type(e).__name__}: {e}"
            exec_infra = infra  # before the post-run check can override it (LLM Lab accounting)
            wall = time.monotonic() - mono0   # the run's own time: taken BEFORE the post-run health check, which is not part of it
            post: dict[str, Any] = {}
            if started:  # after every started execution, whatever the executor did
                post_ok, post_detail, at_return = self._health_at_gate()      # the gate's state taken with the check's return: the re-check may open it before the run is classified
                post = {"post_health_ok": int(post_ok), "post_health_detail": post_detail}
                if not post_ok and not (infra is not None and (self._is_unstarted(infra) or infra[0] == KEY_REJECTED)):  # outside the agent's control: wins over the executor's own outcome. Not over a refusal: no sandbox started, so no attempt was used, whatever the check says now. Nor over a refused key: that is charged to no run, whatever the check says now
                    outage = at_return       # healthgate.py: the failed check is the outage's if the gate is CLOSED AT THE INSTANT IT RETURNS (made after the gate closed, or in flight as it closed)
                    if outage is not None:
                        infra = (ONEDATA_OUTAGE, f"post-run check failed during the outage that began {outage['start_utc']}: {post_detail}")
                    else:                    # the gate was still open: counted as today (a one-off, or the first sign of an outage the next pre-run check will close the gate on)
                        infra = ("onedata_health", f"post-run check failed: {post_detail}")
                elif not post_ok:        # the refusal stands; the failed check is recorded beside it (post_health_ok, post_health_detail) and as an event
                    self.store.event(iso(self.clock()), "post_run_check_failed", model, f"{k}: {post_detail}; the {infra[0] if infra else 'executor'} outcome stands and no attempt is charged for the check")
            self._note_endpoint(model, slot, exec_infra, rec is not None, k)
            self._crash_hook("before_marker", k)
            ended = iso(self.clock())
            moved = self._check_run_identity(slot, k, model, rec, ended, ident)
            lost = self._opw_overlap(iso(started_at), ended) if started else None
            if lost and infra is None and harness_err is None:
                infra = ("op_worker_restart", lost)          # in flight when op_worker stopped: an infrastructure failure in every option alike, retried
                self.store.event(ended, "opw_restart_lost_run", model, f"{k}: {lost}")
            elif lost and infra is not None:
                self.store.event(ended, "opw_restart_lost_run", model, f"{k}: {lost} (already classified {infra[0]})")
            if moved and infra is None and harness_err is None:      # the slot changed under the run: not this model's outcome, so retried (the lead, 2 Nov)
                infra = ("slot_identity", moved)
            cols: dict[str, Any] = dict(post)
            usage = _usage(rec) if rec is not None else _usage_from_file(run_dir / "run_record.json")
            saved_wall = usage.pop("wall_time_s", None)
            if saved_wall is not None:
                wall = float(saved_wall)  # the record's own wall time on every path: the scheduler's elapsed time also holds the Onedata slot wait and validation
            cols.update(usage)  # also for an infra-failed run that wrote a record before failing
            canary_not_started = not started and k == self._canary_run
            # the executor's own failure says whether the host's setup is broken: a failed post-run check may have replaced `infra` since (it is outside the agent's control and wins), and
            # that must neither hide a failure before the agent started from the host's count nor charge it on a host held for it
            setup = self._host_setup_failure(exec_infra, model, k, ended) if exec_infra is not None and started else None     # a failure before the agent started, on a host (hosts.py)
            if self.hosts is not None and started and rec is not None:
                self.hosts.note_past_setup(self._host_of(model))       # a record: the run went through the host's setup, so a kind that tripped the host is cleared (no failure clears it)
            if canary_not_started:
                # The unstarted stop's canary failed its pre-run health check: no executor ran, no sandbox started. Only a proven sandbox start clears the latch, and the registered rule is
                # that a canary which starts none uses no attempt, so it is finalized like a refusal but counted as none: the stop holds, the run stays pending. The health gate
                # (`_gate_close`) holds dispatch for the outage whether or not an attempt is charged for it (at most one per outage).
                self._canary_run = None
                cols.update(infra_kind=infra[0], infra_detail=infra[1])        # type: ignore[index]
                state, status = "unstarted", "pending"
                self._not_before[k] = time.monotonic() + self.cfg.retry_delay_s
            elif infra is not None and self._is_unstarted(infra):
                # no sandbox started (no usable slot user): it used no attempt and bills nothing; repeated for one option, it stops all dispatch
                cols.update(infra_kind="unstarted", infra_detail=infra[1])
                state, status = "unstarted", "pending"
                self._not_before[k] = time.monotonic() + self.cfg.retry_delay_s
                if self._host_refused(infra):       # its HOST could not start it (or may have: see below): that host pauses and alarms, no option's count moves
                    self.hosts.mark_down(self._host_of(model), infra[1])
                else:
                    self._note_unstarted(True, ended, k, row["option"])
            elif infra is not None and infra[0] == KEY_REJECTED:
                # The key was refused (HTTP 401/403): not the agent's, not a score, and charged to no run (store.infra_attempts leaves it out), so the run keeps all
                # its attempts. The unit is held in _note_endpoint (below), so no run of this model starts on this backend until a probe run is accepted.
                self._note_unstarted(False, ended, k, row["option"])
                cols.update(infra_kind=infra[0], infra_detail=infra[1])
                state, status = "infra_fail", "pending"
                self._not_before[k] = time.monotonic() + self.cfg.retry_delay_s
            elif infra is not None and infra[0] == ONEDATA_OUTAGE:
                # The post-run check failed while the gate was closed (or in flight as it closed): the run is retried and this attempt is kept with its tokens, as every discarded
                # attempt is, but charged to nobody (store.infra_attempts leaves the kind out), so the run keeps all of its attempts. The outage's record counts it.
                self._note_unstarted(False, ended, k, row["option"])      # the sandbox did start
                cols.update(infra_kind=infra[0], infra_detail=infra[1])
                state, status = "infra_fail", "pending"
                self._not_before[k] = time.monotonic() + self.cfg.retry_delay_s
            elif infra is not None and setup == "held":
                # this failure came before the agent started, on a host that is already held for failures of its kind (an outage of the host's own setup in progress): it is the host's, not
                # this run's, so it is not charged a second outage's attempt; nothing is tried or charged, and the run stays pending (as for the gate: D8). The attempt is kept, as a refusal is
                cols.update(infra_kind=exec_infra[0], infra_detail=exec_infra[1])        # type: ignore[index]  # the executor's failure, whatever the post-run check said
                state, status = "unstarted", "pending"
                self._not_before[k] = time.monotonic() + self.cfg.retry_delay_s
                self.store.event(ended, "host_setup_failure_uncharged", model, f"{k}: {exec_infra[0]}: {exec_infra[1][:200]} on {self._host_of(model)}, which is held for it: no attempt is charged")  # type: ignore[index]
            elif infra is not None:
                if started:                  # only a proven sandbox start clears an option's refusal count or ends the stop: a failed pre-run check started none
                    self._note_unstarted(False, ended, k, row["option"])
                cols.update(infra_kind=infra[0], infra_detail=infra[1])
                exhausted = attempt >= self.cfg.max_attempts
                state, status = "infra_fail", "infra_exhausted" if exhausted else "pending"
                if not exhausted:
                    self._not_before[k] = time.monotonic() + self.cfg.retry_delay_s
            elif harness_err is not None:
                # Not infra by the rule, not an agent outcome: a harness bug. Loud: counted under its kind, and the run goes back to pending when the campaign next opens
                # (after the fix), from the same row and seed. Three of one kind stop all dispatch.
                cols.update(infra_detail=harness_err)
                state, status = "harness_error", "harness_error"
                self._note_harness_error(ended, k, harness_error_kind(harness_err), model, row["option"])
            else:
                self._note_unstarted(False, ended, k, row["option"])
                state, status = "done", "done"
            sl = getattr(self.execute_run, "still_live", None)
            if (exec_infra is not None and (self._host_lost(exec_infra) or self._host_uncertain(exec_infra))) or (callable(sl) and sl(run_dir)):
                # by the EXECUTOR's outcome, whatever the post-run check made of it: the run may still execute on its host (lost mid-run, or a
                # start whose answer never came), so it stays counted and is not dispatched again until that host shows it gone
                h = self._host_of(model)
                if exec_infra is not None and self._worker_died(exec_infra):
                    self.hosts.note_worker_death(h, exec_infra[1])       # one run's failure: the host answered, and its other runs go on (the lead, 2 Nov)
                elif exec_infra is not None and (self._host_lost(exec_infra) or self._host_uncertain(exec_infra)):
                    self.hosts.mark_down(h, exec_infra[1])
                if not usage:                  # no record reached here: what it billed is unknown, never zero (lane-dress, 2 Nov). Read when the orphan ends (_orphan_ended)
                    cols["usage_complete"] = 0
                self.hosts.add_orphan(f"{k}#{aid}", {"run_key": k, "model": model, "host": h, "run_dir": str(run_dir), "slot": slot, "option": row["option"],
                                                     "group": self._group_name.get(k, ""), "why": exec_infra[1] if exec_infra else "teardown not proven on its host"})
            self._check_served(model, k, rec, ended)         # BEFORE the attempt is finalized: a crash between the two replays the run, never skips the stop
            self._note_provider_i_fingerprint(slot, model, k, rec, ended)   # recorded and alarmed, never a stop
            self._finalize(aid, row, attempt, run_dir, state, status, ended, wall, cols, infra, rec)
            if infra is not None and infra[0] == ONEDATA_OUTAGE and outage is not None:
                self._gate_note_discard(outage, cols)
            if state == "harness_error":
                self.store.event(ended, "harness_error", model, f"{k}: {harness_err}")
        finally:
            with self._cv:
                self._inflight.pop(k, None)
                self._cv.notify_all()

    def _opw_entries(self) -> list:
        """The restart log's entries (re-read when the file changes); [] when there is none or it cannot be read: the settings check owns that alarm."""
        from dataset_76 import opwrestart
        path = self.cfg.fr_restart_log
        if not path:
            return []
        try:
            st = os.stat(path)
            m = (st.st_mtime_ns, st.st_size)
            if self._opw_cache[0] != m:
                self._opw_cache = (m, opwrestart.read_log(path))
        except (OSError, ValueError):
            self._opw_cache = (None, [])
        return self._opw_cache[1]

    def _opw_overlap(self, start_utc: str, end_utc: str) -> str | None:
        """Why a run from start to end was lost to an op_worker restart (its window overlapped the time between the stop and the end of a restart), or None."""
        hit = opwrestart.overlapping(self._opw_entries(), start_utc, end_utc)
        if not hit:
            return None
        return "op_worker restart " + ", ".join(f"#{w['no']} on {w['host']} (stop {w['stop_utc']}, end {w['end_utc'] or 'not yet'})" for w in hit) + " during the run"

    def _extend_pause(self, pause_s: float) -> None:
        """Onedata dispatch pause: the deadline is computed and applied under the dispatch lock and only
        ever moves later, so a slower concurrent worker cannot shorten a newer pause."""
        with self._cv:
            self._pause_until = max(self._pause_until, time.monotonic() + pause_s)

    def _load_units(self) -> None:
        """Restore endpoint health after a restart, so an outage window is not closed and a pause not
        forgotten without a success (the pause end is stored as UTC and re-based on the monotonic clock)."""
        assert self.store
        now, mono = self.clock(), time.monotonic()
        known = {*self.cfg.open_models, *self.cfg.api_models}
        for w in self.store.q("SELECT model, parent FROM endpoint_windows WHERE up_utc IS NULL"):
            if w["model"] not in known:   # an open slot/unit window survives the restart under its identity
                self._unit_seen.add(w["model"])
                if w["parent"]:
                    self._unit_parent.setdefault(w["model"], w["parent"])
        try:
            self._key_held = {u: {**r, "since_mono": 0.0} for u, r in json.loads(self.store.get_meta("key_rejected") or "{}").items()}
        except (ValueError, AttributeError, TypeError):
            self._key_held = {}
            self.store.event(iso(now), "key_rejected_record_unreadable", None, "the stored key rejections are unreadable: the units stay down by their own record and are probed")
        for r in self.store.load_units():
            self._unit_fails[r["unit"]] = r["fails"]
            if not r["unit"].startswith("anthropic:"):
                self._unit_seen.add(r["unit"])
                if r["model"]:
                    self._unit_parent[r["unit"]] = r["model"]
            if r["down"]:
                self._unit_down.add(r["unit"])
            if r["pause_until_utc"]:
                left = (dt.datetime.fromisoformat(r["pause_until_utc"]) - now).total_seconds()
                if left > 0:
                    self._unit_pause[r["unit"]] = mono + left

    def _save_unit(self, unit: str, model: str) -> None:
        assert self.store
        left = self._unit_pause.get(unit, 0) - time.monotonic()
        until = iso(self.clock() + dt.timedelta(seconds=left)) if left > 0 else None
        self.store.save_unit(unit, self._unit_fails.get(unit, 0), unit in self._unit_down, until, model)

    def _note_endpoint(self, model: str, slot: str, infra: tuple[str, str] | None, ok: bool, run_key: str | None = None) -> None:
        """Endpoint health of the unit this run used. Repeated endpoint failures (5xx, 429, timeouts that
        outlast the transport retries) pause dispatch to that unit only; a success clears it. A refused key holds the unit at once."""
        with self._unit_lock:  # workers finish concurrently: the row written last must be the state last computed
            self._note_endpoint_locked(model, slot, infra, ok, run_key)

    def _hold_key(self, unit: str, model: str, slot: str, detail: str) -> None:
        """The first refusal of the key holds the unit (this model on this backend; for a Claude model, the model): the pause is `key_rejected_pause_s`, renewed by every
        later refusal. The record is kept in the store so a restart keeps the hold; the status alarms and the operator queue names the key's file (never its value)."""
        assert self.store
        now, mono = iso(self.clock()), time.monotonic()
        first = unit not in self._key_held
        rec = self._key_held.setdefault(unit, {"model": model, "backend": backend_of(slot), "since_utc": now, "since_mono": mono, "count": 0,
                                               "key_path": self._key_path(slot)})
        rec["count"] += 1
        rec["last_utc"], rec["detail"] = now, detail[:200]
        self._unit_down.add(unit)
        self._unit_fails[unit] = 0
        self._unit_pause[unit] = mono + self.cfg.key_rejected_pause_s
        if not unit.startswith("anthropic:"):
            self._unit_seen.add(unit)
            self._unit_parent.setdefault(unit, model)
        self._save_unit(unit, model)
        self._save_key_held(now)
        if first:
            self.store.event(now, "key_rejected", model, f"{unit}: {detail}; no new run of {model} starts on this backend, no attempt is charged; one probe run every "
                                                         f"{self.cfg.key_rejected_pause_s:g} s ends the hold when the key is accepted (key file: {rec['key_path']})")

    def _release_key(self, unit: str) -> None:
        assert self.store
        rec = self._key_held.pop(unit)
        self._save_key_held(iso(self.clock()))
        self.store.event(iso(self.clock()), "key_accepted", rec["model"], f"{unit}: a probe run was accepted; the hold (since {rec['since_utc']}, {rec['count']} refusal(s)) is over")

    def _save_key_held(self, now: str) -> None:
        assert self.store
        self.store.put_meta("key_rejected", json.dumps({u: {k: v for k, v in r.items() if k != "since_mono"} for u, r in self._key_held.items()}, sort_keys=True), now)

    def _key_path(self, slot: str) -> str:
        """The file that holds the key this backend uses (a path, never a value), for the alarm and the operator queue."""
        b = backend_of(slot)
        if b == "provider_g":
            return "(none: a provider_g slot takes no key; see the slot's own server settings)"
        try:
            from dataset_76.harness.config import load_config
            hc = load_config()
            return str(hc.anthropic_key_file if b == "anthropic" else hc.provider_i["key_file"])
        except Exception:  # noqa: BLE001 - the path is for people: its absence must not stop the hold
            return "(the key file could not be looked up)"

    def _note_endpoint_locked(self, model: str, slot: str, infra: tuple[str, str] | None, ok: bool, run_key: str | None = None) -> None:
        unit = self._unit_of(model, slot)
        if infra is not None and infra[0] == KEY_REJECTED:
            self._hold_key(unit, model, slot, infra[1])
            return
        if unit in self._key_held:
            rec = self._key_held[unit]
            probe = run_key is not None and self._dispatched_at.get(run_key, 0.0) >= rec.get("since_mono", 0.0)     # a run that began after the hold, not one already in flight
            if infra is None and ok and probe:
                self._release_key(unit)                 # accepted: falls through to the ordinary success below
            else:
                if probe:                               # a probe that proved nothing (another failure): the next one after the pause
                    self._unit_pause[unit] = time.monotonic() + self.cfg.key_rejected_pause_s
                    self._save_unit(unit, model)
                return
        if infra is not None and infra[0] == "endpoint":
            if not unit.startswith("anthropic:"):
                self._unit_seen.add(unit)
                self._unit_parent.setdefault(unit, model)
            n = self._unit_fails.get(unit, 0) + 1
            self._unit_fails[unit] = n
            if unit in self._unit_down or n >= self.cfg.endpoint_fail_threshold:
                self._unit_pause[unit] = time.monotonic() + self.cfg.endpoint_pause_s
                self._unit_down.add(unit)
                self._unit_fails[unit] = 0
            self._save_unit(unit, model)
        elif infra is None and ok:
            self._unit_fails[unit] = 0
            self._unit_down.discard(unit)
            self._save_unit(unit, model)

    def _crash_hook(self, point: str, run_key: str) -> None:
        """Test hook: STUDY_VAR_36=<point>:<run_key substring> kills this process dead."""
        spec = os.environ.get("STUDY_VAR_36")
        if spec and spec.split(":", 1)[0] == point and spec.split(":", 1)[1] in run_key:
            os._exit(9)

    def _finalize(self, aid: int, row: Any, attempt: int, run_dir: Path, state: str, status: str,
                  ended: str, wall: float, cols: dict[str, Any], infra: tuple[str, str] | None,
                  rec: RunRecord | None) -> None:
        """Marker first (outcome and metrics, durable), then the campaign fields, then the SQLite
        commit. A kill after the marker is reconciled on the next start from the marker; it is never
        re-run. A kill before the marker leaves a run with no recorded outcome: it is re-run."""
        run_dir.mkdir(parents=True, exist_ok=True)
        marker = {"attempt_id": aid, "run_key": row["run_key"], "attempt": attempt, "state": state,
                  "run_status": status, "ended": ended, "wall_s": wall, "cols": cols,
                  "infra": list(infra) if infra else None}
        _atomic_write(run_dir / "attempt_final.json", json.dumps(marker, indent=1))
        self._crash_hook("after_marker", row["run_key"])
        if state != "harness_error":
            self._write_meta(run_dir, row, attempt, infra is not None, infra, rec)
        self._crash_hook("after_meta", row["run_key"])
        self.store.finish_attempt(aid, row["run_key"], state, status, ended, wall, **cols)

    def _reconcile(self) -> None:
        """Called by open() under the lock, before recovery: commit outcomes that were recorded in a
        marker but not yet in SQLite; mark the other stale run dirs as interrupted."""
        assert self.store
        for r in self.store.running_attempts():
            run_dir = Path(r["run_dir"])
            mk = run_dir / "attempt_final.json"
            if mk.exists():
                m = json.loads(mk.read_text())
                infra = tuple(m["infra"]) if m["infra"] else None
                if m["state"] != "harness_error":
                    self._write_meta(run_dir, r, m["attempt"], infra is not None, infra, None)
                self.store.finish_attempt(r["id"], r["run_key"], m["state"], m["run_status"],
                                          m["ended"], m["wall_s"], **m["cols"])
                self.store.event(iso(self.clock()), "reconciled_from_marker", r["model"], r["run_key"])
            elif run_dir.exists():
                _atomic_write(run_dir / "INTERRUPTED", "killed before the outcome was recorded; re-run\n")
                self._stamp_record(run_dir)            # a record written before the crash still gets the hash sidecar's value
                # The execution may have been billed: keep the usage of a record it wrote (cost and
                # bytes totals only; the attempt stays 'interrupted', outside outcome and infra counts).
                u = _usage_from_file(run_dir / "run_record.json")
                u.pop("termination", None); u.pop("wall_time_s", None)
                if u:
                    self.store.set_usage(r["id"], **u)
                else:                          # no record: what it billed is unknown, never a definite zero, on either host (lane-dress, the lead, 2 Nov)
                    self.store.set_usage(r["id"], usage_complete=0)
            else:
                self.store.set_usage(r["id"], usage_complete=0)

    def _stamp_record(self, run_dir: Path) -> None:
        rr = run_dir / "run_record.json"
        h = self._stamped_fr(run_dir)
        try:
            d = json.loads(rr.read_text())
        except (OSError, ValueError):
            return
        if h and not d.get("fr_settings_sha256"):
            d["fr_settings_sha256"] = h
            tmp = run_dir / "run_record.json.tmp"
            tmp.write_text(json.dumps(d, indent=1))
            os.replace(tmp, rr)

    @staticmethod
    def _stamped_fr(run_dir: Path) -> str | None:
        """The fr settings hash written when the attempt was dispatched (None: it was not read, or the attempt predates the stamp)."""
        try:
            t = (run_dir / "fr_settings_sha256").read_text().strip()
        except OSError:
            return None
        return t or None

    @staticmethod
    def _stamped_opw(run_dir: Path) -> int | None:
        """The number of op_worker restarts finished when the attempt was dispatched (None: the attempt predates the stamp)."""
        try:
            return int((run_dir / "opw_restart_no").read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _stamped_out_of_cohort(run_dir: Path) -> bool:
        try:
            return (run_dir / "out_of_cohort").read_text().strip() == "1"
        except OSError:
            return False

    def _write_meta(self, run_dir: Path, row: Any, attempt: int, infra_failure: bool,
                    infra: tuple[str, str] | None, rec: RunRecord | None) -> None:
        """The campaign fields, next to (never inside) the record #8 wrote."""
        if rec is not None:
            rec.attempt, rec.infra_failure, rec.block = attempt, infra_failure, row["block"]
            rec.exclude_from_analysis = bool(row["exclude_from_analysis"])
            rec.out_of_cohort = self._stamped_out_of_cohort(run_dir)
            rec.fr_settings_sha256 = self._stamped_fr(run_dir)
            rec.opw_restart_no = self._stamped_opw(run_dir)
            rec.infra_reason = f"{infra[0]}: {infra[1]}" if infra else None
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = {"run_key": row["run_key"], "block": row["block"], "attempt": attempt,
                "infra_failure": infra_failure, "fr_settings_sha256": self._stamped_fr(run_dir), "opw_restart_no": self._stamped_opw(run_dir),
                "exclude_from_analysis": bool(row["exclude_from_analysis"]), "out_of_cohort": self._stamped_out_of_cohort(run_dir),
                "infra_reason": f"{infra[0]}: {infra[1]}" if infra else None}
        tmp = run_dir / "campaign_attempt.json.tmp"
        tmp.write_text(json.dumps(meta, indent=1))
        os.replace(tmp, run_dir / "campaign_attempt.json")
        # Merge the campaign fields into the record #8 wrote, so readers of run_record.json see them.
        rr = run_dir / "run_record.json"
        if rr.exists():
            d = json.loads(rr.read_text())
            d.update(attempt=attempt, infra_failure=infra_failure, block=row["block"],
                     infra_reason=meta["infra_reason"], fr_settings_sha256=meta["fr_settings_sha256"], opw_restart_no=meta["opw_restart_no"],
                     exclude_from_analysis=bool(row["exclude_from_analysis"]), out_of_cohort=meta["out_of_cohort"])
            tmp = run_dir / "run_record.json.tmp"
            tmp.write_text(json.dumps(d, indent=1))
            os.replace(tmp, rr)

    # --- the loop ----------------------------------------------------------
    def run(self, max_dispatch: int | None = None) -> None:
        """Run until every run in the active blocks is terminal, or stop is set.
        Waits (polling) while models are paused. Joins in-flight runs on exit."""
        assert self.store, "call open() first"
        dispatched = 0
        self._start_prober()
        self._start_gate()
        self._start_live_check()
        try:
            while not self.stop.is_set():
                with self._cv:
                    nxt = self._next_dispatch()
                    if nxt is None:
                        if not self._inflight and not self._any_waiting():
                            break
                        self._cv.wait(timeout=self.cfg.poll_interval_s)
                        continue
                    row, slot = nxt
                    self._inflight[row["run_key"]] = (row["model"], backend_of(slot), slot)
                    self._dispatched_at[row["run_key"]] = time.monotonic()
                    self._use_cache.pop(row["run_key"], None)
                    self._fr_at_dispatch[row["run_key"]] = (self._fr or {}).get("hash")   # fr's settings as they were when this run was sent
                th = threading.Thread(target=self._work, args=(row, slot), daemon=True)
                self._workers.append(th)
                th.start()
                dispatched += 1
                if max_dispatch is not None and dispatched >= max_dispatch:
                    break
        finally:
            for th in self._workers:
                th.join()
            self._workers.clear()
            self._prober_stop.set()
            self._stop_live_check()
        if self._fatal is not None:
            raise RuntimeError(f"campaign stopped by an unexpected error: {self._fatal!r}") from self._fatal

    def _note_m_backlog(self, mono: float) -> None:
        """Science, #34, 16:18Z: with two hosts, the M runs waiting for a slot user on every host, sampled each minute; `m_backlog_state`
        says when that backlog keeps growing over `alarm_m_backlog_window_s`. A host whose pool cannot be read makes the sample unknown."""
        if self.hosts is None or mono - getattr(self, "_mb_at", -1e9) < 60.0:
            return
        self._mb_at = mono
        assert self.store
        from dataset_76.harness import slot_lease as SL
        ld = Path(os.environ.get("STUDY_VAR_63", str(SL.LEASE_DIR)))
        try:
            local: int | None = int((SL.pool_status(ld, SL.SLOT_MAP).get("M") or {}).get("waiting", 0)) if ld.is_dir() else 0
        except Exception:  # noqa: BLE001 - unknown, never zero
            local = None
        by_host = {self.hosts.local: local, **self.hosts.waiting("M")}
        n = None if any(v is None for v in by_host.values()) else sum(by_host.values())   # type: ignore[arg-type]
        try:
            prev = json.loads(self.store.get_meta("m_backlog") or "{}")
        except ValueError:
            prev = {}
        t = time.time()
        w = self.cfg.alarm_m_backlog_window_s
        hist = [x for x in prev.get("history", []) if x[0] >= t - 2 * w] + [[t, n]]
        st = m_backlog_state(hist, w, t)
        self.store.put_meta("m_backlog", json.dumps({"history": hist, "state": st, "now": n, "by_host": by_host, "window_s": w}), iso(self.clock()))
        if st != prev.get("state"):
            self.store.event(iso(self.clock()), "m_backlog", None, f"{st}: {n} M run(s) wait for a slot user now ({by_host}), window {w:g} s")

    def _start_prober(self) -> None:
        """Two hosts: probe each remote host every `host_probe_every_s` in a thread of its own (an ssh call must never sit inside the
        dispatch lock); dispatch reads the last probe, and one older than `host_probe_stale_s` holds that host."""
        self._prober_stop = threading.Event()
        if self._held_back or self._held_back_unknown:
            self._leftover_sweep = threading.Thread(target=self._leftover_loop, daemon=True, name="leftover-sweep")
            self._leftover_sweep.start()
        if self.hosts is None or not any(s.remote for s in self.hosts.specs.values()):
            return
        self.hosts.probe_all()                    # before the first dispatch: a host is never offered unprobed
        assert self.store
        self.store.put_meta("hosts", json.dumps(self.hosts.snapshot(), sort_keys=True), iso(self.clock()))
        def loop():
            while not self._prober_stop.wait(self.cfg.host_probe_every_s):
                try:
                    self.hosts.probe_all()
                    assert self.store
                    self.store.put_meta("hosts", json.dumps(self.hosts.snapshot(), sort_keys=True), iso(self.clock()))
                except Exception as e:  # noqa: BLE001 - a host whose state could not be read is not ok: held at once, not when its probe ages out
                    self.hosts.fail_probes(f"the prober raised {type(e).__name__}: {e}")
                    try:
                        assert self.store
                        self.store.event(iso(self.clock()), "host_prober_error", None, f"{type(e).__name__}: {e}; the remote hosts are held until a good probe")
                    except Exception:  # noqa: BLE001
                        pass
                with self._cv:
                    self._cv.notify_all()
        self._prober = threading.Thread(target=loop, daemon=True, name="host-prober")
        self._prober.start()

    # --- the live check (amendment 25(e); the judgement is in livecheck.py) ----------------------------------------------------
    def _start_live_check(self) -> None:
        """A thread beside the host prober, in this process, on the dispatcher host only: fr's load is shared by both hosts, so one probe serves both. No commands, no thread."""
        if self.cfg.live_check_cmds:
            self._live_stop = threading.Event()
            self._live_thread = threading.Thread(target=self._live_check_loop, daemon=True, name="live-check")
            self._live_thread.start()

    PROBE_STOP_GRACE_S = LC.STOP_GRACE_S

    def _end_probe_process(self, grace_s: float) -> str:
        """Ask the probe process in flight to end (SIGTERM: the probe's own handler stops its sandbox), wait up to `grace_s`, then kill it. Returns what was done."""
        p = self._probe_proc
        if p is None or p.poll() is not None:
            return "none"
        p.terminate()
        try:
            p.wait(grace_s)
            return "terminated"
        except subprocess.TimeoutExpired:
            p.kill()
            return "killed"

    def _probe_process(self, cmd, capture_output=True, text=True, timeout=None, env=None):
        """`subprocess.run` for a probe, except that a timeout or a stop ends it with SIGTERM first (its sandbox is stopped by the probe's own handler), SIGKILL only after the grace."""
        if self._live_stop.is_set():
            raise OSError("the campaign is ending: the probe was not started")
        p = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, env=env)
        self._probe_proc = p
        try:
            if self._live_stop.is_set():                # a stop that landed before the process was visible to it: it ends the probe now, not at its own timeout
                self._end_probe_process(self.PROBE_STOP_GRACE_S)
            try:
                out, err = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                how = self._end_probe_process(self.PROBE_STOP_GRACE_S)
                p.communicate()
                if how == "killed":
                    raise subprocess.TimeoutExpired(cmd, timeout, output=None, stderr=b"killed after the grace: its sandbox may be left")
                raise
        finally:
            self._probe_proc = None
        return subprocess.CompletedProcess(list(cmd), p.returncode, out, err)

    def _stop_live_check(self) -> None:
        """The end of run() and close(): the live-check thread ends, and a probe in flight is asked to end and waited for (at most the grace, then killed), so that no
        probe sandbox outlives the campaign by its own timeout. Idempotent; no thread, nothing to do."""
        self._live_stop.set()
        t = self._live_thread
        if t is None:
            return
        with self._cv:
            self._cv.notify_all()                    # a probe waiting for its place wakes and gives it back
        if t is not threading.current_thread() and t.is_alive():
            self._end_probe_process(self.PROBE_STOP_GRACE_S)
            t.join(self.PROBE_STOP_GRACE_S + 30.0)

    def _live_check_loop(self) -> None:
        every = float(self.cfg.live_check_every_s)
        due = time.monotonic() + every
        while not self._live_stop.wait(max(0.0, due - time.monotonic())):
            t0 = time.monotonic()
            try:
                self._live_check_once(deadline=t0 + every)
            except Exception as e:  # noqa: BLE001 - the guard goes on; the round is on the record
                try:
                    assert self.store
                    self.store.event(iso(self.clock()), "live_check_error", None, f"{type(e).__name__}: {e}"[:300])
                except Exception:  # noqa: BLE001
                    pass
            end = time.monotonic()
            if end > t0 + every and not self._live_stop.is_set():       # T then M took longer than the period: the next round starts at once, and says so
                try:
                    assert self.store
                    self.store.event(iso(self.clock()), "live_check_late", None, f"a round took {end - t0:.0f} s against a period of {every:g} s: the next one starts at once")
                except Exception:  # noqa: BLE001
                    pass
            due = max(t0 + every, end)

    def _live_load(self) -> bool:
        with self._cv:
            return bool(self._inflight) or (self.hosts is not None and self.hosts.n_orphans() > 0)

    def _live_has_room(self) -> bool:
        """Called with `_cv` held: a place for the probe, apart from its own: the runs in flight and the dead holders' sandboxes, plus it, within every cap in flight."""
        held = list(self._inflight) + ([o["run_key"] for o in self.hosts.orphans.values()] if self.hosts is not None else [])
        caps = [c for c in (self._inflight_cap(k) for k in held) if c is not None]       # as _within_every_cap: a calibration run of layout S2 in flight holds the S2 cap
        return not caps or len(held) + len(self._held_back) + self._reserved_n() + 1 <= min(caps)         # the sandboxes of the processes beside the campaign that are up take places too (the probe's own is the +1)

    def _live_take_place(self, deadline: float) -> bool:
        """The probe counts in the total from the moment it asks: dispatch starts nothing over a level that has a probe waiting, and the probe starts when a run's end leaves it
        a place, so the sandboxes up never exceed the cap. False if no place came before `deadline` (the place is given back).
        With `live_check_holds_place` off the probe's sandbox is on a host that is none of the run hosts: it takes no place, so it counts in no cap, level or host hold."""
        if not self.cfg.live_check_holds_place:
            return True
        with self._cv:
            self._probe_places = 1
            if self._cv.wait_for(lambda: self._live_stop.is_set() or self._live_has_room(), timeout=max(0.0, deadline - time.monotonic())) and not self._live_stop.is_set():
                return True
            self._probe_places = 0
            self._cv.notify_all()
            return False

    def _live_give_back_place(self) -> None:
        with self._cv:
            self._probe_places = 0
            self._cv.notify_all()

    def _live_update(self, fn: Callable[[LC.LiveState], tuple[Any, list[tuple[str, str | None, str]]]], now: str | None = None) -> Any:
        """Change the level record (`live_check`) in one transaction that starts from what the store holds NOW, so a step another writer took (a command run from a shell) is never
        overwritten; then take the result as the dispatcher's own view. `fn(state)` changes the state and returns (a result, the events to write with it)."""
        assert self.store
        now = now or iso(self.clock())
        box: dict[str, Any] = {}

        def go(raw: str | None):
            st = LC.LiveState.from_json(raw)
            box["result"], events = fn(st)
            box["st"], box["raw"] = st, st.to_json()
            return box["raw"], events, None
        self.store.update_meta(LC.META, go, now)
        with self._live_lock:
            self._live, self._live_raw = box["st"], box["raw"]
        return box["result"]

    def _caps_sync(self, at_start: bool = False) -> None:
        """Amendment 41(b): the endpoint caps follow the record in the store (`endpoint_caps`, written by `caps-set`), re-read every dispatch round and at start. It is an override
        over the launch file's `provider_i_caps` and `provider_g_slot_caps`: a key it does not name keeps the launch value. A record that does not validate is rejected with an event (once)
        and the caps in force stay. A change is applied to `self.cfg` in place, the object the status thread and the host book hold too, and logged with its old and new values,
        source, who and time. Nothing derived from the caps is cached across rounds (`_provider_g_cap` is recomputed each round), and runs in flight are never touched."""
        assert self.store
        raw = self.store.get_meta(endpoint_caps.META)
        if raw == self._caps_raw:
            return
        self._caps_raw = raw
        now = iso(self.clock())
        rec, problems = endpoint_caps.check(raw, self.cfg.open_models)
        if problems:
            self.store.event(now, "endpoint_caps_rejected", None, f"the record in the store is not applied, the previous caps stay in force ({'; '.join(problems)}): {(raw or '')[:300]}")
            return
        if rec is None and self._caps_launch is None:
            return                                   # no record, and none was ever applied: the launch values are in force
        if self._caps_launch is None:
            self._caps_launch = (dict(self.cfg.provider_i_caps), dict(self.cfg.provider_g_slot_caps))
        old = (self.cfg.provider_i_caps, self.cfg.provider_g_slot_caps)
        new = endpoint_caps.effective(*self._caps_launch, rec)
        self.cfg.provider_i_caps, self.cfg.provider_g_slot_caps = new
        moved = endpoint_caps.diff(old, new)
        self.store.event(now, "endpoint_caps_changed", None,
                         ("at start: " if at_start else "") + ("; ".join(moved) if moved else "no value changed") + "; "
                         + (endpoint_caps.describe(rec) if rec else "the record was removed: launch values") + f"; applied_utc: {now}")

    def _live_sync(self) -> None:
        """The dispatcher honours a change made outside it (the level-down command, run from a shell against this campaign's root) without a restart: the record in the store is
        re-read at every dispatch round, and a different one is the view from then on."""
        assert self.store
        raw = self.store.get_meta(LC.META)
        if raw == self._live_raw:
            return
        st = LC.LiveState.from_json(raw)           # more steps than the ladder has: the cap is the ladder's lowest (`lowered_cap` clamps)
        with self._live_lock:
            changed = st.steps != self._live.steps
            self._live, self._live_raw = st, raw
        if changed:
            with self._cv:
                self._cv.notify_all()

    def _live_skip(self, kind: str, why: str) -> None:
        now = iso(self.clock())

        def fn(st: LC.LiveState):
            st.skips += 1
            st.last_skip = {"utc": now, "kind": kind, "why": why}
            return None, [("live_check_skipped", None, f"{kind}: {why}")]
        self._live_update(fn, now)

    def _live_check_once(self, deadline: float | None = None) -> None:
        """One round: T's probe, then M's, each through `_fr_run` with a timeout and never under the dispatch lock. Nothing in flight: no probe, and the skip is recorded.
        A probe waits for its place in the level (it counts in the total while it waits and while it is up) for at most the rest of the period."""
        assert self.store
        cmds = self.cfg.live_check_cmds or {}
        deadline = time.monotonic() + self.cfg.live_check_every_s if deadline is None else deadline
        lowered_now = False
        for shape in (s for s in LC.SHAPES if cmds.get(s)):
            if self.stop.is_set() or self._live_stop.is_set():
                return
            if not self._live_load():
                self._live_skip("no_load", f"{shape}: nothing is in flight (between blocks, or a block ended): no load means no probe")
                return
            t_ask = time.monotonic()
            if not self._live_take_place(deadline):
                if self._live_stop.is_set():
                    return
                self._live_skip("no_place", f"{shape}: the level stayed full for {time.monotonic() - t_ask:.0f} s, so there was no place for the probe's sandbox")
                return
            waited = time.monotonic() - t_ask
            env = {**os.environ, "STUDY_VAR_95": self.cfg.live_check_slot_keys[shape], "STUDY_VAR_106": LC.TOKEN_SOURCE}
            try:
                probe = LC.run_probe(shape, cmds[shape], env, self.cfg.live_check_timeout_s, self.cfg.live_check_limit, run=self._live_run or self._probe_process,
                                     files=(self.cfg.live_check_probe_files or {}).get(shape))
            finally:
                self._live_give_back_place()
            if self._live_stop.is_set():          # the campaign is ending: a probe it ended is no reading and no unknown to count; the sandbox went with the SIGTERM
                self.store.event(iso(self.clock()), "live_check_interrupted", None, f"{shape}: the probe was ended with the campaign ({probe['why'] or 'it finished as the campaign stopped'}); no reading recorded")
                return
            lowered_now = self._live_record(shape, probe, lowered_now, waited) or lowered_now

    def _live_record(self, shape: str, probe: dict, lowered_now: bool, waited_s: float = 0.0) -> bool:
        """Fold a probe into the state and persist it with its events in one transaction. True if this probe lowered the level (`livecheck.record_probe`: one step at most per round)."""
        now = iso(self.clock())                    # one instant for the change in the state and for its event
        return self._live_update(lambda st: LC.record_probe(st, self.cfg, shape, probe, now, lowered_now, waited_s), now)

    def _live_raise(self, now: dt.datetime) -> None:
        """Every dispatch round: the record is re-read (a change made from a shell binds at once), then the rise rule is checked, at most every `level_rise_check_every_s`.
        The rule (amendment 29(f), `livecheck.rise_decision`) is the only way up: one step per rise, never above the configured cap (nothing to raise at step 0), and the clock
        restarts at every change of level. A hold or an unknown is recorded (state and why, in the record and once as an event) and shown by the status."""
        self._live_sync()
        st = self._live
        if st.steps <= 0 or not st.changes:
            return
        mono = time.monotonic()
        if mono - self._rise_at < self.cfg.level_rise_check_every_s:
            return
        self._rise_at = mono
        cfg, task_s = self.cfg, now.timestamp()
        since = release.epoch_of(st.changes[-1]["utc"])
        if task_s - since < cfg.level_rise_after_s:
            dec = LC.rise_decision(now=task_s, since=since, history=[], limit=cfg.live_check_limit, memory=(None, ""), errors=(None, ""), window_s=cfg.level_rise_after_s)
        else:
            t0 = task_s - cfg.level_rise_after_s
            memory = LC.read_memory(cfg.level_memory_file, cfg.level_memory_field, cfg.level_memory_time_field, cfg.level_memory_id_field, t0, task_s, cfg.level_memory_gap_s)
            errors = (self._rise_errors or (lambda a, b: LC.attempt_errors(self.store, a, b, self._attempt_errors_cache)))(t0, task_s)
            dec = LC.rise_decision(now=task_s, since=since, history=st.history, limit=cfg.live_check_limit, memory=memory, errors=errors, window_s=cfg.level_rise_after_s)
        if dec["state"] == "waiting":
            return
        stamp, key = iso(now), (st.changes[-1]["utc"], dec["state"], dec["why"])
        if dec["state"] == "rise":
            last_change = st.changes[-1]["utc"]

            def fn(s: LC.LiveState):
                if not s.changes or s.changes[-1]["utc"] != last_change or s.steps <= 0:     # another writer changed the level since: the clock restarted, this reading is stale
                    return False, []
                return LC.raise_one(s, cfg, dec["why"], stamp, dec["inputs"])
            self._live_update(fn, stamp)
            self._rise_noted = None
            return
        if key != self._rise_noted:
            self._rise_noted = key

            def note(s: LC.LiveState):
                s.rise = {"state": dec["state"], "why": dec["why"], "utc": stamp, "inputs": dec["inputs"]}
                return None, [("level_rise_held", None, f"{dec['state']}: {dec['why']}")]
            self._live_update(note, stamp)

    def _fincore(self, mono: float, now: dt.datetime) -> None:
        """The hourly passive signal (config.py). It alarms through the status, and stops nothing."""
        if not self.cfg.fincore_cmd or (self._fc_at is not None and mono - self._fc_at < self.cfg.fincore_every_s):
            return
        assert self.store
        from dataset_76 import provider_l
        self._fc_at = mono
        try:
            st = json.loads(self.store.get_meta("fincore") or "{}")
        except ValueError:
            st = {}
        r = provider_l.read_warm_share(self.cfg.fincore_cmd, timeout_s=self.cfg.fr_settings_timeout_s, run=self._fr_run)
        was = st.get("state")
        st.update(checked_utc=iso(now), last=r["warm_share"], why=r["why"])
        if r["state"] != "ok":
            st["state"] = "unknown"
        else:
            st.setdefault("reference", r["warm_share"])
            st["low_streak"] = st.get("low_streak", 0) + 1 if r["warm_share"] < st["reference"] * (1 - self.cfg.fincore_drop) else 0
            st["state"] = "low" if st["low_streak"] >= self.cfg.fincore_sustained else "ok"
        self.store.put_meta("fincore", json.dumps(st), iso(now))
        if st["state"] != was:
            self.store.event(iso(now), "fincore", None, f"{st['state']}: warm share {st['last']}, first reading {st.get('reference')}{' (' + st['why'] + ')' if st['why'] else ''}")

    def _fr_memory(self, mono: float, now: dt.datetime) -> None:
        """fr's memory, from the provider monitor's JSONL on this host, every `fr_memory_every_s` (science, #34, 23:57Z). It keeps one bounded record in the store (the
        judgement, the unknown streak, the samples for op_worker's growth) and the status alarms from it. An ALARM ONLY: nothing in dispatch reads the record, and no failure
        here may reach `_fatal`, which would stop the campaign, so every exception is caught and logged."""
        cfg = self.cfg
        if not (cfg.fr_memory_file or cfg.fr_memory_required) or (self._frm_at is not None and mono - self._frm_at < cfg.fr_memory_every_s):
            return
        self._frm_at = mono
        try:
            assert self.store
            from .progress import read_fr_memory_file, update_fr_memory
            try:
                prev = json.loads(self.store.get_meta("fr_memory") or "null")
            except ValueError:
                prev = None
            raw, why = read_fr_memory_file(cfg.fr_memory_file) if cfg.fr_memory_file else (None, "no fr_memory_file is set in the launch config: the monitor's JSONL is not read")
            rec = update_fr_memory(prev, raw, why, now.timestamp(), sample_every_s=cfg.fr_memory_sample_every_s, window_h=cfg.fr_memory_growth_window_h, max_age_s=cfg.fr_memory_max_age_s)
            self.store.put_meta("fr_memory", json.dumps(rec, allow_nan=False), iso(now))
            was = prev.get("state") if isinstance(prev, dict) else None
            r, o = rec["reading"] or {}, (rec["reading"] or {}).get("opw") or {}
            if rec["state"] != was:
                self.store.event(iso(now), "fr_memory", None, f"{rec['state']}: op_worker resident {o.get('rss_mib')} MiB, working set {o.get('ws_pct_node')}% of the node, node {(r.get('node') or {}).get('mem_pct')}%"
                                 + (f" ({rec['why']})" if rec["why"] else "") + "; an alarm only, dispatch goes on")
            ids = lambda x: [s[2] for s in (x.get("samples") if isinstance(x, dict) and isinstance(x.get("samples"), list) else []) if isinstance(s, (list, tuple)) and len(s) == 3]  # noqa: E731
            was_id, now_id = (ids(prev) or [None])[-1], (ids(rec) or [None])[-1]
            if was_id and now_id and was_id != now_id:                 # op_worker restarted (a new sample is kept at once when the id changes): the growth figure starts afresh there
                self.store.event(iso(now), "fr_opw_restart", None, f"op_worker's process id changed from {was_id} to {now_id}: a restart, not a leak; the growth figure starts there")
        except Exception as e:  # noqa: BLE001 - see above
            try:
                assert self.store
                self.store.event(iso(now), "fr_memory_check_error", None, f"{type(e).__name__}: {e}"[:300] + "; dispatch goes on")
            except Exception:  # noqa: BLE001
                pass

    def _past_hard_stop(self, now: dt.datetime) -> bool:
        """Nothing new starts after the hard stop, until the operator says so, once (science, #2, 2 Nov 11:54Z). Runs in flight finish: it stops them from nothing."""
        if self._hard is None or now < self._hard:
            return False
        assert self.store
        if self.store.get_meta("hard_stop_released"):
            return False
        said = release.take(self.cfg.hard_stop_release_file, self._hard.timestamp())
        if said:
            self.store.put_meta("hard_stop_released", said, iso(now))
            self.store.event(iso(now), "hard_stop_released", None, f"by {said}")
            return False
        if not self._hard_logged:
            self._hard_logged = True
            self.store.event(iso(now), "hard_stop", None, f"the hard stop {iso(self._hard)} has passed: dispatch is STOPPED; {len(self._pending())} run(s) stay "
                                                          f"unfinished, {len(self._inflight)} in flight finish")
        return True

    def _any_waiting(self) -> bool:
        """Pending runs exist in the active blocks (paused, backing off, capped, or stopped until someone looks)."""
        assert self.store
        return bool(self._pending())


def m_backlog_state(hist: list, window_s: float, now: float) -> str:
    """`growing`: the count now is above its value at the window's start and was never below that start inside the window; `unknown`:
    a sample in the window could not be read; `short`: the history does not cover the window yet; else `ok`."""
    inside = [(t, n) for t, n in hist if t >= now - window_s]
    before = [(t, n) for t, n in hist if t < now - window_s]
    if any(n is None for _, n in inside):
        return "unknown"
    if not before or not inside or before[-1][1] is None:
        return "short"
    start = before[-1][1]
    return "growing" if inside[-1][1] > start and min(n for _, n in inside) >= start else "ok"


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    """Durable and atomic: fsync the temp file, rename, fsync the directory (and its ancestors, which hold the entries of newly created directories)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    for d in list(path.parents)[:6]:  # the new directories' entries, up through runs/<key>/ and runs/
        _fsync_dir(d)


def _usage_dict(d: dict[str, Any]) -> dict[str, Any]:
    turns = d.get("turns") or []
    seen = (d.get("extras") or {}).get("usage_seen")
    # A record with no turn is not a zero when the adapter saw replies: a harness error in the agent loop leaves `turns` empty but not the billed requests, which the
    # adapter counts as they arrive (what infra.json's fallback below also reads). No turn and no reply seen is a true zero.
    from_seen = not turns and isinstance(seen, dict) and bool(seen.get("responses"))
    return dict(
        termination=d.get("termination"),
        input_tokens=int(seen.get("input") or 0) if from_seen else sum(t.get("input_tokens", 0) for t in turns),
        output_tokens=int(seen.get("output") or 0) if from_seen else sum(t.get("output_tokens", 0) for t in turns),
        cache_creation_tokens=sum(t.get("cache_creation_input_tokens", 0) or 0 for t in turns),
        cache_read_tokens=sum(t.get("cache_read_input_tokens", 0) or 0 for t in turns),
        bytes_into_context=sum(t.get("bytes_into_context", 0) for t in turns),
        bytes_moved=(d.get("extras") or {}).get("bytes_between_sites"),       # None = not measured (never 0)
        usage_complete=0 if d.get("usage_complete") is False else 1,           # False: a request was abandoned or a reply had no usage
        lease_wait_s=(d.get("extras") or {}).get("onedata_slot_wait_s"),        # the Onedata slot wait: in neither the wall time nor the time cap
        container_death_cause=_death_cause(d.get("sandbox")),                    # a gate or sidecar death of the attempt, by cause (amendment 24): also the agent's own, which is no failure
        wall_time_s=d.get("wall_time_s"))


def _death_cause(sb_meta: Any) -> str | None:
    """The one cause of the gate or sidecar deaths in a run's sandbox record or infra.json, None when none died."""
    from dataset_76.sandbox.infradeath import attempt_cause
    return attempt_cause((sb_meta or {}).get("container_deaths")) if isinstance(sb_meta, dict) else None


def _usage(rec: RunRecord) -> dict[str, Any]:
    return _usage_dict(json.loads(rec.to_json()))   # the record's own measured wall time stands, on every return path


def _usage_from_file(path: Path) -> dict[str, Any]:
    """Usage from a run record the executor wrote before failing (none if absent or unreadable)."""
    try:
        return _usage_dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass
    try:                                            # no run record: an infrastructure failure leaves infra.json with the usage seen so far
        d = json.loads((path.parent / "infra.json").read_text())
    except (OSError, ValueError):
        return {}
    cause = _death_cause(d)
    seen = d.get("usage_seen") or {}
    if not isinstance(seen, dict) or "input" not in seen:
        return {"container_death_cause": cause} if cause else {}
    return dict(termination=None, input_tokens=int(seen.get("input") or 0), output_tokens=int(seen.get("output") or 0),
                cache_creation_tokens=0, cache_read_tokens=0, bytes_into_context=0, bytes_moved=None,
                usage_complete=1 if d.get("usage_complete") else 0, lease_wait_s=None, wall_time_s=None, container_death_cause=cause)


def _opt(v: str):
    from dataset_76.records import Option
    return Option(v)


def _lay(v: str):
    from dataset_76.records import Layout
    return Layout(v)

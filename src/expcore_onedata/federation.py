# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/federation.py, blob 8195d16935d767970798b6c4ceb4b420f3bd633d
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Data targets and per-run tokens on the federation_1 federation.

Tokens: one per run, time-limited, `data.readonly`, `data.path` = the target space, and scoped to the one interface
the run uses. They reach only the gate-side client (T, condition_a, D) or the mount sidecar (M, M+). Never printed,
logged or recorded. Admin credentials stay on the host, read from `/home/ubuntu/.secrets/dataset-80/onedata.pass`
through day0's `odpw`.
"""
from __future__ import annotations

import base64
import dataclasses
import importlib
import sys
from pathlib import Path

from ..records import Option
from ..sandbox import DataConfig, Limits, SandboxConfig, campaign_data_config
from ..sandbox.config import pin_image

REPO = Path(__file__).resolve().parents[2]
ZONE = "zone.example.org"
condition_e_BYTES, MAIN_S2_BYTES = 4254680853, 3785412176      # #3: listed by fr's attributes


@dataclasses.dataclass(frozen=True)
class DataTarget:
    """A space holding the study data, and the provider the options read it through."""
    name: str
    space_name: str
    space_id: str
    provider: str           # day0 name of the provider ("fr", "de", "cloud")
    provider_host: str
    s1_path: str = "dataset_15"
    s2_path: str = "s2z/dataset-2.nc"
    s1_bytes: int = condition_e_BYTES
    s2_bytes: int = MAIN_S2_BYTES


RUN_TIME_CAP_S = 2 * 3600            # a scripted run's own timeout (runner.run_one)
MAIN = DataTarget("main", "study_space_15", "a0000000000000000000000000000003ch0000",
                  "provider_e", "host-5.example.org")


def od_module(changes: Path | str | None = None):
    """day0's Onedata helper with the persistent secret path; `changes` redirects its change log (M+, WAN)."""
    scripts = str(REPO / "day0" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    importlib.import_module("odpw")      # replaces od's password reader by the persistent secret file
    od = importlib.import_module("od")
    if changes is not None:
        od.CHANGES = Path(changes)
    return od


def caveats(space_id: str, interface: str) -> list[dict]:
    if interface not in ("rest", "oneclient"):
        raise ValueError(f"unknown interface {interface!r}")
    return [{"type": "data.readonly"},
            {"type": "data.path", "whitelist": [base64.b64encode(f"/{space_id}".encode()).decode()]},
            {"type": "interface", "interface": interface}]


def mint_run_token(target: DataTarget, interface: str, ttl_s: int) -> str:
    """The run's token, through the lease its runner holds (tokens.py): the slot user's, or admin's when no lease is held."""
    cav = caveats(target.space_id, interface)
    admin = lambda: od_module().mint_token(ttl_s=ttl_s, caveats=cav)  # noqa: E731
    from . import tokens
    lease = tokens.current()
    if lease is not None and lease.slot is not None and target.space_id != MAIN.space_id:
        # a slot token is scoped to the study space (lane-slots' SPACE_ID); another space's data would fail, never fall back to admin
        raise RuntimeError(f"a slot-user token covers the study space only, not {target.space_name}: run M+ and WAN on the admin source")
    return lease.mint(admin, interface, ttl_s, cav) if lease is not None else admin()


def data_config(target: DataTarget, option: Option | str, *, ttl_s: int | None = None,
                oneclient_extra_args: list[str] | None = None, replicate=None, perf_log: bool = False, via: str | None = None) -> DataConfig:
    """The DataConfig of one run, on the configured provider route (`via="traefik"` only for fault calibration). M and M+ hold a oneclient token only; the REST options hold a REST token only."""
    option = Option(option)
    if ttl_s is None:                # the shared rule: the run's time cap plus a margin (explicit values, e.g. WAN's 12 h, stay)
        from ..harness.config import required_token_ttl_s
        ttl_s = required_token_ttl_s(RUN_TIME_CAP_S)
    mount = option in (Option.M, Option.condition_d)
    kw = dict(onezone_host=ZONE, space_name=target.space_name, provider_hosts=[target.provider_host],
              oneclient_provider_host=target.provider_host, s1_path=target.s1_path, s2_path=target.s2_path,
              oneclient_extra_args=list(oneclient_extra_args or []), replicate=replicate)
    if mount:
        return campaign_data_config(oneclient_token=mint_run_token(target, "oneclient", ttl_s), oneclient_perf_log=perf_log, via=via, **kw)
    if perf_log:
        raise ValueError("the oneclient perf log exists for M and M+ only")
    return campaign_data_config(token=mint_run_token(target, "rest", ttl_s), via=via, **kw)


def traced_slot() -> tuple[dict, dict]:
    """(SandboxConfig arguments, what to record) for #9's tracer, through the harness's one builder (content-addressed,
    never replaced under a running sandbox). Fails loudly while the builder or the tracer is not in this tree: a run asked
    to be traced is never run untraced."""
    try:
        from ..harness.real_sandbox import ensure_instruments
    except ImportError as e:
        raise RuntimeError("the tracer's builder (dataset_76.harness.real_sandbox.ensure_instruments, #8) is not in this code; "
                           "a traced run cannot start; use --untraced only for the comparison") from e
    d, module, sha = ensure_instruments()
    return {"instruments_dir": Path(d), "instrument_module": module}, {"instrument_module": module, "instruments_sha256": sha}


def sandbox_limits_kw(sandbox_limits=None) -> dict:
    """The agent sandbox's resource limits as `Limits` keywords, from `config/harness.toml` [sandbox_limits]: the SAME memory
    (the agent's allowance plus the tracer's measured headroom), cpus, pids and open files as every agent run (science ruling
    on #9, 1 Nov), so a scripted run measures the sandbox the agents get. An unset headroom is refused, never defaulted."""
    if sandbox_limits is None:
        from ..harness.config import load_config
        sandbox_limits = load_config().sandbox_limits
    if sandbox_limits.tracer_headroom_bytes is None:
        raise RuntimeError("[sandbox_limits] tracer_headroom_gib is not set in config/harness.toml: the container memory limit is the "
                           "agent's allowance plus the tracer's measured headroom, and a scripted run refuses to start without it")
    return dict(cpus=sandbox_limits.cpus, pids=sandbox_limits.pids, nofile=sandbox_limits.nofile,
                agent_memory_bytes=sandbox_limits.agent_memory_bytes, tracer_headroom_bytes=sandbox_limits.tracer_headroom_bytes)


def sandbox_config(target: DataTarget, image: str, work_root: Path | str | None = None, *,
                   instruments: dict | None = None, sandbox_limits=None) -> SandboxConfig:
    """The campaign's defaults, from the real sizes; the host page cache is not dropped (see the README). `instruments`
    (from `traced_slot`) puts #9's tracer in the instrumentation slot. The resource limits are the agent runs' own
    (`sandbox_limits_kw`); `sandbox_limits` overrides the config file (tests)."""
    kw = {"work_root": Path(work_root)} if work_root else {}
    kw.update(instruments or {})
    # the image is resolved once to its immutable id (#7, PR #54): a tag moved under the run is refused at start
    return SandboxConfig(image=image, pinned_image_id=pin_image(image),
                         limits=Limits.for_dataset(target.s1_bytes, target.s2_bytes, **sandbox_limits_kw(sandbox_limits),
                                                   agent_trace_bytes=Limits.trace_bytes),   # science #34, 18:31Z: the 1 GiB /trace is the agent runs' only; a scripted sandbox keeps its 256 MiB
                         drop_host_caches=False, **kw)

# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/real_sandbox.py, blob 021a144aa05994ecd37086bcab409a40a8615e15
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The one place the real sandbox is configured for a run: used by the oracle driver and by the campaign's factory.

Per run (never per campaign): fresh read-only, space-scoped tokens whose lifetime is `required_token_ttl_s(time cap)`
(the time cap stated in that run's prompt plus a margin; `check_token_ttl` before anything is minted), a `DataConfig`
from `campaign_data_config` (oneclient `--insecure` for the whole campaign, operator decision), and a `SandboxConfig`
with the image pinned by id and limits from the real layout sizes.

Credentials are read at use time and never printed, logged or recorded: the run record gets only a short fingerprint
of each token (the token's sha256, first 16 hex digits), its expiry and its caveat types.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import hashlib
import time
from pathlib import Path
from typing import Any, Callable

from dataset_76.records import Layout, Option

from .config import check_token_ttl, required_token_ttl_s

# the data space on fr (read-only for the study): S1 = s1z/dataset-8.nc, S2 = s2z/dataset-2.nc
ONEZONE_HOST = "zone.example.org"
SPACE_NAME = "study_space_15"
SPACE_ID = "a0000000000000000000000000000003ch0000"
PROVIDER_HOST = "host-5.example.org"           # fr
S1_DIR, S2_FILE = "dataset_15", "s2z/dataset-2.nc"
ADMIN_PASS_FILE = Path("/home/ubuntu/.secrets/dataset-80/onedata.pass")
S1_LOCAL_DIR = Path("/mnt/volume2/dataset-80/s1z")    # the local copy whose file sizes give S1's size
S2_BYTES = 3785412176
# oneclient's read/write perf log: ON in every M (and M+) run from the pilot, never for T, condition_a or D, so M has the same
# outside view that the gate log gives them (science, #9). One value; the sidecar keeps the CSV out of the agent's sight.
M_ONECLIENT_PERF_LOG = True


@dataclasses.dataclass(frozen=True)
class TokenRecord:
    """What a run's record says about a token: never the value."""
    kind: str                  # "rest" | "oneclient"
    fingerprint: str           # sha256(token)[:16]
    valid_until_utc: str
    ttl_s: int
    caveats: tuple[str, ...]
    principal: str = "admin"           # "slot:<key>" for a run's token (the slot user), "admin" for the knowledge plane's own
    user_id: str | None = None         # the Onezone user the token belongs to (a slot user's id)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"caveats": list(self.caveats)}


def mint_token(*, oneclient: bool, time_cap_s: float, ttl_s: int | None = None,
               post: Callable | None = None, pass_file: Path | None = None,
               interface: str | None = None, service: bool = False) -> tuple[str, TokenRecord]:
    """A read-only, space-scoped Onezone access token that outlives the run's time cap plus the margin.

    `ttl_s` defaults to `required_token_ttl_s(time_cap_s)`; whatever is used is checked first (`ConfigError` when too
    short), so a free number can never produce a token that expires mid-run. Fails loudly; never logs a value.
    `post` is the HTTP call and `pass_file` the admin password file (tests inject fakes).

    THIS IS A SERVICE PRINCIPAL (admin): the knowledge plane's (`interface="rest"`) or the health check's (`service=True`), and
    nothing else: it requires one of them. A run's own token is
    `mint_slot_token` (the leased slot user); a run never starts on the admin user, so there is no way to get one here.
    """
    if not (interface or service):
        raise ValueError("mint_token is the services' admin principal (interface=... or service=True); a run's token is minted by "
                         "mint_slot_token as its leased slot user -- there is no admin fallback for a run")
    pass_file = Path(pass_file) if pass_file is not None else ADMIN_PASS_FILE
    ttl = required_token_ttl_s(time_cap_s) if ttl_s is None else int(ttl_s)
    check_token_ttl(ttl, time_cap_s, "run token")
    if not pass_file.is_file():
        raise FileNotFoundError(f"Onedata admin password file missing: {pass_file}")
    pw = pass_file.read_text().strip()
    valid_until = int(time.time()) + ttl
    caveats = [{"type": "time", "validUntil": valid_until},
               {"type": "data.readonly"},
               {"type": "data.path", "whitelist": [base64.b64encode(f"/{SPACE_ID}".encode()).decode()]}]
    if oneclient:
        caveats.append({"type": "interface", "interface": "oneclient"})
    elif interface:                       # e.g. "rest" for the knowledge-plane server's token
        caveats.append({"type": "interface", "interface": interface})
    if post is None:
        import requests
        post = requests.post
    r = post(f"https://{ONEZONE_HOST}/api/v3/onezone/user/tokens/temporary",
             headers={"authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()},
             json={"type": {"accessToken": {}}, "caveats": caveats}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"token mint failed: HTTP {r.status_code}")
    tok = r.json().get("token")
    if not tok:
        raise RuntimeError("token mint returned no token")
    rec = TokenRecord("oneclient" if oneclient else (interface or "rest"), hashlib.sha256(tok.encode()).hexdigest()[:16],
                      dt.datetime.fromtimestamp(valid_until, dt.timezone.utc).isoformat(), ttl,
                      tuple(c["type"] + (f"={c['interface']}" if c["type"] == "interface" else "") for c in caveats))
    return tok, rec


def mint_slot_token(lease, *, time_cap_s: float, ttl_s: int | None = None, client=None,
                    secrets_dir: Path | None = None) -> tuple[str, TokenRecord]:
    """A run's token AS THE LEASED SLOT USER: read-only, scoped to the study space, lifetime from the run's cap
    (`required_token_ttl_s`, checked first), oneclient-interfaced for M. The value is returned for memory only; the
    record has its fingerprint, expiry, caveats and the user id, never the value."""
    from .slot_users import SECRETS_DIR, Client, SlotUserError, slot_user_token, token_caveats
    ttl = required_token_ttl_s(time_cap_s) if ttl_s is None else int(ttl_s)
    check_token_ttl(ttl, time_cap_s, "run token")
    caveats = token_caveats(lease.option, ttl, SPACE_ID)
    try:
        tok = slot_user_token(client or Client(), lease.username, caveats, secrets_dir or SECRETS_DIR)
    except SlotUserError as e:
        raise RuntimeError(f"slot token mint failed for {lease.slot}: {e}") from None
    if not tok:
        raise RuntimeError("slot token mint returned no token")
    valid_until = next(c["validUntil"] for c in caveats if c["type"] == "time")
    oneclient = lease.option in ("M", "condition_b")
    rec = TokenRecord("oneclient" if oneclient else "rest", hashlib.sha256(tok.encode()).hexdigest()[:16],
                      dt.datetime.fromtimestamp(valid_until, dt.timezone.utc).isoformat(), ttl,
                      tuple(c["type"] + (f"={c['interface']}" if c["type"] == "interface" else "") for c in caveats),
                      principal=f"slot:{lease.slot}", user_id=lease.user_id)
    return tok, rec


def data_config(option: Option, time_cap_s: float, *, lease, ttl_s: int | None = None, client=None, secrets_dir: Path | None = None,
                m_perf_log: bool | None = None):
    """The DataConfig of one run with a fresh token of the leased slot user (REST for T, condition_a and D; oneclient for M
    and M+; the factory itself starts no M+), and the TokenRecord of the one minted. `lease` is required and must be of
    the run's own option."""
    if lease is None or lease.option != Option(option).value:
        raise ValueError(f"a lease of option {Option(option).value} is required, got {getattr(lease, 'option', None)}")
    from dataset_76.sandbox import campaign_data_config
    kw = dict(onezone_host=ONEZONE_HOST, space_name=SPACE_NAME, s1_path=S1_DIR, s2_path=S2_FILE,
              provider_hosts=[PROVIDER_HOST], oneclient_provider_host=PROVIDER_HOST)
    if option in (Option.M, Option.condition_d):
        kw["oneclient_perf_log"] = M_ONECLIENT_PERF_LOG if m_perf_log is None else bool(m_perf_log)   # M and M+ only
        kw["oneclient_token"], rec = mint_slot_token(lease, time_cap_s=time_cap_s, ttl_s=ttl_s, client=client, secrets_dir=secrets_dir)
    else:
        kw["token"], rec = mint_slot_token(lease, time_cap_s=time_cap_s, ttl_s=ttl_s, client=client, secrets_dir=secrets_dir)
    return campaign_data_config(**kw), rec


def build_image(sha: str) -> str:
    """Build the sandbox image from this checkout under a tag of our own (never a shared one: other lanes' test
    sessions rebuild those) and return its immutable id."""
    import subprocess
    tag = f"dataset-167:oracle-{sha[:12]}"
    repo = Path(__file__).resolve().parents[2]
    r = subprocess.run(["docker", "build", "-q", "-f", "sandbox/Dockerfile", "-t", tag, "."], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("image build failed: " + r.stderr[-500:])
    iid = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", tag], capture_output=True, text=True, check=True).stdout.strip()
    if not iid.startswith("sha256:"):
        raise RuntimeError("no image id for " + tag)
    return iid


def s1_bytes(local_dir: Path = S1_LOCAL_DIR) -> int:
    return sum(p.stat().st_size for p in Path(local_dir).iterdir())


def instruments_hash(d: Path) -> str:
    """sha256 over the relative paths and bytes of every file of the instruments directory (sorted)."""
    h = hashlib.sha256()
    for p in sorted(Path(d).rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(p.relative_to(d).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def ensure_instruments(builder: Callable | None = None, root: Path | None = None) -> tuple[Path, str, str]:
    """Build #9's tracer directory ONCE into a content-addressed place (never replaced under a running sandbox that mounts it)
    and return (dir, boot module name, sha256). A REQUIRED dependency: if #9 is absent the real factory refuses to run,
    there is never a run without the tracer."""
    import os
    import shutil
    import tempfile
    from dataset_76.paths import DATA_ROOT
    if builder is None:
        try:
            from dataset_76.instruments.sandbox_dir import build_instruments_dir as builder
        except ImportError as e:
            raise RuntimeError("dataset_76.instruments (#9) is required for agent runs and is not available: a run without the "
                               "tracer cannot be analysed, so the real sandbox refuses to start (no silent run)") from e
    root = Path(root) if root is not None else Path(DATA_ROOT) / "sandboxes" / "_instruments"
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="build-", dir=root))
    try:
        d, module = builder(tmp / "i")
        if not (Path(d) / f"{module}.py").is_file():
            raise RuntimeError(f"the instruments directory has no boot module {module}.py")
        h = instruments_hash(d)
        final = root / f"instruments-{h[:16]}"
        if not final.exists():
            try:
                os.replace(d, final)
            except OSError:                       # another process published the same content first: use that one
                if not final.exists():
                    raise
        return final, module, h        # an existing directory is only ever reused, never replaced or rebuilt in place
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sandbox_config(image: str, pinned_image_id: str, *, sandbox_limits, s1_size: int | None = None, s2_size: int = S2_BYTES,
                   instruments: tuple[Path, str, str] | None = None):
    """SandboxConfig on the pinned image, limits from the real layout sizes (a quota that cannot hold a wholesale copy),
    and, when given, #9's tracer mounted (the same directory and module in every option). `sandbox_limits` is
    `HarnessConfig.sandbox_limits`: memory = the agent's allowance + the tracer's measured headroom, cpus, pids; the same in
    every option. An unset headroom is refused here (it is a measurement, never a default)."""
    from dataset_76.sandbox import Limits, SandboxConfig
    if sandbox_limits is None or sandbox_limits.tracer_headroom_bytes is None:
        raise RuntimeError("[sandbox_limits] tracer_headroom_gib is not set in config/harness.toml: the container memory limit is the "
                           "agent's allowance plus the tracer's measured headroom, and the real sandbox refuses to start without it")
    if not image or not pinned_image_id:
        raise RuntimeError("no pinned image: the real sandbox runs only on an image pinned by id")
    kw = {}
    if instruments is not None:
        kw = {"instruments_dir": Path(instruments[0]), "instrument_module": instruments[1]}
    return SandboxConfig(image=image, pinned_image_id=pinned_image_id,
                         limits=Limits.for_dataset(s1_bytes() if s1_size is None else s1_size, s2_size, cpus=sandbox_limits.cpus,
                                                   pids=sandbox_limits.pids, nofile=sandbox_limits.nofile, agent_memory_bytes=sandbox_limits.agent_memory_bytes,
                                                   tracer_headroom_bytes=sandbox_limits.tracer_headroom_bytes), **kw)


class RealSandboxFactory:
    """The harness `SandboxFactory` over the real `dataset_76.sandbox.Sandbox`.

    Built once per campaign with the pinned image and the time cap that the run prompts state
    (`HarnessConfig.limits.time_cap_s`); each call mints that run's own tokens, builds the configs and starts the
    sandbox, then puts the token records (fingerprints, never values) and the pinned image id in the run's metadata.
    The `data_config` dict the runner passes must be empty: the data configuration is built here, and a non-empty
    dict would otherwise be silently ignored.
    """

    def __init__(self, image: str, pinned_image_id: str, time_cap_s: float, *, sandbox_limits, token_ttl_s: int | None = None,
                 s1_size: int | None = None, pool=None, client=None, secrets_dir: Path | None = None,
                 instruments_builder: Callable | None = None, instruments_root: Path | None = None):
        if not pinned_image_id.startswith("sha256:"):
            raise ValueError("pinned_image_id must be an image id (sha256:...), not a tag")
        if sandbox_limits is None or sandbox_limits.tracer_headroom_bytes is None:     # before anything else: no run starts without it
            raise RuntimeError("[sandbox_limits] tracer_headroom_gib is not set: the real sandbox factory refuses to start "
                               "(container memory = the agent's allowance + the tracer's measured headroom)")
        self.sandbox_limits = sandbox_limits
        self.image, self.pinned_image_id, self.time_cap_s = image, pinned_image_id, float(time_cap_s)
        self.token_ttl_s, self.s1_size = token_ttl_s, s1_size
        from .slot_lease import SlotPool
        self.pool, self.client, self.secrets_dir = pool or SlotPool(), client, secrets_dir       # the lease pool and the Onezone client of the slot users
        self._instruments_builder, self._instruments_root = instruments_builder, instruments_root
        self._instruments: tuple[Path, str, str] | None = None
        import threading
        self._lock = threading.Lock()
        self.instruments()        # required, built once: fails here (loudly) if #9 is absent, before any run starts

    def instruments(self) -> tuple[Path, str, str]:
        with self._lock:
            if self._instruments is None:
                self._instruments = ensure_instruments(self._instruments_builder, self._instruments_root)
            return self._instruments

    def __call__(self, run_key: str, option: Option, layout: Layout, data_config_dict: dict | None = None):
        from dataset_76.sandbox import Sandbox
        if Option(option) is Option.condition_d:   # before anything is minted
            raise ValueError("M+ is for scripted measurements (#12), never for agent runs: the factory does not start it")
        if data_config_dict:
            raise ValueError("RealSandboxFactory builds the data configuration itself; pass an empty dict")
        # waits for a free slot user of this option; with all of them in quarantine none may ever come, so the run is refused, using no attempt
        lease = self.pool.acquire(Option(option).value, run_key=run_key, refuse_quarantined=True)
        rec = None
        try:
            dc, rec = data_config(Option(option), self.time_cap_s, lease=lease, ttl_s=self.token_ttl_s, client=self.client, secrets_dir=self.secrets_dir)
            instr = self.instruments()
            cfg = sandbox_config(self.image, self.pinned_image_id, sandbox_limits=self.sandbox_limits, s1_size=self.s1_size, instruments=instr)
            sbx = Sandbox.start(run_key, option, layout, dc, cfg)
            sbx.record(tokens=[rec.as_dict()], token_time_cap_s=self.time_cap_s, pinned_image_id=self.pinned_image_id,
                       instruments_sha256=instr[2], instrument_module=instr[1], onedata_slot_wait_s=lease.wait_s)
        except BaseException as e:
            release_or_quarantine(lease, e, _epoch(rec.valid_until_utc) if rec else None)
            raise
        return bind_lease(sbx, lease, token_valid_until=_epoch(rec.valid_until_utc))


def _epoch(iso: str) -> float:
    return dt.datetime.fromisoformat(iso).timestamp()


def _stop_again(stop) -> bool:
    """Whether a teardown that failed has now finished: the only end of a quarantine."""
    try:
        stop()
    except BaseException:  # noqa: BLE001 -- not yet
        return False
    return True


def release_or_quarantine(lease, exc: BaseException, token_valid_until: float | None) -> None:
    """After a failed start: a sandbox that survived its own failed cleanup (`exc.sandbox`, see Sandbox.start) may still
    use the token, so its slot is quarantined until its teardown is proven; otherwise the slot is released."""
    if getattr(exc, "sandbox", None) is not None:
        lease.quarantine(token_valid_until, proven=lambda: _stop_again(exc.sandbox.stop))
    else:
        lease.release()


def bind_lease(sbx, lease, token_valid_until: float | None = None):
    """The slot is released when the sandbox has stopped, and the sandbox reports it: `sbx.onedata_slot()` -> {slot,
    user_id, start, end, wait_s} for the run record. If stop() raises, the sandbox may still be alive and using the token:
    the slot is then quarantined (`end` set, slot kept) until a later stop() proves it gone, never handed to another run, and tried again meanwhile."""
    inner = sbx.stop

    def stop(*a, **k):
        try:
            r = inner(*a, **k)
        except BaseException:
            lease.quarantine(token_valid_until, proven=lambda: _stop_again(inner))
            raise
        lease.release()
        return r
    sbx.stop = stop
    sbx.onedata_slot = lambda: {**lease.record(), "wait_s": lease.wait_s}
    return sbx


@dataclasses.dataclass
class LeasedToken:
    token: str                    # memory only: never print or record it
    record: TokenRecord
    lease: Any

    def __repr__(self) -> str:
        return f"LeasedToken(slot={self.lease.slot}, fingerprint={self.record.fingerprint})"


@contextlib.contextmanager
def leased_token(option, time_cap_s: float, *, run_key: str | None = None, ttl_s: int | None = None,
                 pool=None, client=None, secrets_dir: Path | None = None, timeout_s: float | None = None):
    """For scripted work (and any caller outside the factory): wait for a free slot user of `option`, mint a token as
    that user (REST for T, condition_a, D; oneclient for M), hold the slot for the body, release on every exit.

        with leased_token("T", time_cap_s, run_key=key) as lt:
            use(lt.token)                      # a secret: not printed, not logged
            row["onedata_slot"] = lt.lease.record() | {"wait_s": lt.lease.wait_s}   # after the block: end is set
    After the block `lt.lease.interval(run_key)` is the RunInterval. M+ has no slot user and raises NoSlotForOption."""
    from .slot_lease import SlotPool
    opt = Option(option).value
    lease = (pool or SlotPool()).acquire(opt, run_key=run_key, timeout_s=timeout_s)
    rec = None
    try:
        tok, rec = mint_slot_token(lease, time_cap_s=time_cap_s, ttl_s=ttl_s, client=client, secrets_dir=secrets_dir)
        yield LeasedToken(tok, rec, lease)
    finally:
        # exclusive attribution is what makes the audit log per run: a sandbox of this run that is not verifiably gone may still use the token
        from dataset_76.sandbox.sandbox import retry_teardown, survives
        if rec is not None and survives(run_key):
            lease.quarantine(_epoch(rec.valid_until_utc), proven=lambda: retry_teardown(run_key))
        else:
            lease.release()

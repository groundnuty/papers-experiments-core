# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/tokens.py, blob 502f83b763e6c4750b2a826866780be1155e3a9e
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The one seam where a run gets its Onedata token (science, #2, 1 Nov 18:17Z): the 24 baselines must run on slot-user tokens,
because a non-owner with `space_read_data` may not see what admin sees, and the audit log must show the slot user per run.

A `TokenSource.lease(run_key, option)` is held for the whole run (a free slot user of the option's pool, for the slot source) and
gives the `Lease` that mints the run's tokens (the REST token or the oneclient token, and M's sidecar token) as that user. The
runner records `r["token"]` = {source, subject, slot, start_utc, end_utc}: the run's interval, to join to the audit log's lines.

The admin source is the default and the only one in this lane. The slot source comes from the slots lane (`feat/8-slot-tokens`):
a module `pkg.mod:factory` named in `STUDY_VAR_106`, whose factory returns a TokenSource with `kind == "slot"`. `baseline`
refuses to start on anything else; `smoke` and `pilot` may use admin.
"""
from __future__ import annotations

import contextlib
import contextvars
import importlib
import os
from typing import Callable, ContextManager, Protocol


class Lease(Protocol):
    slot: str | None            # the slot user's name; None for admin
    subject: str                # the user the tokens are minted as

    def mint(self, mint_admin: Callable[[], str], interface: str, ttl_s: int, caveats: list[dict]) -> str: ...


class TokenSource(Protocol):
    kind: str                   # "admin" | "slot"

    def lease(self, run_key: str, option: str) -> ContextManager[Lease]: ...


class _AdminLease:
    slot = None
    subject = "admin"

    def mint(self, mint_admin, interface, ttl_s, caveats):
        return mint_admin()


class AdminTokens:
    kind = "admin"

    @contextlib.contextmanager
    def lease(self, run_key: str, option: str):
        yield _AdminLease()


_CURRENT: contextvars.ContextVar[Lease | None] = contextvars.ContextVar("dataset_76_scripted_lease", default=None)
_SOURCE: TokenSource | None = None


def source() -> TokenSource:
    """The configured source: `STUDY_VAR_106=pkg.mod:factory`, else admin. Resolved once."""
    global _SOURCE
    if _SOURCE is None:
        spec = os.environ.get("STUDY_VAR_106", "")
        if spec:
            mod, _, fn = spec.partition(":")
            if not mod or not fn:
                raise SystemExit(f"STUDY_VAR_106={spec!r}: expected pkg.mod:factory")
            _SOURCE = getattr(importlib.import_module(mod), fn)()
        else:
            _SOURCE = AdminTokens()
    return _SOURCE


def set_source(s: TokenSource | None) -> None:
    global _SOURCE
    _SOURCE = s


def current() -> Lease | None:
    return _CURRENT.get()


@contextlib.contextmanager
def held(lease: Lease):
    tok = _CURRENT.set(lease)
    try:
        yield lease
    finally:
        _CURRENT.reset(tok)


def require_slot_tokens(command: str) -> None:
    """Baselines do not start on admin tokens."""
    k = getattr(source(), "kind", None)
    if k != "slot":
        raise SystemExit(f"{command}: runs on slot-user tokens only (science, #2, 18:17Z); the token source is {k!r}. "
                         "Set STUDY_VAR_106=pkg.mod:factory from the slots lane (feat/8-slot-tokens).")


def utc() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def leased(run_key: str, option: str, r: dict, body: Callable[[], object], finalize: Callable[[], None],
           on_no_lease: Callable[[], None] | None = None) -> dict:
    """Run `body()` under a lease of the configured source, recording `r["token"]` (the run's interval) and ALWAYS calling
    `finalize()` (which writes the record), whatever happened:
    - a lease that cannot be had, or fails on release after a good run, is an error in the record and `ok` false (a result,
      not a crash of the batch);
    - KeyboardInterrupt and SystemExit finalize the record, then propagate."""
    pending: BaseException | None = None
    lease = None
    try:
        with contextlib.ExitStack() as stack:
            try:
                src = source()
                lease = stack.enter_context(src.lease(run_key, option))
                r["token"] = {"source": src.kind, "subject": lease.subject, "slot": lease.slot, "start_utc": utc(), "end_utc": None}
                stack.enter_context(held(lease))
                body()
                r["token"]["end_utc"] = utc()
            except Exception:
                raise
            except BaseException as e:      # held back, so that the release below runs and cannot replace it
                pending = e
    except BaseException as e:             # from the lease's acquisition or release
        r["ok"] = False
        r["errors"].append(f"token lease: {type(e).__name__}: {e}"[:1500])
        if not isinstance(e, Exception):
            if pending is None:
                pending = e                # a release interrupt never replaces one already pending
    final = getattr(lease, "final_record", None)
    if callable(final):                    # the slot's interval, complete only after the release
        try:
            r["onedata_slot"] = final()
        except Exception as e:  # noqa: BLE001
            r["ok"] = False
            r["errors"].append(f"slot record: {type(e).__name__}: {e}"[:300])
    if pending is not None:
        r["ok"] = False
        r["errors"].append(f"interrupted: {type(pending).__name__}")
    if "token" not in r and on_no_lease is not None:
        on_no_lease()                      # a run that never had its lease must not leave its peers waiting at the barrier
    _close(r)
    finalize()
    if pending is not None:
        raise pending
    return r


def _close(r: dict) -> None:
    if r.get("token") and r["token"].get("end_utc") is None:
        r["token"]["end_utc"] = utc()

# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/termination.py, blob 253f1c693d75b815cb545e0b24c09119027dd8b9
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""SIGTERM as a normal exit for a driver of real sandboxes.

Python's default for SIGTERM ends the process on the spot: no `finally` runs, so a mount sandbox's sidecar and FUSE mount are
left to the orphan reaper. `exit_on_sigterm()`, called at the top of a driver's `main`, makes it an orderly exit instead:

  * the FIRST SIGTERM stops every sandbox this process has up, in a thread of its own (at most `MAX_PARALLEL_STOPS` at a
    time; `Sandbox.stop` is the ordered teardown, and it serialises with the driver's own stop). A worker blocked in a
    cell's exec could otherwise hold a thread pool's exit for the rest of the cell, and with no way out the sender escalates
    to SIGKILL, which is the orphan this is here to avoid;
  * and raises SystemExit(143) in the main thread, so the driver's `finally` blocks run;
  * no new sandbox starts from then on, in any thread: `Sandbox.start` raises SystemExit(143), and one that was starting
    when the signal came is stopped before it is handed out. SystemExit, not an error: a driver must stop on it, not
    record a failed run and go on to the next;
  * in a `sigterm_deferred()` region of the main thread (a teardown, a quarantine hold) nothing is raised: cutting a teardown
    short would leave what it exists to remove, and a false "teardown failed" would quarantine a slot. The signal is
    remembered: the region's work goes on, and the next `Sandbox.start` or `raise_if_requested` ends the driver;
  * a later SIGTERM changes nothing: it cannot interrupt the teardown the first one started.

The campaign CLI has its own graceful stop (a stop event that drains the runs) and does not use this.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading

log = logging.getLogger(__name__)

EXIT_CODE = 143  # 128 + SIGTERM, what a shell reports for a process SIGTERM ended
MAX_PARALLEL_STOPS = 4  # each teardown unmounts and stops containers: not all of a level's at the same instant

_requested = threading.Event()
_live: list = []
_live_lock = threading.Lock()
_stopper: threading.Thread | None = None
_main_deferred = 0  # main thread only: how many sigterm_deferred() regions it is in


def register(sbx) -> None:
    """A sandbox that is up: the first SIGTERM stops it. Call before `raise_if_requested`: a signal after that check then
    finds it in the list, and one before it is seen by the check."""
    with _live_lock:
        if sbx not in _live:
            _live.append(sbx)


def unregister(sbx) -> None:
    with _live_lock:
        if sbx in _live:
            _live.remove(sbx)


def requested() -> bool:
    return _requested.is_set()


def raise_if_requested() -> None:
    if _requested.is_set():
        raise SystemExit(EXIT_CODE)


class sigterm_deferred:
    """In the main thread, a SIGTERM raises nothing inside this block (other threads are never interrupted anyway)."""

    def __enter__(self):
        global _main_deferred
        self._main = threading.current_thread() is threading.main_thread()
        if self._main:
            _main_deferred += 1
        return self

    def __exit__(self, *a):
        global _main_deferred
        if self._main:
            _main_deferred -= 1
        return False


def _stop_one(sbx) -> None:
    try:
        sbx.stop()
    except BaseException as e:
        log.error("SIGTERM: stopping %s failed: %r", getattr(sbx, "name", sbx), e)


def _stop_live() -> None:
    with _live_lock:
        todo = list(_live)
    # plain threads, not an executor: the main thread is on its way out, and once its exit has begun an executor refuses new work
    slots = threading.BoundedSemaphore(MAX_PARALLEL_STOPS)

    def one(sbx) -> None:
        with slots:
            _stop_one(sbx)

    threads = [threading.Thread(target=one, args=(s,), name="sigterm-stop", daemon=False) for s in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def wait_for_stop(timeout_s: float | None = None) -> None:
    """Until the stops the SIGTERM started have ended (at once when there was none): before anything that would remove the
    same containers without knowing about them."""
    t = _stopper
    if t is not None:
        t.join(timeout_s)


def _on_sigterm(signum, frame) -> None:
    global _stopper
    first = not _requested.is_set()
    _requested.set()
    if not first:
        return
    with contextlib.suppress(OSError):
        os.write(2, b"SIGTERM: stopping this process's sandboxes, no new ones start\n")
    try:
        t = threading.Thread(target=_stop_live, name="sigterm-stopper", daemon=False)  # the process waits for it
        t.start()
        _stopper = t
    except RuntimeError as e:  # already shutting down: the driver's own finally blocks are all there is
        log.error("SIGTERM: the stopper could not start: %r", e)
    if _main_deferred == 0:
        raise SystemExit(EXIT_CODE)


def exit_on_sigterm(driver_comm: str | None = None) -> bool:
    """Install the handler. Only the main thread can; returns False (and logs it) anywhere else. With `driver_comm`
    (`[sandbox] earlyoom_comm`) it also names this process as a driver, which the host's earlyoom ignores (earlyoom.py)."""
    from expcore_onedata.sandbox import earlyoom

    if driver_comm and threading.current_thread() is threading.main_thread():
        earlyoom.name_this_driver(driver_comm)
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError as e:
        log.error("exit_on_sigterm must be called from the main thread: %r", e)
        return False
    return True

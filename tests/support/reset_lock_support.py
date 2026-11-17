"""Child processes for the tests of the reset lock (`federation.reset`) that every reset takes exclusively."""

import asyncio
import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path

SLEEPER = "import time; time.sleep(600)"  # a stand-in for the tool server: it outlives its parent unless killed


async def _hang(config, out: str, point: str) -> None:
    """Start a child exactly as the MCP client starts the tool server (`mcp.client.stdio`: `anyio.open_process`, in a
    session of its own, the default `close_fds`), then hang with it running, inside a shortcut reset's exclusive hold
    when `point` is "reset"; the loop never ends, so asyncio never kills the child as it would at the loop's close."""
    import anyio

    from expcore_onedata.rangemap import PlacementSettings
    from expcore_onedata.resetgrant import exclusive

    process = await anyio.open_process([sys.executable, "-c", SLEEPER], start_new_session=True)
    pids = json.dumps({"parent": os.getpid(), "child": process.pid})
    if point == "reset":
        settings = PlacementSettings(holder="alpha", reset_shortcut=True, reset_exclusive_wait_s=5.0)
        with exclusive(config, settings) as grant:
            assert grant.shortcut, grant.facts()
            Path(out).write_text(pids, encoding="utf-8")
            await anyio.sleep(600)
    Path(out).write_text(pids, encoding="utf-8")
    await anyio.sleep(600)


def run_and_hang(platform: str, out: str, point: str) -> None:
    """In a child process: take a campaign run's shared admission the way the campaign does (`run_admission`), start a
    child the way the tool server is started and hang (`_hang`), for the test to kill this process with SIGKILL."""
    from expcore_onedata.admission import run_admission
    from expcore_onedata.config import load_platform

    config = load_platform(Path(platform), require_secrets=False)
    admit = run_admission(config)
    assert admit is not None
    with admit((), {}, threading.Event(), False):
        asyncio.run(_hang(config, out, point))


def _publish(path: str, text: str) -> None:
    """Write a file whole, so the other process never reads it half-written."""
    partial = Path(f"{path}.partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def hold_until_told(platform: str, held: str, release: str, releasing: str) -> None:
    """In a child process: take a campaign run's shared admission the way the campaign does (`run_admission`), its
    share of `federation.reset` with it, write `held` (this pid), wait for `release` to appear, and write `releasing`
    (this host's monotonic instant, just before the admission is let go) before letting it go."""
    from expcore_onedata.admission import run_admission
    from expcore_onedata.config import load_platform

    config = load_platform(Path(platform), require_secrets=False)
    admit = run_admission(config)
    assert admit is not None
    with admit((), {}, threading.Event(), False):
        _publish(held, str(os.getpid()))
        deadline = time.monotonic() + 120
        while not Path(release).exists():
            if time.monotonic() > deadline:
                raise TimeoutError("the parent never told the holder to let go")
            time.sleep(0.02)
        _publish(releasing, repr(time.monotonic()))


def hold_and_fork(platform: str, out: str) -> None:
    """In a child process: take the shared admission, fork a grandchild that keeps every descriptor the fork hook left
    it for 3 s, let the admission go, and write whether `federation.reset` can then be taken exclusively (it can only
    when the grandchild holds no copy of the share), whether the grandchild was still alive then, and its pid."""
    from expcore_onedata.admission import FederationLock
    from expcore_onedata.config import load_platform
    from expcore_onedata.faults import load_faults
    from expcore_onedata.resetlock import FEDERATION_RESET

    path = Path(platform)
    config = load_platform(path, require_secrets=False)
    settings = load_faults(path, config)
    admission = FederationLock(settings).acquire(exclusive=False, timeout_s=1.0)
    pid = os.fork()
    if pid == 0:
        time.sleep(3)  # still alive, with whatever it inherited, when the parent tries the lock
        os._exit(0)
    admission.close()
    descriptor = os.open(settings.journal / FEDERATION_RESET, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        free = True
    except BlockingIOError:
        free = False
    finally:
        os.close(descriptor)
    alive = os.waitpid(pid, os.WNOHANG) == (0, 0)  # not yet ended
    Path(out).write_text(json.dumps({"free": free, "grandchild": pid, "grandchild_alive": alive}), encoding="utf-8")
    os.waitpid(pid, 0)

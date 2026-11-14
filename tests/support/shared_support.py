"""Child processes for the tests of an out-of-campaign tool's shared admission."""

import os
from pathlib import Path


def hold_and_die(platform: str, reason: str) -> None:
    """In a child process: enter the tool's shared admission and die inside it, as a killed smoke driver would."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.tool_admission import shared_admission

    with shared_admission(load_platform(Path(platform), require_secrets=False), reason=reason):
        os._exit(0)  # no finally, no release: the kernel drops the lock, and the record has no end


def hold_fork_and_unwind(platform: str, result: str) -> None:
    """In a child process: enter the tool's shared admission, fork, and let the forked child give up its copy of the
    admission, as a child that unwinds the parent's `with` would; the parent then writes what the federation shows."""
    import json

    from expcore_onedata.admission import FederationBusy, FederationLock
    from expcore_onedata.config import load_platform
    from expcore_onedata.faults import load_faults
    from expcore_onedata.shared_holds import SharedHolds
    from expcore_onedata.tool_admission import shared_admission

    path = Path(platform)
    config = load_platform(path, require_secrets=False)
    settings = load_faults(path, config)
    with shared_admission(config, reason="a tool that forks") as admission:
        pid = os.fork()
        if pid == 0:
            try:
                admission.close()  # the copy in the child holds nothing: it ends no record and drops no lock
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        try:
            FederationLock(settings).acquire(exclusive=True, timeout_s=0.2).close()
            refused = False
        except FederationBusy:
            refused = True
        open_holds = [hold["reason"] for hold in SharedHolds(settings.journal).open_holds()]
        Path(result).write_text(json.dumps({"refused": refused, "open": open_holds}), encoding="utf-8")

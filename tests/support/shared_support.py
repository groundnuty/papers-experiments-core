"""Child processes for the tests of an out-of-campaign tool's shared admission."""

import os
from pathlib import Path


def hold_and_die(platform: str, reason: str) -> None:
    """In a child process: enter the tool's shared admission and die inside it, as a killed smoke driver would."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.tool_admission import shared_admission

    with shared_admission(load_platform(Path(platform), require_secrets=False), reason=reason):
        os._exit(0)  # no finally, no release: the kernel drops the lock, and the record has no end

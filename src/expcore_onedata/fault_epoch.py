# Provenance: new
"""The federation's link epoch: a count of the heals and link failures a run's admission compares with its check (O35).

`link-epoch.json`, beside the fault journal, holds a count every process on the federation shares, raised:

- before any heal's first action, the fault gate's (`fault_gate_heal`) and a restore's (`fault_restore`), and before
  the heal's own line, so a heal that cannot raise it does not begin;
- whenever the fault gate's own link check, in any call, finds a pair failing (`fault_gate.FaultGate`).

A run's pre-run check notes the count it read, and the run's admission compares it with the count then
(`admission_recheck`). The record also says which event raised it last (`heal` or `links_failed`) and when (`utc`), for
the words of a refusal only: what is compared is the count, never a clock. It is written whole or not at all, a draft
fsynced and renamed over it with the rename made durable, under the journal-wide lock, and only ever upwards.
"""

import contextlib
import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore_onedata.fault_journal import InjectionJournal, _fsync_directory, make_directories

LINK_EPOCH = "link-epoch.json"
GATE_NOTE = "expcore_onedata.link_epoch"  # the key of the count a pre-run check notes for its try (`prerun`)
HEAL, LINKS_FAILED = "heal", "links_failed"  # what raised the count


class LinkEpoch:
    """The federation's link epoch in the fault journal's directory (see the module docstring)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / LINK_EPOCH
        self._journal = InjectionJournal(self.directory)  # its journal-wide lock

    def read(self) -> dict[str, Any]:
        """The record: `epoch` (0 when none was ever raised), and `event` and `utc` once one was. A record that is
        not one (not JSON, no whole count) raises ValueError: nobody can tell what it counted."""
        if not self.path.exists():
            return {"epoch": 0}
        with self._journal._section(fcntl.LOCK_SH):
            return self._unlocked()

    def _unlocked(self) -> dict[str, Any]:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"epoch": 0}
        except ValueError:
            raise ValueError("the federation's link epoch is not JSON") from None
        epoch = record.get("epoch") if isinstance(record, dict) else None
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("the federation's link epoch holds no count")
        return record

    def raise_for(self, event: str) -> int:
        """Raise the count by one for `event` (`HEAL` or `LINKS_FAILED`), durably; return the new count. A record
        that cannot be written raises OSError, and one that cannot be read ValueError."""
        make_directories(self.directory)
        with self._journal._section():
            epoch = int(self._unlocked()["epoch"]) + 1
            draft = self.directory / f"{LINK_EPOCH}.draft"  # one writer at a time: the section is held
            utc = datetime.now(UTC).isoformat(timespec="milliseconds")
            try:
                with open(draft, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"epoch": epoch, "event": event, "utc": utc}))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(draft, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    draft.unlink(missing_ok=True)
                raise
            _fsync_directory(self.directory)
        return epoch

# Provenance: new
"""The `/etc/resolv.conf` every agent cell reads: one neutral text, the same on both backends (O56).

A Docker container's own `/etc/resolv.conf` is the engine's: on a user-defined network it names the engine's embedded
resolver (127.0.0.11) and copies the host's `search` and `options` lines, so a host's search domain, a site detail,
could reach the agent. The Apptainer backend's cells bind a file of the sandbox's own over it. The Docker sandbox binds
the same file, read-only, so a cell on either backend reads exactly `RESOLV_TEXT`: no `nameserver`, no `search` or
`domain` line, only the two `options` that keep a lookup that cannot be answered short. The names a cell resolves come
from `/etc/hosts` alone: the federation's, as `--add-host` gives them on Docker and as the cell's own hosts file does on
Apptainer. The Docker agent resolves no name through the engine's embedded resolver (the zone and the provider through
`--add-host`, the gate by the same lines, a cell's code arrives through `docker exec`), so nothing relies on the file
the bind replaces.

This is the one constant: `apptainer.argv` re-exports it, and `containers.agent_run_args` binds the run's copy of it.
`/etc/resolv.conf` is in `inputs.TAKEN`, so a study cannot bind another file onto it.

Each run records the SHA-256 of the file a cell read (`infrastructure_facts.resolv_conf_sha256`, `FACT`), the same key
and the same value on both backends. The start has a cell read it back (`PROBE`) and refuses the run when it reads
anything else: the file the engine or the node would otherwise show is never the one the agent reads, and the record's
value is a reading from inside a cell, never the constant's own hash. The file's text is never recorded, only its hash.

What binding the file does not do: a program of a cell that sends a query to a name server by its address (127.0.0.11
on Docker, where the engine's resolver still listens in the container's network namespace) is not stopped. It reads no
host's search domain from the file; what the engine's resolver answers is the engine's.
"""

from __future__ import annotations

import hashlib
import re

from expcore_onedata.sandbox.infrafacts import CELL_RESOLV, InfrastructureFacts

RESOLV_CONF = "/etc/resolv.conf"
# No name server (`apptainer/facts.py`, `dns_none`). A resolv.conf that names none does not alone leave the C library
# without one: it then asks the name server on 127.0.0.1. On Apptainer that is the node's loopback, which every user
# of the node shares, so its hosts database is `files` alone there (`argv.NSSWITCH_TEXT`); a Docker agent has a loopback
# of its own, where nothing listens, so the lookup fails at once and its `nsswitch.conf` is the image's own.
RESOLV_TEXT = "# no name server: a cell resolves the names of /etc/hosts alone\noptions attempts:1 timeout:1\n"
RESOLV_SHA256 = hashlib.sha256(RESOLV_TEXT.encode("utf-8")).hexdigest()
FACT = "resolv_conf_sha256"  # the key of the run's `infrastructure_facts`
# the program a cell runs to read the file back: the hash of the bytes it sees at the path, never the text
PROBE = f"import hashlib\nprint('resolv', hashlib.sha256(open({RESOLV_CONF!r}, 'rb').read()).hexdigest())\n"
_ANSWER = re.compile(r"resolv ([0-9a-f]{64})")
REFUSAL = "a cell's /etc/resolv.conf is not the sandbox's own neutral file"


def judge(exit_code: int, stdout: str) -> tuple[str | None, str | None]:
    """The probe's answer: `(the hash a cell read, None)` when it is `RESOLV_SHA256`, else `(the hash it read or None,
    the problem)`. A probe that did not run, or whose answer is not one hash, reads nothing: no answer is taken for the
    file."""
    found = _ANSWER.fullmatch(stdout.strip()) if exit_code == 0 else None
    if found is None:
        return None, "a cell could not read its /etc/resolv.conf"
    if found.group(1) != RESOLV_SHA256:
        return found.group(1), REFUSAL
    return found.group(1), None


def note(facts: InfrastructureFacts, digest: str) -> None:
    """Put the hash a cell read in the run's infrastructure facts, beside what they hold, through the run's one writer
    (`infrafacts`, O89): the key is `CELL_RESOLV`'s, so no other writer may take it."""
    facts.write(CELL_RESOLV, {FACT: digest})


__all__ = ["FACT", "PROBE", "REFUSAL", "RESOLV_CONF", "RESOLV_SHA256", "RESOLV_TEXT", "judge", "note"]

# Provenance: new
"""What the Apptainer backend keeps of the Docker sandbox's guarantees and where it departs, as each run records it.

Every run's metadata holds `infrastructure_facts` (this list, by key) so a reader of a record knows which bounds held
on the node it ran on. A key is stable; its text explains it. Each departure is the node's (an HPC batch node gives an
unprivileged user no namespace but setuid Apptainer's and no cgroup of its own), never a choice of the study's.
"""

from __future__ import annotations

from typing import Any

FACTS: dict[str, str] = {
    "network_shared": (
        "no network namespace: the cell shares the node's network, so it can reach the internet and the "
        "federation's public ports (8443, and 6665 on a provider). Egress is detected, not prevented (the PI's "
        "ruling of 13 Nov 2026): every connect and new send of the cell's dynamically linked processes is logged by "
        "the egress shim to a collector outside the cell; a static binary, raw system calls, io_uring and glibc's "
        "own DNS queries pass the shim"
    ),
    "gate_on_loopback": (
        "the data gate is a process of the driver's on 127.0.0.1 at a high port: the node's loopback is shared with "
        "every user of the node, and the gate takes connections from any of them unless its launch options say "
        "otherwise (hook a)"
    ),
    "token_held_by_gate": (
        "the cell holds no token: the gate reads it from an owner-only file outside every bind, which is removed "
        "once the gate is ready; the gate's process is in the node's PID namespace, which no cell can address"
    ),
    "same_uid": (
        "every cell runs as the driver's own uid (no agent user, no privilege): it sees only the image and its "
        "binds, but whatever the node lets that uid do outside the namespaces (a connect, a FUSE mount) it can do"
    ),
    "memory_watch": (
        "no cgroup per container: the agent's memory limit is the harness's watch, which sums the RssAnon and "
        "RssShmem of the run's processes every memory_poll_s and kills them all over the limit (Docker's kernel "
        "kills the largest process alone); a spike shorter than the period is missed, and the kernel's limit is "
        "the step's, shared with the driver and the gate, so its OOM kill is infrastructure. A process that makes "
        "itself non-dumpable is hidden by /proc's hidepid, so the watch neither counts nor kills it; the end of its "
        "cell's PID namespace when the exec returns ends it"
    ),
    "disk_watch": (
        "/work, /tmp and /dev/shm are directories on the node's tmpfs, charged to the step's memory: the watch "
        "walks them every disk_poll_s and kills the run's processes when one is past its size or its inode count "
        "(disk_limit), where Docker refuses the write with ENOSPC; a directory the cell makes unreadable to its own "
        "uid is unknown to the walk and taken as past its size"
    ),
    "cpu_pids_nofile": "cpus, pids, nofile and the gate's memory limit are recorded and not enforced per container",
    "cell_ends_its_processes": (
        "each cell is one apptainer exec with a PID namespace of its own: whatever of it is left when the exec "
        "returns is killed and shown gone, so no worker it detached lives on into the next cell, as one would on "
        "Docker until the next cap or the collection"
    ),
    "no_freezer": (
        "the outputs are copied with no cgroup freezer: no process of the run is left between cells (each cell's "
        "end shows them gone), and the copy and the removals run as the one uid there is, with no privilege"
    ),
    "apptainer_stderr": "Apptainer's own warnings would be the cell's stderr: it runs with -q, which keeps its errors",
    "mount_refused": (
        "a mount data path is refused at the start until the mount's helper lands (hook c): a cell would share the "
        "uid of the oneclient that holds the mount's token"
    ),
}


def record() -> dict[str, Any]:
    """The run's `infrastructure_facts`: every key, sorted, for a reader to look up."""
    return {"backend": "apptainer", "facts": sorted(FACTS)}


__all__ = ["FACTS", "record"]

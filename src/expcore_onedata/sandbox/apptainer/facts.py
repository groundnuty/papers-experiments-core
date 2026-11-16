# Provenance: new
"""What the Apptainer backend keeps of the Docker sandbox's guarantees and where it departs, as each run records it.

Every run's metadata holds `infrastructure_facts` (this list, by key) so a reader of a record knows which bounds held
on the node it ran on. A key is stable; its text explains it. Each departure is the node's (an HPC batch node gives an
unprivileged user no namespace but setuid Apptainer's and no cgroup of its own), never a choice of the study's. One
key, `dns_none`, records what a cell resolves and is no departure: a Docker cell resolves the same (measured).
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
        "every user of the node: the gate refuses another uid's connection only with its peer-uid check, on by "
        "default whenever the gate has it; `gate.peer_uid_check` in the metadata is true only when it was asked of a "
        "gate that has it, which refuses to start when it cannot make it, and a check asked of a gate without it "
        "refuses the run's start"
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
        "itself non-dumpable is hidden by /proc's hidepid, so this sum neither counts nor kills it (hidden_memory); "
        "the end of its cell's PID namespace when the exec returns ends it"
    ),
    "hidden_memory": (
        "a process /proc hides is checked from outside: at every poll the watch compares the anonymous memory of the "
        "driver's own cgroup (memory.stat anon, which counts a hidden process) with the RssAnon of every process of "
        "the uid /proc shows in it (the larger of two listings' totals around the cgroup's), and memory nothing "
        "visible explains that grew past hidden_memory_margin_bytes during a cell for two polls kills the cell as "
        "memory_limit (memory_hidden); one that cannot be told the "
        "cell's (another run's cell in the cgroup, or a margin the start's own cells passed) makes the run's memory "
        "reading unknown, never within, as do a child of a cell process that /proc does not show (hidden_process, "
        "which a process that forks twice under the cell's hidden init escapes) and a reading that failed; hidden "
        "memory under the margin, and a hidden process's shared memory (a memfd, a shared anonymous mapping), which "
        "the cgroup counts with the run's tmpfs files, are bounded by the step's limit alone"
    ),
    "dns_none": (
        "a cell has no name server: its /etc/resolv.conf names none and its /etc/nsswitch.conf looks hosts up in "
        "files alone, so the C library resolves the names of /etc/hosts (the federation's, on the loopback) and no "
        "other; a program with a resolver of its own can still send a query, which is egress like any other. This "
        "is no departure from Docker: the agent's network there is --internal with --add-host entries and no --dns; "
        "the engine's embedded resolver (127.0.0.11) answers every outside name with SERVFAIL at once (no addresses, "
        "no timeout), the --add-host names resolve only through /etc/hosts, and a connect to an outside address "
        "fails at once with ENETUNREACH (no default route); measured on engine 29.2.1"
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


DNS = "none"  # what a cell's name lookups reach (dns_none)


def record() -> dict[str, Any]:
    """The run's `infrastructure_facts`: every key, sorted, for a reader to look up, and its name service. The sandbox's
    metadata adds the watch's memory reading (`memory`) once it exists."""
    return {"backend": "apptainer", "facts": sorted(FACTS), "dns": DNS}


__all__ = ["FACTS", "record"]

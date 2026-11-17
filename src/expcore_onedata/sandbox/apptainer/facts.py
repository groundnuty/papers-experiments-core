# Provenance: new
"""What the Apptainer backend keeps of the Docker sandbox's guarantees and where it departs, as each run records it.

Every run's metadata holds `infrastructure_facts` (this list, by key) so a reader of a record knows which bounds held
on the node it ran on. A key is stable; its text explains it. Each departure is the node's (an HPC batch node gives an
unprivileged user no namespace but setuid Apptainer's and no cgroup of its own), never a choice of the study's. One
key, `dns_none`, records what a cell resolves and is no departure: a Docker cell resolves the same (measured).

One key is conditional, `mount_refused`: it is listed only for a run whose sandbox refuses a mount data path at its
start (`ApptainerSandbox._mount_refused`: no mount helper is given, or the platform declares no `[sandbox.mount]`), so a
mount run, which was not refused, never carries it, nor does any run of a sandbox that would accept one.

The work disk's keys are conditional too (`WORK_DISK_KEYS`, O71): a run of `work_disk = "directory"` lists
`disk_watch`, one of `"image"` lists `disk_watch_tmp` and `work_disk_unverified` until its start has proved the image,
`work_disk_image` after; none lists another's.

A cell's processes end with its exec here, its PID namespace ending with it, and on Docker since O72 too (the sandbox
kills what a cell left once its `docker exec` returns), so the code tool's "State does not carry between calls" holds
on both: that is no departure, and has no key.
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
        "RssShmem of the run's processes every memory_poll_s and, over the limit, kills them all: they are the "
        "running cell's (nothing of a cell outlives its exec), so the kill is the cell's (scope cell) and the run "
        "goes on, as after a container's OOM kill on Docker, whose kernel kills the cell's largest process alone, "
        "so a cell may live on there where here it ends; a spike shorter than the period is missed, and the "
        "kernel's limit is the step's, shared with the driver and the gate, so its OOM kill is infrastructure. A "
        "process that makes itself non-dumpable is hidden by /proc's hidepid, so this sum neither counts nor kills "
        "it (hidden_memory); the end of its cell's PID namespace when the exec returns ends it"
    ),
    "hidden_memory": (
        "a process /proc hides is checked from outside: at every poll the watch compares the anonymous memory of the "
        "driver's own cgroup (memory.stat anon, which counts a hidden process) with the RssAnon of every process of "
        "the uid /proc shows in it (the larger of two listings' totals around the cgroup's), and memory nothing "
        "visible explains that grew past hidden_memory_margin_bytes during a cell for two polls kills the cell as "
        "memory_limit (memory_hidden); with another run's cell charged to the same cgroup it cannot be told this "
        "cell's, so the cell cannot be isolated and the watch kills it and ends the run (scope run, infrastructure: "
        "set aside), its memory reading unknown, never over: the memory may be the other run's, and every run whose "
        "watch sees it ends so; a margin the start's own cells passed makes the run's memory reading unknown, "
        "never within, with no kill, as do a child of a cell process that /proc does not show (hidden_process, "
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
        "uid is unknown to the walk and taken as past its size. The node gives an unprivileged user no file system "
        'of a size of their own (no loop mount, no root); work_disk = "image" gives /work one (work_disk_image)'
    ),
    "disk_watch_tmp": (
        "/tmp and /dev/shm are directories on the node's tmpfs, charged to the step's memory: the watch walks them "
        "every disk_poll_s and kills the run's processes when one is past its size or its inode count (disk_limit), "
        "where Docker refuses the write with ENOSPC; a directory the cell makes unreadable to its own uid is unknown "
        "to the walk and taken as past its size. /work is the run's work disk image (work_disk_image)"
    ),
    "work_disk_image": (
        "/work is a directory of an ext3 image of work_disk_bytes (rounded down to 2 MiB) that the run makes with "
        "the node's mkfs.ext3 on its tmpfs and every cell binds (-B <image>:/work:image-src=/work), so a write past "
        "its size is refused with ENOSPC and nothing is killed, as on Docker: at the start a cell wrote past the "
        "size of an image made the same way and got ENOSPC, and the run's own image was shown a file system of its "
        "own of at most that size, or the start failed. How Apptainer mounts it (the kernel's driver where the "
        "site's apptainer.conf allows setuid-mount extfs, fuse2fs otherwise) is the node's. Its pages are charged to "
        "the step's memory as they are written, up to its size (the start's probe fills one whole image once, given "
        "back before the agent's first cell); the watch still walks the image file's allocated "
        "size. /work holds no lost+found, which Docker's disk does. The outputs are copied out by the copy helper in "
        "an exec of the image, with every refusal of Docker's copy"
    ),
    "work_disk_unverified": (
        'the run asked for the work disk image (work_disk = "image") and its start had not yet shown a write past '
        "/work's size refused with ENOSPC: a record that holds this key is of a start that did not complete"
    ),
    "cpu_pids_nofile": "cpus, pids, nofile and the gate's memory limit are recorded and not enforced per container",
    "no_freezer": (
        "the outputs are copied with no cgroup freezer: no process of the run is left between cells (each cell's "
        "end shows them gone), and the copy and the removals run as the one uid there is, with no privilege"
    ),
    "apptainer_stderr": "Apptainer's own warnings would be the cell's stderr: it runs with -q, which keeps its errors",
    "mount_refused": (
        "this sandbox refuses a mount data path at its start: it is given no mount helper (`mount_helper=`, hook c, "
        "`host_mount_factory()`) or the platform declares no `[sandbox.mount]`; listed only for a run whose sandbox "
        "refuses one, so a mount run never carries it. The helper accepts proxy I/O only, and refuses a direct "
        "mount: HPC mounts are proxy-only until the direct-I/O comparison passes"
    ),
}


DNS = "none"  # what a cell's name lookups reach (dns_none)
# the keys of each work disk (O71): a run records its own and none of the others'; the directory's record is the one
# every run had before the image existed
WORK_DISK_KEYS = {
    "directory": ("disk_watch",),
    "image_unverified": ("disk_watch_tmp", "work_disk_unverified"),
    "image": ("disk_watch_tmp", "work_disk_image"),
}


def record(work_disk: str = "directory", *, mount_refused: bool) -> dict[str, Any]:
    """The run's `infrastructure_facts`: every key, sorted, for a reader to look up, and its name service. Of the work
    disk's keys (`WORK_DISK_KEYS`) only its own are listed, and `mount_refused` only when the run's sandbox refuses a
    mount data path (the module's docstring). The sandbox's metadata adds the watch's memory reading (`memory`) once it
    exists."""
    others = {key for keys in WORK_DISK_KEYS.values() for key in keys} - set(WORK_DISK_KEYS[work_disk])
    keys = sorted(key for key in FACTS if key not in others and (mount_refused or key != "mount_refused"))
    return {"backend": "apptainer", "facts": keys, "dns": DNS}


__all__ = ["FACTS", "WORK_DISK_KEYS", "record"]

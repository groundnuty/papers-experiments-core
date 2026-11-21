# Provenance: new
"""What the restore of a Docker run's mount says, in fixed words, and what shows the mount back (O97).

After a cell's deadline aborted the run's mount and the survivors were the agent's, `DockerSandbox` removes the
sidecar the abort ended, unmounts the dead share and starts a new sidecar on it (`_restore_host_mount`). The reasons it
gives when a step fails are the constants here, one for each step, and no other text: they land in the run's record
(`restore_failed`, the end `mount_lost`), so they name no path, container or host.

The mount is shown back by what two mount tables say, never by reading through it (a read could wait on the very
request that wedged the cell, O60): `bar` judges the host's table and the agent container's own. And a direct run's
reads went to the storage itself only if no generation's oneclient log shows a line of detection or fallback:
`direct_lines` joins the generations' lines, and a generation whose log could not be read makes the whole unknown.
"""

from __future__ import annotations

from collections.abc import Sequence

from expcore_onedata.sandbox import hostops

# the restore's own bound, in seconds, across every step: below the runner's wait for a restore under way (`cells.
# RESTORE_WAIT_S`, 100 s), so a stop that waits for it never gives up on one that is still inside its time
RESTORE_TOTAL_S = 90.0
NOT_WANTED = "the restore is not wanted any more"  # the run gave up on it: nothing is made
OUT_OF_TIME = "the restore ran out of its time"  # past `RESTORE_TOTAL_S`: no token is written and no sidecar started
NO_MOUNT = "the run has no mount to restore"
HOST_TABLE = "the host's mount table could not be read"
OLD_SIDECAR = "the old sidecar could not be removed"
UNMOUNT = "the dead mount could not be unmounted"
TOKEN = "the token file could not be written"
NEW_SIDECAR = "the new sidecar did not start"
NOT_MOUNTED = "the new mount did not appear"
NOT_ON_HOST = "the new mount is not on the host"
ON_HOST_SPLIT = "the host's mount table shows the new mount on more than one device"
AGENT_TABLE = "the agent's mount table could not be read"
STALE_IN_AGENT = "the dead mount is still in the agent's mount table"
NOT_IN_AGENT = "the new mount is not in the agent's mount table"
IN_AGENT_TWICE = "the agent's mount table shows the mount more than once"
AGENT_OTHER = "the agent's mount is another device"


def _fuse_at(table: str | None, mountpoint: str) -> list[dict[str, str]] | None:
    """The FUSE mounts at `mountpoint` in a mountinfo table, each with its mount `id` and `dev`; None when there is no
    table or it cannot be shown whole."""
    if table is None:
        return None
    try:
        return [
            m
            for m in hostops.parse_mountinfo(table)
            if m["mountpoint"] == mountpoint and m["fstype"].startswith("fuse")
        ]
    except hostops.MountTableError:
        return None


def bar(
    host_mountinfo: str | None,
    ns_mountinfo: str | None,
    *,
    share: str,
    mount_path: str,
) -> str | None:
    """None when the mount is shown back, else the fixed words for what is not: a FUSE mount at `share` in the host's
    table, every entry of it on one device, and exactly one entry at `mount_path` in the agent container's own table,
    on that device. A table that cannot be read is unknown: a reason, never a pass.

    **It does not ask whether the mount is a new one by a number the kernel gives it**, neither its device nor its mount
    id: the kernel hands both back to the next mount once the dead one is released, and whether it does depends on how
    soon (measured live on the study VM over five restores: the dead mount's device number came back in three, its mount
    id in a fourth, and only one restore got new numbers). What makes a mount in either table the new one rather than a
    stale copy is the restore's own sequence, which the caller owns: the dead mount shown gone from the host's table
    (READ, and empty of FUSE entries at the share: `DockerSandbox._remount` re-reads it after the unmount and fails
    closed on a table it could not read, or one that still lists the mount) and from the agent's (read as root, every
    FUSE mount in it detached and the table read empty: `_clear_agent_copy`), then the new sidecar started, so a mount
    seen afterwards cannot be the dead one. The device only has to agree between the two tables, where both are live at
    once.

    The host's table lists one FUSE mount more than once: the share is bound onto itself and made shared, so the bind is
    a peer of the volume mount under it, and a mount stacked on the bind is propagated to that peer too (measured live
    on a Linux 7.0 host: two entries at the share, one device). Counting entries there would refuse every start; the
    device is what says it is one mount, and a second device at the share is not the run's mount."""
    on_host = _fuse_at(host_mountinfo, share)
    if on_host is None:
        return HOST_TABLE
    if not on_host:
        return NOT_ON_HOST
    devices = {m["dev"] for m in on_host}
    if len(devices) > 1:
        return ON_HOST_SPLIT
    (device,) = devices
    in_agent = _fuse_at(ns_mountinfo, mount_path)
    if in_agent is None:
        return AGENT_TABLE
    if not in_agent:
        return NOT_IN_AGENT
    if len(in_agent) > 1:
        return IN_AGENT_TWICE
    return None if in_agent[0]["dev"] == device else AGENT_OTHER


def direct_lines(
    earlier: Sequence[Sequence[str] | None], current: Sequence[str] | None, *, order: Sequence[str]
) -> list[str] | None:
    """The names of the direct-I/O lines (`directio.MARKERS`) that some generation's oneclient log shows, in `order`:
    `earlier` are the generations a restore closed and `current` the live one's. None when any of them could not be
    read: unknown is never direct."""
    if current is None or any(lines is None for lines in earlier):
        return None
    seen = {*current, *(name for lines in earlier if lines is not None for name in lines)}
    return [name for name in order if name in seen]


__all__ = ["bar", "direct_lines"]

"""O97: the kernel's mount propagation, as the Docker backend's layout relies on it, played against the real kernel.

The restore of a Docker run's mount unmounts the dead FUSE mount on the host and stacks the next generation's on the
same shared directory. Whether the agent container sees that depends on WHEN its `rslave` bind of the directory is
taken:

- **before the FUSE mount is stacked** (the layout the start makes): the bind is a slave of the bare shared directory.
  The first generation propagates in, the host's unmount of it propagates in, and the next generation's mount appears;
- **after it is stacked** (the layout the start used to make): the bind resolves to the top mount, the FUSE mount
  itself, and is a clone of it. The host's unmount does not reach the clone, and the next generation's mount, a child of
  the directory the clone was not taken from, never appears: every restore would end `mount_lost`.

The test stands tmpfs in for FUSE (propagation does not depend on the file system) in a private user and mount
namespace (`unshare -Urm`): nothing outside the test's own directory is touched, and nothing needs root. It is skipped
where such namespaces are not allowed.
"""

import shutil
import subprocess

import pytest

PRELUDE = """set -e
D=$1
mkdir "$D/share" "$D/data"
mount --bind "$D/share" "$D/share"
mount --make-rshared "$D/share"
"""
STACK_GENERATION_ONE = 'mount -t tmpfs gen1 "$D/share"\necho one > "$D/share/f"\n'
AGENT_BINDS_THE_SHARE = 'mount --rbind "$D/share" "$D/data"\nmount --make-rslave "$D/data"\n'
HOST_UNMOUNTS = 'umount "$D/share"\n'
STACK_GENERATION_TWO = 'mount -t tmpfs gen2 "$D/share"\necho two > "$D/share/f"\n'


def sees(label: str) -> str:
    """What the agent's directory shows of the file the host's FUSE mount holds (`none` when it is not there)."""
    return f'echo "{label}:$(cat "$D/data/f" 2>/dev/null || echo none)"\n'


@pytest.fixture(scope="module")
def userns():
    """Skip unless an unprivileged user and mount namespace can be made here."""
    if shutil.which("unshare") is None:
        pytest.skip("`unshare` is not installed")
    probe = subprocess.run(["unshare", "-Urm", "true"], capture_output=True, timeout=30, check=False)
    if probe.returncode != 0:
        pytest.skip("unprivileged user and mount namespaces are not allowed here")


def played(tmp_path, script: str) -> dict[str, str]:
    done = subprocess.run(
        ["unshare", "-Urm", "bash", "-c", script, "bash", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return dict(line.split(":", 1) for line in done.stdout.splitlines())


def test_o97_an_agent_bound_before_the_fuse_mount_is_stacked_follows_every_generation(userns, tmp_path):
    """O97: the layout the start makes. The agent sees generation one, loses it when the host unmounts it, and sees
    generation two when the host stacks it: the restore needs no detach inside the agent's namespace."""
    script = (
        PRELUDE
        + AGENT_BINDS_THE_SHARE
        + STACK_GENERATION_ONE
        + sees("first")
        + HOST_UNMOUNTS
        + sees("unmounted")
        + STACK_GENERATION_TWO
        + sees("second")
    )
    assert played(tmp_path, script) == {"first": "one", "unmounted": "none", "second": "two"}


def test_o97_an_agent_bound_after_the_fuse_mount_is_stacked_holds_a_clone_the_next_generation_never_reaches(
    userns, tmp_path
):
    """O97: the layout the start used to make, and why it was replaced. The agent's bind is a clone of the FUSE mount:
    the host's unmount leaves it standing, detaching it leaves nothing, and generation two never appears in it."""
    script = (
        PRELUDE
        + STACK_GENERATION_ONE
        + AGENT_BINDS_THE_SHARE
        + sees("first")
        + HOST_UNMOUNTS
        + sees("unmounted")
        + 'umount -l "$D/data"\n'
        + sees("detached")
        + STACK_GENERATION_TWO
        + sees("second")
    )
    seen = played(tmp_path, script)
    assert seen == {"first": "one", "unmounted": "one", "detached": "none", "second": "none"}

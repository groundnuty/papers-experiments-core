"""importstorage.py: the shared imported trees a provider mounts read-only (O56). Run with python3.11 -I, stdlib only.

A tree on the cluster's shared filesystem can be the data of a space in several federations at once: each federation's
provider imports it (Onedata's storage import) from a read-only mount, so none of them can write to it. This script
holds what the node does for that, and nothing about Onedata's panel (the federation's bootstrap makes the storage):

  importstorage.py prepare --workdir W --import=SPACE=PATH ...
      configure.sh's step. Check each tree (below), make its empty mountpoint /volumes/import/SPACE in the rootfs, and
      write the list `W/imports` (0600, one `SPACE<TAB>PATH` per line). Nothing is created unless every tree passes.
  importstorage.py binds --workdir W
      run_service's step. Check the list again, since a start may come long after prepare, and print the arguments
      for `apptainer exec`, one per line: `--bind` and `PATH:/volumes/import/SPACE:ro`.
  importstorage.py mounts --workdir W [--proc DIR]
      After the service is up, in a control step on its node: read the mount table of this service's container
      processes (the children of the starter its supervisor, `W/service.pid`, started) and print, per space, how many
      hold /volumes/import/SPACE, or a mount below it, read-only and how many writable, as JSON. Exit 0 only when every
      listed space is held by at least one process and by none writable: the kernel's own answer to "is the tree
      read-only", not Onedata's.

A tree is accepted only as a plain absolute path of characters that cannot end a bind option, with no symbolic link on
the way (a link could be re-pointed between the check and the mount), an existing directory this user can read, not on
node-local memory (tmpfs, ramfs: the other nodes would not see it), and not inside or holding the work directory.
A refusal names the space and never the path. The script writes only in the work directory: the tree is read, and
named as a read-only bind source.
"""

import os
import re
import secrets
import stat
import sys

SPACE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
PATH = re.compile(r"/[A-Za-z0-9._+/-]{1,4095}")
MOUNT_ROOT = "/volumes/import"  # where the service sees an imported tree: the Compose federation's path, too
LOCAL_FS = frozenset({"tmpfs", "ramfs", "devtmpfs"})  # memory of one node
MOUNTINFO = "/proc/self/mountinfo"
LIST = "imports"
WORKDIR = re.compile(r"hpcop-op\.[A-Za-z0-9]{6}")
ESCAPE = re.compile(r"\\([0-7]{3})")


class ImportRefused(Exception):
    """A tree, an argument or a work directory this script will not use; the message names no path."""


def parse_arg(arg):
    """`--import=SPACE=PATH` as (space, path), or refused."""
    prefix = "--import="
    if not arg.startswith(prefix) or "=" not in arg[len(prefix) :]:
        raise ImportRefused("an import is written --import=SPACE=PATH")
    space, path = arg[len(prefix) :].split("=", 1)
    if not SPACE.fullmatch(space):
        raise ImportRefused("an import's space is lower-case letters, digits, '_' and '-', from 1 to 63")
    if not PATH.fullmatch(path):
        raise ImportRefused(
            f"the path of the tree of {space} is an absolute path of A-Z a-z 0-9 . _ + / - alone, of at most 4096"
        )
    return space, path


def parse_mountinfo(text):
    """A mount table (proc(5)) as (mountpoint, per-mount options, filesystem type) for each line that can be read."""
    found = []
    for line in text.splitlines():
        left, hyphen, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not hyphen or len(fields) < 6 or not tail:
            continue
        point = ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
        found.append((point, set(fields[5].split(",")), tail[0]))
    return found


def filesystem_of(path, mounts):
    """The type of the filesystem the longest mountpoint holding `path` is: None when the table names none."""
    best, kind = -1, None
    for point, _, fstype in mounts:
        under = path == point or path.startswith(point.rstrip("/") + "/")
        if under and len(point) > best:
            best, kind = len(point), fstype
    return kind


def check_source(space, path, workdir, mountinfo):
    """Refuse a tree that must not be bound; `mountinfo` is the mount table's text. Nothing is created or changed."""
    if not path.startswith("/") or os.path.normpath(path) != path or path == "/":
        raise ImportRefused(
            f"the path of the tree of {space} is not a plain absolute path: no '.', '..' or '//', and not '/' itself"
        )
    parts = path.split("/")[1:]
    for depth in range(1, len(parts) + 1):
        step = "/" + "/".join(parts[:depth])
        try:
            mode = os.lstat(step).st_mode
        except FileNotFoundError:
            raise ImportRefused(f"the tree of {space} does not exist") from None
        except OSError as error:
            raise ImportRefused(f"the tree of {space} cannot be read ({type(error).__name__})") from None
        if stat.S_ISLNK(mode):
            raise ImportRefused(f"the tree of {space} passes through a symbolic link: name its real path")
    if not stat.S_ISDIR(mode):
        raise ImportRefused(f"the tree of {space} is not a directory")
    if not os.access(path, os.R_OK | os.X_OK):
        raise ImportRefused(f"the tree of {space} is not readable by this user")
    work = os.path.realpath(workdir)
    if path == work or path.startswith(work + "/") or work.startswith(path + "/"):
        raise ImportRefused(f"the tree of {space} is inside or holds this service's work directory")
    if filesystem_of(path, parse_mountinfo(mountinfo)) in LOCAL_FS:
        raise ImportRefused(
            f"the tree of {space} is on node-local memory: a shared tree is on the shared filesystem, which every "
            "node sees"
        )


def read_mounts():
    try:
        with open(MOUNTINFO, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        raise ImportRefused(
            "the node's mount table cannot be read, so it cannot be told whether a tree is on node-local memory"
        ) from None


def private_workdir(workdir):
    """The work directory of a provider, private to this user, with its rootfs unpacked."""
    try:
        status = os.lstat(workdir)
    except OSError:
        status = None
    if (
        status is None
        or not WORKDIR.fullmatch(os.path.basename(workdir))
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise ImportRefused("--workdir is not a provider's private work directory (hpcop-op.*, mode 0700, yours)")
    if not os.path.exists(os.path.join(workdir, "rootfs.complete")):
        raise ImportRefused("the work directory has no complete rootfs: run node-init.sh first")


def real_dir(path):
    """True for a directory that is no link; False when it is absent; refused for anything else."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise ImportRefused("a mountpoint of the rootfs is a link or not a directory")
    return True


def mountpoint_steps(workdir, space):
    """The directories from the rootfs's /volumes down to the space's mountpoint, as paths."""
    walked, steps = os.path.join(workdir, "rootfs"), []
    for part in ("volumes", "import", space):
        walked = os.path.join(walked, part)
        steps.append(walked)
    return steps


def check_mountpoint(workdir, space):
    """Refuse a mountpoint a bind could not safely cover: a step that is a link, or one that already holds files.
    Nothing is created."""
    steps = mountpoint_steps(workdir, space)
    for step in steps:
        real_dir(step)
    if os.path.isdir(steps[-1]) and os.listdir(steps[-1]):
        raise ImportRefused(f"the mountpoint of {space} holds files: a bind would hide them")


def make_mountpoint(workdir, space):
    """Make the empty directory the tree is bound over, below the rootfs, after check_mountpoint."""
    for step in mountpoint_steps(workdir, space):
        if not real_dir(step):
            os.mkdir(step, 0o755)
            os.chmod(step, 0o755)


def prepare(workdir, imports, mountinfo=None):
    """Check every tree and mountpoint, then make the mountpoints and write the list; nothing is made when one is
    refused."""
    private_workdir(workdir)
    table = read_mounts() if mountinfo is None else mountinfo
    seen_spaces, seen_trees = set(), set()
    for space, path in imports:
        if space in seen_spaces or path in seen_trees:
            raise ImportRefused(f"{space} names a space or a tree that another import names too (twice)")
        seen_spaces.add(space)
        seen_trees.add(path)
        check_source(space, path, workdir, table)
        check_mountpoint(workdir, space)
    for space, _ in imports:
        make_mountpoint(workdir, space)
    if imports:
        body = "".join("%s\t%s\n" % pair for pair in sorted(imports))
        target = os.path.join(workdir, LIST)
        # A name no one has made, created exclusively and never through a link: a draft that was a link into a tree
        # would otherwise be truncated and written through.
        draft = "%s.draft.%d.%s" % (target, os.getpid(), secrets.token_hex(4))
        fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(draft, target)


def listed(workdir):
    """The list as (space, path) pairs, each line read as strictly as an argument; empty when there is none. The file
    must be a regular file of this user that no one else can read, and is never opened through a link."""
    private_workdir(workdir)
    try:
        fd = os.open(os.path.join(workdir, LIST), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError:
        raise ImportRefused("the list of imports cannot be opened (a link, or unreadable)") from None
    with os.fdopen(fd, encoding="utf-8") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077:
            raise ImportRefused("the list of imports is not a regular file of this user, readable by no one else")
        lines = handle.read().splitlines()
    pairs = []
    for line in lines:
        space, tab, path = line.partition("\t")
        if not tab:
            raise ImportRefused("the list of imports holds a line that is not SPACE<TAB>PATH")
        pairs.append(parse_arg("--import=%s=%s" % (space, path)))
    return pairs


def binds(workdir):
    """The `apptainer exec` arguments that mount each listed tree read-only, after checking each again (with no mount
    table: the node-local check is prepare's, made once)."""
    out = []
    for space, path in listed(workdir):
        check_source(space, path, workdir, "")
        out += ["--bind", "%s:%s/%s:ro" % (path, MOUNT_ROOT, space)]
    return out


def proc_stat(proc, pid):
    """(parent pid, start tick) of a process from its stat (proc(5), fields 4 and 22), or None when unreadable."""
    try:
        with open(os.path.join(proc, str(pid), "stat"), encoding="ascii", errors="replace") as handle:
            text = handle.read()
        rest = text[text.rindex(")") + 1 :].split()
        return int(rest[1]), int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def owned_by(proc, pid, uid):
    try:
        return os.stat(os.path.join(proc, str(pid))).st_uid == uid
    except OSError:
        return False


def service_processes(workdir, proc, uid):
    """The pids of this service's container processes: the children of the Apptainer starter that the service's own
    supervisor started. The supervisor is the one `service.pid` names (its pid and start tick, as run_service wrote
    them); the starter is setuid root, so hidden from this user under hidepid, but the supervisor's `children` file
    names it, and the container's first process, which this user can see, has it as its parent. Nothing is guessed:
    no running supervisor or no child of it is no process, and a mount held only by another service of this user on
    the node is never this service's."""
    try:
        with open(os.path.join(workdir, "service.pid"), encoding="ascii") as handle:
            pid, tick = handle.read().split()
        if not (pid.isdigit() and tick.isdigit()):
            return []
    except (OSError, ValueError):
        return []
    supervisor = proc_stat(proc, pid)
    if supervisor is None or str(supervisor[1]) != tick or not owned_by(proc, pid, uid):
        return []
    starters = set()
    try:
        for task in os.listdir(os.path.join(proc, pid, "task")):
            with open(os.path.join(proc, pid, "task", task, "children"), encoding="ascii") as handle:
                starters.update(int(child) for child in handle.read().split())
    except (OSError, ValueError):
        return []
    found = []
    for name in sorted(os.listdir(proc)):
        stat_of = proc_stat(proc, name) if name.isdigit() else None
        if stat_of is not None and stat_of[0] in starters and owned_by(proc, name, uid):
            found.append(name)
    return found


def check_mounts(workdir, proc="/proc", uid=None):
    """What this service's processes hold at each listed space's mountpoint: (facts, ok). `ok` is true when every listed
    space is held by at least one of them and by none with a mount writable. The mount's own options decide, never the
    filesystem's (a read-only bind of a writable filesystem is read-only), and every mount at the mountpoint or below
    it counts: a writable mount inside the tree makes files in it writable, whatever the mount above it says."""
    uid = os.getuid() if uid is None else uid
    spaces = [space for space, _ in listed(workdir)]
    tally = {space: {"space": space, "processes": 0, "read_only": 0, "writable": 0} for space in spaces}
    for name in service_processes(workdir, proc, uid):
        try:
            with open(os.path.join(proc, name, "mountinfo"), encoding="utf-8") as handle:
                mounts = parse_mountinfo(handle.read())
        except OSError:
            continue  # a process that ended since the listing
        for space in spaces:
            here = "%s/%s" % (MOUNT_ROOT, space)
            held = [options for point, options, _ in mounts if point == here or point.startswith(here + "/")]
            if held:
                tally[space]["processes"] += 1
                tally[space]["read_only" if all("ro" in options for options in held) else "writable"] += 1
    facts = [tally[space] for space in spaces]
    return facts, all(f["processes"] > 0 and f["writable"] == 0 for f in facts)


def options(argv):
    """`--workdir W`, `--proc DIR` and the rest, as (named, rest)."""
    named, rest = {}, []
    index = 0
    while index < len(argv):
        if argv[index] in ("--workdir", "--proc"):
            if index + 1 >= len(argv):
                raise ImportRefused("%s needs a value" % argv[index])
            named[argv[index][2:]] = argv[index + 1]
            index += 2
        else:
            rest.append(argv[index])
            index += 1
    return named, rest


def main(argv):
    try:
        named, rest = options(argv[1:])
        verb = argv[0] if argv else ""
        if "workdir" not in named or verb not in ("prepare", "binds", "mounts"):
            raise ImportRefused("usage: importstorage.py prepare|binds|mounts --workdir W [--import=SPACE=PATH ...]")
        if verb == "prepare":
            prepare(named["workdir"], [parse_arg(arg) for arg in rest])
            print("ok: %d shared import(s) prepared" % len(rest), file=sys.stderr)
            return 0
        if rest:
            raise ImportRefused("%s takes only --workdir" % verb)
        if verb == "binds":
            for argument in binds(named["workdir"]):
                print(argument)
            return 0
        facts, ok = check_mounts(named["workdir"], named.get("proc", "/proc"))
        import json

        print(json.dumps(facts))
        for fact in facts:
            held = "%d process(es), %d read-only, %d writable" % (
                fact["processes"],
                fact["read_only"],
                fact["writable"],
            )
            good = fact["processes"] > 0 and fact["writable"] == 0
            print(
                "%s: shared import %s is mounted read-only (%s)" % ("ok" if good else "FAIL", fact["space"], held),
                file=sys.stderr,
            )
        return 0 if ok else 1
    except ImportRefused as refusal:
        print("importstorage: ERROR: %s" % refusal, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

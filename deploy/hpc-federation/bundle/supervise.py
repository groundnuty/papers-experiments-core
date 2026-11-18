"""supervise.py: the service process of a launch, and the only way the bundle signals it.

  run -- CMD ARG...        run CMD (run_service's `apptainer exec ...`) as this process's child, forward SIGTERM to it,
                           turn SIGUSR1 into SIGKILL for it, and exit with its status once it ends
  signal PID START SIG     send SIG (TERM, USR1 or KILL) to PID only if it is still the process that started at clock
                           tick START (field 22 of /proc/PID/stat); exit 0 if sent, 1 if PID is gone or is another
                           process
  check                    exit 0 if pidfd_open and pidfd_send_signal work here (Linux 5.3 and 5.1): through os and
                           signal where this Python has them, else the raw system calls through ctypes; or if
                           pidfd_open answers ENOSYS (Linux before 5.3), the start-tick fallback below, which it
                           names on stdout (`mode: start-tick fallback ...`); where pidfd works it prints nothing

Why (live run 4): run_service used to exec apptainer, which execs the setuid-root starter under the same PID. On a node
with hidepid=invisible that process is hidden from its own user: /proc/<pid> is absent while kill -0 succeeds. The
bundle read /proc to decide whether the service ran, and to tell it from a reused PID before signalling it, so it
declared every start dead, and neither its stop nor teardown.sh ever signalled a running service.

Now the recorded service process is this one: our uid, not setuid, so /proc shows it to us, and its PID and start time
identify it as before. Only this process signals the starter, which is its own child. It never reaps the child before
it ends, so the child's PID cannot be recycled while this process holds it; it holds a pidfd to it (pidfd_open(2),
NOTES: even a terminated child's PID "will not have been recycled and the returned file descriptor will refer to the
resulting zombie process", provided SIGCHLD is not ignored, which run() ensures). Signals go through that pidfd
(pidfd_send_signal(2): "a stable reference to a specific process; if that process terminates, pidfd_send_signal()
fails with the error ESRCH"). pidfd_open(2) takes a PID and goes through no /proc directory, so hidepid, a procfs mount
option, does not apply to it; its only errors are EINVAL, EMFILE, ENFILE, ENODEV, ENOMEM and ESRCH. Sending needs the
permission kill(2) needs (the sender's real or effective UID equals the target's real or saved set-user-ID): the
starter's real UID is ours, so the kernel allows it, as kill -0 showed live.

`signal` is how lib.sh and teardown.sh signal this process: it opens a pidfd for PID first, then checks PID's start
time in /proc. If the start time is the recorded one, the process holding PID at that moment is the recorded one, and
the pidfd refers to it (a PID taken over before the pidfd_open would show a later start time), so a reused PID is
never signalled.

A Python built without `os.pidfd_open` or `signal.pidfd_send_signal` (a standalone build, made against an old C
library, on a node whose kernel has both) makes the system calls itself: `pidfd_open` is 434 and `pidfd_send_signal`
424 in Linux's generic table, which every architecture shares for calls this new, through the C library's `syscall`
(ctypes), as the platform layer's node code does (`expcore_onedata.sandbox.apptainer.node`). A failing call raises
OSError with its errno, so ESRCH is ProcessLookupError and EPERM PermissionError, as from `os` and `signal`.

A kernel without pidfd_open (Linux before 5.3: an EL8 4.18 kernel answers ENOSYS, errno 38, to pidfd_open and has
pidfd_send_signal) has no descriptor to hold a process by. Only when pidfd_open answers ENOSYS, never for another
refusal (EPERM from a filter, EMFILE, ...), which stays an error as it always was, `check`, `run` and `signal` use the
start-tick fallback, which follows `expcore_onedata.fault_relay._kill_if_started`. On a kernel that has pidfd_open the
code above runs as before and the fallback is never entered. It is chosen by who is signalled:

- `signal PID START SIG` signals the process lib.sh and teardown.sh recorded: supervise.py itself, our uid, not setuid,
  so /proc shows it. The fallback reads PID's real and effective uid from /proc/PID/status and signals only if both are
  this process's own uid (another user's process is refused, and kill(2) would refuse it anyway), then reads the start
  tick (field 22 of /proc/PID/stat) last and signals with os.kill at once, only if it is START; after the signal it
  reads the tick again and says on stderr if it changed. A process gone, or one whose uid or tick cannot be read or is
  not as recorded, is not signalled (exit 1).
- `run` signals its own child. Its identity is that it is this process's unreaped child, which the kernel guarantees:
  a PID is free to be reused only once its parent has waited for it. Before every os.kill, os.waitid(P_PID, pid, WEXITED
  | WNOHANG | WNOWAIT) asks the kernel whether the pid is still such a child (it does not reap): ChildProcessError says
  it was reaped, and nothing is sent. The signal handler and the wait run in this process's one main thread, so nothing
  reaps the child between that question and the kill. Neither /proc nor a start tick is consulted: under hidepid the
  setuid starter is absent from /proc (live run 4), and where /proc shows it the entry is owned by its effective user,
  root, so a tick-and-uid gate would never forward a stop and the container would outlive its supervisor.

What remains of the PID-reuse window is `signal`'s: between the last read of the start tick and the kill (two system
calls) the process could end and another process of the same uid be given its PID, which the kill would then reach.
That needs the whole PID space to cycle inside that gap, and the start tick and the uid make it negligible, but it is
not the pidfd's guarantee, which holds the process itself across the checks. `run` has no such window. The stop that
`signal` starts is followed by lib.sh's own re-checks of the recorded process (running_ours: same PID, same start tick).
"""

import errno
import os
import signal
import sys

SIGNALS = {"TERM": signal.SIGTERM, "USR1": signal.SIGUSR1, "KILL": signal.SIGKILL}
SYS_PIDFD_SEND_SIGNAL, SYS_PIDFD_OPEN = 424, 434  # Linux's generic syscall table (x86_64 and aarch64 alike)
PIDFD, FALLBACK = "pidfd", "fallback"
FALLBACK_LINE = "mode: start-tick fallback (pidfd_open answers ENOSYS here, Linux before 5.3)"
FALLBACK_NOTE = "pidfd_open answers ENOSYS here (Linux before 5.3): the start-tick fallback"


def say(msg):
    sys.stderr.write("hpcop: supervise: %s\n" % msg)
    sys.stderr.flush()


def _syscall(number, *args):
    """A raw Linux system call through the C library's `syscall`, every argument a C long; OSError with the call's
    errno when it fails (OSError's constructor gives ESRCH as ProcessLookupError, EPERM as PermissionError)."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    found = libc.syscall(ctypes.c_long(number), *(ctypes.c_long(arg) for arg in args))
    if found < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(found)


def pidfd_open(pid):
    """pidfd_open(2): os.pidfd_open where this Python has it, else the system call itself."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _syscall(SYS_PIDFD_OPEN, pid, 0)


def pidfd_send_signal(fd, sig):
    """pidfd_send_signal(2), likewise: no siginfo, no flags."""
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, sig)
        return
    _syscall(SYS_PIDFD_SEND_SIGNAL, fd, int(sig), 0, 0)


def pidfd_mode():
    """PIDFD where pidfd_open and pidfd_send_signal work here; FALLBACK where pidfd_open answers ENOSYS (the one failure
    the start-tick fallback covers). Any other failure is raised, as `check` always reported it."""
    try:
        fd = pidfd_open(os.getpid())
    except OSError as e:
        if e.errno == errno.ENOSYS:  # the kernel has no such system call: no descriptor to hold a process by
            return FALLBACK
        raise
    try:
        pidfd_send_signal(fd, 0)
    finally:
        os.close(fd)
    return PIDFD


def usable_mode():
    """The mode this kernel allows, or None after saying why neither does."""
    try:
        return pidfd_mode()
    except (OSError, ImportError, AttributeError) as e:  # ImportError: a Python without ctypes, and no pidfd calls
        say("pidfd_open or pidfd_send_signal failed here: %s (Linux 5.3 or later is needed)" % e)
        return None


def check():
    mode = usable_mode()
    if mode is None:
        return 2
    if mode == FALLBACK:  # named for the run's evidence; where pidfd works nothing is printed, as it never was
        print(FALLBACK_LINE)
        sys.stdout.flush()
    return 0


def start_time(pid):
    """Field 22 of /proc/PID/stat (starttime, clock ticks after boot), or None if unreadable."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            stat = f.read().decode("ascii", "replace")
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2 :].split()  # fields 3.. follow the last ')'; comm may hold spaces
    return fields[19] if len(fields) > 19 else None


def signal_recorded(pid, start, sig):
    if pid < 1:
        return 1
    try:
        fd = pidfd_open(pid)
    except ProcessLookupError:
        return 1
    try:
        if start_time(pid) != start:  # another process holds PID now (or none we may read): not ours to signal
            return 1
        pidfd_send_signal(fd, sig)
    except (ProcessLookupError, PermissionError):
        return 1
    finally:
        os.close(fd)
    return 0


def run(cmd):
    state = {"pidfd": None, "pid": None}
    pending = []

    def deliver(sig):
        try:
            pidfd_send_signal(state["pidfd"], sig)
            say("%s sent to the container's starter (pid %d)" % (signal.Signals(sig).name, state["pid"]))
        except ProcessLookupError:
            pass  # it has ended and been reaped: nothing to signal

    def on_signal(signum, _frame):
        sig = signal.SIGKILL if signum == signal.SIGUSR1 else signum
        if state["pidfd"] is None:
            pending.append(sig)
        else:
            deliver(sig)

    # pidfd_open(2)'s condition for a child's PID to stay ours until we reap it: SIGCHLD not ignored (an ignored
    # disposition survives exec, so set it here, whatever the parent left).
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGUSR1, on_signal)
    if pending:  # stopped before the container started: do not start it
        return 128 + pending[0]
    # posix_spawn: no Python code runs in the child; caught signals revert to their defaults there, ignored ones stay
    # ignored, as they were for the apptainer this process used to exec.
    pid = os.posix_spawnp(cmd[0], cmd, os.environ)
    state["pid"] = pid
    state["pidfd"] = pidfd_open(pid)  # our unreaped child: PID not recycled before this
    for sig in pending:
        deliver(sig)
    _, status = os.waitpid(pid, 0)  # reaps it; a signal arriving meanwhile is forwarded, then the wait resumes
    os.close(state["pidfd"])
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def uids(pid):
    """(real, effective) user id of PID from /proc/PID/status, or None if it cannot be read."""
    try:
        with open("/proc/%d/status" % pid, "rb") as f:
            lines = f.read().decode("ascii", "replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("Uid:"):
            fields = line.split()[1:3]
            return (int(fields[0]), int(fields[1])) if len(fields) == 2 and all(x.isdigit() for x in fields) else None
    return None


def signal_by_tick(pid, start, sig):
    """signal_recorded where pidfd_open answers ENOSYS: the same exit codes. PID's uids first, then its start tick,
    last, then os.kill at once, then the tick again (the module's docstring states the window that leaves)."""
    if pid <= 1:  # init, or no process of ours
        return 1
    mine = os.getuid()
    if uids(pid) != (mine, mine):  # another user's process, or none we may read: not ours to signal
        return 1
    if start_time(pid) != start:  # another process holds PID now (or none we may read): not ours to signal
        return 1
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return 1
    after = start_time(pid)
    if after is not None and after != start:
        say("pid %d now shows start tick %s, not %s: it may have been taken over" % (pid, after, start))
    return 0


def kill_child(pid, sig):
    """os.kill(pid, sig) only while PID is still this process's own child that nobody has waited for (the kernel's word:
    os.waitid with WNOWAIT, which does not reap; ChildProcessError once it was reaped, or if PID never was our child),
    so a PID given to another process after the child's end is never signalled. Return whether it was sent."""
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    os.kill(pid, sig)
    return True


def run_by_tick(cmd):
    """run() where pidfd_open answers ENOSYS: the same start, the same forwarding (SIGTERM as it is, SIGUSR1 as
    SIGKILL), the same wait and exit status, the child signalled with kill_child instead of through a pidfd. run()
    itself is not touched, so a kernel with pidfd_open runs exactly the code it ran before."""
    state = {"pid": None}
    pending = []

    def deliver(sig):
        if kill_child(state["pid"], sig):
            say("%s sent to the container's starter (pid %d)" % (signal.Signals(sig).name, state["pid"]))

    def on_signal(signum, _frame):
        sig = signal.SIGKILL if signum == signal.SIGUSR1 else signum
        if state["pid"] is None:
            pending.append(sig)
        else:
            deliver(sig)

    signal.signal(signal.SIGCHLD, signal.SIG_DFL)  # an ignored SIGCHLD would reap the child without our wait
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGUSR1, on_signal)
    if pending:  # stopped before the container started: do not start it
        return 128 + pending[0]
    pid = os.posix_spawnp(cmd[0], cmd, os.environ)
    state["pid"] = pid  # from here a stop is forwarded at once; one that came before it is forwarded now
    for sig in pending:
        deliver(sig)
    _, status = os.waitpid(pid, 0)  # reaps it; a signal arriving meanwhile is forwarded, then the wait resumes
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def main(argv):
    if len(argv) >= 3 and argv[0] == "run" and argv[1] == "--":
        mode = usable_mode()
        if mode is None:
            return 2
        if mode == FALLBACK:
            say(FALLBACK_NOTE)  # into run.log, which the run keeps
            return run_by_tick(argv[2:])
        return run(argv[2:])
    if len(argv) == 4 and argv[0] == "signal" and argv[1].isdigit() and argv[2].isdigit() and argv[3] in SIGNALS:
        mode = usable_mode()
        if mode is None:
            say("no pidfd support here: not signalling")
            return 2
        if mode == FALLBACK:
            say(FALLBACK_NOTE)
            return signal_by_tick(int(argv[1]), argv[2], SIGNALS[argv[3]])
        return signal_recorded(int(argv[1]), argv[2], SIGNALS[argv[3]])
    if argv == ["check"]:
        return check()
    say("usage: supervise.py run -- CMD [ARG...] | signal PID START TERM|USR1|KILL | check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

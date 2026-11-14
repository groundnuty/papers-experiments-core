"""supervise.py: the service process of a launch, and the only way the bundle signals it.

  run -- CMD ARG...        run CMD (run_service's `apptainer exec ...`) as this process's child, forward SIGTERM to it,
                           turn SIGUSR1 into SIGKILL for it, and exit with its status once it ends
  signal PID START SIG     send SIG (TERM, USR1 or KILL) to PID only if it is still the process that started at clock
                           tick START (field 22 of /proc/PID/stat); exit 0 if sent, 1 if PID is gone or is another
                           process
  check                    exit 0 if this Python and kernel have pidfd_open and pidfd_send_signal (Linux 5.3 and 5.1)

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
"""

import os
import signal
import sys

SIGNALS = {"TERM": signal.SIGTERM, "USR1": signal.SIGUSR1, "KILL": signal.SIGKILL}


def say(msg):
    sys.stderr.write("hpcop: supervise: %s\n" % msg)
    sys.stderr.flush()


def pidfd_supported():
    return hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


def check():
    if not pidfd_supported():
        say(
            "this Python has no os.pidfd_open or signal.pidfd_send_signal (Python 3.9 or later, built on Linux 5.3 "
            "or later)"
        )
        return 2
    try:
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
    except OSError as e:
        say("pidfd_open or pidfd_send_signal failed here: %s (Linux 5.3 or later is needed)" % e)
        return 2
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
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return 1
    try:
        if start_time(pid) != start:  # another process holds PID now (or none we may read): not ours to signal
            return 1
        signal.pidfd_send_signal(fd, sig)
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
            signal.pidfd_send_signal(state["pidfd"], sig)
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
    state["pidfd"] = os.pidfd_open(pid)  # our unreaped child: PID not recycled before this
    for sig in pending:
        deliver(sig)
    _, status = os.waitpid(pid, 0)  # reaps it; a signal arriving meanwhile is forwarded, then the wait resumes
    os.close(state["pidfd"])
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def main(argv):
    if len(argv) >= 3 and argv[0] == "run" and argv[1] == "--":
        if check() != 0:
            return 2
        return run(argv[2:])
    if len(argv) == 4 and argv[0] == "signal" and argv[1].isdigit() and argv[2].isdigit() and argv[3] in SIGNALS:
        if not pidfd_supported():
            say("no pidfd support here: not signalling")
            return 2
        return signal_recorded(int(argv[1]), argv[2], SIGNALS[argv[3]])
    if argv == ["check"]:
        return check()
    say("usage: supervise.py run -- CMD [ARG...] | signal PID START TERM|USR1|KILL | check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

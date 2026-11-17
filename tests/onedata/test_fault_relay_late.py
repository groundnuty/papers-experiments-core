"""O58: a verb the fault relay answered `timed_out` can still be queued at the Slurm controller, so the relay stamps
each verb's steps with a tag of its own, and after a timed-out verb it starts a watch that cancels any step of that
exact name and writes what it saw to its journal (`late-steps.jsonl`, in the relay's directory).

The relay runs in this process over a fake runner and a recording watcher; what the watch does with Slurm is
`tests/deploy/test_hpc_late_steps.py`'s.
"""

import errno
import json
import re
import stat
import subprocess

from expcore_onedata.fault_relay_late import (
    JOURNAL,
    LATE_POLL_S,
    LATE_WINDOW_S,
    journal_path,
    step_tag,
    watch_argv,
)
from fault_support import SERVICES, FakeClock
from hpc_support import write_state

from expcore_onedata.compose import CommandResult, DriverError
from expcore_onedata.fault_hpc import Request
from expcore_onedata.fault_relay import Relay, RelayClient, relay_dir

SERVICE = SERVICES["beta"]
PREFIX = ["/opt/venv/bin/python", "-I", "-B", "hpcfed.py", "--layout", "LAYOUT", "--root", "ROOT"]
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "HOME": "/home/someone"}
TAG = re.compile(r"[0-9a-f]{12}")


class Verbs:
    """The federation's verbs behind the relay: each command recorded, each answered by `behaviour`."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda argv: CommandResult(0, '{"running": true}\n'))
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, timeout_s):
        self.calls.append((list(argv), timeout_s))
        return self.behaviour(argv)


def hangs(argv):
    raise subprocess.TimeoutExpired(argv, 1)


class Watchers:
    """The relay's way to start a watch, recorded with what its journal held at that moment."""

    def __init__(self, root, fail=False):
        self.root, self.fail, self.started = root, fail, []

    def __call__(self, argv, env):
        try:
            held = journal_path(self.root).read_text(encoding="utf-8").splitlines()
        except OSError:
            held = []
        self.started.append({"argv": list(argv), "env": dict(env), "journal": [json.loads(line) for line in held]})
        if self.fail:
            raise OSError(errno.EAGAIN, "no process to spare")
        return type("Handle", (), {"poll": staticmethod(lambda: 0)})()


def lines_of(root):
    path = journal_path(root)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def setup(tmp_path, behaviour=None, fail=False):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    write_state(root, tuple(SERVICES.values()))
    verbs, watchers = Verbs(behaviour), Watchers(root, fail)
    relay = Relay(root, PREFIX, runner=verbs, environ=ENVIRONMENT, watcher=watchers)
    return root, relay, verbs, watchers


def ask(root, relay, request, timeout_s=10):
    """One request through a client whose pause serves the relay once; its end, or the exception it raised."""
    clock = FakeClock()

    def pause(seconds):
        clock.sleep(seconds)
        relay.serve_once()

    client = RelayClient(root, wait_s=5, clock=clock, sleep=pause)
    try:
        return client(request, timeout_s=timeout_s)
    except (subprocess.TimeoutExpired, OSError, DriverError) as error:
        return error


def test_o58_the_relay_stamps_every_verb_with_a_tag_of_its_own_that_names_its_steps(tmp_path):
    """O58: the verb's argv carries `--step-tag <12 hex>` before the verb, derived from the relay's nonce and the
    request's id: unique to the request, so the verb's steps are named by something no other request or hand-run verb
    can share, and the relay knows their names."""
    root, relay, verbs, _ = setup(tmp_path)
    with relay.holding():
        ask(root, relay, Request("status", SERVICE))
        ask(root, relay, Request("status", SERVICE))
    (first, _), (second, _) = verbs.calls
    for argv in (first, second):
        assert argv[: len(PREFIX)] == PREFIX
        assert argv[len(PREFIX)] == "--step-tag"
        assert TAG.fullmatch(argv[len(PREFIX) + 1])
        assert argv[len(PREFIX) + 2 :] == ["status", SERVICE]
    assert first[len(PREFIX) + 1] != second[len(PREFIX) + 1]


def test_o58_the_tag_depends_on_the_relay_and_the_request_and_on_nothing_a_client_chooses_alone():
    """O58: another relay or another request id gives another tag; the same pair always the same one."""
    key, other = "a" * 32, "b" * 32
    assert step_tag("1" * 32, key) == step_tag("1" * 32, key)
    assert step_tag("1" * 32, key) != step_tag("1" * 32, other)
    assert step_tag("1" * 32, key) != step_tag("2" * 32, key)
    assert TAG.fullmatch(step_tag("1" * 32, key))


def test_o58_a_verb_that_times_out_is_answered_timed_out_and_a_watch_is_opened_before_it_starts(tmp_path):
    """O58: the answer stays `timed_out` (the injector still reads the outcome as unknown); the relay writes a
    `watch_opened` line (0600, to disk) and then starts the watch with the verb's own tag, its request id, the
    journal's path and the window, so a relay that dies in the window leaves the record that a watch was owed."""
    root, relay, _, watchers = setup(tmp_path, hangs)
    with relay.holding():
        end = ask(root, relay, Request("stop", SERVICE), timeout_s=3)
        assert isinstance(end, subprocess.TimeoutExpired)  # no answer but `timed_out`: unknown, never done
        (started,) = watchers.started
    (opened,) = started["journal"]
    assert opened["event"] == "watch_opened"
    assert (opened["verb"], opened["service"], opened["window_s"]) == ("stop", SERVICE, LATE_WINDOW_S)
    tag, key = opened["tag"], opened["request"]
    assert TAG.fullmatch(tag)
    assert re.fullmatch(r"[0-9a-f]{32}", key)
    assert started["argv"] == watch_argv(
        PREFIX, tag, "stop", SERVICE, key, journal_path(root), LATE_WINDOW_S, LATE_POLL_S
    )
    assert started["env"] == ENVIRONMENT
    assert stat.S_IMODE(journal_path(root).stat().st_mode) == 0o600
    assert journal_path(root) == relay_dir(root) / JOURNAL


def test_o58_the_watch_is_told_the_tag_the_verb_ran_with(tmp_path):
    """O58: the tag the watch looks for is the one in the verb's argv, so it names exactly that verb's steps."""
    root, relay, verbs, watchers = setup(tmp_path, hangs)
    with relay.holding():
        ask(root, relay, Request("exec", SERVICE, "restart_link"), timeout_s=3)
    ((argv, _),) = verbs.calls
    tag = argv[len(PREFIX) + 1]
    watch = watchers.started[0]["argv"]
    assert watch[len(PREFIX) : len(PREFIX) + 2] == ["--step-tag", tag]
    assert "late-steps" in watch
    assert watch[watch.index("--for-verb") + 1] == "exec"


def test_o58_a_verb_that_fails_after_it_may_have_run_opens_a_watch_too(tmp_path):
    """O58: any end that leaves the outcome unknown (output the relay could not decode, say) is answered `timed_out`
    and its steps are watched for the same as a verb at its bound."""

    def broken(argv):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    root, relay, _, watchers = setup(tmp_path, broken)
    with relay.holding():
        ask(root, relay, Request("status", SERVICE))
    assert len(watchers.started) == 1


def test_o58_a_verb_that_ends_within_its_bound_opens_no_watch_and_is_answered_as_before(tmp_path):
    """O58: nothing changes for a verb that answers in time: its end, no journal and no watch."""
    root, relay, _, watchers = setup(tmp_path, lambda argv: CommandResult(0, "ok\n", "note\n"))
    with relay.holding():
        end = ask(root, relay, Request("status", SERVICE))
    assert end == CommandResult(0, "ok\n", "note\n")
    assert watchers.started == []
    assert lines_of(root) == []


def test_o58_a_verb_that_could_not_be_started_and_a_request_refused_open_no_watch(tmp_path):
    """O58: where nothing ran (the process could not be started, a refused request) there are no steps to watch."""

    def cannot_start(argv):
        raise OSError(errno.ENOENT, "no such file")

    root, relay, _, watchers = setup(tmp_path, cannot_start)
    with relay.holding():
        assert isinstance(ask(root, relay, Request("status", SERVICE)), Exception)  # not_run: a refusal
    assert watchers.started == []
    assert lines_of(root) == []


def test_o58_a_watch_that_cannot_be_started_is_journalled_and_the_answer_stays_timed_out(tmp_path):
    """O58: no process to start the watch with: the journal says so (`watch_failed`, after `watch_opened`), the answer
    is still `timed_out`, and the relay serves on."""
    root, relay, verbs, _ = setup(tmp_path, hangs, fail=True)
    with relay.holding():
        end = ask(root, relay, Request("stop", SERVICE), timeout_s=3)
        assert isinstance(end, subprocess.TimeoutExpired)
        assert [line["event"] for line in lines_of(root)] == ["watch_opened", "watch_failed"]
        verbs.behaviour = lambda argv: CommandResult(0, "ok\n")
        assert ask(root, relay, Request("status", SERVICE)) == CommandResult(0, "ok\n")


def test_o58_an_unwritable_journal_never_keeps_the_watch_from_starting(tmp_path):
    """O58: the watch is the guard and the journal its record: a journal that cannot be written (a directory where the
    file should be) still lets the watch start."""
    root, relay, _, watchers = setup(tmp_path, hangs)
    with relay.holding():
        journal_path(root).mkdir()
        ask(root, relay, Request("stop", SERVICE), timeout_s=3)
    assert len(watchers.started) == 1


def test_o58_the_window_covers_twice_the_controllers_message_timeout():
    """O58: the site's controller stalls for up to about 200 s before it answers a step's creation, and a late step can
    follow a second wait: the watch lasts twice that, counted from the bound."""
    assert LATE_WINDOW_S >= 2 * 200

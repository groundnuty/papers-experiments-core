"""O56: what `/proc` says of a process, parsed strictly, so a cut or foreign file is unknown and never a number."""

from expcore_onedata.sandbox.apptainer import procs as P

STAT = "4242 (python3 (cell)) S 4200 4242 4242 0 -1 4194560 " + " ".join(["0"] * 12) + " 987654 0 0"


def test_stat_reads_the_fields_after_a_command_name_with_spaces_and_parentheses():
    assert P.parse_stat(STAT) == ("python3 (cell)", "S", 4200, "987654")


def test_a_cut_stat_is_not_read():
    assert P.parse_stat(STAT[:40]) is None
    assert P.parse_stat("no parentheses at all") is None


def test_rss_is_anonymous_and_shared_memory_and_nothing_file_backed():
    status = "Name:\tpython3\nRssAnon:\t  2048 kB\nRssFile:\t 99999 kB\nRssShmem:\t  1024 kB\nUid:\t1000\t1000\n"
    assert P.parse_rss(status) == 3072 * 1024
    assert P.parse_uid(status) == 1000


def test_rss_with_a_line_missing_or_not_in_kb_is_unknown():
    assert P.parse_rss("RssAnon:\t2048 kB\n") is None
    assert P.parse_rss("RssAnon:\t2048 MB\nRssShmem:\t0 kB\n") is None


def test_a_namespace_link_is_one_only_in_its_kernel_form():
    assert P.namespace("pid:[4026532001]") == "pid:[4026532001]"
    assert P.namespace("net:[4026532001]") is None


def test_tags_are_matched_as_an_environment_entry_s_start_or_in_an_argument():
    environ = b"PATH=/usr/bin\0EXPCORE_SANDBOX=run-1-abcd1234\0"
    cmdline = b"timeout\0-s\0KILL\0python3\0nonce-xyz\0"
    found = P.tags_in(cmdline, environ, ["EXPCORE_SANDBOX=run-1-", "nonce-xyz", "absent"])
    assert found == frozenset({"EXPCORE_SANDBOX=run-1-", "nonce-xyz"})
    assert P.tags_in(cmdline, b"X=EXPCORE_SANDBOX=run-1-\0", ["EXPCORE_SANDBOX=run-1-"]) == frozenset()


def test_the_cgroup_path_is_the_v2_line_alone():
    assert P.cgroup_path("0::/system.slice/job_1/step_0/user/task_0\n") == "/system.slice/job_1/step_0/user/task_0"
    assert P.cgroup_path("12:memory:/job\n") is None


def test_o56_rss_anon_alone_is_read_and_a_zombie_has_none():
    """O56: the cgroup's `anon` is compared with each process's RssAnon alone, never its shared memory."""
    assert P.parse_anon("RssAnon:\t  2048 kB\nRssShmem:\t 1024 kB\n") == 2048 * 1024
    assert P.parse_anon("Name:\tpython3\nState:\tZ (zombie)\n") is None
    assert P.parse_anon("RssAnon:\t2048 MB\n") is None


def test_o56_a_memory_stat_value_is_read_by_its_key_alone():
    text = "anon 536870912\nfile 4096\nanon_thp 0\nshmem 1048576\n"
    assert P.stat_value(text, "anon") == 536870912
    assert P.stat_value(text, "shmem") == 1048576
    assert P.stat_value(text, "kernel") is None
    assert P.stat_value("anon lots\n", "anon") is None


def test_o56_a_process_is_in_a_cgroup_at_it_or_below_and_never_beside_it():
    root = "/system.slice/job_1/step_0"
    assert P.in_cgroup(root, root)
    assert P.in_cgroup(root + "/user/task_0", root)
    assert not P.in_cgroup("/system.slice/job_1/step_0_extern", root)
    assert not P.in_cgroup("/system.slice/job_1/step_1/user/task_0", root)
    assert not P.in_cgroup(None, root)
    assert P.in_cgroup("/anything", "/")


def test_o56_children_are_read_as_pids_or_not_at_all():
    assert P.parse_children("186509 186510 ") == {186509, 186510}
    assert P.parse_children("") == set()
    assert P.parse_children("186509 x") is None


def test_o56_a_child_proc_does_not_show_while_a_process_holds_it_is_hidden():
    """O56: probe F's case: the visible parent's children list a pid whose /proc entry is not shown."""
    assert P.hidden_children(lambda: {11, 12}, lambda pid: pid == 11, lambda pid: True) == [12]


def test_o56_a_child_that_ended_between_the_reads_is_never_hidden():
    """O56: a child that exits and is reaped after its parent's children were read has no entry either: it is not
    there for `exists`, or not listed by the second read, and is no hidden process."""
    assert P.hidden_children(lambda: {12}, lambda pid: False, lambda pid: False) == []
    reads = iter([{12}, set()])
    assert P.hidden_children(lambda: next(reads), lambda pid: False, lambda pid: True) == []


def test_o56_a_child_shown_by_the_second_look_is_not_hidden():
    looks = iter([False, True])
    assert P.hidden_children(lambda: {12}, lambda pid: next(looks), lambda pid: True) == []


def test_o56_children_that_cannot_be_read_are_unknown_never_none():
    assert P.hidden_children(lambda: None, lambda pid: False, lambda pid: True) is None
    reads = iter([{12}, None])
    assert P.hidden_children(lambda: next(reads), lambda pid: False, lambda pid: True) is None


def test_o56_a_pid_whose_existence_cannot_be_told_is_taken_as_a_process():
    assert P.hidden_children(lambda: {12}, lambda pid: False, lambda pid: None) == [12]

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

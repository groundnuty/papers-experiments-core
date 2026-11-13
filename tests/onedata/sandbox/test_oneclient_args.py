"""What a study may pass to the sidecar's oneclient, and what a run's metadata keeps of it (O36)."""

import hashlib

import pytest

from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.oneclient_args import (
    LONG_OPTIONS,
    UNKNOWN,
    extra_arg_problem,
    mount_problem,
    option_name,
    short_letters,
)
from expcore_onedata.sandbox.settings import MountSettings


@pytest.mark.parametrize(
    "argument",
    [
        "--force-proxy-io",
        "--force-direct-io",
        "--force-proxy-io=true",
        "--force-direct-io=1",
        "--force-p",  # boost takes any unique prefix of a long option
        "--force-direct",
        "--force-",
    ],
)
def test_o36_a_force_io_option_or_an_abbreviation_of_one_is_refused_as_an_extra_argument(argument):
    """O36: the I/O mode is `io_mode` alone: oneclient given a second force option, or the other one, has two."""
    assert "io_mode" in extra_arg_problem(argument)


@pytest.mark.parametrize(
    "argument",
    ["--config", "--config=/etc/other.conf", "--conf", "--co", "-c", "-cother.conf", "-fc", "-fdc/x", "-c/path"],
)
def test_o36_the_config_file_option_in_any_spelling_is_refused_as_an_extra_argument(argument):
    """O36: the sidecar always passes `--config /dev/null`; a study's own would be parsed instead, or in addition."""
    assert "config" in extra_arg_problem(argument)


@pytest.mark.parametrize(
    "argument",
    [
        "ONECLIENT_CONFIG=/x",
        "ONECLIENT_OVERRIDE=a:b:c",
        "oneclient_insecure=1",
        "ONECLIENT_ACCESS_TOKEN=x",
        "ONECLIENT_=",
    ],
)
def test_o36_an_environment_setting_of_oneclient_is_refused_as_an_extra_argument(argument):
    """O36: the environment is closed: no variable of oneclient's reaches it through the study's arguments."""
    assert "environment" in extra_arg_problem(argument)


@pytest.mark.parametrize("argument", ["--insecure", "--insecure=true", "--insec", "-i", "-fi", "-if"])
def test_the_tls_flag_in_any_spelling_is_refused_as_an_extra_argument(argument):
    assert "TLS" in extra_arg_problem(argument)


@pytest.mark.parametrize(
    "argument",
    [
        "--provider-timeout",
        "45",
        "--log-dir=/somewhere/private",
        "-l/var/lib/ci/private",  # a short option's attached value may hold any letter
        "-v3",
        "max_read=131072",  # a value of `-o`, in the form of an assignment
        "--communicator-pool-size=3",  # starts as `--co`, but is not a prefix of `--config`
        "ro,allow_other",
    ],
)
def test_the_arguments_a_study_may_pass_are_not_refused(argument):
    assert extra_arg_problem(argument) is None


def test_a_short_option_with_a_value_takes_the_rest_of_the_word_as_its_value():
    """`-fc/x` is the switch `-f` and the option `-c`; `-l/path/ci` is `-l` and its value, which holds a `c`
    and an `i`."""
    assert short_letters("-fc/x") == "fc"
    assert short_letters("-l/path/ci") == "l"
    assert short_letters("-fdi") == "fdi"
    assert short_letters("-Z") == ""  # OneS3's, which oneclient does not have
    assert short_letters("--long") == ""
    assert short_letters("plain") == ""


@pytest.mark.parametrize("mode", [None, "", "auto", "detect", "x", 1])
def test_o36_a_mount_without_a_known_mode_is_a_problem(mode):
    """O36: no word stands for oneclient's own detection."""
    assert "io_mode" in mount_problem(mode, ())


def test_o36_an_extra_argument_that_sets_what_the_core_declares_is_a_problem_of_the_mount():
    assert mount_problem("proxy", ("--provider-timeout", "30")) is None
    assert "extra_args" in mount_problem("proxy", ("--provider-timeout", "30", "--force-direct-io"))


@pytest.mark.parametrize(
    "flag",
    ["--force-direct-io=private-x", "--force-p", "--config=private-x", "-cprivate-x", "ONECLIENT_CONFIG=private-x"],
)
def test_o36_settings_built_in_code_are_held_to_what_the_loader_holds_a_file_to(flag):
    """O36: `MountSettings(io_mode="proxy", extra_args=("--force-direct-io",))` would have given oneclient both force
    options. The settings refuse it where they are built, and the message never holds the argument's value."""
    with pytest.raises(ValueError, match="extra_args") as caught:
        MountSettings("/data", "proxy", ("--provider-timeout", "30", flag))
    assert "private-x" not in str(caught.value)


@pytest.mark.parametrize("mode", [None, "", "auto"])
def test_o36_settings_built_in_code_need_a_known_io_mode(mode):
    with pytest.raises(ValueError, match="io_mode"):
        MountSettings("/data", mode)


@pytest.mark.parametrize(
    "argument", ["--override", "--override=abc:mountPoint:/x", "--overr", "-r", "-rabc:mountPoint:/x", "-fr"]
)
def test_o40_a_storage_override_in_any_spelling_is_refused_as_an_extra_argument(argument):
    """O40: an override changes where the POSIX helper reads (`--override <storage>:mountPoint:<path>`), so only the
    declared storage mapping sets one, in either mode."""
    assert "[sandbox.mount.storages]" in extra_arg_problem(argument)
    assert "[sandbox.mount.storages]" in mount_problem("proxy", (argument,))


@pytest.mark.parametrize("argument", ["--disable-log-buffering", "--disable-log", "--disable-log-buffering=1"])
def test_o40_the_log_buffering_switch_is_the_cores_own_with_direct_io_and_a_studys_with_proxy(argument):
    """O40: with direct I/O the sidecar passes `--disable-log-buffering`, so the start check reads every line the test
    read wrote (glog holds INFO lines up to 30 s otherwise); a second one would make oneclient refuse to start."""
    storages, check = (("0123abcd", "/srv/a"),), "space/f"
    assert "direct" in mount_problem("direct", (argument,), storages, check)
    assert mount_problem("proxy", (argument,)) is None
    assert mount_problem("direct", ("--disable-read-events",), storages, check) is None


def test_o36_oneclients_own_options_are_kept_by_name_and_nothing_else_is():
    """O36: the metadata keeps the name of an option oneclient has, whatever its value, and `<value>` for any other
    word that starts with a dash: such a word may be a value (a path), which is never recorded (O12)."""
    assert option_name("--provider-timeout") == "--provider-timeout"
    assert option_name("--log-dir=/somewhere/private") == "--log-dir"
    assert option_name("-l/attached/private") == "-l"
    assert option_name("45") is None  # a value with no dash is not a name
    assert option_name("--private/path") == UNKNOWN
    assert option_name("--no-such-option=secret") == UNKNOWN
    assert option_name("-/private") == UNKNOWN
    assert option_name("-Zhost") == UNKNOWN
    assert "force-proxy-io" in LONG_OPTIONS
    assert not any(name.startswith("ones3-") for name in LONG_OPTIONS)


def test_o36_a_value_that_starts_with_a_dash_is_never_recorded_verbatim():
    """O36: `["--log-dir", "--private/path"]` is accepted by the loader (the second word is a value), and the metadata
    recorded `--private/path` as if it were an option name. It records `<value>`, and the hash of the whole list."""
    extra = ("--log-dir", "--private/path", "-/another/private", "--provider-timeout=30")
    record = C.extra_args_record(extra)
    assert record["names"] == ["--log-dir", UNKNOWN, UNKNOWN, "--provider-timeout"]
    assert record["sha256"] == hashlib.sha256("\0".join(extra).encode()).hexdigest()
    assert "private" not in str(record)

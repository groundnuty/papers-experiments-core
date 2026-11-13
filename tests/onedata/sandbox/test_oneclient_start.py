"""O36: oneclient's start-up lines against what the sandbox's argv says they must be."""

import pytest
from mount_effective import option_env_names

from expcore_onedata.sandbox import oneclient_start as start

LOG = """Log file created at: 2026/11/13 10:00:00
Running on machine: invented-host
Log line format: [IWEF]mmdd hh:mm:ss.uuuuuu threadid file:line] msg
I1113 10:00:00.100000     1 logging.cc:76] Oneclient version: 25.0.0
I1113 10:00:00.100100     1 logging.cc:79] Verbose logging level: 0
I1113 10:00:00.100200     1 logging.cc:82] Connecting to Oneprovider: provider.invented.test
I1113 10:00:00.100300     1 logging.cc:84] Forced direct IO: 0
I1113 10:00:00.100400     1 logging.cc:85] Forced proxy IO: 1
I1113 10:00:00.100500     1 logging.cc:86] Verify server certificate: 1
I1113 10:00:00.100600     1 logging.cc:87] File read events disabled: 0
I1113 10:00:00.100700     1 logging.cc:89] IO buffered: 1
I1113 10:00:00.100800     1 logging.cc:90] Oneprovider connection timeout [s]: 120
I1113 10:00:00.100900     1 logging.cc:91] Monitoring enabled: 0
I1113 10:00:00.101000     1 logging.cc:109] Read/write perf log enabled: 0
I1113 10:00:00.101100     1 logging.cc:110] Mountpoint: /mnt/oneclient
I1113 10:00:01.000000     1 helpersCache.h:467] Forced proxy IO: 9
"""
OPTIONS = ["-o", "ro,allow_other", "--force-proxy-io", "--config", "/dev/null"]


def expected(options=OPTIONS):
    return start.expected_settings(options, "provider.invented.test", "/mnt/oneclient")


def test_o36_the_start_lines_are_read_by_name_from_the_logging_file_and_the_first_of_a_name_wins():
    """O36: only the `logging.cc` lines are the start-up settings; a later line of another file with the same words, as
    helpersCache's, is not one."""
    found = start.start_settings(LOG)
    assert found["Forced proxy IO"] == "1"
    assert found["Oneprovider connection timeout [s]"] == "120"
    assert found["Mountpoint"] == "/mnt/oneclient"
    assert found["Oneclient version"] == "25.0.0"
    assert len(found) == 12


def test_o36_a_log_that_is_what_the_argv_says_has_no_difference():
    """O36: proxy mode with the certificate checked and every other logged option at oneclient's default."""
    assert start.differences(start.start_settings(LOG), expected()) == []


def test_o36_the_expected_lines_follow_the_argv_for_the_mode_and_the_certificate_check():
    direct = expected(["-o", "ro", "--force-direct-io", "--config", "/dev/null", "--insecure"])
    assert (direct["Forced direct IO"], direct["Forced proxy IO"], direct["Verify server certificate"]) == (
        "1",
        "0",
        "0",
    )
    proxy = expected()
    assert (proxy["Forced direct IO"], proxy["Forced proxy IO"], proxy["Verify server certificate"]) == ("0", "1", "1")


@pytest.mark.parametrize(
    ("old", "new", "name"),
    [
        (
            "Forced direct IO: 0",
            "Forced direct IO: 1",
            "Forced direct IO",
        ),  # an environment or a file set the other mode
        ("Verify server certificate: 1", "Verify server certificate: 0", "Verify server certificate"),
        ("IO buffered: 1", "IO buffered: 0", "IO buffered"),  # a configuration file's `no_buffer`
        ("timeout [s]: 120", "timeout [s]: 5", "Oneprovider connection timeout [s]"),
        ("Monitoring enabled: 0", "Monitoring enabled: 1", "Monitoring enabled"),
        ("Verbose logging level: 0", "Verbose logging level: 3", "Verbose logging level"),
    ],
)
def test_o36_an_option_something_other_than_the_argv_set_is_a_difference_named_by_its_line(old, new, name):
    """O36: a line that does not say what the argv and oneclient's defaults make it say is reported by its name."""
    found = start.start_settings(LOG.replace(old, new))
    assert [d.split(":")[0] for d in start.differences(found, expected())] == [name]


def test_o36_a_start_line_that_is_missing_is_a_difference_and_never_a_pass():
    found = start.start_settings(
        LOG.replace("I1113 10:00:00.100600     1 logging.cc:87] File read events disabled: 0\n", "")
    )
    assert start.differences(found, expected()) == ["File read events disabled: not logged, expected '0'"]
    assert len(start.differences({}, expected())) == len(expected())


def test_o36_a_mount_with_extra_arguments_has_options_this_check_does_not_model():
    with pytest.raises(ValueError, match="extra arguments"):
        start.expected_settings(OPTIONS, "provider.invented.test", "/mnt/oneclient", ("--provider-timeout", "30"))


def test_o36_the_variables_oneclient_reads_an_option_from_are_found_by_name_with_or_without_its_prefix():
    """O36: `ONECLIENT_CONFIG`, `config` and `Oneclient_Override` are inputs of an option; `PATH` and the token's
    own variable's siblings that name no option are not."""
    names = [
        "PATH",
        "HOME",
        "ONECLIENT_ACCESS_TOKEN",
        "ONECLIENT_CONFIG",
        "oneclient_override",
        "PROVIDER_HOST",
        "SSL_CERT_FILE",
    ]
    assert option_env_names(names) == [
        "ONECLIENT_ACCESS_TOKEN",
        "ONECLIENT_CONFIG",
        "PROVIDER_HOST",
        "oneclient_override",
    ]

"""Day-0 checks as code: a registry, a dated report, unknown that is not a pass, and clean-up that is verified (O11)."""

import json

import httpx
import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.day0 import __main__ as cli
from expcore_onedata.day0.registry import REGISTRY, Check, register
from expcore_onedata.day0.report import scrub, write_report
from expcore_onedata.day0.runner import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_UNKNOWN,
    build_context,
    exit_code,
    mutation_allowed,
    run,
    run_checks,
)
from expcore_onedata.testing import BETA_ID, with_placement, write_platform
from expcore_onedata.testing.fake_federation import ZONE

NAMES = ("reachability", "health", "tokens", "content_type", "listing", "read_only", "round_trip")


def edit(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform(tmp_path, fake):
    """A platform whose support set holds both providers, which the fake now reports as supporting the space."""
    fake.supports[BETA_ID] = {"readonly": False}
    directory = tmp_path / "target"
    directory.mkdir()
    return write_platform(directory, edit)


@pytest.fixture
def context(platform, fake, tmp_path, ticker):
    def make(**kw):
        client = OnezoneClient(load_config(platform), http=fake.client_http())
        options = {"client": client, "tcp": lambda host, port, timeout: 1.5, "samples": 3}
        options.update(clock=ticker.clock, sleep=ticker.sleep, now=lambda: 1_800_000_000.0)
        options.update(kw)
        return build_context(platform, tmp_path / "out", **options)

    return make


def load_config(path):
    from expcore_onedata.config import load_platform

    return load_platform(path)


def by_name(entries):
    return {entry.name: entry for entry in entries}


def mutating_calls(fake):
    return [c for c in fake.provider_calls if c[1] in ("POST", "PUT", "DELETE")]


def test_the_registry_holds_the_checks_the_brief_names_and_marks_those_that_change_state():
    assert set(NAMES) <= set(REGISTRY)
    assert {name for name, check in REGISTRY.items() if check.mutates} == {"read_only", "round_trip"}
    assert all(isinstance(check, Check) for check in REGISTRY.values())


def test_a_check_is_registered_once():
    with pytest.raises(ValueError, match="registered twice"):
        register("health", "again")(lambda ctx: None)


def test_the_read_only_checks_pass_against_a_conforming_federation(context, fake):
    fake.add_file("item.bin", b"x")  # a space that holds something: an empty one measures nothing for the listing
    entries = by_name(run_checks(context(), ["reachability", "health", "tokens", "listing"]))
    # the fixed tool-server user holds the baseline only, so the condition that needs more is not checked with it
    assert {n: e.ok for n, e in entries.items()} == {
        "reachability": True,
        "health": True,
        "tokens": None,
        "listing": True,
    }
    assert "not checked" in entries["tokens"].summary
    assert mutating_calls(fake) == []


def test_every_condition_passes_when_the_fixed_user_matches_its_rights(context, fake):
    """Tokens: with a user whose rights equal every condition's, each plane is minted and examined."""
    from expcore_onedata.testing import BASELINE

    fake.grant("u-tools", BASELINE)
    entries = by_name(run_checks(context(), ["tokens"]))
    planes = entries["tokens"].facts["planes"]
    assert planes["data_path"]["examined"] is True
    assert planes["tool_server:read"]["examined"] is True
    assert planes["tool_server:manage"]["examined"] is None
    assert planes["data_path"]["caveats"]["data.readonly"] == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda fake: fake.add_space("sp-outside-0009", "unrelated-space", [BETA_ID]),
            "the token's scope differs from the plane's spaces: has also 1 space the configuration does not declare",
        ),
        (
            lambda fake: fake.admin_privileges.update({"u-tools": ["oz_groups_view"]}),
            "the principal holds Onezone admin privileges: oz_groups_view",
        ),
        (
            lambda fake: fake.clusters.update({"u-tools": ["cl-provider-0001"]}),
            "the principal is an effective member of 1 cluster",
        ),
    ],
    ids=["a member of an outside space", "an admin", "a member of a cluster"],
)
def test_o09_a_writing_conditions_user_beyond_its_plane_fails_the_tokens_check(change, reason, context, fake):
    """O09: the day-0 tokens check mints what a run mints, so a writing condition's fixed user who is a member of a
    space outside the plane or of a provider's cluster, or holds an admin privilege of Onezone, is refused: the check
    fails, naming no space and no cluster, and the read-only planes it has still pass."""
    from expcore_onedata.testing import MANAGE

    fake.grant("u-tools", MANAGE)
    change(fake)
    entries = by_name(run_checks(context(), ["tokens"]))
    planes = entries["tokens"].facts["planes"]
    assert entries["tokens"].ok is False
    assert planes["tool_server:manage"]["examined"] is False
    assert planes["tool_server:manage"]["refusal"] == reason
    assert planes["data_path"]["examined"] is True
    assert "sp-outside-0009" not in json.dumps(entries["tokens"].facts)
    assert "cl-provider-0001" not in json.dumps(entries["tokens"].facts)


def test_a_provider_that_does_not_answer_fails_reachability_without_naming_its_host(context, fake):
    fake.resets.append(["/configuration", 99, BETA_ID])
    entry = by_name(run_checks(context(), ["reachability"]))["reachability"]
    assert entry.ok is False
    assert entry.facts["providers"]["beta"]["configuration"]["probe_ok"] is False
    assert entry.facts["providers"]["alpha"]["configuration"]["probe_ok"] is True
    assert "beta.test" not in json.dumps(entry.facts)


def test_reachability_without_a_tcp_probe_is_unknown_not_a_pass(context):
    entry = by_name(run_checks(context(tcp=None), ["reachability"]))["reachability"]
    assert entry.ok is None


def test_health_fails_when_the_federation_reports_a_provider_that_is_not_declared(context, fake):
    fake.add_provider("prmock0003", "gamma.test")
    entry = by_name(run_checks(context(), ["health"]))["health"]
    assert entry.ok is False


def test_a_listing_is_read_by_the_servers_pages_and_two_at_a_time_and_the_two_agree(context, fake):
    for number in range(5):
        fake.add_file(f"item{number}.bin", b"x")
    fake.page_size = 2
    entry = by_name(run_checks(context(), ["listing"]))["listing"]
    assert entry.ok is True  # whole and paged reads agree, so the walk is exercised and passes
    assert entry.facts["providers"]["alpha"]["pages"] >= 2


def test_an_empty_listing_is_unknown_naming_the_missing_baseline_never_a_pass(context, fake):
    """A study space declared baseline = false holds nothing until a study ingests: a root with no entry has nothing to
    list or to walk page by page, so the listing check reports unknown, which is not a pass, and says why."""
    entry = by_name(run_checks(context(), ["listing"]))["listing"]
    assert entry.ok is None
    assert "no entry on 2 supporting provider(s): alpha, beta" in entry.summary
    assert "no baseline file is in the space" in entry.summary
    assert {row["entries"] for row in entry.facts["providers"].values()} == {0}


def test_a_study_space_with_no_baseline_files_fails_no_check_and_passes_none_that_needs_them(context, fake):
    """The baseline-off study space through the whole run: the checks that read no file of the space are as they are
    on any space (the two that change state work on a scratch file of their own and remove it) and the listing is
    unknown. The content type reads the configured check file, which a baseline-off space lacks: it is unknown when a
    data path is REST (test_o05_the_check_is_a_day0_check_too, and the day-0 health check in test_precampaign.py), and
    unknown here too, where none is and it measures nothing. Nothing fails for want of a file and nothing passes on
    one."""
    assert [n for n in fake.nodes.values() if n.parent is not None] == []
    entries = by_name(run_checks(context(allow_mutation=True)))
    assert {n: e.ok for n, e in entries.items()} == {
        "reachability": True,
        "health": True,
        "tokens": None,  # the fixed tool-server user holds the baseline rights only, as in every run here
        "content_type": None,  # no data path is REST here, so nothing is measured (O08)
        "listing": None,
        "read_only": True,
        "round_trip": True,
    }
    assert [n for n in fake.nodes.values() if n.name.startswith("day0-")] == []


def test_o11_without_consent_the_checks_that_change_state_are_unknown_and_send_nothing(context, fake):
    """O11: a check that would change state does nothing without consent, and reports unknown."""
    entries = by_name(run_checks(context(allow_mutation=False), ["read_only", "round_trip"]))
    assert [e.ok for e in entries.values()] == [None, None]
    assert all("EXPCORE_LIVE_MUTATE=1" in e.summary for e in entries.values())
    assert fake.requests == []


def test_the_consent_is_the_variable_set_to_exactly_one():
    assert mutation_allowed({"EXPCORE_LIVE_MUTATE": "1"})
    assert not mutation_allowed({"EXPCORE_LIVE_MUTATE": "true"})
    assert not mutation_allowed({})


def test_read_only_enforcement_passes_when_every_write_is_refused(context, fake):
    """O05: every provider refuses the read-only token's write as Onedata 25.0 does, 400 posix eacces: a refusal."""
    entry = by_name(run_checks(context(allow_mutation=True), ["read_only"]))["read_only"]
    assert entry.ok is True
    rows = entry.facts["providers"].values()
    assert {(row["status"], row["code"], row["errno"]) for row in rows} == {(400, "posix", "eacces")}
    assert not [c for c in fake.provider_calls if c[1] in ("PUT", "DELETE")]


def test_read_only_enforcement_fails_when_a_write_is_accepted_and_the_write_is_removed(context, fake):
    fake.enforce_readonly = False
    entry = by_name(run_checks(context(allow_mutation=True), ["read_only"]))["read_only"]
    assert entry.ok is False
    assert all(row["cleaned"] for row in entry.facts["providers"].values())
    assert [n.name for n in fake.nodes.values() if n.name.startswith("day0-")] == []


def refusing_the_gates_name_read(fake):
    """A transport on which Onezone answers the gate's read of the space's name with 400 `badValueNotAllowed`, the
    answer the gate's own lookup got on Onedata 25.0 when it asked for a path; everything else is the fake's."""

    def handler(request):
        if request.method == "GET" and request.url.path == f"{ZONE}/spaces/{fake.SPACE_ID}":
            return httpx.Response(400, json={"error": {"id": "badValueNotAllowed", "details": {}}})
        return fake.respond(request)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("cause", ["the gate's lookup", "the space root"])
def test_o08_a_write_that_was_never_sent_makes_read_only_unknown_because_the_probe_could_not_run(
    context, platform, fake, cause
):
    """O08, O05: when the gate's own read of the target's space fails, or the space root cannot be read, no write is
    sent, so nothing was refused or accepted. The check is unknown and says the probe could not run and no write was
    sent, never that a write was not refused with a 401 or 403."""
    options = {"allow_mutation": True}
    if cause == "the gate's lookup":
        options["client"] = OnezoneClient(load_config(platform), http=refusing_the_gates_name_read(fake))
    else:
        fake.statuses = [[f"/spaces/{fake.SPACE_ID}", 99, 503]]
    entry = by_name(run_checks(context(**options), ["read_only"]))["read_only"]
    assert entry.ok is None
    assert "could not run" in entry.summary
    assert "no write was sent" in entry.summary
    assert "not refused" not in entry.summary
    assert {row["sent"] for row in entry.facts["providers"].values()} == {False}
    assert mutating_calls(fake) == []


def test_o08_a_write_answered_neither_way_is_unknown_and_says_it_was_sent(context, fake):
    """O08: a write that was sent and answered with neither an acceptance nor a 401 or 403 (here a 500) is unknown,
    and the summary says the write was sent and how it was answered."""
    fake.statuses = [["/children", 99, 500]]
    entry = by_name(run_checks(context(allow_mutation=True), ["read_only"]))["read_only"]
    assert entry.ok is None
    assert "was sent" in entry.summary
    assert "but 500 internalServerError" in entry.summary
    rows = entry.facts["providers"].values()
    assert {(row["sent"], row["status"], row["code"]) for row in rows} == {(True, 500, "internalServerError")}


@pytest.mark.parametrize(
    ("answer", "refused"),
    [
        ((400, "posix", "eacces"), True),  # how Onedata 25.0 refuses it
        ((400, "posix", "eperm"), None),  # the same id with another errno
        ((400, "posix", None), None),
        ((400, "badValueNotAllowed", None), None),  # another 400
    ],
)
def test_o05_a_write_refused_as_400_posix_eacces_is_a_refusal_and_no_other_400_is(context, fake, answer, refused):
    """O05: Onedata 25.0 refuses a write made with the read-only token as 400 posix with errno eacces, and that exact
    answer is a refusal, so the check passes. Another errno of the same id, none, or another 400 is not: the check is
    unknown, with the answer as it came."""
    fake.readonly_answer = answer
    entry = by_name(run_checks(context(allow_mutation=True), ["read_only"]))["read_only"]
    assert entry.ok is refused
    assert {row["enforced"] for row in entry.facts["providers"].values()} == {refused}
    assert mutating_calls(fake)  # the writes were sent
    assert not [c for c in fake.provider_calls if c[1] in ("PUT", "DELETE")]


def test_o11_an_accepted_write_whose_directory_reads_400_posix_enoent_is_cleaned(context, fake):
    """O11: when a broken enforcement accepts the write, its directory is deleted and read until gone; on Onedata
    25.0 a deleted entry reads 400 posix enoent, which is absence as a 404 is, so the clean-up is confirmed."""
    fake.enforce_readonly = False
    fake.answer_missing_as_25()
    entry, rows = read_only_rows(context)
    assert entry.ok is False  # the write was accepted: that is the failure
    assert {row["cleaned"] for row in rows} == {True}
    assert [n.name for n in fake.nodes.values() if n.name.startswith("day0-")] == []


@pytest.mark.parametrize("answer", [(400, "posix", "eacces"), (400, "badValueNotAllowed", None), (400, None, None)])
def test_o11_an_accepted_write_whose_directory_reads_any_other_400_is_not_cleaned(context, fake, ticker, answer):
    """O11: after the delete, an answer that is neither a 404 nor 400 posix enoent (another errno, another 400, a body
    that is no error object) is not absence. The reads go on to their bound and the directory is reported not cleaned,
    never removed."""
    fake.enforce_readonly = False
    fake.answer_missing_as_25(answer)
    began = ticker.clock()
    entry, rows = read_only_rows(context)
    assert entry.ok is False
    assert {row["cleaned"] for row in rows} == {False}
    assert {row["cleanup_reason"] for row in rows} == {"the entry could not be read to confirm that it is gone"}
    assert ticker.clock() - began >= 2 * 5.0  # each provider's reads went on to their bound, with pauses


def attribute_reads(fake):
    """The reads of the scratch directory itself: a GET of its entry that is not one of the gate's reads."""
    return [r for r in fake.requests if r.method == "GET" and "/fid-" in r.path and not r.query]


def read_only_rows(context):
    entry = by_name(run_checks(context(allow_mutation=True), ["read_only"]))["read_only"]
    return entry, entry.facts["providers"].values()


def test_o11_a_delete_that_answers_204_but_leaves_the_directory_is_not_cleaned(context, fake):
    """O11: the accepted write's directory is deleted as the admin and then read; a DELETE that answers 204 and
    removes nothing leaves it there, so cleaned is False with a reason, and the check is failed whatever else."""
    fake.enforce_readonly = False  # the write is accepted, which creates a directory
    fake.ignore_deletes = True  # and the delete answers 204 without removing it
    entry, rows = read_only_rows(context)
    assert entry.ok is False
    assert {row["cleaned"] for row in rows} == {False}
    assert all("still there" in row["cleanup_reason"] for row in rows)
    assert [n for n in fake.nodes.values() if n.name.startswith("day0-")]  # it really is left


def test_o11_a_delete_that_fails_is_not_cleaned_and_says_why(context, fake):
    """O11: a refused or failed delete is cleaned False with its own reason."""
    fake.enforce_readonly = False
    fake.fail_deletes = True
    _, rows = read_only_rows(context)
    assert {row["cleaned"] for row in rows} == {False}
    assert all(row["cleanup_reason"] for row in rows)


def test_o11_cleaned_is_true_only_when_the_provider_answers_not_found_on_consecutive_reads(context, fake, ticker):
    """O11: after a DELETE the entry is read; two 404s in a row confirm it gone, within a bounded number of reads."""
    fake.enforce_readonly = False
    entry, rows = read_only_rows(context)
    assert entry.ok is False  # the write was accepted
    assert {row["cleaned"] for row in rows} == {True}
    assert all("cleanup_reason" not in row for row in rows)
    assert len(attribute_reads(fake)) == 2 * len(list(rows))  # two confirming reads per provider, none more


def test_o11_the_reads_that_confirm_absence_are_bounded(context, fake, ticker):
    """O11: a directory that never goes is read a bounded number of times, with pauses, and then given up on."""
    fake.enforce_readonly = False
    fake.ignore_deletes = True
    read_only_rows(context)
    from expcore_onedata.day0.mutating import ABSENCE_POLL_S, ABSENCE_POLLS

    providers = 2
    assert len(attribute_reads(fake)) == ABSENCE_POLLS * providers
    assert ticker.slept.count(ABSENCE_POLL_S) == (ABSENCE_POLLS - 1) * providers


def test_o11_the_round_trip_places_confirms_reads_back_evicts_and_cleans_up(context, fake):
    """O11: the scratch file is placed by reads, confirmed, read back, evicted and removed, and removal is verified."""
    entry = by_name(run_checks(context(allow_mutation=True), ["round_trip"]))["round_trip"]
    assert entry.ok is True, entry.summary
    assert entry.facts["steps"] == ["created", "placed", "read back", "evicted"]
    assert entry.facts["held_bytes"] == entry.facts["range_bytes"] == 4096
    assert entry.facts["bytes_match"] is True
    assert entry.facts["cleanup_verified"] is True
    assert [n for n in fake.nodes.values() if n.name.startswith("day0-")] == []
    assert not [c for c in fake.provider_calls if c[1] == "POST" and c[2] == "/transfers" and c[0] == "prmock0001"]


def test_o11_the_round_trip_accepts_held_bytes_that_cover_the_range_read_and_records_both(context, fake):
    """O11: on Onedata 25.0 reading half of the 8 KiB scratch file through the receiver places all of it, and an
    eviction right after completes with nothing evicted. The round trip passes when the block map covers the range
    read, records the bytes held (8192) beside the bytes read (4096) with the held ranges, and evicts again until the
    receiver holds nothing."""
    fake.onthefly_unit = 1 << 20
    fake.evictions_noop_first = 1
    entry = by_name(run_checks(context(allow_mutation=True), ["round_trip"]))["round_trip"]
    assert entry.ok is True, entry.summary
    assert (entry.facts["range_bytes"], entry.facts["held_bytes"]) == (4096, 8192)
    assert entry.facts["held_ranges"] == [[0, 8192]]
    assert entry.facts["eviction_attempts"] == 2
    assert entry.facts["cleanup_verified"] is True


@pytest.mark.parametrize("answer", [(404, "notFound", None), (400, "posix", "enoent")])
def test_o07_the_round_trip_waits_for_the_receiver_to_see_its_scratch_file(context, fake, answer):
    """O07: as on Onedata 25.0, the receiver sees the scratch file written through the data provider only after some
    reads (404 or 400 posix enoent meanwhile). The round trip waits, bounded, before it reads through the receiver,
    and passes."""
    fake.unseen_after_create = {BETA_ID: 3}
    fake.unseen_answer = answer
    fake.answer_missing_as_25()
    entry = by_name(run_checks(context(allow_mutation=True), ["round_trip"]))["round_trip"]
    assert entry.ok is True, entry.summary
    assert entry.facts["steps"] == ["created", "placed", "read back", "evicted"]
    assert entry.facts["cleanup_verified"] is True


def test_o11_a_round_trip_that_raises_still_tears_down_and_says_so(context, fake):
    """O11: a trip that fails after creating the scratch file still removes it, in a finally, and verifies it."""
    fake.onthefly_places = False  # the placement fails after the scratch file exists
    entry = by_name(run_checks(context(allow_mutation=True), ["round_trip"]))["round_trip"]
    assert entry.ok is False
    assert "NotPlaced after created" in entry.summary
    assert entry.facts["cleanup_verified"] is True
    assert [n for n in fake.nodes.values() if n.name.startswith("day0-")] == []


def test_o11_a_scratch_that_cannot_be_removed_fails_the_check_even_when_the_trip_worked(context, fake):
    """O11: clearing is verified; a scratch entry left behind fails the check whatever else passed."""
    fake.fail_deletes = True
    entry = by_name(run_checks(context(allow_mutation=True), ["round_trip"]))["round_trip"]
    assert entry.ok is False
    assert entry.facts["cleanup_verified"] is False
    assert "not removed" in entry.summary


def test_the_round_trip_needs_a_provider_besides_the_data_provider(tmp_path, fake):
    path = write_platform(tmp_path, lambda d: with_placement(d))  # the support set is alpha only
    client = OnezoneClient(load_config(path), http=fake.client_http())
    entry = by_name(
        run_checks(build_context(path, tmp_path / "out", client=client, allow_mutation=True), ["round_trip"])
    )
    assert entry["round_trip"].ok is None
    assert mutating_calls(fake) == []


def test_the_exit_code_separates_pass_fail_and_unknown(context):
    from expcore_onedata.day0.report import Entry

    def entry(ok):
        return Entry("x", "x", False, ok, "", {}, 0.0)

    assert exit_code([entry(True)]) == EXIT_PASS
    assert exit_code([entry(True), entry(None)]) == EXIT_UNKNOWN
    assert exit_code([entry(None), entry(False)]) == EXIT_FAIL


def test_an_unknown_check_name_is_a_usage_error(context):
    with pytest.raises(ConfigError, match="unknown day-0 check"):
        run_checks(context(), ["nope"])


def test_run_writes_a_dated_report_in_json_and_markdown_with_unknown_apart(platform, fake, tmp_path, ticker):
    fake.add_file("item.bin", b"x")
    client = OnezoneClient(load_config(platform), http=fake.client_http())
    code, json_path, md_path = run(
        platform,
        tmp_path / "reports",
        client=client,
        tcp=lambda *a: 2.0,
        now=lambda: 1_800_000_000.0,
        sleep=ticker.sleep,
        clock=ticker.clock,
        allow_mutation=False,
    )
    assert code == EXIT_UNKNOWN
    assert json_path.name == "day0-20270115T080000Z.json"
    assert md_path.name == "day0-20270115T080000Z.md"
    report = json.loads(json_path.read_text())
    # content_type is unknown: no data path is REST, so it measured nothing, which is never a pass (O08)
    assert report["counts"] == {"pass": 3, "fail": 0, "unknown": 4}
    assert [c["name"] for c in report["checks"]] == list(NAMES)
    text = md_path.read_text()
    assert "## Blocked or unknown" in text
    assert "Unknown is not a pass" in text
    assert "| read_only | unknown |" in text


def test_the_report_names_no_host_address_url_space_or_token(platform, fake, tmp_path, ticker):
    """The report holds provider labels and numbers; the deployment's identifiers are scrubbed wherever they appear."""
    client = OnezoneClient(load_config(platform), http=fake.client_http())
    _, json_path, md_path = run(
        platform,
        tmp_path / "reports",
        client=client,
        tcp=lambda *a: 2.0,
        now=lambda: 1_800_000_000.0,
        sleep=ticker.sleep,
        clock=ticker.clock,
        allow_mutation=True,
    )
    text = json_path.read_text() + md_path.read_text()
    for needle in ("alpha.test", "beta.test", "zone.test", "https://", "spmock0001", "study-a-data", "prmock0001"):
        assert needle not in text, needle
    for value in [*fake.issued, fake.admin_token]:
        assert value not in text


def test_scrub_replaces_identifiers_in_keys_and_values_and_leaves_numbers():
    cleaned = scrub({"host-x.example": ["see host-x.example now", 4, None]}, ["host-x.example"])
    assert cleaned == {"<withheld>": ["see <withheld> now", 4, None]}


def test_a_report_is_never_written_over_an_earlier_one(tmp_path):
    report = {
        "utc": "20270101T000000Z",
        "counts": {"pass": 0, "fail": 0, "unknown": 0},
        "checks": [],
        "providers": [],
        "mutation_allowed": False,
        "platform_file_sha256": "0",
    }
    write_report(tmp_path, report)
    with pytest.raises(FileExistsError):
        write_report(tmp_path, report)


def test_no_check_runs_without_a_named_target(capsys, tmp_path):
    assert cli.main([]) == 2
    assert cli.main(["only-a-platform.toml"]) == 2
    assert cli.main(["--out", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "name the platform.toml" in err
    assert not list(tmp_path.iterdir())


def test_a_missing_target_file_is_a_configuration_error_not_a_crash(capsys, tmp_path):
    assert cli.main([str(tmp_path / "missing.toml"), "--out", str(tmp_path / "o")]) == 2
    assert "error:" in capsys.readouterr().err


def test_list_names_the_checks_and_marks_those_that_change_state(capsys):
    assert cli.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "round_trip (changes state)" in out
    assert "reachability:" in out

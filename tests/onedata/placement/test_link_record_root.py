"""O35: the link-check record is the federation's, written in its root, whichever platform file a check ran through.

`up` writes `link-check.json` in the federation's root and an operator's pre-check reads it there. A check made through
a platform file derived elsewhere (the outage check's example, in a work directory) names the federation by its
`[faults] journal`, which `up` makes in its root (`<root>/fault-journal`), so the root is the journal's parent: every
check that needs `[faults]` (a restore's, a heal's reads, and the health gate's routine pre-run check) writes the
root's record, and nothing lands beside the derived file. A check run without `[faults]` declares no root and writes
nothing. A `[faults] journal` whose parent holds no `platform.toml` is refused at load, and so is a relative one in a
platform file beside a `study.toml`, where it would resolve into the study and the root check would pass vacuously.
"""

import json
from functools import partial

import pytest
from fault_support import SERVICES, faults_table
from link_support import BOUND_S, GAMMA_ID, add_providers, three_providers

from expcore.config.loading import ConfigError
from expcore_onedata import fault_serves, fault_settings, health, linkcheck
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.linkcheck import check_links
from expcore_onedata.linkrecord import LOCK, RECORD, record_text
from expcore_onedata.testing import ALPHA_ID, BETA_ID, write_platform
from expcore_onedata.testing.world import with_link_check
from expcore_onedata.tokens import TokenIssuer

STORES = {**SERVICES, "gamma": "store-three"}
SERVICE = STORES["beta"]
OWN_SPACE_ID, OWN_SPACE_NAME, OWN_PREFIX = "sp-own-0010", "own-probe-links", "own-"  # the derived file's probe space
ISSUER_CLOCK = 1_800_000_000.0


@pytest.fixture(autouse=True)
def checks_on_the_tickers_clock(monkeypatch, ticker):
    """Every check runs on the test's `Ticker`, a pair at a time (as `test_link_record` and `test_link_check` do)."""
    monkeypatch.setattr(linkcheck, "WORKERS", 1)
    monkeypatch.setattr(
        fault_serves,
        "check_links",
        partial(check_links, clock=ticker.clock, sleep=ticker.sleep, now=ticker.clock, workers=1),
    )


@pytest.fixture
def fake(fake):
    add_providers(fake)
    fake.add_data_space(OWN_SPACE_ID, OWN_SPACE_NAME, [ALPHA_ID, BETA_ID, GAMMA_ID])  # a probe space of its own
    return fake


def federation_document(data, **faults):
    three_providers(data)
    data["faults"] = faults_table(services=STORES, **faults)


@pytest.fixture
def root(tmp_path):
    """The federation's root as `up` leaves it: its `platform.toml`, with the journal in it (`<root>/fault-journal`)."""
    directory = tmp_path / "root"
    directory.mkdir()
    write_platform(directory, federation_document)
    return directory


@pytest.fixture
def derived(tmp_path, root):
    """A platform file derived in a work directory, as the outage check's README example derives one: its own
    `[link_check]` (its own name, id and prefixes, the same providers) and a `[faults]` table whose journal and Compose
    file are the root's."""
    work = tmp_path / "work"
    work.mkdir()

    def edit(data):
        three_providers(data, link_check=False)
        with_link_check(
            data,
            ["alpha", "beta", "gamma"],
            name=OWN_SPACE_NAME,
            id=OWN_SPACE_ID,
            prefixes=[OWN_PREFIX],
            timeout_s=BOUND_S,
        )
        data["faults"] = faults_table(
            services=STORES, journal=str(root / "fault-journal"), compose_file=str(root / "compose.yaml")
        )

    return write_platform(work, edit)


def probe_through(platform, fake):
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    return provider_serves(config, load_faults(platform, config), client)


def health_through(platform, fake, ticker):
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: ISSUER_CLOCK)
    return PlatformHealth(config, client, issuer, monotonic=ticker.clock, sleep=ticker.sleep, clock=ticker.clock)


def record(directory):
    return json.loads((directory / RECORD).read_text(encoding="utf-8"))


def listing(directory):
    return sorted(path.name for path in directory.iterdir())


def test_o35_the_routine_pre_run_check_writes_the_federations_record_in_ups_bytes(root, fake, ticker):
    """O35: the health gate's routine all-pairs check (`PlatformHealth`, at its first call) writes the record, as a
    restore's and a heal's checks do: the file is `record_text` of the report the check made."""
    gate = health_through(root / "platform.toml", fake, ticker)
    assert not (root / RECORD).exists()
    verdict = gate()
    assert verdict.ok, verdict.detail
    assert gate.links is not None
    assert (root / RECORD).read_text(encoding="utf-8") == record_text(gate.links)
    assert record(root)["ok"] is True
    assert len(record(root)["pairs"]) == 6
    assert (root / LOCK).exists()  # one check at a time makes it, the routine check's among them


def test_o35_a_routine_check_that_finds_a_pair_stuck_writes_its_failing_verdict_over_the_older_pass(root, fake, ticker):
    """O35: the same rules as a restore's check: the latest verdict on disk is the latest check's, a stuck pair's
    failing verdict replaces the pass before it."""
    gate = health_through(root / "platform.toml", fake, ticker)
    assert gate().ok
    assert record(root)["ok"] is True
    fake.links[(ALPHA_ID, BETA_ID)] = "fail"
    ticker.sleep(gate._config.link_check.every_s)  # the next call checks the links again
    assert not gate().ok
    failed = record(root)
    assert failed["ok"] is False
    assert [(p["reader"], p["source"]) for p in failed["pairs"] if not p["ok"]] == [("providers[0]", "providers[1]")]


def test_o35_a_routine_check_that_raises_removes_the_older_record(root, fake, ticker, monkeypatch):
    """O35: the same rules: a check that cannot answer leaves no older verdict standing, and still fails closed."""
    gate = health_through(root / "platform.toml", fake, ticker)
    assert gate().ok
    assert (root / RECORD).exists()

    def broken(*args, **kwargs):
        raise RuntimeError("the check could not run")

    monkeypatch.setattr(health, "check_links", broken)
    ticker.sleep(gate._config.link_check.every_s)
    verdict = gate()
    assert not verdict.ok
    assert "RuntimeError" in verdict.detail
    assert not (root / RECORD).exists()


def test_o35_a_restore_check_through_a_derived_file_writes_the_roots_record_and_nothing_beside_the_derived_file(
    root, derived, fake
):
    """O35: the derived file lies in another directory and names the federation by its `[faults] journal`: the record
    goes where `up` writes it, in the root, and the derived file's directory gains no record and no lock."""
    before = listing(derived.parent)
    matrix = probe_through(derived, fake).matrix(SERVICE)
    assert matrix is not None
    assert matrix.ok
    assert record(root)["ok"] is True
    assert (root / LOCK).exists()
    assert listing(derived.parent) == before


def test_o35_the_routine_check_through_a_derived_file_writes_the_roots_record_and_nothing_beside_the_derived_file(
    root, derived, fake, ticker
):
    """O35: the gate's routine check, the same: through the derived file it writes the root's record."""
    before = listing(derived.parent)
    gate = health_through(derived, fake, ticker)
    verdict = gate()
    assert verdict.ok, verdict.detail
    assert (root / RECORD).read_text(encoding="utf-8") == record_text(gate.links)
    assert listing(derived.parent) == before


def test_o35_a_derived_files_record_is_in_the_format_the_roots_readers_read_with_the_checks_own_pairs(
    root, derived, fake
):
    """O35: a check with its own `[link_check]` (name, id and prefixes) writes the same report format `up` writes: the
    keys a reader takes (`ok`, `pairs`, `window`), each pair by position, the check's own transfers and window, and
    nothing that names its probe space (the report never holds a configured name, an id or a prefix, O12)."""
    probe_through(root / "platform.toml", fake).matrix(SERVICE)
    standard = record(root)
    probe_through(derived, fake).matrix(SERVICE)
    own = record(root)
    assert set(own) == set(standard) == {"version", "ok", "detail", "window", "transfers", "pairs"}
    assert own["version"] == standard["version"]
    assert own["ok"] is True
    assert [(p["reader"], p["source"]) for p in own["pairs"]] == [(p["reader"], p["source"]) for p in standard["pairs"]]
    assert set(own["pairs"][0]) == set(standard["pairs"][0])
    assert own["window"]["end_epoch_s"] > standard["window"]["end_epoch_s"]  # the derived check's own record
    text = (root / RECORD).read_text(encoding="utf-8")
    assert not any(value in text for value in (OWN_SPACE_NAME, OWN_SPACE_ID, OWN_PREFIX))


def test_o35_two_files_of_one_federation_write_one_record_and_one_lock(root, derived, fake, ticker):
    """O35: the root's file and a derived file are checks of one federation: one record, one lock, ordered together
    (`up`, which writes at `<root>`, takes the same lock)."""
    probe_through(root / "platform.toml", fake).matrix(SERVICE)
    probe_through(derived, fake).matrix(SERVICE)
    assert sorted(name for name in listing(root) if name.startswith("link-check")) == sorted([RECORD, LOCK])


def test_o35_a_check_without_faults_declares_no_root_and_writes_nothing(tmp_path, fake, ticker):
    """O35: no `[faults]`, no journal, so no federation root is declared: the routine check answers as it always did
    and writes no record and no lock, not beside the platform file either."""
    directory = tmp_path / "plain"
    directory.mkdir()
    platform = write_platform(directory, three_providers)
    before = listing(directory)
    gate = health_through(platform, fake, ticker)
    assert gate().ok
    assert gate.links is not None
    assert gate.links.ok
    assert listing(directory) == before


def test_o35_a_journal_whose_parent_holds_no_platform_file_is_refused_at_load(tmp_path, fake):
    """O35: the root is the journal's parent, so it must hold the federation's platform.toml: a journal elsewhere is
    refused loudly at load, naming the file and the key, never a path."""
    platform = write_platform(
        tmp_path,
        lambda d: (
            three_providers(d),
            d.__setitem__("faults", faults_table(services=STORES, journal="state/fault-journal")),
        ),
    )
    (tmp_path / "state").mkdir()
    config = load_platform(platform)
    with pytest.raises(ConfigError, match=r"platform\.toml\.faults: key 'journal'") as refused:
        load_faults(platform, config)
    assert "state" not in str(refused.value).split("key 'journal'")[1]
    (tmp_path / "state" / "platform.toml").write_text("# the federation's own\n", encoding="utf-8")
    assert load_faults(platform, config).journal == (tmp_path / "state" / "fault-journal").resolve()


STUDY_DIRECTORY = "study"
RELATIVE_JOURNALS = ("fault-journal", "../root/fault-journal")  # the template's, and one that points at the root


def platform_in_a_study(tmp_path, **faults):
    """The federation's platform.toml, copied beside a `study.toml` (a study directory)."""
    study = tmp_path / STUDY_DIRECTORY
    study.mkdir()
    (study / "study.toml").write_text("# a study directory is the one that holds study.toml\n", encoding="utf-8")
    return write_platform(study, lambda d: federation_document(d, **faults))


@pytest.mark.parametrize("journal", RELATIVE_JOURNALS)
def test_o35_a_relative_journal_beside_study_toml_is_refused_naming_the_absolute_form(tmp_path, root, fake, journal):
    """O35: a platform.toml copied beside study.toml, with a relative `[faults] journal`, resolves the journal into the
    study directory, whose parent then holds the copy, so the root check would pass vacuously and the lock and the
    journal would land in the study. Refused at load, whatever the relative path says, naming the key and the absolute
    form to write (never a path of this host)."""
    platform = platform_in_a_study(tmp_path, journal=journal)
    config = load_platform(platform)
    with pytest.raises(ConfigError, match=r"platform\.toml\.faults: key 'journal'") as refused:
        load_faults(platform, config)
    message = str(refused.value).split("key 'journal'")[1]
    assert "<absolute path of the federation's root>/fault-journal" in message
    assert "study.toml" in message
    assert str(tmp_path) not in message


def test_o35_the_federations_journal_written_in_full_loads_beside_study_toml(tmp_path, root, fake):
    """O35: the absolute form the refusal names loads, and the journal is the root's, so its lock and the link-check
    record are the federation's, not the study's."""
    platform = platform_in_a_study(tmp_path, journal=str(root / "fault-journal"))
    settings = load_faults(platform, load_platform(platform))
    assert settings.journal == (root / "fault-journal").resolve()
    assert not (tmp_path / STUDY_DIRECTORY / "fault-journal").exists()


def test_o35_a_relative_journal_still_loads_where_the_file_lies_in_the_federations_root(root, fake):
    """O35: the root's own platform.toml, rendered by `up` with the relative `fault-journal`, has no study.toml beside
    it: the relative journal is the root's and loads, as before."""
    platform = root / "platform.toml"
    assert not (root / "study.toml").exists()
    assert load_faults(platform, load_platform(platform)).journal == (root / "fault-journal").resolve()


def test_o35_a_relative_journal_still_loads_in_a_derived_directory_that_holds_no_study_toml(tmp_path, root, fake):
    """O35: a file derived in a work directory (no study.toml) may state the root's journal relative to itself."""
    work = tmp_path / "work"
    work.mkdir()
    platform = write_platform(work, lambda d: federation_document(d, journal="../root/fault-journal"))
    assert load_faults(platform, load_platform(platform)).journal == (root / "fault-journal").resolve()


def test_o35_a_study_directory_is_the_one_the_study_loader_names():
    """O35: the refusal's marker is the file the study loader looks for (the two are pinned together, as the core's
    `expcore_onedata` does not import the study layer)."""
    from expcore.study.config import STUDY_FILE

    assert fault_settings.STUDY_FILE == STUDY_FILE

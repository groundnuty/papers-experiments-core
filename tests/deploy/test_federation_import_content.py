"""What an imported space is spared (deploy/compose-federation, #184): the content writer never clears, writes or
replicates in it, `reset --clean` refuses a layout that has one before it touches anything, `reset --replicas` is as it
was, and the facts and the day-0 table record the import without its path. The providers are a fake that records every
call."""

import pytest
from federation_support import MINIMAL, STUDY_BASELINE_OFF, WITH_IMPORT, WITH_WRITABLE_IMPORT, import_layout

# alpha-space (the study space, no baseline) and fern-space, imported read-only from a variable: nothing writes either
NO_BASELINE = STUDY_BASELINE_OFF + WITH_IMPORT.removeprefix(MINIMAL)
# the study space itself imported, with a mirror, as a study that reads a dataset it never changes would declare it
STUDY_IMPORTED = MINIMAL.replace(
    'mirrors = ["elm"]',
    'mirrors = ["elm"]\nbaseline = false\nimport = { readonly = true, source_env = "EXPCORE_FED_IMPORT_FERN" }',
    1,
)


class FakeProviders:
    """The provider calls the content writer makes, recorded; each space's root lists one file, so a writer that
    cleared a space would have something to delete."""

    def __init__(self, fed, layout) -> None:
        spec = layout.spec
        self.layout = layout
        self.ids = fed.boot.Ids(
            providers={p.name: f"pid-{p.name}" for p in spec.providers},
            spaces={s.name: f"sid-{s.name}" for s in spec.spaces},
        )
        self.rest = self
        self.calls: list[tuple[str, str, str]] = []

    def admin_token(self) -> str:
        return "token"

    def provider(self, service, method: str, path: str, label: str, **options):
        self.calls.append((service.name, method, path))
        parts = path.split("/")
        if method == "GET" and parts[1] == "spaces":
            return {"fileId": "root-" + parts[2]}
        if method == "GET" and path.endswith("/children"):
            entry = {"file_id": "file-" + parts[2], "name": "held.txt", "type": "REG", "size": 4}
            return {"children": [entry], "isLast": True, "nextPageToken": "end"}
        return {}

    def touching(self, space: str) -> list[tuple[str, str, str]]:
        return [call for call in self.calls if f"sid-{space}" in call[2]]


def writer(fed, tmp_path, text: str):
    import fedcontent

    layout, _ = import_layout(fed, tmp_path, text)
    fake = FakeProviders(fed, layout)
    return fake, fedcontent.Content(fake)


def test_the_writer_never_touches_an_imported_space_and_still_empties_the_others(fed, tmp_path):
    """Clearing an imported space would unlink its files from the import directory on a writable storage, and on a
    read-only one leave them there for a later scan to import again under new ids."""
    fake, content = writer(fed, tmp_path, NO_BASELINE)
    removed = content.reset(timeout_s=5)
    assert fake.touching("fern-space") == []
    assert removed == {"alpha-space": 1, "fern-space": 0}
    assert [method for _, method, _ in fake.calls if method != "GET"] == ["DELETE"]  # alpha's held file, nothing else
    assert fake.calls[-1] == ("oak", "DELETE", "/data/file-root-sid-alpha-space")


def test_the_writer_makes_no_call_for_a_layout_whose_only_space_is_imported(fed, tmp_path):
    """The study space imported, with a mirror: no baseline to write, no file to replicate, no transfer, no call."""
    fake, content = writer(fed, tmp_path, STUDY_IMPORTED)
    assert content.reset(timeout_s=5) == {"alpha-space": 0}
    assert fake.calls == []


def test_a_layout_with_no_import_is_written_as_it_was(fed, tmp_path):
    fake, content = writer(fed, tmp_path, STUDY_BASELINE_OFF)
    assert content.reset(timeout_s=5) == {"alpha-space": 1}
    assert [method for _, method, _ in fake.calls if method != "GET"] == ["DELETE"]


# -- the resets -------------------------------------------------------------------------------------------------------


class Touched(AssertionError):
    """A command went past its refusal."""


@pytest.fixture
def nothing_runs(fed, monkeypatch):
    """Everything reset --clean could touch fails the test: docker's listings, the REST client, the bootstrap."""

    def forbidden(name):
        def fail(*args, **options):
            raise Touched(name)

        return fail

    for name in ("project_containers", "project_networks", "compose", "Rest", "Bootstrap", "bring_up", "write_facts"):
        monkeypatch.setattr(fed.cli, name, forbidden(name))


def test_reset_clean_refuses_a_layout_with_an_imported_space_before_it_touches_anything(fed, tmp_path, nothing_runs):
    layout, _ = import_layout(fed, tmp_path)
    with pytest.raises(RuntimeError, match=r"reset --clean.*imported space.*fern-space") as found:
        fed.cli.reset_clean(layout)
    message = str(found.value)
    assert "moss-space" in message  # every imported space is named
    assert "unlinks" in message  # why: a delete in an imported space reaches the import directory
    assert "scan" in message  # and on a read-only storage the file stays, to be imported again under a new id
    assert "down" in message
    assert not layout.root.exists()  # nothing was made


def test_reset_clean_still_goes_on_to_its_checks_for_a_layout_with_no_import(fed, tmp_path, monkeypatch):
    layout, _ = import_layout(fed, tmp_path, MINIMAL)

    def not_up(layout):
        raise RuntimeError("the federation is not up")

    monkeypatch.setattr(fed.cli, "require_up", not_up)
    with pytest.raises(RuntimeError, match="the federation is not up"):
        fed.cli.reset_clean(layout)


def test_the_command_reports_the_refusal_and_exits_nonzero(fed, tmp_path, monkeypatch, capsys, nothing_runs):
    layout, _ = import_layout(fed, tmp_path)
    monkeypatch.setenv("EXPCORE_FED_ROOT_PARENT", str(tmp_path.resolve()))
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(layout.root))
    assert fed.cli.main(["--layout", str(tmp_path.resolve() / "federation.toml"), "reset", "--clean"]) == 1
    assert "error: reset --clean:" in capsys.readouterr().err


def test_reset_replicas_is_unchanged_for_a_layout_with_an_imported_space(fed, tmp_path, monkeypatch):
    """It evicts from mirrors only, whose storage is regular, and removes no file."""
    layout, _ = import_layout(fed, tmp_path)
    ran = []
    monkeypatch.setattr(fed.cli, "require_up", lambda layout: ran.append("require_up"))
    monkeypatch.setattr(fed.cli, "Rest", lambda ca: type("Rest", (), {"close": lambda self: ran.append("close")})())
    monkeypatch.setattr(fed.cli, "Bootstrap", lambda rest, layout: "boot")
    monkeypatch.setattr(fed.cli, "bring_up", lambda layout, boot, timer: ran.append("bring_up"))
    monkeypatch.setattr(fed.cli, "evict_replicas", lambda boot, timeout: ran.append("evict") or {})
    assert fed.cli.reset_replicas(layout).startswith("reset --replicas")
    assert ran == ["require_up", "bring_up", "evict", "close"]


# -- the facts and the day-0 table ------------------------------------------------------------------------------------

SOURCE = "/srv/invented-datasets/fern"  # what a variable holds: never recorded


def facts_boot(fed, tmp_path, text: str = WITH_IMPORT):
    """A bootstrap with made-up ids over a layout, as up holds them once the federation answered."""
    layout, _ = import_layout(fed, tmp_path, text)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout = layout
    spec = layout.spec
    boot.ids = fed.boot.Ids(
        users={p.name: f"u{i}" for i, p in enumerate(spec.principals)},
        providers={p.name: f"p{i}" for i, p in enumerate(spec.providers)},
        spaces={s.name: f"s{i}" for i, s in enumerate(spec.spaces)},
    )
    return boot


def test_a_space_that_is_not_imported_records_a_null_import(fed, tmp_path):
    facts = fed.facts.facts_of(facts_boot(fed, tmp_path))
    assert facts["spaces"]["alpha-space"]["import"] is None


def test_an_imported_space_records_its_mode_its_variable_its_storage_and_no_scan_before_one_is_read(
    fed, tmp_path, monkeypatch
):
    monkeypatch.setenv("EXPCORE_FED_IMPORT_FERN", SOURCE)
    facts = fed.facts.facts_of(facts_boot(fed, tmp_path))
    assert facts["spaces"]["fern-space"]["import"] == {
        "readonly": True,
        "source_env": "EXPCORE_FED_IMPORT_FERN",
        "storage": "import-fern-space",
        "scan": None,
    }


def test_an_imported_space_records_the_scan_the_wait_read(fed, tmp_path):
    boot = facts_boot(fed, tmp_path)
    scan = {"status": "completed", "createdFiles": 12, "failedFiles": 0}
    boot.ids.scans["fern-space"] = scan
    assert fed.facts.facts_of(boot)["spaces"]["fern-space"]["import"]["scan"] == scan


def test_the_facts_never_hold_the_path_a_variable_names(fed, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPCORE_FED_IMPORT_FERN", SOURCE)
    monkeypatch.setenv("EXPCORE_FED_IMPORT_MOSS", SOURCE + "-copy")
    boot = facts_boot(fed, tmp_path, WITH_WRITABLE_IMPORT)
    boot.layout.root.mkdir()
    fed.facts.write_facts(boot.layout, boot)
    text = boot.layout.facts.read_text(encoding="utf-8")
    assert "EXPCORE_FED_IMPORT_FERN" in text
    assert "invented-datasets" not in text


def test_the_day0_table_says_how_an_imported_space_is_held_and_by_which_variable(fed, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPCORE_FED_IMPORT_FERN", SOURCE)
    monkeypatch.setenv("EXPCORE_FED_IMPORT_MOSS", SOURCE + "-copy")
    layout, _ = import_layout(fed, tmp_path, WITH_WRITABLE_IMPORT)
    rows = {row.split(" | ")[0].removeprefix("| "): row for row in fed.facts.describe(layout).splitlines()}
    assert rows["Space `fern-space`"].endswith(
        "; no baseline files; imported (read-only) from $EXPCORE_FED_IMPORT_FERN |"
    )
    assert rows["Space `moss-space`"].endswith(
        "; no baseline files; imported (writable) from $EXPCORE_FED_IMPORT_MOSS |"
    )
    assert "imported" not in rows["Space `alpha-space` (study space)"]
    assert "invented-datasets" not in "\n".join(rows.values())

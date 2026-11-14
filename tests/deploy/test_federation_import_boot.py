"""What the bootstrap does for an imported space (deploy/compose-federation, #184): the imported storage on its owner's
panel, the support on the right storage with the import configuration, and the wait for the scan. The zone, the
panels and the providers are one fake that answers as the 25.0 interfaces do and records every call; nothing starts a
container."""

import pytest
from federation_support import MINIMAL, WITH_IMPORT, WITH_WRITABLE_IMPORT, import_layout

NO_DETECTION = {"continuousScan": False, "detectModifications": False, "detectDeletions": False}
DONE = {
    "status": "completed",
    "start": 1_700_000_000,
    "stop": 1_700_000_042,
    "createdFiles": 12,
    "modifiedFiles": 0,
    "deletedFiles": 0,
    "unmodifiedFiles": 0,
    "failedFiles": 0,
    "totalScans": 1,
}
FERN = {
    "type": "posix",
    "mountPoint": "/volumes/import/fern-space",
    "importedStorage": True,
    "readonly": True,
}


class FakeFederation:
    """The zone, the providers' panels and the providers, for one layout. Each provider starts with the batch-mode
    storage `posix`; a panel creates the storages it is asked for; a support call makes the provider support the space
    its token names; the scan of an imported space answers from a script. Every panel call is recorded."""

    def __init__(self, fed, layout, *, newest_first: bool = False, drop_creates: bool = False) -> None:
        self.boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
        spec = layout.spec
        self.boot.layout, self.boot.spec = layout, spec
        self.boot.ids = fed.boot.Ids(
            providers={p.name: f"pid-{p.name}" for p in spec.providers},
            spaces={s.name: f"sid-{s.name}" for s in spec.spaces},
        )
        self.storages: dict[str, dict[str, dict]] = {p.name: {} for p in spec.providers}
        for provider in self.storages:
            self.add_storage(
                provider, "posix", mountPoint="/volumes/storage"
            )  # the batch-mode storage of every provider
        self.newest_first = newest_first  # list a provider's storages the newest first, so ids[0] is the wrong one
        self.drop_creates = drop_creates  # a panel that answers a create and makes nothing
        self.supported: dict[str, set[str]] = {s.name: set() for s in spec.spaces}  # provider ids, by space
        self.panel_calls: list[tuple[str, str, str, dict]] = []
        self.supports: list[tuple[str, dict]] = []  # (provider, body) of every support call
        self.scans: dict[str, list] = {}  # a space's scan answers in turn; the last one stays
        self.boot.panel, self.boot.zone, self.boot.provider = self.panel, self.zone, self.provider

    def add_storage(self, provider: str, name: str, **details) -> str:
        found = f"{name}-{sum(len(s) for s in self.storages.values())}"
        self.storages[provider][found] = {"name": name, "type": "posix", **details}
        return found

    def id_of(self, provider: str, name: str) -> str:
        return next(i for i, s in self.storages[provider].items() if s["name"] == name)

    def posts(self) -> list[tuple[str, str, dict]]:
        return [(who, path, options["json"]) for who, method, path, options in self.panel_calls if method == "POST"]

    def panel(self, service, method: str, path: str, label: str, **options):
        self.panel_calls.append((service.name, method, path, options))
        parts, mine = path.split("/"), self.storages[service.name]
        if (method, path) == ("GET", "/provider/storages"):
            ids = list(mine)
            return {"ids": ids[::-1] if self.newest_first else ids}
        if method == "GET" and path.startswith("/provider/storages/"):
            return {"id": parts[3], **mine[parts[3]]}
        if (method, path) == ("POST", "/provider/storages"):
            for name, config in options["json"].items():
                if not self.drop_creates:
                    self.add_storage(service.name, name, **config)
            return {}  # the panel's answer is empty
        if (method, path) == ("POST", "/provider/spaces"):
            body = options["json"]
            self.supports.append((service.name, body))
            self.supported[body["token"].removeprefix("token-")].add(f"pid-{service.name}")
            return {}
        if method == "GET" and path.endswith("/storage-import/auto/info"):
            answers = self.scans[parts[3].removeprefix("sid-")]
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer
        raise AssertionError(f"unexpected panel call: {method} {path}")

    def zone(self, method: str, path: str, label: str, **options):
        if method == "GET" and path.endswith("/providers"):
            return {"providers": sorted(self.supported[path.split("/")[2].removeprefix("sid-")])}
        if (method, path) == ("POST", "/user/tokens/temporary"):
            invite = options["json"]["type"]["inviteToken"]
            return {"token": "token-" + invite["spaceId"].removeprefix("sid-")}
        raise AssertionError(f"unexpected zone call: {method} {path}")

    def provider(self, service, method: str, path: str, label: str, **options):
        if method == "GET" and path.startswith("/spaces/"):
            return {"fileId": "root"}
        raise AssertionError(f"unexpected provider call: {method} {path}")


def federation(fed, tmp_path, text: str = WITH_IMPORT, **options) -> FakeFederation:
    layout, _ = import_layout(fed, tmp_path, text)
    return FakeFederation(fed, layout, **options)


# -- the imported storage ---------------------------------------------------------------------------------------------


def test_a_missing_import_storage_is_created_on_its_owners_panel_as_the_layout_declares_it(fed, tmp_path):
    fake = federation(fed, tmp_path)
    fake.boot.ensure_import_storages()
    assert fake.posts() == [("elm", "/provider/storages", {"import-fern-space": FERN})]
    assert fake.boot.ids.storages == {("elm", "fern-space"): fake.id_of("elm", "import-fern-space")}


def test_a_writable_import_storage_is_created_writable_on_its_owners_panel(fed, tmp_path):
    fake = federation(fed, tmp_path, WITH_WRITABLE_IMPORT)
    fake.boot.ensure_import_storages()
    moss = {"type": "posix", "mountPoint": "/volumes/import/moss-space", "importedStorage": True, "readonly": False}
    assert sorted(fake.posts()) == [
        ("elm", "/provider/storages", {"import-fern-space": FERN}),
        ("oak", "/provider/storages", {"import-moss-space": moss}),
    ]
    assert set(fake.boot.ids.storages) == {("elm", "fern-space"), ("oak", "moss-space")}


def test_an_import_storage_that_exists_as_declared_is_found_by_its_name_and_not_made_again(fed, tmp_path):
    fake = federation(fed, tmp_path)
    fake.add_storage("elm", "import-other-space", **{**FERN, "mountPoint": "/volumes/import/other-space"})
    found = fake.add_storage("elm", "import-fern-space", **FERN)
    fake.add_storage("elm", "import-later-space", **FERN)
    fake.boot.ensure_import_storages()
    assert fake.posts() == []
    assert fake.boot.ids.storages == {("elm", "fern-space"): found}


def test_only_the_owners_panel_is_asked_about_an_import_storage(fed, tmp_path):
    fake = federation(fed, tmp_path)
    fake.boot.ensure_import_storages()
    assert {who for who, *_ in fake.panel_calls} == {"elm"}  # the mirror, oak, is never asked


def test_a_layout_with_no_import_asks_no_panel_for_one(fed, tmp_path):
    fake = federation(fed, tmp_path, MINIMAL)
    fake.boot.ensure_import_storages()
    assert fake.panel_calls == []
    assert fake.boot.ids.storages == {}


MISMATCHES = [
    ("type", "nfs"),
    ("importedStorage", False),
    ("readonly", False),
    ("mountPoint", "/volumes/storage"),
]


@pytest.mark.parametrize(("key", "value"), MISMATCHES, ids=[key for key, _ in MISMATCHES])
def test_an_import_storage_that_differs_from_the_layout_is_refused_and_nothing_is_changed(fed, tmp_path, key, value):
    """Onezone refuses to change `importedStorage` once a storage supports a space (oz-worker's
    check_imported_storage_value, ERR_STORAGE_IN_USE), and a storage that is read-only or mounted elsewhere than the
    layout says would import another directory or let the platform write where the layout says it never does."""
    fake = federation(fed, tmp_path)
    fake.add_storage("elm", "import-fern-space", **{**FERN, key: value})
    with pytest.raises(RuntimeError, match=rf"import-fern-space.*{key}") as found:
        fake.boot.ensure_import_storages()
    assert "down" in str(found.value)  # what makes it anew
    assert {method for _, method, *_ in fake.panel_calls} == {"GET"}  # read, never changed


def test_an_import_storage_the_panel_did_not_make_is_refused(fed, tmp_path):
    fake = federation(fed, tmp_path, drop_creates=True)
    with pytest.raises(RuntimeError, match="elm has no storage import-fern-space after creating it"):
        fake.boot.ensure_import_storages()


# -- the support ------------------------------------------------------------------------------------------------------


def supported(fake: FakeFederation, space: str, provider: str) -> dict:
    """The body of the support call a provider made for a space."""
    (body,) = [b for who, b in fake.supports if who == provider and b["token"] == f"token-{space}"]
    return body


def test_every_support_is_on_the_storage_of_its_name_never_the_first_one_listed(fed, tmp_path):
    """A provider with two storages lists them in no order the support may rely on: the owner of an imported space
    supports it on its import storage, and every other support is on the batch-mode `posix`, even where the provider
    also owns an imported space."""
    fake = federation(fed, tmp_path, newest_first=True)
    fake.boot.ensure_import_storages()
    fake.boot.ensure_support(5)
    import_storage = fake.id_of("elm", "import-fern-space")
    assert supported(fake, "fern-space", "elm")["storageId"] == import_storage
    assert supported(fake, "fern-space", "oak")["storageId"] == fake.id_of("oak", "posix")
    assert supported(fake, "alpha-space", "elm")["storageId"] == fake.id_of("elm", "posix")  # elm mirrors alpha
    assert supported(fake, "alpha-space", "oak")["storageId"] == fake.id_of("oak", "posix")


def test_the_owner_of_an_imported_space_supports_it_with_a_one_scan_import_that_only_adds_files(fed, tmp_path):
    fake = federation(fed, tmp_path)
    fake.boot.ensure_import_storages()
    fake.boot.ensure_support(5)
    assert supported(fake, "fern-space", "elm") == {
        "token": "token-fern-space",
        "size": fake.boot.spec.space("fern-space").support_bytes,
        "storageId": fake.id_of("elm", "import-fern-space"),
        "storageImport": {"mode": "auto", "autoStorageImportConfig": NO_DETECTION},
    }


def test_a_mirror_and_a_space_that_is_not_imported_are_supported_without_an_import_configuration(fed, tmp_path):
    fake = federation(fed, tmp_path)
    fake.boot.ensure_import_storages()
    fake.boot.ensure_support(5)
    assert "storageImport" not in supported(fake, "fern-space", "oak")
    for provider in ("oak", "elm"):
        assert set(supported(fake, "alpha-space", provider)) == {"token", "size", "storageId"}


def test_a_provider_that_is_already_supporting_a_space_is_not_asked_again(fed, tmp_path):
    fake = federation(fed, tmp_path)
    fake.boot.ensure_import_storages()
    fake.boot.ensure_support(5)
    asked = len(fake.panel_calls)
    fake.boot.ensure_support(5)  # up on a federation that is up
    assert len(fake.panel_calls) == asked
    assert len(fake.supports) == 4


def test_a_provider_without_the_batch_storage_cannot_support_a_space(fed, tmp_path):
    fake = federation(fed, tmp_path)
    del fake.storages["oak"][fake.id_of("oak", "posix")]
    with pytest.raises(RuntimeError, match="oak has no storage named posix"):
        fake.boot.ensure_support(5)
    assert fake.supports == []


def test_an_owner_whose_import_storage_was_not_made_cannot_support_its_space(fed, tmp_path):
    fake = federation(fed, tmp_path)
    with pytest.raises(RuntimeError, match="elm has no storage named import-fern-space"):
        fake.boot.ensure_support(5)
    assert [b for who, b in fake.supports if "storageImport" in b] == []


def test_two_storages_of_one_name_on_a_provider_are_refused_as_ambiguous(fed, tmp_path):
    fake = federation(fed, tmp_path)
    fake.add_storage("oak", "posix", mountPoint="/volumes/elsewhere")
    with pytest.raises(RuntimeError, match="oak has 2 storages named posix"):
        fake.boot.ensure_support(5)


# -- the scan ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def quick(fed, monkeypatch):
    monkeypatch.setattr(fed.boot, "IMPORT_POLL_S", 0.0)


def test_the_wait_returns_each_imported_spaces_scan_once_it_is_completed(fed, tmp_path, quick):
    fake = federation(fed, tmp_path, WITH_WRITABLE_IMPORT)
    fake.scans = {"fern-space": [DONE], "moss-space": [{**DONE, "createdFiles": 3}]}
    found = fake.boot.wait_import(5)
    assert found == {"fern-space": DONE, "moss-space": {**DONE, "createdFiles": 3}}
    assert fake.boot.ids.scans == found  # kept for the facts
    assert {who for who, *_ in fake.panel_calls} == {"elm", "oak"}  # each space's own owner
    paths = {path for _, _, path, _ in fake.panel_calls}
    assert paths == {f"/provider/spaces/sid-{s}/storage-import/auto/info" for s in ("fern-space", "moss-space")}


def test_the_wait_keeps_asking_while_the_scan_is_enqueued_running_or_not_yet_answered(fed, tmp_path, quick):
    fake = federation(fed, tmp_path)
    no_answer = fed.boot.RestError("GET storage import: answered 404", 404)
    fake.scans = {"fern-space": [no_answer, {"status": "enqueued"}, {**DONE, "status": "running"}, DONE]}
    assert fake.boot.wait_import(5) == {"fern-space": DONE}
    assert len(fake.panel_calls) == 4


def test_the_wait_returns_only_the_keys_of_the_scan_record_and_none_for_one_the_answer_lacks(fed, tmp_path, quick):
    fake = federation(fed, tmp_path)
    fake.scans = {"fern-space": [{"status": "completed", "createdFiles": 2, "somethingNew": "x"}]}
    found = fake.boot.wait_import(5)["fern-space"]
    assert found == {**dict.fromkeys(DONE), "status": "completed", "createdFiles": 2}


@pytest.mark.parametrize("status", ["failed", "aborted"])
def test_a_scan_that_failed_or_was_aborted_is_refused_naming_the_space(fed, tmp_path, quick, status):
    fake = federation(fed, tmp_path)
    fake.scans = {"fern-space": [{**DONE, "status": status}]}
    with pytest.raises(RuntimeError, match=rf"storage import of fern-space {status}"):
        fake.boot.wait_import(5)


@pytest.mark.parametrize("status", ["completed", "running"])
def test_a_scan_with_failed_files_is_refused_even_when_it_completed(fed, tmp_path, quick, status):
    fake = federation(fed, tmp_path)
    fake.scans = {"fern-space": [{**DONE, "status": status, "failedFiles": 2}]}
    with pytest.raises(RuntimeError, match=r"storage import of fern-space: 2 file\(s\) failed"):
        fake.boot.wait_import(5)


@pytest.mark.parametrize("answer", [{"status": "paused"}, {}, {"status": None}, ["completed"], "completed"])
def test_an_answer_with_no_known_status_is_refused_not_waited_on(fed, tmp_path, quick, answer):
    fake = federation(fed, tmp_path)
    fake.scans = {"fern-space": [answer]}
    with pytest.raises(RuntimeError, match="storage import of fern-space: the answer holds no known status"):
        fake.boot.wait_import(5)


def test_a_scan_that_never_completes_times_out_naming_the_space(fed, tmp_path, monkeypatch):
    monkeypatch.setattr(fed.boot, "IMPORT_POLL_S", 0.01)
    fake = federation(fed, tmp_path)
    fake.scans = {"fern-space": [{"status": "running"}]}
    with pytest.raises(TimeoutError, match="the storage import of fern-space completing did not happen"):
        fake.boot.wait_import(0.05)


def test_a_layout_with_no_import_waits_for_nothing(fed, tmp_path):
    fake = federation(fed, tmp_path, MINIMAL)
    assert fake.boot.wait_import(5) == {}
    assert fake.panel_calls == []


# -- up's order -------------------------------------------------------------------------------------------------------


class Steps:
    """A bootstrap that only records the steps up takes, in order."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def step(*args):
            self.steps.append((name, args))

        return step


def test_up_makes_the_import_storage_before_the_support_and_waits_for_the_scan_after_the_members(
    fed, tmp_path, monkeypatch
):
    layout, _ = import_layout(fed, tmp_path)
    monkeypatch.setattr(fed.cli, "compose", lambda *args, **options: "")
    steps = Steps()
    fed.cli.bring_up(layout, steps, fed.cli.Timer())
    names = [name for name, _ in steps.steps]
    assert names.index("ensure_spaces") < names.index("ensure_import_storages") < names.index("ensure_support")
    assert names.index("ensure_support") < names.index("ensure_members") < names.index("wait_import")
    assert names[-1] == "wait_import"
    assert dict(steps.steps)["wait_import"] == (fed.cli.IMPORT_TIMEOUT_S,)
    assert fed.cli.IMPORT_TIMEOUT_S == 1800.0


def test_the_scan_and_storage_steps_are_taken_for_a_layout_with_no_import_too(fed, tmp_path, monkeypatch):
    """They are the bootstrap's own no-ops there, so up and the resets need no branch of their own."""
    layout, _ = import_layout(fed, tmp_path, MINIMAL)
    monkeypatch.setattr(fed.cli, "compose", lambda *args, **options: "")
    steps = Steps()
    fed.cli.bring_up(layout, steps, fed.cli.Timer())
    names = [name for name, _ in steps.steps]
    assert "ensure_import_storages" in names
    assert names[-1] == "wait_import"

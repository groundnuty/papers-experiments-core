"""The verification of a space on a tree shared with other federations: the same checks as any imported space (the scan,
the listing against the manifest, the block maps), and a storage that is not read-only refused outright (O56)."""

import pytest

from expcore_onedata.storageimport import ImportedSpace, ImportFailed, manifest_of_tree
from expcore_onedata.testing import ADMIN_ENV, ALPHA_ID, BETA_ID, with_placement, write_platform
from expcore_onedata.testing.fake_import import make_import_federation

TREE = {
    "records/day-01.csv": b"01,ok\n" * 40,
    "records/day-02.csv": b"02,ok\n" * 70,
    "notes/readme.txt": b"an invented dataset\n",
    "empty.dat": b"",
}


def declared(shared: bool):
    def edit(data):
        with_placement(data)
        data["support"]["providers"] = ["alpha", "beta"]
        data["space"]["import"] = {"readonly": True, **({"shared": True} if shared else {})}

    return edit


@pytest.fixture(params=[True], ids=["shared"])
def shared(request):
    return request.param


@pytest.fixture
def platform_file(tmp_path, shared):
    return write_platform(tmp_path, declared(shared))


@pytest.fixture
def fake(monkeypatch, ticker):
    """The data provider holds the space on a read-only imported storage of `TREE`; the initial scan is enqueued."""
    federation = make_import_federation(clock=ticker.clock, readonly=True)
    federation.add_provider(BETA_ID, "beta.test")
    for path, content in TREE.items():
        federation.put_on_storage(path, content)
    federation.scan_polls = 2
    federation.start_scan()
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    return federation


@pytest.fixture
def manifest(tmp_path):
    root = tmp_path / "dataset"
    for path, content in TREE.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    return manifest_of_tree(root)


@pytest.fixture
def imported(provisioner, manifest):
    return ImportedSpace(provisioner, manifest)


def test_o56_a_shared_import_is_provisioned_and_verified_like_any_import_and_its_record_says_shared(imported, fake):
    """O56: sharing changes nothing of the checks: the scan completes, the listing is the manifest's, every file is
    whole on the data provider; the record carries `shared` so an evidence reader knows the tree was shared."""
    record = imported.provision(timeout_s=60)
    assert record["ok"] is True
    assert record["shared"] is True
    assert record["readonly"] is True
    assert record["storage"]["readonly"] is True
    assert record["blocks"] == {"files": 4, "whole": 4, "not_whole": []}
    assert fake.held("records/day-01.csv", ALPHA_ID) == [[0, len(TREE["records/day-01.csv"])]]


@pytest.mark.parametrize("shared", [False], ids=["not shared"])
def test_o56_a_record_of_an_import_that_is_not_shared_has_no_shared_key(imported, fake):
    """O56: the record keeps the shape it had for every import declared before the key existed."""
    assert "shared" not in imported.provision(timeout_s=60)


DIFFERENCES = {
    "a file missing": (lambda f: f.drop_from_storage("notes/readme.txt"), "missing", ["manifest.files[1]"]),
    "a file of another size": (
        lambda f: f.put_on_storage("records/day-02.csv", b"short"),
        "wrong_size",
        ["manifest.files[3]"],
    ),
}


@pytest.mark.parametrize("case", list(DIFFERENCES))
def test_o56_a_shared_tree_that_differs_from_its_manifest_fails_the_check_naming_no_path(case, imported, fake):
    """O56: another federation, or a person, changed the shared tree after the manifest was made: a file missing or of
    another size fails the verification by its position in the manifest, and no path is in the message."""
    change, key, expected = DIFFERENCES[case]
    change(fake)
    with pytest.raises(ImportFailed, match="does not hold exactly the manifest") as caught:
        imported.provision(timeout_s=30)
    assert caught.value.record["ok"] is False
    assert caught.value.record["listing"]["providers[0]"][key] == expected
    assert caught.value.record["shared"] is True
    for path in TREE:
        assert path not in str(caught.value)


def test_o56_a_storage_that_is_not_read_only_fails_a_shared_import_before_the_scan_is_waited_for(imported, fake):
    """O56: the panel shows the shared directory's storage writable: refused outright, with the reason in the message,
    and the scan is never read, so nothing waits on a tree that a federation could write to."""
    fake.readonly = False
    with pytest.raises(ImportFailed, match="shared with other federations") as caught:
        imported.provision(timeout_s=60)
    assert caught.value.record["storage"]["readonly"] is False
    assert not [call for call in fake.panel_calls if call[2].endswith("/storage-import/auto/info")]


@pytest.mark.parametrize("answer", [1, "true", None, "absent"])
def test_o56_a_read_only_flag_that_is_not_the_boolean_true_fails_a_shared_import(answer, imported, fake, monkeypatch):
    """O56: the flag is type-strict: `1`, the string "true", null and a missing key are none of them read-only."""
    client = imported._p.client
    real = client.panel_get

    def panel_get(provider, path, **options):
        shown = real(provider, path, **options)
        if "/provider/storages/" in path:
            shown = {key: value for key, value in shown.items() if key != "readonly"}
            if answer != "absent":
                shown["readonly"] = answer
        return shown

    monkeypatch.setattr(client, "panel_get", panel_get)
    with pytest.raises(ImportFailed, match="shared with other federations"):
        imported.check_storage()

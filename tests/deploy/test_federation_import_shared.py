"""The layout of a space imported from a tree that other federations import too (deploy/compose-federation, O56): the
keys `shared`, `manifest_env` and `direct_io` of `import`, their refusals, and the manifest a variable names. Nothing
here starts a container; every path is a temporary directory's."""

import re
from pathlib import Path

import pytest
from federation_support import WITH_IMPORT, import_layout

SHARED = (
    'import = { readonly = true, shared = true, source_env = "EXPCORE_FED_IMPORT_FERN", '
    'manifest_env = "EXPCORE_FED_IMPORT_FERN_MANIFEST" }'
)
WITH_SHARED = WITH_IMPORT.replace('import = { readonly = true, source_env = "EXPCORE_FED_IMPORT_FERN" }', SHARED)
WITH_DIRECT = WITH_SHARED.replace("shared = true,", "shared = true, direct_io = true,")


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "federation.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_o56_a_shared_import_declares_its_manifest_and_whether_the_mount_reads_it_directly(fed, tmp_path):
    """O56: a shared import declares its manifest and whether the mount reads it directly."""
    spec = fed.spec.load_spec(write(tmp_path, WITH_DIRECT))
    assert spec.space("fern-space").imported == fed.spec.Import(
        readonly=True,
        source_env="EXPCORE_FED_IMPORT_FERN",
        shared=True,
        manifest_env="EXPCORE_FED_IMPORT_FERN_MANIFEST",
        direct_io=True,
    )


def test_o56_the_new_keys_default_to_what_an_import_always_was(fed, tmp_path):
    """O56: the new keys default to what an import always was."""
    imported = fed.spec.load_spec(write(tmp_path, WITH_IMPORT)).space("fern-space").imported
    assert (imported.shared, imported.manifest_env, imported.direct_io) == (False, None, False)


def test_o56_a_layout_that_does_not_share_may_still_name_a_manifest(fed, tmp_path):
    """O56: a layout that does not share may still name a manifest."""
    text = WITH_IMPORT.replace(
        'source_env = "EXPCORE_FED_IMPORT_FERN"',
        'source_env = "EXPCORE_FED_IMPORT_FERN", manifest_env = "EXPCORE_FED_IMPORT_FERN_MANIFEST"',
    )
    imported = fed.spec.load_spec(write(tmp_path, text)).space("fern-space").imported
    assert imported.shared is False
    assert imported.manifest_env == "EXPCORE_FED_IMPORT_FERN_MANIFEST"


REFUSED = [
    (
        "a shared import that is writable",
        WITH_SHARED.replace("readonly = true", "readonly = false"),
        "spaces.fern-space.import.readonly: must be true when shared is true",
    ),
    (
        "a shared import with no manifest",
        WITH_SHARED.replace(', manifest_env = "EXPCORE_FED_IMPORT_FERN_MANIFEST"', ""),
        "spaces.fern-space.import.manifest_env: is required when shared is true",
    ),
    (
        "shared that is not a boolean",
        WITH_SHARED.replace("shared = true", 'shared = "yes"'),
        "spaces.fern-space.import.shared: must be true or false",
    ),
    (
        "direct_io that is not a boolean",
        WITH_DIRECT.replace("direct_io = true", "direct_io = 1"),
        "spaces.fern-space.import.direct_io: must be true or false",
    ),
    (
        "a direct mount of a writable import",
        WITH_IMPORT.replace("readonly = true", "readonly = false, direct_io = true"),
        "spaces.fern-space.import.direct_io: needs readonly = true",
    ),
    (
        "a path in place of the manifest's variable",
        WITH_SHARED.replace("EXPCORE_FED_IMPORT_FERN_MANIFEST", "/srv/data/manifest.json"),
        "spaces.fern-space.import.manifest_env: must be the name of an environment variable",
    ),
    (
        "a manifest variable outside the prefix",
        WITH_SHARED.replace("EXPCORE_FED_IMPORT_FERN_MANIFEST", "HOME"),
        "spaces.fern-space.import.manifest_env",
    ),
    (
        "the manifest's variable as the source's",
        WITH_SHARED.replace("EXPCORE_FED_IMPORT_FERN_MANIFEST", "EXPCORE_FED_IMPORT_FERN"),
        "spaces.fern-space.import.manifest_env: is also the source_env of this import",
    ),
]


@pytest.mark.parametrize(("text", "words"), [c[1:] for c in REFUSED], ids=[c[0] for c in REFUSED])
def test_o56_an_import_the_layout_cannot_hold_is_refused_naming_the_file_and_the_key(fed, tmp_path, text, words):
    """O56: an import the layout cannot hold is refused naming the file and the key."""
    path = write(tmp_path, text)
    with pytest.raises(fed.spec.LayoutError, match=re.escape(f"{path}: {words}")):
        fed.spec.load_spec(path)


SECOND = (
    '\n[spaces.reed-space]\nowner = "oak"\nbaseline = false\n'
    'import = { readonly = true, shared = true, source_env = "{src}", manifest_env = "{man}" }\n'
)


def two(src: str, man: str) -> str:
    return WITH_SHARED + SECOND.replace("{src}", src).replace("{man}", man)


def test_o56_two_spaces_may_not_import_one_shared_tree(fed, tmp_path):
    """O56: Each shared space has a tree of its own: one tree under two spaces would be verified against two
    manifests."""
    path = write(tmp_path, two("EXPCORE_FED_IMPORT_FERN", "EXPCORE_FED_IMPORT_REED_MANIFEST"))
    with pytest.raises(fed.spec.LayoutError, match=re.escape(f"{path}: spaces.reed-space.import.source_env")) as found:
        fed.spec.load_spec(path)
    assert "fern-space" in str(found.value)


def test_o56_two_spaces_may_not_share_a_manifest_or_swap_a_source_for_a_manifest(fed, tmp_path):
    """O56: two spaces may not share a manifest or swap a source for a manifest."""
    for src, man, key in (
        ("EXPCORE_FED_IMPORT_REED", "EXPCORE_FED_IMPORT_FERN_MANIFEST", "manifest_env"),
        ("EXPCORE_FED_IMPORT_REED", "EXPCORE_FED_IMPORT_FERN", "manifest_env"),
        ("EXPCORE_FED_IMPORT_FERN_MANIFEST", "EXPCORE_FED_IMPORT_REED_MANIFEST", "source_env"),
    ):
        path = write(tmp_path, two(src, man))
        with pytest.raises(fed.spec.LayoutError, match=re.escape(f"{path}: spaces.reed-space.import.{key}")):
            fed.spec.load_spec(path)


def test_o56_distinct_trees_and_manifests_load_together(fed, tmp_path):
    """O56: distinct trees and manifests load together."""
    spec = fed.spec.load_spec(write(tmp_path, two("EXPCORE_FED_IMPORT_REED", "EXPCORE_FED_IMPORT_REED_MANIFEST")))
    assert [s.name for s in spec.spaces if s.imported and s.imported.shared] == ["fern-space", "reed-space"]


# -- the manifest a variable names ------------------------------------------------------------------------------------


def shared_layout(fed, tmp_path: Path):
    layout, data = import_layout(fed, tmp_path, WITH_SHARED)
    manifest = data.parent / "manifest.json"
    manifest.write_text('{"version": 1, "files": [], "directories": []}', encoding="utf-8")
    return layout, data, manifest


def manifest_env(path) -> dict[str, str]:
    return {"EXPCORE_FED_IMPORT_FERN_MANIFEST": str(path)}


def test_o56_the_manifest_is_the_file_the_variable_names(fed, tmp_path):
    """O56: the manifest is the file the variable names."""
    layout, _, manifest = shared_layout(fed, tmp_path)
    assert layout.import_manifest("fern-space", manifest_env(manifest)) == manifest


def refused(layout, value: str | None, space: str = "fern-space", **more: str) -> str:
    environ = {**({} if value is None else manifest_env(value)), **more}
    with pytest.raises(ValueError, match="EXPCORE_FED_IMPORT_FERN_MANIFEST") as found:
        layout.import_manifest(space, environ)
    assert not value or value not in str(found.value)  # never the value itself
    return str(found.value)


def test_o56_a_manifest_variable_that_is_unset_empty_or_relative_is_refused(fed, tmp_path):
    """O56: a manifest variable that is unset empty or relative is refused."""
    layout, _, _ = shared_layout(fed, tmp_path)
    assert "is not set" in refused(layout, None)
    assert "must be an absolute path" in refused(layout, "")
    assert "must be an absolute path" in refused(layout, "manifest.json")


def test_o56_a_manifest_with_a_parent_step_or_a_link_on_its_way_is_refused(fed, tmp_path):
    """O56: a manifest with a parent step or a link on its way is refused."""
    layout, data, manifest = shared_layout(fed, tmp_path)
    base = data.parent
    assert "'..'" in refused(layout, f"{base}/data/../manifest.json")
    (base / "link.json").symlink_to(manifest)
    assert "symbolic link" in refused(layout, str(base / "link.json"))
    (base / "dirlink").symlink_to(base)
    assert "symbolic link" in refused(layout, str(base / "dirlink" / "manifest.json"))


def test_o56_a_manifest_that_is_missing_or_not_a_regular_file_is_refused(fed, tmp_path):
    """O56: a manifest that is missing or not a regular file is refused."""
    layout, data, _ = shared_layout(fed, tmp_path)
    assert "must be an existing regular file" in refused(layout, str(data.parent / "absent.json"))
    assert "must be an existing regular file" in refused(layout, str(data))


def test_o56_a_manifest_inside_a_tree_that_is_imported_is_refused(fed, tmp_path):
    """O56: The scan would import the manifest as a file of the dataset, and the listing would then hold one entry the
    manifest does not."""
    layout, data, _ = shared_layout(fed, tmp_path)
    inside = data / "manifest.json"
    inside.write_text("{}", encoding="utf-8")
    assert "inside the tree of fern-space" in refused(layout, str(inside), EXPCORE_FED_IMPORT_FERN=str(data))


def test_o56_a_manifest_inside_the_tree_of_another_space_is_refused_too(fed, tmp_path):
    """O56: a manifest inside the tree of another space is refused too."""
    base = tmp_path.resolve()
    (base / "data").mkdir()
    (base / "reed").mkdir()
    text = two("EXPCORE_FED_IMPORT_REED", "EXPCORE_FED_IMPORT_REED_MANIFEST")
    (base / "federation.toml").write_text(text, encoding="utf-8")
    layout = fed.layout.Layout(base / "fed", spec=fed.spec.load_spec(base / "federation.toml"))
    inside = base / "reed" / "manifest.json"
    inside.write_text("{}", encoding="utf-8")
    environ = {**manifest_env(inside), "EXPCORE_FED_IMPORT_REED": str(base / "reed")}
    with pytest.raises(ValueError, match="inside the tree of reed-space"):
        layout.import_manifest("fern-space", environ)


def test_o56_a_manifest_in_the_federations_root_is_refused(fed, tmp_path):
    """O56: Down empties the root, and the manifest is the study's, kept beside the dataset."""
    layout, _, _ = shared_layout(fed, tmp_path)
    layout.root.mkdir(parents=True)
    inside = layout.root / "manifest.json"
    inside.write_text("{}", encoding="utf-8")
    assert "root of the federation" in refused(layout, str(inside))


def test_o56_a_space_with_no_manifest_variable_has_no_manifest(fed, tmp_path):
    """O56: a space with no manifest variable has no manifest."""
    layout, _ = import_layout(fed, tmp_path, WITH_IMPORT)
    with pytest.raises(ValueError, match="declares no manifest_env"):
        layout.import_manifest("fern-space", {})
    with pytest.raises(ValueError, match="is not an imported space"):
        layout.import_manifest("alpha-space", {})


def test_o56_the_layout_lists_its_shared_imports_with_the_provider_that_mounts_each(fed, tmp_path):
    """O56: One owner imports and mounts a tree; its mirrors keep a regular storage and mount nothing."""
    layout, _, _ = shared_layout(fed, tmp_path)
    assert [(s.name, s.owner) for s in layout.shared_imports()] == [("fern-space", "elm")]
    assert [s.name for s in layout.owned_imports("elm")] == ["fern-space"]
    assert layout.owned_imports("oak") == []


# -- the check after up -----------------------------------------------------------------------------------------------

TREE = {
    "records/day-01.csv": b"01,ok\n" * 40,
    "records/day-02.csv": b"02,ok\n" * 70,
    "notes/readme.txt": b"an invented dataset\n",
}
CHECKED = """\
project = "expcore-test"
domain = "expcore-test.test"
study_space = "study-a-data"

[zone]
port = 29643

[providers.alpha]
port = 29644

[providers.beta]
port = 29645

[spaces.study-a-data]
owner = "alpha"
mirrors = ["beta"]
baseline = false
import = { readonly = true, shared = true, source_env = "EXPCORE_FED_IMPORT_STUDY", manifest_env = "EXPCORE_FED_IMPORT_STUDY_MANIFEST" }

[principals.reader]
privileges = ["space_view", "space_read_data"]
"""  # noqa: E501


@pytest.fixture
def checked(fed, tmp_path, monkeypatch):
    """A layout with one shared import, the manifest of its tree, a rendered platform.toml that declares it as the
    layout does, and a fake federation whose owner holds it on a read-only imported storage after one scan."""
    from expcore_onedata.storageimport import manifest_of_tree, write_manifest
    from expcore_onedata.testing import ADMIN_ENV, BETA_ID, Ticker, with_placement, write_platform
    from expcore_onedata.testing.fake_import import make_import_federation

    layout, data = import_layout(fed, tmp_path, CHECKED)
    for path, content in TREE.items():
        (data / path).parent.mkdir(parents=True, exist_ok=True)
        (data / path).write_bytes(content)
    manifest = data.parent / "manifest.json"
    write_manifest(manifest_of_tree(data), manifest)
    layout.root.mkdir()

    def declared(document):
        with_placement(document)
        document["support"]["providers"] = ["alpha", "beta"]
        document["space"]["import"] = {"readonly": True, "shared": True}

    write_platform(layout.root, declared)
    ticker = Ticker()
    federation = make_import_federation(clock=ticker.clock, readonly=True)
    federation.add_provider(BETA_ID, "beta.test")
    for path, content in TREE.items():
        federation.put_on_storage(path, content)
    federation.scan_polls = 2
    federation.start_scan()
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    environ = {"EXPCORE_FED_IMPORT_STUDY": str(data), "EXPCORE_FED_IMPORT_STUDY_MANIFEST": str(manifest)}

    def run(**options):
        return fed.importing.verify_imports(
            layout,
            environ,
            timeout_s=60,
            http=federation.client_http(),
            clock=ticker.clock,
            sleep=ticker.sleep,
            **options,
        )

    return type("Checked", (), {"layout": layout, "fake": federation, "run": staticmethod(run), "environ": environ})


def test_o56_a_shared_import_is_shown_to_hold_exactly_its_manifest_after_up(checked):
    """O56: a shared import is shown to hold exactly its manifest after up."""
    records = checked.run()
    record = records["study-a-data"]
    assert (record["ok"], record["shared"], record["readonly"]) == (True, True, True)
    assert record["scan"]["status"] == "completed"
    assert record["blocks"] == {"files": 3, "whole": 3, "not_whole": []}
    assert record["manifest"]["files"] == 3
    assert list(record["listing"]) == ["providers[0]", "providers[1]"]  # the owner's listing and the mirror's


def test_o56_a_layout_with_no_shared_import_is_checked_for_nothing(fed, tmp_path):
    """O56: a layout with no shared import is checked for nothing."""
    layout, _ = import_layout(fed, tmp_path, WITH_IMPORT)
    assert fed.importing.verify_imports(layout, {}, timeout_s=1) == {}  # no platform.toml is read


def test_o56_a_manifest_variable_that_is_not_set_is_refused_before_any_call(checked):
    """O56: a manifest variable that is not set is refused before any call."""
    del checked.environ["EXPCORE_FED_IMPORT_STUDY_MANIFEST"]
    with pytest.raises(ValueError, match="EXPCORE_FED_IMPORT_STUDY_MANIFEST is not set"):
        checked.run()
    assert checked.fake.panel_calls == []
    assert checked.fake.provider_calls == []


def test_o56_a_platform_file_that_leaves_the_sharing_out_is_refused_before_any_call(checked):
    """O56: a platform file that leaves the sharing out is refused before any call."""
    from expcore_onedata.testing import with_placement, write_platform

    def not_shared(document):
        with_placement(document)
        document["support"]["providers"] = ["alpha", "beta"]
        document["space"]["import"] = {"readonly": True}

    write_platform(checked.layout.root, not_shared)
    with pytest.raises(RuntimeError, match="does not declare study-a-data as a shared, read-only import"):
        checked.run()
    assert checked.fake.panel_calls == []


def test_o56_a_space_the_platform_file_does_not_declare_is_refused(checked):
    """O56: a space the platform file does not declare is refused."""
    from expcore_onedata.testing import with_placement, write_platform

    def other(document):
        with_placement(document)
        document["support"]["providers"] = ["alpha", "beta"]
        document["space"]["name"] = "study-a-other"

    write_platform(checked.layout.root, other)
    with pytest.raises(RuntimeError, match="does not declare study-a-data"):
        checked.run()


def test_o56_a_file_missing_from_the_tree_fails_the_check_naming_the_space_and_no_path(checked):
    """O56: a file missing from the tree fails the check naming the space and no path."""
    checked.fake.drop_from_storage("notes/readme.txt")
    with pytest.raises(RuntimeError, match="the import of study-a-data was not shown to hold its dataset") as caught:
        checked.run()
    assert "does not hold exactly the manifest" in str(caught.value)
    assert "readme" not in str(caught.value)
    assert "EXPCORE_FED_IMPORT_STUDY" not in str(caught.value).replace("EXPCORE_FED_IMPORT_STUDY_MANIFEST", "")


def test_o56_a_file_of_another_size_fails_the_check(checked):
    """O56: a file of another size fails the check."""
    checked.fake.put_on_storage("records/day-02.csv", b"short")
    with pytest.raises(RuntimeError, match="does not hold exactly the manifest"):
        checked.run()


def test_o56_a_storage_that_is_writable_fails_the_check_before_the_scan_is_read(checked):
    """O56: a storage that is writable fails the check before the scan is read."""
    checked.fake.readonly = False
    with pytest.raises(RuntimeError, match="shared with other federations"):
        checked.run()
    assert not [call for call in checked.fake.panel_calls if call[2].endswith("/storage-import/auto/info")]


def test_o56_the_check_sends_nothing_that_could_write_to_the_tree(checked):
    """O56: the check sends nothing that could write to the tree."""
    checked.run(hashes=True)
    writes = [c for c in checked.fake.provider_calls if c[1] in ("POST", "PUT", "PATCH", "DELETE")]
    assert writes == []
    assert [c for c in checked.fake.panel_calls if c[1] != "GET"] == []

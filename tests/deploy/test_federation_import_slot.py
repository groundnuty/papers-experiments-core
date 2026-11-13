"""The layout of the storage-import slot (deploy/compose-federation/layouts/storage-import.toml, #184), its platform
template, and the script that makes a dataset to import (make_import_tree.py)."""

import tomllib

from federation_support import DEPLOY, STORAGE_IMPORT

TEMPLATE = DEPLOY / "layouts" / "storage-import.platform.toml.template"


def test_the_slot_has_its_own_project_domain_and_ports_beside_the_other_layouts(fed):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    assert (spec.project, spec.domain) == ("expcore-fedimp", "expcore-fedimp.test")
    assert [(s.name, s.port) for s in spec.services] == [
        ("zone", 29643),
        ("pine", 29644),
        ("birch", 29645),
        ("cedar", 29646),
    ]
    others = [fed.spec.load_spec(path) for path in (None, DEPLOY / "layouts" / "five-providers.toml")]
    assert all(spec.project != other.project and spec.domain != other.domain for other in others)
    assert not {s.port for s in spec.services} & {s.port for other in others for s in other.services}


def test_the_slot_declares_an_imported_read_only_space_and_an_uploaded_one_with_the_same_owner_and_mirrors(fed):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    imported, uploaded = spec.space("alpha-import"), spec.space("alpha-upload")
    assert imported.imported == fed.spec.Import(readonly=True, source_env="EXPCORE_FED_IMPORT_ALPHA")
    assert uploaded.imported is None
    for space in (imported, uploaded):
        assert (space.owner, space.mirrors, space.baseline) == ("pine", ("birch", "cedar"), False)
    assert spec.study_space == "alpha-upload"
    assert spec.holder == "pine"


def test_the_slots_three_users_read_both_spaces_and_nothing_else(fed):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    assert [(p.name, p.privileges, p.spaces, p.agent) for p in spec.principals] == [
        (user, ("space_view", "space_read_data"), ("alpha-import", "alpha-upload"), True)
        for user in ("alpha-reader", "alpha-tools", "alpha-health")
    ]


def test_the_slots_layout_holds_no_path_and_names_the_variable_only(fed):
    text = STORAGE_IMPORT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert 'source_env = "EXPCORE_FED_IMPORT_ALPHA"' in code
    assert "/" not in code.replace("deploy/", "")  # no path of any kind outside the comments


def test_only_the_owner_of_the_slot_binds_the_dataset_and_only_read_only(fed):
    model = fed.compose.model(fed.spec.load_spec(STORAGE_IMPORT))["services"]
    binds = {name: [v for v in s["volumes"] if v["target"].startswith("/volumes/import/")] for name, s in model.items()}
    assert binds["pine"] == [
        {
            "type": "bind",
            "source": "${EXPCORE_FED_IMPORT_ALPHA:?set by federation.py}",
            "target": "/volumes/import/alpha-import",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    assert not [name for name, found in binds.items() if found and name != "pine"]


# -- the platform template --------------------------------------------------------------------------------------------


def rendered(fed, tmp_path) -> str:
    """The template filled from facts, as up fills it, over made-up ids."""
    import fedboot

    spec = fed.spec.load_spec(STORAGE_IMPORT)
    layout = fed.layout.Layout(tmp_path, spec=spec)
    boot = fedboot.Bootstrap.__new__(fedboot.Bootstrap)
    boot.layout = layout
    boot.ids = fedboot.Ids(
        users={p.name: f"user-{p.name}" for p in spec.principals},
        providers={p.name: f"provider-{p.name}" for p in spec.providers},
        spaces={s.name: f"space-{s.name}" for s in spec.spaces},
    )
    facts = fed.facts.facts_of(boot)
    return fed.facts.render(TEMPLATE.read_text(encoding="utf-8"), fed.facts.placeholders(facts))


def test_the_template_fills_every_placeholder_and_is_a_toml_file_with_two_spaces(fed, tmp_path):
    text = rendered(fed, tmp_path)
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert fed.facts.PLACEHOLDER.search(body) is None
    spaces = tomllib.loads(text)["spaces"]
    assert [(s["label"], s["name"], s["id"]) for s in spaces] == [
        ("imported", "alpha-import", "space-alpha-import"),
        ("uploaded", "alpha-upload", "space-alpha-upload"),
    ]
    for entry in spaces:
        assert (entry["prefixes"], entry["providers"], entry["local"], entry["holder"]) == (
            ["alpha-"],
            ["pine", "birch", "cedar"],
            "pine",
            "pine",
        )


def test_the_template_marks_the_imported_entry_read_only_as_the_layout_declares_it(fed, tmp_path):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    spaces = {s["name"]: s for s in tomllib.loads(rendered(fed, tmp_path))["spaces"]}
    assert spaces["alpha-import"]["import"] == {"readonly": spec.space("alpha-import").imported.readonly}
    assert "import" not in spaces["alpha-upload"]


def test_the_templates_ids_and_users_are_those_the_layout_declares(fed, tmp_path):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    data = tomllib.loads(rendered(fed, tmp_path))
    assert set(data["providers"]) == {p.name for p in spec.providers}
    assert {p["username"] for p in data["principals"].values()} == {p.name for p in spec.principals}
    assert {"space", "support"}.isdisjoint(data)  # the one-space form is never beside [[spaces]]


def test_the_template_names_no_variable_and_no_path(fed):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "EXPCORE_FED_IMPORT" not in text
    for host_path in ("/srv", "/mnt", "/home"):
        assert host_path not in text

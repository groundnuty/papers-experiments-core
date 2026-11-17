"""O35: a Compose platform template can name the federation's root, so the rendered file's paths are absolute.

`up` renders platform.toml into the root with every path relative to it, which is right there and wrong for a copy
beside a study's `study.toml`: the core refuses a relative `[faults] journal` in a study directory, and a relative CA
bundle, credential path or Compose file would resolve into the study. A template that writes `@hpc_root@/...` (the
name the HPC render has for it) gets the root's absolute path, validated at render, and facts.json records it. A
template that does not name it renders as it always did.
"""

import json
import tomllib
from pathlib import Path

import pytest
from federation_support import MINIMAL
from hpc_fed_support import hpc_on_path
from slots_support import TEMPLATE, ids_of, layout_with_template, make_root

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.fault_settings import load_faults

ROOT_NAME = "@hpc_root@"

# a Compose template whose every path into the root is written through the placeholder
ROOTED = f"""\
zone = {{ name = "zone", url = "@zone_url@" }}
providers.oak = {{ url = "@provider_url.oak@", id = "@provider_id.oak@" }}
providers.elm = {{ url = "@provider_url.elm@", id = "@provider_id.elm@" }}
space = {{ name = "@space_name@", id = "@space_id.alpha-space@", prefixes = ["alpha-"] }}
support = {{ providers = ["oak", "elm"], local = "oak" }}
credentials.admin = {{ file = "{ROOT_NAME}/secrets/admin.token" }}
credentials.users = {{ directory = "{ROOT_NAME}/secrets/users", pattern = "{{user}}.pass" }}
principals.data_path = {{ username = "alpha-reader", user_id = "@user_id.alpha-reader@" }}
placement = {{ holder = "oak" }}
tls = {{ ca_file = "{ROOT_NAME}/ca/federation-ca.pem" }}

[faults]
sanctioned = true
journal = "{ROOT_NAME}/fault-journal"
compose_file = "{ROOT_NAME}/outage.compose.yaml"
project = "expcore-test"
poll_s = 1
settle_timeout_s = 5
ready_timeout_s = 5
clear_attempts = 2
admission_timeout_s = 1
session_drain_s = 0

[faults.services]
oak = "oak"
elm = "elm"

[tokens]
margin_s = 900
"""


def staged(fed, tmp_path, template=ROOTED, *, root=None, marker=True):
    """A layout of `template`, a root (`tmp_path/root` unless `root` is given) holding the federation's mark, and a
    bootstrap that holds the ids Onezone would have given."""
    spec = fed.spec.load_spec(layout_with_template(template, tmp_path / "layout", MINIMAL))
    where = tmp_path / "root" if root is None else root
    if root is None:
        where.mkdir()
    layout = fed.layout.Layout(where, spec=spec)
    if marker and where.is_absolute() and where.is_dir():
        layout.marker.write_text(spec.project + "\n", encoding="utf-8")
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout, boot.ids = layout, fed.boot.Ids(admin="uid-admin", **ids_of(spec))
    return layout, boot


def study_copy(layout, tmp_path) -> Path:
    """The rendered platform.toml copied into a study directory, the one that holds study.toml."""
    study = tmp_path / "study"
    study.mkdir()
    (study / "study.toml").write_text("# a study directory is the one that holds study.toml\n", encoding="utf-8")
    copy = study / "platform.toml"
    copy.write_bytes(layout.platform.read_bytes())
    return copy


def test_o35_a_template_naming_the_root_has_every_path_into_it_absolute_and_facts_record_the_root(fed, tmp_path):
    """O35: the CA bundle, the credential paths, the faults journal and the Compose file, each written
    `@hpc_root@/...`, render as absolute paths into the root, and facts.json records the root they were filled with."""
    layout, boot = staged(fed, tmp_path)
    assert fed.facts.write_facts(layout, boot) == "facts.json and platform.toml written"
    root = str(layout.root)
    document = tomllib.loads(layout.platform.read_text(encoding="utf-8"))
    assert document["credentials"]["admin"]["file"] == f"{root}/secrets/admin.token"
    assert document["credentials"]["users"]["directory"] == f"{root}/secrets/users"
    assert document["tls"]["ca_file"] == f"{root}/ca/federation-ca.pem"
    assert document["faults"]["journal"] == f"{root}/fault-journal"
    assert document["faults"]["compose_file"] == f"{root}/outage.compose.yaml"
    assert ROOT_NAME not in layout.platform.read_text(encoding="utf-8")
    assert json.loads(layout.facts.read_text(encoding="utf-8"))["root"] == root


def test_o35_the_rendered_file_copied_beside_study_toml_loads_and_reads_the_roots_own_files(fed, tmp_path):
    """O35: the render's copy in a study directory passes the load check that refuses a relative journal there
    (`load_faults`), and its CA bundle, credentials, journal and Compose file are the root's, not the study's."""
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("the openssl command makes the CA")
    layout, boot = staged(fed, tmp_path)
    make_root(layout)
    layout.ca_pem.parent.mkdir()
    layout.ca_pem.write_bytes(tls.make_federation_ca(tmp_path / "pki").ca.read_bytes())
    fed.facts.write_facts(layout, boot)
    copy = study_copy(layout, tmp_path)
    config = load_platform(copy)
    settings = load_faults(copy, config)
    assert settings.journal == layout.journal.resolve()
    assert settings.compose_file == (layout.root / "outage.compose.yaml").resolve()
    assert config.tls.ca_file == layout.ca_pem.resolve()
    assert Path(config.admin.ref) == layout.admin_token.resolve()
    assert Path(config.users.ref) == layout.users.resolve()
    assert not (copy.parent / "fault-journal").exists()  # nothing of the federation lands in the study


def test_o35_the_same_file_with_its_paths_relative_is_refused_beside_study_toml(fed, tmp_path):
    """O35: the render before the placeholder (every path relative to the root) copied beside study.toml is what the
    core refuses: its paths would resolve into the study."""
    layout, boot = staged(fed, tmp_path)
    make_root(layout)
    fed.facts.write_facts(layout, boot)
    lines = layout.platform.read_text(encoding="utf-8").replace(f"{layout.root}/", "").splitlines(keepends=True)
    layout.platform.write_text("".join(x for x in lines if not x.startswith("tls =")), encoding="utf-8")
    copy = study_copy(layout, tmp_path)
    with pytest.raises(ConfigError):  # the admin token it names is not in the study
        load_platform(copy)
    with pytest.raises(ConfigError, match=r"key 'journal'"):
        load_faults(copy, load_platform(copy, require_secrets=False))


def test_o35_a_template_that_does_not_name_the_root_renders_exactly_as_before(fed, tmp_path):
    """O35: no `@hpc_root@` in the template: platform.toml is the template filled from the facts and nothing else, and
    facts.json holds no `root`, so a layout that never used it is as it was."""
    layout, boot = staged(fed, tmp_path, TEMPLATE)
    facts = fed.facts.facts_of(boot)
    fed.facts.write_facts(layout, boot)
    assert layout.platform.read_text(encoding="utf-8") == fed.facts.render(TEMPLATE, fed.facts.placeholders(facts))
    assert json.loads(layout.facts.read_text(encoding="utf-8")) == json.loads(json.dumps(facts))
    assert "root" not in facts


def test_o35_the_default_layouts_template_renders_as_before(fed, tmp_path):
    """O35: the committed default template (relative paths, no `@hpc_root@`) renders as `render` and `placeholders`
    alone fill it, whatever the root is."""
    spec = fed.spec.load_spec()
    layout = fed.layout.Layout(tmp_path, spec=spec)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout, boot.ids = layout, fed.boot.Ids(admin="uid-admin", **ids_of(spec))
    template = spec.platform_template.read_text(encoding="utf-8")
    assert ROOT_NAME not in template
    facts = fed.facts.facts_of(boot)
    fed.facts.write_facts(layout, boot)
    assert layout.platform.read_text(encoding="utf-8") == fed.facts.render(template, fed.facts.placeholders(facts))


def refuse_relative(fed, tmp_path):
    return staged(fed, tmp_path, root=Path("fed-root-given-relative"))


def refuse_missing(fed, tmp_path):
    return staged(fed, tmp_path, root=tmp_path / "absent")


def refuse_a_file(fed, tmp_path):
    (tmp_path / "plain-file").write_text("not a directory\n", encoding="utf-8")
    return staged(fed, tmp_path, root=tmp_path / "plain-file")


def refuse_unmarked(fed, tmp_path):
    (tmp_path / "root").mkdir()
    return staged(fed, tmp_path, root=tmp_path / "root", marker=False)


def refuse_another_projects(fed, tmp_path):
    layout, boot = staged(fed, tmp_path)
    layout.marker.write_text("another-project\n", encoding="utf-8")
    return layout, boot


def refuse_a_linked_root(fed, tmp_path):
    real, _ = staged(fed, tmp_path)
    (tmp_path / "linked").symlink_to(real.root)
    return staged(fed, tmp_path / "again", root=tmp_path / "linked")


def refuse_a_link_on_the_way(fed, tmp_path):
    staged(fed, tmp_path)
    (tmp_path / "via").symlink_to(tmp_path)
    return staged(fed, tmp_path / "again", root=tmp_path / "via" / "root")


def refuse_a_quote_in_the_path(fed, tmp_path):
    (tmp_path / 'a"b').mkdir()
    return staged(fed, tmp_path, root=tmp_path / 'a"b')


REFUSED = {
    "relative": (refuse_relative, "absolute"),
    "missing": (refuse_missing, "exist"),
    "a file": (refuse_a_file, "directory"),
    "unmarked": (refuse_unmarked, "mark"),
    "another project's": (refuse_another_projects, "mark"),
    "a linked root": (refuse_a_linked_root, "symbolic link"),
    "a link on the way": (refuse_a_link_on_the_way, "symbolic link"),
    "a quote in the path": (refuse_a_quote_in_the_path, "TOML"),
}


@pytest.mark.parametrize(("make", "reason"), list(REFUSED.values()), ids=list(REFUSED))
def test_o35_a_root_that_is_no_directory_of_ours_is_refused_at_render_before_anything_is_written(
    fed, tmp_path, make, reason
):
    """O35: a template names the root, and the root is no absolute, existing, unlinked directory carrying this
    federation's mark whose path a TOML string can hold: refused, naming the placeholder and the reason (never the
    path), with neither facts.json nor platform.toml written."""
    layout, boot = make(fed, tmp_path)
    with pytest.raises(RuntimeError, match=ROOT_NAME) as refused:
        fed.facts.write_facts(layout, boot)
    assert reason in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
    assert not layout.facts.exists()
    assert not layout.platform.exists()


def test_o35_a_root_that_is_a_link_is_refused_only_where_a_template_names_it(fed, tmp_path):
    """O35: the root checks guard the placeholder's value, not every render: a layout whose template never names the
    root renders into a root reached through a link as it always did."""
    real, _ = staged(fed, tmp_path)
    (tmp_path / "linked").symlink_to(real.root)
    layout, boot = staged(fed, tmp_path / "again", TEMPLATE, root=tmp_path / "linked")
    assert fed.facts.write_facts(layout, boot) == "facts.json and platform.toml written"


def test_o35_compose_and_hpc_name_the_roots_placeholder_alike(fed, tmp_path):
    """O35: one template text can name the root on either platform: the key the HPC render fills
    (`hpcboot.hpc_values`) is the name Compose's `fedfacts` fills, and the value is the root as given."""
    with hpc_on_path() as hpc:
        values = hpc.boot.hpc_values(["unit"], 1, 1, tmp_path)
    assert f"@{fed.facts.ROOT_PLACEHOLDER}@" == ROOT_NAME
    assert values[fed.facts.ROOT_PLACEHOLDER] == str(tmp_path)

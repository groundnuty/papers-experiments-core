"""O74: what `hpcfed.py up` does with `HPC_STUDY_ARTIFACTS`, site.env's optional directory of the study's own artifacts
(its tool server's clone and venv, its manifests). In its preflight, on the login node and before anything is submitted,
`up` takes the digest of the directory's tree (`hpcidentity.tree_sha256`), records it in the state file and hands it to
the core's phase among its values, which writes it into the render identity; the core's step on a node, whose
environment is an allowlist, then has it from the state and never from the environment. Slurm is the fake of
hpc_fed_support; the core's phase is recorded; nothing runs srun."""

import os
import re
import shutil
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path

ARTIFACTS = "study_artifacts_tree_sha256"
HEX = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcidentity

        modules.identity = hpcidentity
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


@pytest.fixture
def tree(tmp_path) -> Path:
    """A study's artifacts directory under `tmp_path`: a clone, a venv with a link, a manifest."""
    root = tmp_path / "studies" / "a-study" / "artifacts"
    for name, data in {"clone/server.py": b"print('serve')\n", "manifests/space.json": b"{}\n",
                       "venv/lib/python3.12/site-packages/pkg/__init__.py": b"VALUE = 1\n"}.items():  # fmt: skip
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    (root / "venv" / "bin").mkdir()
    (root / "venv" / "bin" / "python").symlink_to("/an/interpreter/bin/python3.12")
    return root


class Rig:
    """A driver over the fake Slurm, the core's phase recorded with the values it is handed."""

    def __init__(self, hpc, tmp_path, site, layout=LAYOUT):
        self.hpc, self.site, self.fake, self.root = hpc, site, FakeSlurm(), tmp_path / "fed" / "root"
        self.driver = hpc.cli.make_driver(layout, str(self.root), runner=self.fake)
        self.driver.sleep = self.driver.slurm.sleep = lambda s: None
        self.driver.poll_s = self.driver.slurm.poll_s = 0.0
        self.driver.relay_available = lambda: False
        self.values = []
        self.driver.core = lambda layout, owner, values, environ=None: self.values.append(values) or "core: linked"

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def submitted(self) -> bool:
        return any(call[0] == "sbatch" for call in self.fake.calls)

    def state(self) -> dict:
        return self.driver.store.read()


# -- the site's key --------------------------------------------------------------------------------------------------


def test_o74_the_site_reads_hpc_study_artifacts_and_records_it_in_the_state_form(hpc, site, tree):
    """O74: `HPC_STUDY_ARTIFACTS` is an optional site value, an absolute path, kept in the site's JSON form (the state
    file's) and read back; with none the field is empty, as it is for every other optional key."""
    declared = hpc.settings.site_from_environment({**site, "HPC_STUDY_ARTIFACTS": str(tree)})
    assert declared.study_artifacts == str(tree)
    assert declared.to_json()["study_artifacts"] == str(tree)
    assert hpc.settings.Site.from_json(declared.to_json()).study_artifacts == str(tree)
    left_out = hpc.settings.site_from_environment(site)
    assert left_out.study_artifacts == ""
    assert hpc.settings.Site.from_json(left_out.to_json()).study_artifacts == ""


@pytest.mark.parametrize("value", ["studies/artifacts", "/shared/../artifacts", "/shared/with space", "/a;b"])
def test_o74_a_study_artifacts_path_that_is_not_a_plain_absolute_one_is_refused_naming_the_variable(hpc, site, value):
    """O74: a relative path, a `..`, or a character the driver cannot pass safely is refused, naming the variable."""
    with pytest.raises(hpc.settings.SettingsError, match="HPC_STUDY_ARTIFACTS"):
        hpc.settings.site_from_environment({**site, "HPC_STUDY_ARTIFACTS": value})


# -- up: the digest, recorded and handed on ----------------------------------------------------------------------------


def test_o74_up_digests_the_artifacts_directory_records_it_and_hands_it_to_the_cores_phase(hpc, tmp_path, site, tree):
    """O74: with the key declared, the core's phase gets `study_artifacts_tree_sha256` among its values, the digest of
    the tree as `hpcidentity.tree_sha256` takes it, and the state file records the same digest."""
    rig = Rig(hpc, tmp_path, {**site, "HPC_STUDY_ARTIFACTS": str(tree)})
    rig.up()
    (values,) = rig.values
    assert HEX.fullmatch(values[ARTIFACTS])
    assert values[ARTIFACTS] == hpc.identity.tree_sha256(tree)
    assert rig.state()[ARTIFACTS] == values[ARTIFACTS]


def test_o74_up_without_the_key_hands_the_core_no_digest_and_records_none(hpc, tmp_path, site):
    """O74: no `HPC_STUDY_ARTIFACTS`: no digest among the core's values and none in the state (never a null)."""
    rig = Rig(hpc, tmp_path, site)
    rig.up()
    (values,) = rig.values
    assert ARTIFACTS not in values
    assert ARTIFACTS not in rig.state()
    assert rig.state()["site"]["study_artifacts"] == ""


def test_o74_the_digest_follows_the_tree_as_it_is_at_up(hpc, tmp_path, site, tree):
    """O74: two ups over a tree differing by one byte record two digests: the digest is of the tree `up` finds."""
    first = Rig(hpc, tmp_path, {**site, "HPC_STUDY_ARTIFACTS": str(tree)})
    first.up()
    (tree / "clone" / "server.py").write_bytes(b"print('sErve')\n")
    other = tmp_path / "other"
    (other / "fed").mkdir(parents=True)
    second = Rig(hpc, other, {**site, "HPC_STUDY_ARTIFACTS": str(tree)})
    second.up()
    assert first.state()[ARTIFACTS] != second.state()[ARTIFACTS]


def test_o74_the_cores_step_on_a_node_has_the_digest_from_the_state_and_not_its_environment(hpc, tmp_path, site, tree):
    """O74: `[hpc] core = "step"` runs `hpcfed.py core` on the zone's node, whose environment is an allowlist that
    carries no site variable: the core's phase there is handed the digest `up` recorded, though its environment holds
    neither the key nor the directory, and the tree is not read again."""
    rig = Rig(hpc, tmp_path, {**site, "HPC_STUDY_ARTIFACTS": str(tree)})
    rig.up()
    digest = rig.state()[ARTIFACTS]
    shutil.rmtree(tree)  # a node that cannot see the directory has the digest all the same
    rig.driver.environ = {}
    rig.driver.core_here()
    assert rig.values[-1][ARTIFACTS] == digest


# -- up: what is refused before the cluster is touched ---------------------------------------------------------------


def refuse(rig: Rig, match: str) -> str:
    """`up` raises `RuntimeError` naming the variable and `match`, and made nothing: no job, no root, no core phase."""
    with pytest.raises(RuntimeError, match=f"HPC_STUDY_ARTIFACTS.*{match}") as refused:
        rig.up()
    assert not rig.submitted()
    assert not rig.root.exists()
    assert rig.values == []
    assert str(rig.root.parent.parent) not in str(refused.value)  # no absolute path: tmp_path is the tree's parent
    return str(refused.value)


def test_o74_up_refuses_a_tree_holding_a_special_file_naming_its_relative_path(hpc, tmp_path, site, tree):
    """O74: a FIFO in the artifacts directory fails `up` before any job: the message names the variable and the
    file's path relative to the directory."""
    os.mkfifo(tree / "venv" / "run")
    refuse(Rig(hpc, tmp_path, {**site, "HPC_STUDY_ARTIFACTS": str(tree)}), "venv/run")


@pytest.mark.parametrize("what", ["missing", "a file", "a link"])
def test_o74_up_refuses_an_artifacts_directory_that_is_no_directory_of_its_own(hpc, tmp_path, site, tree, what):
    """O74: a path that does not exist, is a file, or is a link (to a directory) is no artifacts directory."""
    places = {"missing": tmp_path / "missing", "a file": tree / "clone" / "server.py", "a link": tmp_path / "link"}
    chosen = places[what]
    if what == "a link":
        chosen.symlink_to(tree)
    refuse(Rig(hpc, tmp_path, {**site, "HPC_STUDY_ARTIFACTS": str(chosen)}), "directory")


def test_o74_a_layout_with_no_platform_template_writes_no_record_so_up_takes_no_digest(hpc, tmp_path, site, tree):
    """O74: a layout that names no platform template writes no render identity, so its `up` reads no tree: the key is
    left out of the state and the values, whatever the site declares."""
    folder = tmp_path / "layout"
    folder.mkdir()
    text = LAYOUT.read_text(encoding="utf-8")
    kept = [line for line in text.splitlines() if not line.startswith(("platform_template", "probe_space"))]
    assert len(kept) == len(text.splitlines()) - 2  # the probe space needs a template, so it goes with it
    (folder / LAYOUT.name).write_text("\n".join(kept) + "\n", encoding="utf-8")
    site = {k: v for k, v in site.items() if not k.startswith(("HPC_AGENT", "HPC_EGRESS", "HPC_ONECLIENT"))}
    rig = Rig(hpc, tmp_path, {**site, "HPC_STUDY_ARTIFACTS": str(tmp_path / "missing")}, layout=folder / LAYOUT.name)
    rig.up()
    assert ARTIFACTS not in rig.values[0]
    assert ARTIFACTS not in rig.state()

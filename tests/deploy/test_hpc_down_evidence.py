"""O63: `hpcfed.py down` does not remove a federation's root while evidence a campaign left in it is uncollected. A root
that has shard results recorded against it (a step ran: `shards.py step` leaves `shard-steps/<label>.json` there) and
no collected-evidence marker (`shards.py collect` writes one, naming the root archive's hash) is refused before
anything is stopped, unless `--discard-evidence` is given, which down's output records. A root where no step ever ran
goes as before. Against the fake Slurm of hpc_fed_support; nothing reaches a cluster."""

import json

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path
from root_evidence_support import record_step

ARCHIVE = "9" * 64  # a root archive's SHA-256, as a marker names it


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcevidence

        modules.evidence = hpcevidence
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


class Rig:
    """A driver over the fake Slurm, up once, the core's phase recorded instead of run."""

    def __init__(self, hpc, tmp_path, site):
        self.hpc, self.fake, self.root = hpc, FakeSlurm(), tmp_path / "fed" / "root"
        self.driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
        self.driver.sleep = lambda s: None
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False
        self.driver.up(hpc.settings.site_from_environment(site))
        self.fake.calls.clear()

    def collected(self, label="shard-1"):
        """What `collect` leaves in the root once it has archived it (the marker, as `hpcevidence` writes it)."""
        self.hpc.evidence.write_marker(self.root, label, ARCHIVE)


def test_down_refuses_a_root_with_a_step_record_and_no_marker_before_it_stops_anything(hpc, tmp_path, site):
    """O63: the refusal names the shard and the remedies (collect, or `--discard-evidence`); no job is cancelled, no
    service is stopped, the state is not touched, and the root stays, so a collect can still read it."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    state_before = (rig.root / "hpc-federation.json").read_bytes()
    with pytest.raises(RuntimeError, match=r"shard-1.*collect.*--discard-evidence") as refused:
        rig.driver.down()
    assert "evidence" in str(refused.value)
    assert rig.fake.cancelled == []
    assert rig.fake.calls == []  # not a squeue, a scancel or an srun
    assert (rig.root / "hpc-federation.json").read_bytes() == state_before
    assert rig.root.exists()


def test_down_names_every_shard_with_results_and_no_marker(hpc, tmp_path, site):
    """O63: two shards recorded in one root (a resume on another shard's label never happens, but a root may hold
    more than one record): both are named."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    record_step(rig.root, "shard-2")
    with pytest.raises(RuntimeError, match=r"shard-1.*shard-2"):
        rig.driver.down()


def test_down_proceeds_once_collect_has_written_the_marker(hpc, tmp_path, site):
    """O63: a marker for the step records that are there lets down go as before: the job cancelled, the root removed."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "discard" not in printed
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_down_refuses_when_a_step_started_after_the_evidence_was_collected(hpc, tmp_path, site):
    """O63: a resume after `collect` adds to the journals the archive lacks: the marker names the step records it was
    collected against, so a changed record makes it stale and down refuses, saying to collect again."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    record_step(rig.root, "shard-1", starts=2)
    with pytest.raises(RuntimeError, match=r"shard-1.*after.*collect"):
        rig.driver.down()
    assert rig.fake.cancelled == []
    assert rig.root.exists()


def test_down_refuses_when_a_second_shard_started_after_the_evidence_was_collected(hpc, tmp_path, site):
    """O63: a record that did not exist at the collect is as stale as one that changed."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    record_step(rig.root, "shard-2")
    with pytest.raises(RuntimeError, match=r"shard-2"):
        rig.driver.down()


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"format": "something else"}',
                                     json.dumps({"format": "expcore-hpc-evidence-collected/1"})])  # fmt: skip
def test_down_refuses_a_marker_it_cannot_read_as_collects(hpc, tmp_path, site, content):
    """O63: a marker is a file of a fixed form (format, the root archive's SHA-256, the step digest): anything else
    is no evidence that anything was collected."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    (rig.root / "evidence-collected.json").write_text(content)
    with pytest.raises(RuntimeError, match="evidence"):
        rig.driver.down()
    assert rig.fake.cancelled == []


def test_down_refuses_a_marker_that_is_a_link(hpc, tmp_path, site):
    """O63: a marker is read as a file of the root, never through a link (here to a file that would pass)."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    real = rig.root / "evidence-collected.json"
    kept = tmp_path / "kept.json"
    real.rename(kept)
    real.symlink_to(kept)
    with pytest.raises(RuntimeError, match="evidence"):
        rig.driver.down()


def test_discard_evidence_lets_down_remove_an_uncollected_root_and_the_output_records_it(hpc, tmp_path, site):
    """O63: down goes as before, and what it prints (and says while it works) names the shards whose evidence was not
    collected and is removed with the root."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    said = []
    rig.driver.say = said.append
    printed = rig.driver.down(discard_evidence=True)
    assert printed.startswith("down: --discard-evidence")
    assert "shard-1" in printed.splitlines()[0]
    assert "down: the held job 4242 is gone, and so is the root" in printed
    assert any("--discard-evidence" in line and "shard-1" in line for line in said)
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_a_root_where_no_step_ever_ran_goes_down_as_it_always_did(hpc, tmp_path, site):
    """O63: no record, no marker, no flag: nothing changes, and the output is the old output."""
    rig = Rig(hpc, tmp_path, site)
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "evidence" not in printed
    assert not rig.root.exists()


def test_discard_evidence_on_a_root_with_nothing_to_discard_says_nothing_of_it(hpc, tmp_path, site):
    """O63: the flag records a discard only when there was something to discard."""
    rig = Rig(hpc, tmp_path, site)
    printed = rig.driver.down(discard_evidence=True)
    assert "evidence" not in printed


def test_the_command_takes_the_flag_for_down_alone(hpc, tmp_path, site):
    """O63: `hpcfed.py down --discard-evidence` is accepted; the flag on any other command is an error, never ignored
    (a discard is down's alone), and without it a root with uncollected evidence exits 1 with the refusal."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    words = ["--layout", str(LAYOUT), "--root", str(rig.root)]
    assert hpc.cli.main([*words, "describe", "--discard-evidence"], {}, runner=rig.fake) == 2
    assert hpc.cli.main([*words, "down"], {}, runner=rig.fake) == 1
    assert rig.root.exists()
    assert hpc.cli.main([*words, "down", "--discard-evidence"], {}, runner=rig.fake) == 0
    assert not rig.root.exists()

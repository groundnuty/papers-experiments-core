"""The rig of the `hpcfed.py down` evidence tests (O63): a driver over the fake Slurm of `hpc_fed_support`, up once, the
core's phase recorded instead of run, and the site it was brought up with. Shared by `test_hpc_down_evidence.py` and
`test_hpc_down_self_rewritten.py`. Nothing here reaches a cluster; every name is invented."""

from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site


def make_site(tmp_path):
    """The site values `up` needs: image archives with their provenance, a log directory, the agent's node."""
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
        (self.root / "facts.json").write_text("{}")  # what the core's phase writes, which this rig stands in for
        self.fake.calls.clear()

    def collected(self, label="shard-1"):
        """What `collect` leaves in the root once it has archived it: the marker naming the archive of the evidence
        as it is now and the hash of each file of it, as `hpcevidence` writes it."""
        held = self.hpc.evidence.archive_root(self.root, label)
        self.hpc.evidence.write_marker(self.root, label, held.sha256, held.steps_sha256, files=held.hashes)

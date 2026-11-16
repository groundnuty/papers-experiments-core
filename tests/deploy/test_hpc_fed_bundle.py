"""The node-side scripts the HPC federation driver adds to the bundle (deploy/hpc-federation/bundle), run offline on a
work directory laid out as node-init.sh makes one: zone-export.sh prints the zone's passphrase or its CA, and only from
a private work directory of a ready zone."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle"
PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
PASSPHRASE = "A1b2C3d4E5f6G7h8I9j0K1l2"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash on this host")


@pytest.fixture
def zone(tmp_path):
    work = tmp_path / "hpcop-oz.abcdef"
    secrets = work / "rootfs" / "root" / ".hpcop"
    secrets.mkdir(parents=True)
    work.chmod(0o700)
    (secrets / "passphrase").write_text(PASSPHRASE + "\n", encoding="utf-8")
    (secrets / "passphrase").chmod(0o600)
    ca = work / "rootfs" / "volumes" / "persistence_backup" / "etc" / "oz_panel" / "OnedataTestWebServerCa.pem"
    ca.parent.mkdir(parents=True)
    ca.write_text(PEM, encoding="utf-8")
    return work


def export(work: Path, what: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BUNDLE / "zone-export.sh"), what, "--workdir", str(work)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
    )


def test_the_zone_export_refuses_a_zone_that_is_not_ready(zone):
    """O57: the zone export refuses a zone that is not ready."""
    done = export(zone, "passphrase")
    assert done.returncode == 1
    assert "not ready" in done.stderr
    assert done.stdout == ""


def test_the_zone_export_prints_the_passphrase_alone_on_stdout_and_the_ca(zone):
    """O57: stdout carries the one line the driver writes into the root's 0600 file, and nothing else; the CA is the
    image's public test CA."""
    (zone / "rootfs" / "root" / "service-ready.lock").touch()
    done = export(zone, "passphrase")
    assert done.returncode == 0
    assert done.stdout == PASSPHRASE + "\n"
    assert PASSPHRASE not in done.stderr
    assert export(zone, "ca").stdout == PEM


def test_the_zone_export_refuses_a_passphrase_file_others_could_read(zone):
    """O57: the zone export refuses a passphrase file others could read."""
    (zone / "rootfs" / "root" / "service-ready.lock").touch()
    (zone / "rootfs" / "root" / ".hpcop" / "passphrase").chmod(0o644)
    done = export(zone, "passphrase")
    assert done.returncode == 1
    assert "not mode 0600" in done.stderr
    assert done.stdout == ""


def test_the_zone_export_takes_only_a_private_work_directory_of_the_zone(zone, tmp_path):
    """O57: the zone export takes only a private work directory of the zone."""
    (zone / "rootfs" / "root" / "service-ready.lock").touch()
    zone.chmod(0o755)
    assert export(zone, "passphrase").returncode == 1
    other = tmp_path / "hpcop-op.abcdef"
    other.mkdir(mode=0o700)
    assert "not an hpcop-oz work directory" in export(other, "ca").stderr

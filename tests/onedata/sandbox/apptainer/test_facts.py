"""O56, O60: a run's `infrastructure_facts` lists `mount_refused` only when its sandbox refuses a mount data path.

The Apptainer sandbox refuses a mount run at its start when it is given no mount helper (hook c) or the platform
declares no `[sandbox.mount]`; the shipped helper then refuses `io_mode = "direct"` (HPC mounts are proxy-only until the
direct-I/O comparison passes). A run whose sandbox accepts a mount, a mount run itself included, never carries the key.
"""

import dataclasses

import pytest
from apptainer_support import make_settings, make_spec, options, pinned_bundle, start
from fake_mount_node import FakeMountNode
from fake_node import FakeNode

from expcore_onedata.sandbox.api import StartRefused
from expcore_onedata.sandbox.apptainer import facts
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.mounthook import MOUNT_REFUSED
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.settings import MountSettings

KEY = "mount_refused"
# a run of the default work disk, the directory, lists none of the image's keys (O71, `facts.WORK_DISK_KEYS`)
DIRECTORY = sorted(set(facts.FACTS) - ({k for keys in facts.WORK_DISK_KEYS.values() for k in keys} - {"disk_watch"}))
OTHERS = sorted(set(DIRECTORY) - {KEY})


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))


def recorded(box) -> list[str]:
    return box.metadata()["infrastructure_facts"]["facts"]


def test_o56_the_record_lists_every_departure_and_the_mount_refusal_only_when_asked():
    """O56: the key is stable and its text is looked up in `FACTS`; the list is sorted, with or without the key."""
    refused, accepted = facts.record(mount_refused=True), facts.record(mount_refused=False)
    assert refused["facts"] == DIRECTORY
    assert accepted["facts"] == OTHERS
    assert {**refused, "facts": None} == {**accepted, "facts": None}  # the rest of the record does not depend on it
    assert KEY in facts.FACTS


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o56_a_run_whose_sandbox_has_no_mount_helper_records_that_a_mount_is_refused(settings, level):
    """O56: a REST run, or a run with no data path, on a sandbox given no helper: a mount would be refused there."""
    box = start(FakeNode(), settings, make_spec(level))
    try:
        assert recorded(box) == DIRECTORY
    finally:
        box.stop()


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o56_a_run_whose_sandbox_would_accept_a_mount_does_not_record_it(settings, level):
    """O56: with the helper given and `[sandbox.mount]` declared, a mount is not refused: no run of that sandbox,
    whatever its own data path, carries the key."""
    box = start(FakeNode(), settings, make_spec(level), mount_helper=HM.host_mount_factory())
    try:
        assert recorded(box) == OTHERS
    finally:
        box.stop()


def test_o56_a_sandbox_with_the_helper_and_no_mount_table_still_records_the_refusal(settings):
    """O56: the helper is not enough, the platform's file must declare `[sandbox.mount]` too (the refusal's other
    condition), and a mount run on it is refused with the same words."""
    without_table = dataclasses.replace(settings, mount=None)
    factory = HM.host_mount_factory()
    box = start(FakeNode(), without_table, make_spec("via-gate"), mount_helper=factory)
    try:
        assert KEY in recorded(box)
    finally:
        box.stop()
    with pytest.raises(StartRefused) as refused:
        start(FakeNode(), without_table, make_spec("mounted"), mount_helper=factory)
    assert str(refused.value) == MOUNT_REFUSED


def test_o60_a_mount_run_does_not_record_that_a_mount_is_refused(settings):
    """O60: the run that mounted is the proof the mount was not refused: its facts lack the key, and its record holds
    the helper's own metadata under `mount`."""
    node = FakeMountNode()
    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
    box = ApptainerSandbox.start(node, settings, make_spec("mounted"), **options(mount_helper=factory))
    try:
        assert recorded(box) == OTHERS
        assert box.metadata()["mount"]["oneclient"] == "node"
    finally:
        box.stop()


def test_o60_hpc_mounts_are_proxy_only_a_direct_mount_run_is_refused_with_the_helper_given(settings):
    """O60: with the helper and a `[sandbox.mount]` that declares `io_mode = "direct"`, the sandbox's start refuses the
    mount run before any cell: HPC mounts are proxy-only until the direct-I/O comparison passes."""
    direct = MountSettings("/data", "direct", storages=(("st1", "/invented/storage"),), direct_check_file="sp/f")
    node = FakeMountNode()
    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
    with pytest.raises(StartRefused, match="direct"):
        ApptainerSandbox.start(
            node, dataclasses.replace(settings, mount=direct), make_spec("mounted"), **options(mount_helper=factory)
        )
    assert not node.execs


@pytest.mark.parametrize("kind", ["tmpfs", "shared_filesystem"])
def test_o57_a_run_records_where_the_federation_s_providers_keep_their_storage(tmp_path, kind):
    """O57: where a provider stores its data shapes a run's I/O timing, so each run's facts and its backend record
    carry the kind the platform declares (`provider_storage`), and a reader of one run knows which kind it ran on."""
    box = start(FakeNode(), make_settings(tmp_path, provider_storage=kind), make_spec("via-gate"))
    try:
        meta = box.metadata()
        assert meta["infrastructure_facts"]["provider_storage"] == kind
        assert meta["backend"]["provider_storage"] == kind
    finally:
        box.stop()


def test_o57_a_run_of_a_platform_that_declares_no_kind_carries_none(tmp_path):
    """O57: no declared kind, no key: a Compose federation's runs and an older platform's keep the record they had."""
    box = start(FakeNode(), make_settings(tmp_path), make_spec("via-gate"))
    try:
        meta = box.metadata()
        assert "provider_storage" not in meta["infrastructure_facts"]
        assert "provider_storage" not in meta["backend"]
    finally:
        box.stop()


def test_o57_the_facts_record_echoes_the_kind_only_when_given():
    """O57: `facts.record` adds `provider_storage` beside the list of keys when given one, and changes nothing else."""
    given = facts.record(mount_refused=False, provider_storage="shared_filesystem")
    assert given["provider_storage"] == "shared_filesystem"
    assert {k: v for k, v in given.items() if k != "provider_storage"} == facts.record(mount_refused=False)


@pytest.mark.parametrize("kind", ["tmpfs", "shared_filesystem"])
def test_o57_an_image_run_keeps_the_provider_storage_kind_after_its_work_disk_is_proved(tmp_path, kind):
    """O57, O71: a run whose `/work` is an image replaces its facts once the work disk is proved, on the first start of
    the node's driver (the full fill) and on a later one (the invariants against the kept proof), and either record
    carries the kind the platform declares, as a directory run's does."""
    settings = make_settings(tmp_path, work_disk="image", provider_storage=kind)
    node = FakeNode()
    for proof in ("full_fill", "invariants"):
        box = start(node, settings, make_spec("via-gate"))
        try:
            meta = box.metadata()
            assert meta["work_disk"]["proof"]["kind"] == proof
            assert "work_disk_image" in meta["infrastructure_facts"]["facts"]  # the record of the proved disk
            assert meta["infrastructure_facts"]["provider_storage"] == kind
            assert meta["backend"]["provider_storage"] == kind
        finally:
            box.stop()

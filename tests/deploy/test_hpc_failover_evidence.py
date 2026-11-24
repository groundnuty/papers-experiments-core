"""O98, the code review of the admission (deploy/hpc-federation/hpcfailover.py, hpcboot.py): the layout and the rendered
platform file are tied through the ids the bootstrap gave out and never through names; a record of an earlier pass does
not outlive a run that does not end in a pass, whatever way it fails; and a record is published whole or not at all.

The layout is the pilot-shape one with every name invented (see `hpc_failover_support`). The ids are the test's own."""

import errno
import os
import re
import stat
import uuid
from types import SimpleNamespace

import pytest
from hpc_failover_support import (
    DECLARED,
    FILE,
    LARCH_FIRST,
    MIRROR_FIRST,
    core_root,
    layout_over,
    run_core,
)
from hpc_fed_support import hpc_on_path

from expcore.config.loading import ConfigError


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture(scope="module")
def make_ca():
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca


def edit_template(layout, old, new, *, count=1):
    """The layout's platform template with `old` (which it holds `count` times) replaced by `new`."""
    template = layout.spec.platform_template
    text = template.read_text(encoding="utf-8")
    assert text.count(old) == count, old
    template.write_text(text.replace(old, new), encoding="utf-8")


def refused_by(hpc, layout, ids):
    """What the core's phase refuses with, for a template an edit made; the link check never ran."""
    events = []
    with pytest.raises(RuntimeError) as refused:
        run_core(hpc, layout, ids, events)
    assert "links" not in events
    return str(refused.value)


# -- the layout's owner and its spaces are matched to the file through the bootstrap's ids ----------------------------


@pytest.mark.parametrize("ids", [LARCH_FIRST, {**LARCH_FIRST, "larch": "id-02", "aspen": "id-01"}])
def test_o98_a_template_that_swaps_the_ids_behind_two_providers_is_refused_whichever_order_names_would_tell(
    hpc, tmp_path, make_ca, ids
):
    """O98: the file gives larch's name aspen's id and aspen's name larch's, so the support set still matches and the
    holder's name is the owner's, but the file's data provider is not the provider the bootstrap made the owner. By
    names the order would read as a mirror asked first when the real owner sorts first (the first ids) and as the owner
    first when it does not (the second): by ids the member is refused for the swap itself, whatever the order says."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    edit_template(layout, "@provider_id.larch@", "@@swap@@")
    edit_template(layout, "@provider_id.aspen@", "@provider_id.larch@")
    edit_template(layout, "@@swap@@", "@provider_id.aspen@")
    text = refused_by(hpc, layout, ids)
    assert text.startswith("failover order: spaces[0]: the platform file's data provider is not the layout's owner")
    assert text.endswith("refuse this member")
    for hidden in ("larch", "aspen", "rowan", "alpha-space", *ids.values()):
        assert hidden not in text
    assert not (root / FILE).exists()


def test_o98_a_space_renamed_in_the_file_is_matched_by_its_id_and_cannot_leave_the_scope_with_its_holder(
    hpc, tmp_path, make_ca
):
    """O98: the file calls the layout's alpha-space by another name and gives it to aspen. Matched by name the space
    would be skipped and, with larch declared, nothing would be in scope: a pass with no word. The id is the same, so
    the layout's owner is compared with the file's data provider and the member is refused, by the file's label."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    edit_template(layout, 'name = "alpha-space"', 'name = "alpha-renamed"')
    edit_template(layout, 'holder = "larch"', 'holder = "aspen"')
    text = refused_by(hpc, layout, MIRROR_FIRST)
    assert text.startswith("failover order: spaces[0]: the platform file's data provider is not the layout's owner")
    for hidden in ("larch", "aspen", "alpha-space", "alpha-renamed"):
        assert hidden not in text
    assert not (root / FILE).exists()


def test_o98_a_declared_owners_space_the_file_leaves_out_is_refused_by_count_not_dropped_from_scope(
    hpc, tmp_path, make_ca
):
    """O98: the file holds no space with the id of alpha-space, which larch, declared, owns and aspen and rowan mirror.
    That is no probe space, so it cannot be skipped: the member is refused, counting the spaces and naming no one."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    template = layout.spec.platform_template
    text = template.read_text(encoding="utf-8")
    block = re.search(r'\[\[spaces\]\]\nlabel = "alpha".*?(?=\[\[spaces\]\])', text, re.S)
    assert block is not None
    assert 'holder = "larch"' in block.group(0)
    template.write_text(text.replace(block.group(0), ""), encoding="utf-8")
    said = refused_by(hpc, layout, MIRROR_FIRST)
    assert said.startswith("failover order: 1 space(s) the layout gives an outage target are not in the platform file")
    assert said.endswith("refuse this member")
    for hidden in ("larch", "aspen", "alpha-space"):
        assert hidden not in said
    assert not (root / FILE).exists()


def test_o98_the_probe_space_the_file_does_not_hold_as_a_space_is_the_one_explicit_exception(hpc, tmp_path, make_ca):
    """O98: aspen owns link-probes, the layout's probe space, which no `[[spaces]]` entry of the file holds (the file
    gives it to `[link_check]`): declared, aspen refuses nothing, and no other space is left out for it."""
    root, layout = core_root(hpc, tmp_path, make_ca, 'outage_targets = ["aspen"]')
    assert layout.spec.probe_space == "link-probes"
    events = []
    run_core(hpc, layout, LARCH_FIRST, events)
    assert events == ["steps", "links"]
    assert not (root / FILE).exists()


def test_o98_a_provider_the_template_calls_by_another_name_is_still_the_declared_owner_by_its_id(
    hpc, tmp_path, make_ca
):
    """O98: the file calls larch `cedar` everywhere. Names would make the declared `larch` no provider of the file and
    refuse a member that is right; its id is larch's, so the file's data provider is the layout's owner and the order
    of the ids is what is judged: the first ids refuse it, the second pass it and write the record."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    template = layout.spec.platform_template
    renamed = re.sub(r"(?<!@provider_url\.)(?<!@provider_id\.)\blarch\b", "cedar", template.read_text(encoding="utf-8"))
    template.write_text(renamed, encoding="utf-8")
    assert "owner" in refused_by(hpc, layout, LARCH_FIRST)
    assert not (root / FILE).exists()
    said = run_core(hpc, layout, MIRROR_FIRST, [])
    assert "failover-order.json written" in said
    assert (root / FILE).is_file()


# -- a record of an earlier pass does not outlive a run that does not end in one --------------------------------------


def test_o98_a_rerender_that_fails_before_the_admission_leaves_no_record_of_the_earlier_pass(hpc, tmp_path, make_ca):
    """O98: on a reused root the new platform file is written before the render identity is built, and two providers
    with one id make that build fail, so control never reaches the admission. The identity read would still say
    `passed` of the earlier run's record: the record is removed before the platform file is replaced."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    run_core(hpc, layout, MIRROR_FIRST, [])
    assert (root / FILE).is_file()
    twice = {**MIRROR_FIRST, "alder": MIRROR_FIRST["hazel"]}
    with pytest.raises(ConfigError):
        run_core(hpc, layout, twice, [])
    assert not (root / FILE).exists()


DELTA_FROM_THE_STUDY = (
    (
        'mirrors = ["aspen"]\nbaseline = false\n',
        'mirrors = ["aspen"]\nbaseline = false\ndata_manifest = "@study_artifacts@/manifests/delta.json"\n',
    ),
)


@pytest.mark.parametrize("line", [DECLARED, None], ids=["declared", "dropped"])
def test_o98_a_declared_manifest_the_phase_cannot_load_leaves_no_record_of_the_earlier_pass(
    hpc, tmp_path, make_ca, line
):
    """O98, the second code review: the core's phase loads the layout's data manifests before it writes the platform
    file, and a manifest it cannot load (here one under `@study_artifacts@` while the site sets no such directory)
    raised before the record was removed, so the identity read still said `passed` of the earlier run's record, whether
    the layout still declared the outage or had dropped it. The record is removed before anything that can raise."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    run_core(hpc, layout, MIRROR_FIRST, [])
    assert (root / FILE).is_file()
    changed = layout_over(hpc, root, tmp_path / "changed", line, DELTA_FROM_THE_STUDY)
    with pytest.raises(RuntimeError, match="HPC_STUDY_ARTIFACTS is not set"):
        run_core(hpc, changed, MIRROR_FIRST, [])
    assert not (root / FILE).exists()


def test_o98_a_layout_that_no_longer_declares_the_outage_removes_the_earlier_runs_record(hpc, tmp_path, make_ca):
    """O98: the same root brought up again by a layout without `outage_targets`: nothing is declared, so nothing is
    admitted and nothing is written, and the record of the declaration that was there is not left to be read."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    run_core(hpc, layout, MIRROR_FIRST, [])
    assert (root / FILE).is_file()
    plain = layout_over(hpc, root, tmp_path / "plain", None)
    said = run_core(hpc, plain, LARCH_FIRST, [])
    assert "failover" not in said
    assert not (root / FILE).exists()


def test_o98_a_root_that_never_held_a_record_is_taken_as_before_by_the_removal(hpc, tmp_path, make_ca):
    """O98: removing a record that is not there is nothing: an up with no declaration over a fresh root is as before."""
    root, layout = core_root(hpc, tmp_path, make_ca, None)
    events = []
    said = run_core(hpc, layout, LARCH_FIRST, events)
    assert events == ["steps", "links"]
    assert len(said.splitlines()) == 3
    assert not (root / FILE).exists()


def test_o98_the_name_of_the_record_is_one_in_every_module_that_keeps_it(hpc):
    """O98: `hpcfailover` keeps the name without importing the core (the removal runs with no outage declared), as
    `fleetidentity` does on a login node; the core's own constant, the evidence allow-list and both are one name."""
    import hpcevidence
    import hpcfailover

    from expcore_onedata import failover

    assert hpcfailover.FILE == failover.FILE == FILE
    assert hpcfailover.FILE in hpcevidence.UP_FILES


# -- a record is published whole or not at all ------------------------------------------------------------------------


class FullDisk:
    """A text handle on a filesystem that fills up while the failover record is written: the first half of that text
    reaches the file and the write fails. Every other text goes through as it is."""

    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *error):
        return self.handle.__exit__(*error)

    def write(self, text):
        if "expcore-failover-order" not in text:
            return self.handle.write(text)
        self.handle.write(text[: len(text) // 2])
        self.handle.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    def __getattr__(self, name):
        return getattr(self.handle, name)


def test_o98_a_write_that_runs_out_of_space_leaves_no_partial_record_and_no_draft(hpc, tmp_path, make_ca, monkeypatch):
    """O98: the destination was truncated in place, so a flush that failed on a full filesystem left an empty or
    partial `failover-order.json` while `up` failed. The record is written to a draft in the same directory and
    renamed over the file, so a failure leaves the root as it was: no destination, no draft."""
    root, layout = core_root(hpc, tmp_path, make_ca, DECLARED)
    real = os.fdopen
    with monkeypatch.context() as patched:
        patched.setattr(os, "fdopen", lambda *args, **kwargs: FullDisk(real(*args, **kwargs)))
        with pytest.raises(OSError, match="No space left") as refused:
            run_core(hpc, layout, MIRROR_FIRST, [])
    assert refused.value.errno == errno.ENOSPC
    assert [name for name in os.listdir(root) if "failover" in name] == []


def test_o98_a_failed_write_does_not_replace_what_the_destination_held(hpc, tmp_path):
    """O98: the helper that publishes the record writes a private draft and renames it over the file: a write that
    fails leaves the file it would have replaced, and the draft, gone."""
    import hpcfailover

    destination = tmp_path / FILE
    destination.write_text("what was there\n", encoding="utf-8")
    real = os.fdopen
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(os, "fdopen", lambda *args, **kwargs: FullDisk(real(*args, **kwargs)))
        with pytest.raises(OSError, match="No space left"):
            hpcfailover.publish_private(destination, '{"version": "expcore-failover-order/1"}\n')
    assert destination.read_text(encoding="utf-8") == "what was there\n"
    assert os.listdir(tmp_path) == [FILE]


def test_o98_a_record_is_published_private_and_whole_with_no_draft_left_and_replaces_a_link_not_follows_it(
    hpc, tmp_path
):
    """O98: the helper publishes mode 0600 whatever the umask, leaves no draft, and a symbolic link at the destination
    is replaced by the file, never written through (the target of a link here would be some other file of the root)."""
    import hpcfailover

    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("untouched\n", encoding="utf-8")
    destination = tmp_path / FILE
    destination.symlink_to(elsewhere)
    previous = os.umask(0o000)
    try:
        hpcfailover.publish_private(destination, "whole\n")
    finally:
        os.umask(previous)
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "whole\n"
    assert stat.S_IMODE(os.stat(destination).st_mode) == 0o600
    assert elsewhere.read_text(encoding="utf-8") == "untouched\n"
    assert sorted(os.listdir(tmp_path)) == ["elsewhere", FILE]


def test_o98_a_draft_path_that_already_exists_is_another_writers_and_is_left_alone(hpc, tmp_path, monkeypatch):
    """O98, the second code review: the draft's name is made fresh, and creating it exclusively fails if the path is
    already there. The cleanup ran for that failure too and unlinked the existing path, another writer's draft. Only a
    draft this call made is its to remove: the other draft, and the destination, are as they were."""
    import hpcfailover

    fixed = uuid.UUID(int=7)
    monkeypatch.setattr(hpcfailover, "uuid", SimpleNamespace(uuid4=lambda: fixed))
    other = tmp_path / f".{FILE}.{os.getpid()}.{fixed.hex}.tmp"
    other.write_text("another writer's draft\n", encoding="utf-8")
    destination = tmp_path / FILE
    destination.write_text("what was there\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        hpcfailover.publish_private(destination, "whole\n")
    assert other.read_text(encoding="utf-8") == "another writer's draft\n"
    assert destination.read_text(encoding="utf-8") == "what was there\n"
    assert sorted(os.listdir(tmp_path)) == sorted([other.name, FILE])

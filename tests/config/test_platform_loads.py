"""C11: the core's own platform loaders record that they ran, in a registry scoped to one study build.

A study's plugin can hide the deferral its objects declare, but it cannot build a platform without the platform's loader
running, so a loader records the module a study lists for its platform and the configuration file it read
(`record_platform_load`), and the study's build collects those records (`watching_platform_loads`). When the campaign
starts the scheduler refuses a record of a module `[study] platforms` does not list. The registry holds the module and
the path only, a loader run outside a study build leaves nothing, and a second build starts empty.
"""

import threading
from pathlib import Path

import pytest

from expcore.config.loading import ConfigError
from expcore.config.platform_loads import (
    PlatformLoad,
    check_loads_listed,
    record_platform_load,
    watching_platform_loads,
)

MODULE = "toy_platform.kinds"
OTHER = "toy_other.kinds"


def test_c11_a_loader_that_ran_in_a_study_build_is_recorded_with_its_module_and_its_path(tmp_path):
    """C11: a record is the module and the configuration file, nothing else: no value of the file, no secret."""
    config = tmp_path / "platform.toml"
    with watching_platform_loads() as loads:
        record_platform_load(MODULE, config)
    assert loads == [PlatformLoad(MODULE, config)]
    assert PlatformLoad._fields == ("module", "config")


def test_c11_a_loader_run_outside_a_study_build_records_nothing_and_leaves_nothing_for_the_next_build(tmp_path):
    """C11: platform tooling (a day-0 tool, a federation script) runs the loaders with no study build open: nothing is
    kept, so a long-lived process does not grow a list and a later build does not inherit the call."""
    record_platform_load(MODULE, tmp_path / "platform.toml")
    with watching_platform_loads() as loads:
        pass
    assert loads == []


def test_c11_two_study_builds_in_one_process_do_not_leak_entries(tmp_path):
    """C11: each study build starts with an empty record, whatever the one before it loaded."""
    with watching_platform_loads() as first:
        record_platform_load(MODULE, tmp_path / "one.toml")
    with watching_platform_loads() as second:
        record_platform_load(OTHER, tmp_path / "two.toml")
    assert first == [PlatformLoad(MODULE, tmp_path / "one.toml")]
    assert second == [PlatformLoad(OTHER, tmp_path / "two.toml")]
    with watching_platform_loads() as third:
        pass
    assert third == []


def test_c11_a_failed_build_leaves_nothing_open(tmp_path):
    """C11: a build that raises closes its watch, so the loads that follow are not recorded for it."""
    kept = []

    def failing_build():
        with watching_platform_loads() as loads:
            kept.append(loads)
            record_platform_load(MODULE, tmp_path / "one.toml")
            raise RuntimeError("the build failed")

    with pytest.raises(RuntimeError, match="the build failed"):
        failing_build()
    record_platform_load(OTHER, tmp_path / "two.toml")
    assert kept == [[PlatformLoad(MODULE, tmp_path / "one.toml")]]


def test_c11_overlapping_watches_each_keep_their_own_record(tmp_path):
    """C11: the core asks a platform's own hook inside the study's build, so the two records overlap: the inner one sees
    only what ran inside it, the outer one sees all."""
    with watching_platform_loads() as outer:
        record_platform_load(MODULE, tmp_path / "before.toml")
        with watching_platform_loads() as inner:
            record_platform_load(OTHER, tmp_path / "inside.toml")
        record_platform_load(MODULE, tmp_path / "after.toml")
    assert [load.config.name for load in inner] == ["inside.toml"]
    assert [load.config.name for load in outer] == ["before.toml", "inside.toml", "after.toml"]


def test_c11_a_loader_run_again_on_the_same_file_is_one_record(tmp_path):
    """C11: a build that loads the same file again records it once, so the record does not grow with each call."""
    with watching_platform_loads() as loads:
        for _ in range(50):
            record_platform_load(MODULE, tmp_path / "platform.toml")
    assert len(loads) == 1


def test_c11_builds_in_two_threads_run_one_at_a_time_and_do_not_receive_each_others_records(tmp_path):
    """C11: a study build that loads nothing is not refused for another build's platform. One build is open in a
    process at a time: a second thread's build waits until the first's ends, so every loader, a worker thread of the
    plugin's included, records into the one build that is open, and each keeps its own record."""
    opened, kept = [], {}

    def other_build():
        with watching_platform_loads() as loads:
            opened.append("the other build")
            record_platform_load(OTHER, tmp_path / "other.toml")
            kept["other"] = loads

    with watching_platform_loads() as mine:
        worker = threading.Thread(target=other_build)
        worker.start()
        worker.join(0.5)  # it waits for this build, which is open
        assert worker.is_alive()
        assert opened == []
        record_platform_load(MODULE, tmp_path / "mine.toml")
    worker.join(10)
    assert not worker.is_alive()
    assert mine == [PlatformLoad(MODULE, tmp_path / "mine.toml")]
    assert kept["other"] == [PlatformLoad(OTHER, tmp_path / "other.toml")]


def test_c11_a_build_opened_again_by_its_own_thread_is_not_waited_for(tmp_path):
    """C11: the core's reading of a listed platform opens a watch inside the build's: the same thread passes, and the
    outer build sees what ran inside."""
    with watching_platform_loads() as outer, watching_platform_loads() as inner:
        record_platform_load(MODULE, tmp_path / "platform.toml")
    assert outer == inner == [PlatformLoad(MODULE, tmp_path / "platform.toml")]


def test_c11_a_loader_in_another_thread_is_recorded_too(tmp_path):
    """C11: a plugin that builds its platform in a worker thread of its own is seen: a loader of any thread records
    into the one build that is open."""
    with watching_platform_loads() as loads:
        worker = threading.Thread(target=record_platform_load, args=(MODULE, tmp_path / "platform.toml"))
        worker.start()
        worker.join()
    assert loads == [PlatformLoad(MODULE, tmp_path / "platform.toml")]


@pytest.mark.parametrize(
    ("module", "config"),
    [("", Path("platform.toml")), (5, Path("platform.toml")), (MODULE, 5), (MODULE, None)],
    ids=["empty module", "module not text", "path not a path", "no path"],
)
def test_c11_a_loader_that_names_no_module_or_no_path_is_refused_at_the_boundary(module, config):
    """C11: a platform's loader that records something the check cannot read fails where it records, naming the
    argument, and never as a silent hole in the record."""
    with watching_platform_loads(), pytest.raises(ValueError, match="record_platform_load"):
        record_platform_load(module, config)


def test_c11_the_check_refuses_a_module_the_study_does_not_list_naming_it(tmp_path):
    """C11: the refusal names the module and the file's name (never its directory), says to list the module in
    `[study] platforms`, and names each unlisted module once."""
    loads = [
        PlatformLoad(MODULE, tmp_path / "secret-dir" / "platform.toml"),
        PlatformLoad(MODULE, tmp_path / "secret-dir" / "other.toml"),
        PlatformLoad(OTHER, tmp_path / "platform.toml"),
    ]
    with pytest.raises(ConfigError) as caught:
        check_loads_listed(loads, ["unrelated.kinds"])
    text = str(caught.value)
    for part in (repr(MODULE), repr(OTHER), "platform.toml", "[study] platforms", "list them"):
        assert part in text, text
    assert text.count(repr(MODULE)) == 1, text
    assert "secret-dir" not in text, text
    with pytest.raises(ConfigError, match="list it"):  # one module: singular
        check_loads_listed(loads[:1], [])


def test_c11_the_check_accepts_listed_modules_and_an_empty_record(tmp_path):
    """C11: a study whose build loaded only listed platforms, or no platform at all, is not refused."""
    check_loads_listed([PlatformLoad(MODULE, tmp_path / "platform.toml")], [MODULE, OTHER])
    check_loads_listed([], [])
    check_loads_listed((), [MODULE])

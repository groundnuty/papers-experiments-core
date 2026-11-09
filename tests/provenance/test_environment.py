"""The environment of a study's code: the closure of the core's and the study's requirements over what is installed,
with each version and, for a direct-URL install, its source without a path or a credential."""

import json
from importlib import metadata
from pathlib import Path

import pytest

from expcore.provenance.environment import (
    CORE_DISTRIBUTION,
    EnvironmentUnknown,
    differences,
    environment,
    is_environment,
    without_sources,
)


def install(site: Path, name: str, version: str, requires=(), direct_url: dict | None = None) -> metadata.Distribution:
    """A fake installed distribution: a dist-info directory with its metadata and, if given, its direct_url.json."""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
    return metadata.Distribution.at(info)


@pytest.fixture
def site(tmp_path) -> dict[str, metadata.Distribution]:
    """A core that needs a numeric library, a platform with an optional extra, and tools nobody requires."""
    where = tmp_path / "site-packages"
    dists = [
        install(where, CORE_DISTRIBUTION, "0.1.0", ["numlib>=1", "winonly ; sys_platform == 'win32'"]),
        install(where, "numlib", "1.4.2"),
        install(where, "toy-platform", "2.0.0", ["wirelib", "fancylib ; extra == 'fancy'"]),
        install(where, "wirelib", "0.9"),
        install(where, "fancylib", "3.1"),
        install(where, "test-runner", "8.0", ["numlib"]),
    ]
    return {d.metadata["Name"]: d for d in dists}


def test_c06_the_environment_is_the_closure_of_the_core_and_the_study_requirements_over_what_is_installed(site):
    """C06: what the fingerprint holds: the core, what it requires, and what `[study] requires` adds, transitively; a
    requirement that is not installed (its marker did not apply), one behind an extra nobody asked for, and a tool
    installed beside the study are left out."""
    alone = environment(found=site)
    assert alone["distributions"] == {CORE_DISTRIBUTION: "0.1.0", "numlib": "1.4.2"}
    assert alone["python"].count(".") == 1
    assert alone["sources"] == {}
    with_platform = environment(["toy-platform"], found=site)
    assert set(with_platform["distributions"]) == {CORE_DISTRIBUTION, "numlib", "toy-platform", "wirelib"}
    with_extra = environment(["Toy_Platform[fancy]"], found=site)
    assert with_extra["distributions"]["fancylib"] == "3.1"
    assert "test-runner" not in with_extra["distributions"]


def test_c06_a_required_distribution_that_is_not_installed_cannot_be_recorded(site):
    """C06: an environment that names a distribution it cannot find is refused rather than recorded without it."""
    with pytest.raises(EnvironmentUnknown, match="'absent-platform' is not installed"):
        environment(["absent-platform"], found=site)
    del site[CORE_DISTRIBUTION]
    with pytest.raises(EnvironmentUnknown, match=CORE_DISTRIBUTION):
        environment(found=site)


def test_c06_a_direct_url_source_keeps_its_commit_and_drops_credentials_and_local_paths(tmp_path):
    """C06: a source is recorded so that a resume after the core moved to another commit is refused, but it never
    keeps a credential, a query or a path of the machine."""
    where = tmp_path / "site"
    vcs = {
        "url": "https://user:token@code.example.org/lab/core?x=1#frag",
        "vcs_info": {"vcs": "git", "commit_id": "c" * 40},
    }
    found = {
        CORE_DISTRIBUTION: install(where, CORE_DISTRIBUTION, "0.1.0", ["toy-platform", "archivelib"], vcs),
        "toy-platform": install(
            where,
            "toy-platform",
            "2.0.0",
            direct_url={"url": "file:///home/someone/plat", "dir_info": {"editable": True}},
        ),
        "archivelib": install(
            where,
            "archivelib",
            "1.0",
            direct_url={"url": "file:///home/someone/a.whl", "archive_info": {"hashes": {"sha256": "d" * 64}}},
        ),
    }
    sources = environment(found=found)["sources"]
    assert sources[CORE_DISTRIBUTION] == {
        "kind": "vcs",
        "vcs": "git",
        "url": "https://code.example.org/lab/core",
        "commit": "c" * 40,
    }
    assert sources["toy-platform"] == {"kind": "directory", "editable": True}
    assert sources["archivelib"] == {"kind": "archive", "sha256": "d" * 64}
    assert "someone" not in json.dumps(sources)
    assert "token" not in json.dumps(sources)


def test_c06_differences_name_every_change_and_never_a_url_or_commit(site):
    """C06: the lines say what changed; with sources off, as in a package, only versions and Python count."""
    recorded = environment(["toy-platform"], found=site)
    recorded["sources"] = {
        "toy-platform": {"kind": "vcs", "vcs": "git", "url": "https://h.example/x", "commit": "e" * 40}
    }
    now = json.loads(json.dumps(recorded))
    assert differences(recorded, now) == []
    now["distributions"]["numlib"] = "1.5.0"
    del now["distributions"]["wirelib"]
    now["distributions"]["extra-lib"] = "1.0"
    now["python"] = "9.9"
    now["sources"] = {}
    lines = differences(recorded, now)
    assert lines == [
        "Python " + recorded["python"] + " recorded, Python 9.9 in use",
        "extra-lib 1.0 in use, not recorded",
        "numlib 1.4.2 recorded, 1.5.0 in use",
        "wirelib 0.9 recorded, not part of the environment in use",
        "toy-platform installed from another source than the one recorded",
    ]
    assert "e" * 12 not in "".join(lines)
    assert differences(recorded, {**recorded, "sources": {}}, sources=False) == []
    assert differences(None, now) == ["no environment is recorded, or the record is not one"]
    assert is_environment(without_sources(recorded))
    assert "sources" not in without_sources(recorded)

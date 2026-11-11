"""The federation's root and what every command refuses before it touches anything: a root outside the allowed
parent or through a link, a configured parent and its stricter rule, a root without this layout's mark, containers of
the project over another root, and a first start the host's memory cannot hold."""

from pathlib import Path

import pytest
from federation_support import EXAMPLE


@pytest.mark.parametrize(
    "value",
    [
        "",
        "relative/root",
        "/",
        "/mnt/volume2",
        "/mnt/volume2/expcore-federation-other",
        "/mnt/volume2/expcore-federation/../x",
    ],
)
def test_a_root_outside_the_allowed_directory_is_refused_before_anything_is_touched(fed, value, monkeypatch, capsys):
    """Down deletes everything the root holds, so a root that is not at or below the allowed directory is refused, by
    the command itself, before it writes a file or starts a container."""
    monkeypatch.delenv("EXPCORE_FED_ROOT_PARENT", raising=False)
    monkeypatch.setenv("EXPCORE_FED_ROOT", value)
    touched = []
    for name in ("up", "reset_clean", "reset_replicas", "down", "status"):
        monkeypatch.setattr(fed.cli, name, lambda layout, name=name: touched.append(name))
    assert fed.cli.main(["down"]) == 2
    assert touched == []
    assert "EXPCORE_FED_ROOT" in capsys.readouterr().err


def test_a_root_through_a_link_is_refused_and_one_below_the_base_is_accepted(fed, tmp_path):
    """A link from the allowed directory down to the root could redirect the deletion; a plain directory below the
    allowed one is the federation's own, for a second federation beside the first."""
    base = tmp_path / "allowed"
    (base / "pilot").mkdir(parents=True)
    (base / "linked").symlink_to(tmp_path)
    assert fed.layout.checked_root(str(base / "pilot"), base) == base / "pilot"
    assert fed.layout.checked_root(str(base), base) == base
    with pytest.raises(ValueError, match="strictly below"):
        fed.layout.checked_root(str(base), base, strict=True)
    with pytest.raises(ValueError, match="symbolic link"):
        fed.layout.checked_root(str(base / "linked" / "data"), base)


def parent_cases(tmp: Path) -> dict[str, tuple[dict[str, str], str]]:
    """Settings of a non-default parent that every command refuses, each with the words its refusal must hold."""
    allowed = tmp / "study"
    (allowed / "other-data").mkdir(parents=True)
    (allowed / "link").symlink_to(tmp)
    (tmp / "linked-parent").symlink_to(allowed)
    return {
        "relative parent": ({"EXPCORE_FED_ROOT_PARENT": "study", "EXPCORE_FED_ROOT": str(allowed / "f")}, "absolute"),
        "the filesystem root": ({"EXPCORE_FED_ROOT_PARENT": "/", "EXPCORE_FED_ROOT": "/fed"}, "may not be /"),
        "missing parent": (
            {"EXPCORE_FED_ROOT_PARENT": str(tmp / "absent"), "EXPCORE_FED_ROOT": str(tmp / "absent" / "f")},
            "existing directory",
        ),
        "root not named": ({"EXPCORE_FED_ROOT_PARENT": str(allowed)}, "strictly below"),
        "root is the parent": (
            {"EXPCORE_FED_ROOT_PARENT": str(allowed), "EXPCORE_FED_ROOT": str(allowed)},
            "strictly below",
        ),
        "root outside": ({"EXPCORE_FED_ROOT_PARENT": str(allowed), "EXPCORE_FED_ROOT": str(tmp / "f")}, "below"),
        "root through a link": (
            {"EXPCORE_FED_ROOT_PARENT": str(allowed), "EXPCORE_FED_ROOT": str(allowed / "link" / "f")},
            "symbolic link",
        ),
        "parent is a link": (
            {"EXPCORE_FED_ROOT_PARENT": str(tmp / "linked-parent"), "EXPCORE_FED_ROOT": str(tmp / "linked-parent/f")},
            "symbolic link",
        ),
    }


@pytest.mark.parametrize(
    "case",
    [
        "relative parent",
        "the filesystem root",
        "missing parent",
        "root not named",
        "root is the parent",
        "root outside",
        "root through a link",
        "parent is a link",
    ],
)
def test_a_configured_parent_gets_every_check_and_its_root_must_lie_strictly_below_it(
    fed, case, tmp_path, monkeypatch, capsys
):
    """A study confined to its own directory names it as the parent: it must be absolute, existing and not `/`, with no
    link from it down to the root, and the root must be named and lie strictly below it, so a parent holding other data
    is never the root that down empties. Every command refuses before it touches anything."""
    environ, words = parent_cases(tmp_path)[case]
    monkeypatch.delenv("EXPCORE_FED_ROOT", raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    touched = []
    for name in ("up", "reset_clean", "reset_replicas", "down", "status"):
        monkeypatch.setattr(fed.cli, name, lambda layout, name=name: touched.append(name))
    assert fed.cli.main(["down"]) == 2
    assert touched == []
    err = capsys.readouterr().err
    assert words in err
    assert "EXPCORE_FED_ROOT" in err


def test_a_configured_parent_takes_a_root_strictly_below_it_and_the_default_keeps_todays_root(fed, tmp_path):
    allowed = tmp_path / "study"
    allowed.mkdir()
    environ = {"EXPCORE_FED_ROOT_PARENT": str(allowed), "EXPCORE_FED_ROOT": str(allowed / "fed" / "one")}
    layout = fed.layout.Layout.from_environment(environ)
    assert (layout.parent, layout.root) == (allowed, allowed / "fed" / "one")
    assert not layout.parent_is_default
    default = fed.layout.Layout.from_environment({})
    assert default.root == default.parent == Path(fed.layout.DEFAULT_ROOT)
    assert default.parent_is_default
    named = fed.layout.Layout.from_environment({"EXPCORE_FED_ROOT_PARENT": fed.layout.DEFAULT_ROOT})
    assert named == default  # the default named explicitly is still the default
    assert fed.facts.root_parent(layout) == {"path": str(allowed), "set_by": "EXPCORE_FED_ROOT_PARENT"}
    assert fed.facts.root_parent(default)["set_by"] == "default"
    assert f"| Root parent | `{allowed}` (EXPCORE_FED_ROOT_PARENT) |" in fed.facts.describe(layout)


# -- what up and down refuse before they touch anything ---------------------------------------------------------------


def no_docker(fed, monkeypatch, containers=()):
    """Record every compose call instead of running it, and show the given containers of the project."""
    ran = []
    monkeypatch.setattr(fed.cli, "compose", lambda *args, **kw: ran.append(args))
    monkeypatch.setattr(fed.cli, "project_containers", lambda project: list(containers))
    monkeypatch.setattr(fed.cli, "project_networks", lambda project: [])
    return ran


def test_up_refuses_a_root_that_holds_files_without_the_mark_and_down_deletes_nothing_there(fed, tmp_path, monkeypatch):
    """A directory with someone else's files is not the federation's: up refuses to write into it and down refuses to
    clear it, before any container runs."""
    root = tmp_path / "someone-else"
    root.mkdir()
    (root / "their-data.txt").write_text("keep me", encoding="utf-8")
    layout = fed.layout.Layout(root)
    ran = no_docker(fed, monkeypatch)
    with pytest.raises(RuntimeError, match="not marked"):
        fed.cli.prepare(layout)
    with pytest.raises(RuntimeError, match="not marked"):
        fed.cli.down(layout)
    assert ran == []
    assert (root / "their-data.txt").read_text(encoding="utf-8") == "keep me"


def test_a_root_marked_for_another_project_is_neither_written_nor_emptied(fed, minimal_layout, tmp_path, monkeypatch):
    """The mark holds the declared project's name, so a layout given the root of another layout's federation refuses
    it, and down stops nothing: the containers it would stop are not that root's."""
    root = tmp_path / "fed"
    root.mkdir()
    (root / fed.layout.ROOT_MARKER).write_text("expcore-fed\n", encoding="utf-8")
    (root / "facts.json").write_text("{}", encoding="utf-8")
    layout = fed.layout.Layout(root, spec=fed.spec.load_spec(minimal_layout))
    ran = no_docker(fed, monkeypatch)
    with pytest.raises(RuntimeError, match="naming expcore-test"):
        fed.cli.prepare(layout)
    with pytest.raises(RuntimeError, match="nothing stopped or deleted"):
        fed.cli.down(layout)
    assert ran == []


def test_containers_of_the_project_over_another_root_are_never_touched(fed, tmp_path, monkeypatch):
    """Another federation of the same project name, over another root (or one brought up before containers named
    their root), is someone else's: up, reset, down and memory refuse before any compose command."""
    layout = fed.layout.Layout(tmp_path / "mine")
    other = fed.docker.Container("expcore-fed-zone-1", "zone", "running", str(tmp_path / "theirs"))
    unlabelled = fed.docker.Container("expcore-fed-pine-1", "pine", "running", "")
    for containers in ([other], [unlabelled]):
        ran = no_docker(fed, monkeypatch, containers)
        monkeypatch.setattr(fed.cli, "require_images", lambda: None)
        for command in (fed.cli.up, fed.cli.reset_clean, fed.cli.reset_replicas, fed.cli.down, fed.cli.memory):
            with pytest.raises(RuntimeError, match="bind another root"):
                command(layout)
        assert ran == []
    assert not (tmp_path / "mine").exists()


def test_a_first_up_refuses_a_layout_whose_memory_limits_exceed_what_the_host_has(fed, tmp_path):
    spec = fed.spec.load_spec(EXAMPLE)  # six services of 6g: 36 GiB
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       57000000 kB\nMemAvailable:   30000000 kB\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"36\.00 GiB for 6 services and the host has 28\.61 GiB available"):
        fed.docker.require_memory_fits(spec, meminfo)
    meminfo.write_text("MemAvailable:   40000000 kB\n", encoding="utf-8")
    fed.docker.require_memory_fits(spec, meminfo)
    meminfo.write_text("MemFree: 1 kB\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be read"):
        fed.docker.require_memory_fits(spec, meminfo)


def test_memory_shows_each_services_limit_current_use_and_peak(fed, monkeypatch):
    """The peak since the container started, from its own cgroup, is what the day-0 report records per service."""
    spec = fed.spec.load_spec()
    figures = {"memory.peak": 3 << 30, "memory.current": 2 << 30, "memory.max": 6 << 30}
    monkeypatch.setattr(fed.docker, "cgroup_value", lambda container, name: figures[name])
    running = [fed.docker.Container(f"expcore-fed-{s}-1", s, "running", "/r") for s in ("zone", "pine")]
    stopped = [fed.docker.Container("expcore-fed-birch-1", "birch", "exited", "/r")]
    usage = fed.docker.memory_usage(running + stopped)
    assert usage["zone"] == {"peak": 3 << 30, "current": 2 << 30, "max": 6 << 30}
    lines = fed.docker.memory_lines(spec, usage)
    assert lines[0] == "zone: limit 6g (cgroup 6.00 GiB), current 2.00 GiB, peak 3.00 GiB"
    assert "birch: limit 6g, not running" in lines


def test_memory_falls_back_to_cgroup_v1_and_a_wedged_service_reads_as_unknown(fed, monkeypatch):
    """A daemon on cgroup v1 still gives a peak; a service that does not answer in time gives none, and down, which
    reads the peaks before it stops anything, never hangs on it."""
    v1 = {"memory/memory.max_usage_in_bytes": 4 << 30, "memory/memory.usage_in_bytes": 1 << 30}
    monkeypatch.setattr(fed.docker, "cgroup_value", lambda container, name: v1.get(name))
    usage = fed.docker.memory_usage([fed.docker.Container("expcore-fed-zone-1", "zone", "running", "/r")])
    assert usage["zone"] == {"peak": 4 << 30, "current": 1 << 30, "max": None}
    monkeypatch.undo()

    def wedged(*args, **kwargs):
        assert kwargs["timeout"] == fed.docker.EXEC_TIMEOUT_S
        raise fed.docker.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(fed.docker.subprocess, "run", wedged)
    assert fed.docker.cgroup_value("expcore-fed-zone-1", "memory.peak") is None

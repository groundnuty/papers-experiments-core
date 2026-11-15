"""`down` removes the project's volumes with its containers and checks that no Docker volume labelled with the project
is left. Docker is never run: compose, the janitor and the listings of the project's containers, networks and volumes
are fakes, and a fake volume goes only when the fake `compose down` is given `--volumes`."""

import pytest

VOLUMES = ["expcore-test_anonymous-1", "expcore-test_cache"]
DOWN_ARGS = ("down", "--volumes", "--remove-orphans", "--timeout", "120")


@pytest.fixture
def docker(fed, tmp_path, monkeypatch):
    """A marked root and a fake Docker holding `VOLUMES` of the project: `compose down` removes them when it is given
    `--volumes` (or `-v`), the janitor takes the mark away so the root can go, and every call is recorded."""
    root = tmp_path / "fed"
    root.mkdir()
    layout = fed.layout.Layout(root)
    layout.marker.write_text(layout.spec.project + "\n", encoding="utf-8")
    world = type("World", (), {"layout": layout, "volumes": list(VOLUMES), "compose": [], "asked": []})()

    def compose(_layout, *args, **options):
        world.compose.append(args)
        if args[0] == "down" and {"--volumes", "-v"} & set(args):
            world.volumes.clear()
        if "janitor" in args:
            layout.marker.unlink()

    def project_volumes(project):
        world.asked.append(project)
        return list(world.volumes)

    monkeypatch.setattr(fed.cli, "compose", compose)
    monkeypatch.setattr(fed.cli, "project_containers", lambda project: [])
    monkeypatch.setattr(fed.cli, "project_networks", lambda project: [])
    monkeypatch.setattr(fed.cli, "project_volumes", project_volumes)
    return world


def test_down_runs_compose_down_with_volumes_and_then_finds_none_of_the_project_left(fed, docker):
    summary = fed.cli.down(docker.layout)
    assert docker.compose[0] == DOWN_ARGS
    assert [args[-1] for args in docker.compose[1:]] == ["janitor"]  # then the root is cleared, as before
    assert docker.asked == [docker.layout.spec.project]  # the check asks for the project's own volumes
    assert docker.volumes == []
    assert not docker.layout.root.exists()
    assert summary.startswith("down")


def test_down_fails_loudly_and_names_a_volume_of_the_project_that_is_left(fed, docker, monkeypatch):
    """A volume that `down --volumes` did not remove, a named one another compose file declared, say, is reported by
    name, with the count, though no container, network or file of the project is left."""
    original = fed.cli.compose

    def compose_that_keeps_volumes(layout, *args, **options):
        original(layout, *args, **options)
        docker.volumes[:] = ["expcore-test_cache"]

    monkeypatch.setattr(fed.cli, "compose", compose_that_keeps_volumes)
    with pytest.raises(RuntimeError, match=r"1 volume\(s\).*expcore-test_cache"):
        fed.cli.down(docker.layout)


def test_the_listing_of_a_projects_volumes_asks_docker_for_its_label_and_only_that(fed, monkeypatch):
    calls = []

    def lines(*args):
        calls.append(args)
        return ["expcore-test_cache", "expcore-test_anonymous-1"]

    monkeypatch.setattr(fed.docker, "docker_lines", lines)
    assert fed.docker.project_volumes("expcore-test") == ["expcore-test_cache", "expcore-test_anonymous-1"]
    assert calls == [
        ("volume", "ls", "--filter", "label=com.docker.compose.project=expcore-test", "--format", "{{.Name}}")
    ]


@pytest.fixture
def gone(fed, tmp_path, monkeypatch):
    """A layout whose root is gone and a fake Docker that lists no container or network of the project and `VOLUMES`
    as its volumes, runs no compose command, and records every volume listing."""
    world = type("World", (), {"layout": fed.layout.Layout(tmp_path / "gone"), "volumes": list(VOLUMES)})()
    world.compose, world.asked = [], []

    def project_volumes(project):
        world.asked.append(project)
        return list(world.volumes)

    monkeypatch.setattr(fed.cli, "compose", lambda *args, **options: world.compose.append(args))
    monkeypatch.setattr(fed.cli, "project_containers", lambda project: [])
    monkeypatch.setattr(fed.cli, "project_networks", lambda project: [])
    monkeypatch.setattr(fed.cli, "project_volumes", project_volumes)
    return world


def test_down_with_the_root_gone_fails_loudly_on_a_volume_of_the_project_still_there(fed, gone):
    """#202: the root is gone and a volume labelled with the project is not, so no federation is down."""
    left = r"2 volume\(s\).*expcore-test_anonymous-1.*expcore-test_cache"
    with pytest.raises(RuntimeError, match=rf"root is gone.*{left}"):
        fed.cli.down(gone.layout)
    assert gone.asked == [gone.layout.spec.project]  # the project's own volumes, listed
    assert gone.compose == []  # compose still runs only over a marked root


def test_down_with_the_root_gone_and_no_volume_left_is_already_down(fed, gone):
    gone.volumes.clear()
    assert "already down" in fed.cli.down(gone.layout)
    assert gone.asked == [gone.layout.spec.project]
    assert gone.compose == []


def test_a_second_down_after_one_that_failed_on_a_volume_does_not_report_success(fed, docker, monkeypatch):
    """#202: the first down empties and removes the root and then finds a volume it could not remove and raises; the
    retry finds no root, and must not take that for a federation that is down while the volume remains."""
    original = fed.cli.compose

    def compose_that_keeps_volumes(layout, *args, **options):
        original(layout, *args, **options)
        docker.volumes[:] = ["expcore-test_cache"]

    monkeypatch.setattr(fed.cli, "compose", compose_that_keeps_volumes)
    with pytest.raises(RuntimeError, match=r"1 volume\(s\).*expcore-test_cache"):
        fed.cli.down(docker.layout)
    assert not docker.layout.root.exists()
    for _ in range(2):  # every retry says so, until the volume is gone
        with pytest.raises(RuntimeError, match=r"root is gone.*1 volume\(s\).*expcore-test_cache"):
            fed.cli.down(docker.layout)
    docker.volumes.clear()  # removed by hand, as the first failure said
    assert "already down" in fed.cli.down(docker.layout)

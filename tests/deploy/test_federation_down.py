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

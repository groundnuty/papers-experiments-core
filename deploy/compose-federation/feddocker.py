"""Docker, for the declared project only: its generated Compose file, its containers and networks, what the host must
offer before a first start, and what each service's memory came to.

Only the layout's project is ever named, and a container of it that binds another root (or names none) belongs to
another federation of the same project name: every command refuses before touching it. Nothing is pulled or pruned.
"""

import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fedcompose import ROOT_LABEL, render
from fedimport import require_no_link
from fedlayout import HERE, IMAGES, Layout
from fedspec import LOOPBACK, Spec

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
CGROUP = "/sys/fs/cgroup"  # a container's own cgroup, as it sees it
MEMORY_FILES = {  # each figure's file under cgroup v2, then under v1
    "peak": ("memory.peak", "memory/memory.max_usage_in_bytes"),
    "current": ("memory.current", "memory/memory.usage_in_bytes"),
    "max": ("memory.max", "memory/memory.limit_in_bytes"),
}
EXEC_TIMEOUT_S = 20.0  # a wedged service must not hang down, which reads the peaks before it stops anything
MEMINFO = Path("/proc/meminfo")


def gib(value: int | None) -> str:
    return "unknown" if value is None else f"{value / (1 << 30):.2f} GiB"


def import_sources(layout: Layout) -> dict[str, str]:
    """Each read-only import's variable with its checked source, read from this process's environment; a source the
    layout would not take is a RuntimeError, so no command meets it as a traceback. A writable import's copy, which
    the file binds read-write from the root, is refused when a symbolic link lies on its way (fedimport)."""
    found = {}
    for space in layout.spec.spaces:
        if space.imported is not None and not space.imported.readonly:
            require_no_link(layout, space.name)  # its copy is mounted read-write: never through a link
        if space.imported is not None and space.imported.readonly:
            try:
                found[space.imported.source_env] = str(layout.import_source(space.name, os.environ))
            except ValueError as error:
                raise RuntimeError(str(error)) from None
    return found


def compose(layout: Layout, *args: str, capture: bool = False) -> str:
    """Run docker compose on the layout's generated file, under its project name, with this directory as the project
    directory; raise on failure.

    The environment names the root and, for each read-only import, the checked source of its dataset: the generated
    file binds that directory by its variable. A command of such a layout therefore needs the variable set, down
    included. A writable import binds its copy in the root, so its source is not needed here."""
    env = {**os.environ, "EXPCORE_FED_ROOT": str(layout.root), **import_sources(layout)}
    project = layout.spec.project
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=f"{project}-", suffix=".compose.yaml") as file:
        file.write(render(layout.spec))
        file.flush()
        command = [
            "docker", "compose", "--file", file.name, "--project-directory", str(HERE), "--project-name", project,
            *args,
        ]  # fmt: skip
        done = subprocess.run(command, env=env, check=False, capture_output=capture, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"docker compose {args[0]} failed with status {done.returncode}")
    return done.stdout if capture else ""


def docker_lines(*args: str) -> list[str]:
    """Run a read-only docker command and return its non-empty output lines."""
    done = subprocess.run(["docker", *args], check=True, capture_output=True, text=True)
    return [line for line in done.stdout.splitlines() if line.strip()]


@dataclass(frozen=True)
class Container:
    """A container of the project: its name, its service, its state and the root its label names."""

    name: str
    service: str
    state: str
    root: str

    def __str__(self) -> str:
        return f"{self.name} {self.state}"


def project_containers(project: str) -> list[Container]:
    """Every container of the project, running or not."""
    fields = ("{{.Names}}", f'{{{{.Label "{SERVICE_LABEL}"}}}}', "{{.State}}", f'{{{{.Label "{ROOT_LABEL}"}}}}')
    lines = docker_lines("ps", "--all", "--filter", f"label={PROJECT_LABEL}={project}", "--format", "\t".join(fields))
    return [Container(*[*line.split("\t"), "", "", ""][:4]) for line in lines]


@dataclass(frozen=True)
class Network:
    """A network of the project: its name and the root its label names."""

    name: str
    root: str

    def __str__(self) -> str:
        return self.name


def project_networks(project: str) -> list[Network]:
    fields = ("{{.Name}}", f'{{{{.Label "{ROOT_LABEL}"}}}}')
    lines = docker_lines("network", "ls", "--filter", f"label={PROJECT_LABEL}={project}", "--format", "\t".join(fields))
    return [Network(*[*line.split("\t"), ""][:2]) for line in lines]


def project_volumes(project: str) -> list[str]:
    """The names of every Docker volume labelled with the project, named or anonymous."""
    return docker_lines("volume", "ls", "--filter", f"label={PROJECT_LABEL}={project}", "--format", "{{.Name}}")


def require_own(layout: Layout, containers: list[Container], networks: list[Network] = ()) -> None:
    """Refuse a project whose containers or networks bind another root, or name none: that is another federation."""
    strangers = [item for item in (*containers, *networks) if item.root != str(layout.root)]
    if strangers:
        raise RuntimeError(
            f"{len(strangers)} container(s) or network(s) of the project {layout.spec.project} bind another root "
            "than this one, or name none; that federation is not this one, and nothing was touched"
        )


def require_images() -> None:
    """Both pinned images must already be present: nothing is ever pulled."""
    for image in IMAGES:
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode != 0:
            raise RuntimeError(f"the image {image} is not present; it is never pulled here")


def require_ports_free(spec: Spec) -> None:
    """Before a first start, nothing may listen on the loopback ports the services will publish."""
    for service in spec.services:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex((LOOPBACK, service.port)) == 0:
                raise RuntimeError(f"something already listens on the loopback port meant for {service.name}")


def available_memory(meminfo: Path = MEMINFO) -> int:
    """The host's available memory in bytes (MemAvailable)."""
    try:
        lines = meminfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise RuntimeError("the host's available memory could not be read; refused") from None
    for line in lines:
        name, _, rest = line.partition(":")
        if name == "MemAvailable" and rest.split()[1:] == ["kB"] and rest.split()[0].isdigit():
            return int(rest.split()[0]) * 1024
    raise RuntimeError("the host's available memory could not be read; refused")


def require_memory_fits(spec: Spec, meminfo: Path = MEMINFO) -> None:
    """Before a first start, the services' limits together must fit in the host's available memory, so the federation
    can never starve the host whatever its services take."""
    need, have = spec.memory_bytes, available_memory(meminfo)
    if need > have:
        raise RuntimeError(
            f"the layout's memory limits add up to {gib(need)} for {len(spec.services)} services and the host has "
            f"{gib(have)} available; refused"
        )


def cgroup_value(container: str, name: str) -> int | None:
    """One memory figure of a running container's own cgroup, in bytes, or None when it cannot be read in time."""
    try:
        done = subprocess.run(
            ["docker", "exec", container, "cat", f"{CGROUP}/{name}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=EXEC_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None
    value = done.stdout.strip()
    return int(value) if done.returncode == 0 and value.isdigit() else None


def memory_figure(container: str, figure: str) -> int | None:
    """A figure from the cgroup v2 file, or from the v1 file when the v2 one reads nothing."""
    for name in MEMORY_FILES[figure]:
        value = cgroup_value(container, name)
        if value is not None:
            return value
    return None


def memory_usage(containers: list[Container]) -> dict[str, dict[str, int | None]]:
    """For each running service: its peak since the container started, its current use and its cgroup's limit."""
    return {
        c.service: {figure: memory_figure(c.name, figure) for figure in MEMORY_FILES}
        for c in containers
        if c.state == "running"
    }


def memory_lines(spec: Spec, usage: dict[str, dict[str, int | None]]) -> list[str]:
    """One line per declared service: its declared limit, and what its cgroup read when it was running."""
    lines = []
    for service in spec.services:
        read = usage.get(service.name)
        if read is None:
            lines.append(f"{service.name}: limit {service.memory_limit}, not running")
            continue
        lines.append(
            f"{service.name}: limit {service.memory_limit} (cgroup {gib(read['max'])}), "
            f"current {gib(read['current'])}, peak {gib(read['peak'])}"
        )
    return lines

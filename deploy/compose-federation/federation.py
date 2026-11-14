"""One command each to bring a throwaway federation up, reset it to its baseline, and take it down, as its layout
declares it.

    uv run python deploy/compose-federation/federation.py [--layout PATH] up
    uv run python deploy/compose-federation/federation.py [--layout PATH] reset --clean | --replicas
    uv run python deploy/compose-federation/federation.py [--layout PATH] down
    uv run python deploy/compose-federation/federation.py [--layout PATH] status | memory | describe | compose

- up: from nothing, make the root, the CA, the certificates and the secrets, start the zone, bootstrap it, start the
  providers, register and support, add the fixed users, write the baseline files (into every space but those the layout
  declares `baseline = false`), and write facts.json (and platform.toml, from the layout's platform template). Last, it
  runs the link check through that platform.toml (fedlinks.py, #221) and fails, naming the pair and the remedy, unless
  every ordered (reader, source) pair of providers sharing a space transfers. On a federation that is already up it
  starts what is stopped and checks every
  step again, the links included, and leaves the files as they are.
- reset --clean: start whatever is stopped, check every step again, and bring every space's content back to the
  baseline, removing whatever was ingested; a space declared `baseline = false` is left empty. The ids of the
  providers, the spaces and the users stay, so platform.toml stays valid.
- reset --replicas: start whatever is stopped, check every step again, keep every owner's data, and evict only what
  the mirrors hold, verified on the block maps (fedreplicas.py). For between runs. A bare `reset` is refused, naming
  both, so nobody wipes ingested datasets by accident.
- down: remove the project's containers, network and volumes (`compose down --volumes`; the federation's data is
  all bind-mounted under the root, so no volume holds anything it needs), then the root with everything in it, the CA
  and the secrets included. It then checks that no container, network or Docker volume labelled with the project is
  left, and fails naming the volumes if one is. Nothing of the federation is left on disk, and the next up makes
  everything anew.
- status: the project's containers, the root's allowed parent, and whether facts.json exists.
- memory: each service's declared limit, and what its cgroup reads now and at its peak since the container started.
- describe: the declared layout and the root, as the table the day-0 report records.
- compose: the Compose file the layout generates.

The layout is `federation.toml` beside this file unless --layout names another (fedspec.py). Only the layout's Compose
project is ever named, and only over its own root: no other container, network or image is touched, nothing is
pulled, and nothing is pruned.
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fedboot import Bootstrap
from fedcompose import render as render_compose
from fedcontent import Content
from feddocker import (
    compose,
    gib,
    memory_lines,
    memory_usage,
    project_containers,
    project_networks,
    project_volumes,
    require_images,
    require_memory_fits,
    require_own,
    require_ports_free,
)
from fedfacts import baseline_left_behind, describe, root_parent, write_facts
from fedlayout import NODE, ROOT_MARKER, Layout
from fedlinks import check_after_up
from fedpki import (
    ensure_secret,
    make_ca,
    make_leaf,
    private_directory,
    subject_alt_names,
    verify_leaf,
    write_secret,
)
from fedreplicas import evict_replicas
from fedrest import Rest
from fedspec import LOOPBACK, ZONE, LayoutError, Service, Spec, load_spec

ZONE_TIMEOUT_S = 1500.0
PROVIDER_TIMEOUT_S = 1800.0
SUPPORT_TIMEOUT_S = 600.0
TRANSFER_TIMEOUT_S = 600.0
GUI_CHECK_OFF = """[
    {oz_worker, [
        % the images are development builds: the zone would refuse the providers' GUI packages, which
        % only the browser interface uses; the REST interfaces and certificate checks are unaffected
        {gui_package_verification, false}
    ]}
].
"""


class Timer:
    """Seconds per phase of a command."""

    def __init__(self) -> None:
        self.began = self.last = time.monotonic()
        self.phases: list[tuple[str, float]] = []

    def mark(self, phase: str) -> None:
        now = time.monotonic()
        self.phases.append((phase, now - self.last))
        self.last = now

    def summary(self, command: str) -> str:
        parts = ", ".join(f"{phase} {seconds:.0f} s" for phase, seconds in self.phases)
        return f"{command}: {time.monotonic() - self.began:.0f} s ({parts})"


# -- the root, the CA and the secrets --------------------------------------------------------------------------------


def seed(layout: Layout, service: Service, key: Path, cert: Path) -> None:
    """Put the service's web certificate and the CA where its panel and worker read them, before its first start.

    The image copies its own defaults into the persistence directory only where a file is missing, so these win."""
    zone = service.name == ZONE
    panel, worker = ("oz_panel", "oz_worker") if zone else ("op_panel", "op_worker")
    etc = layout.persistence(service.name) / "etc"
    certs = etc / panel / "certs"
    certs.mkdir(parents=True)
    shutil.copyfile(cert, certs / "web_cert.pem")
    shutil.copyfile(layout.ca_pem, certs / "web_chain.pem")
    write_secret(certs / "web_key.pem", key.read_text(encoding="utf-8"))
    for component in (panel, worker):
        trusted = etc / component / "cacerts"
        trusted.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(layout.ca_pem, trusted / "expcore-federation-ca.pem")
    if zone:
        settings = etc / worker / "config.d"
        settings.mkdir(parents=True)
        (settings / "disable-gui-verification.config").write_text(GUI_CHECK_OFF, encoding="utf-8")


def prepare(layout: Layout) -> None:
    """Make the root and everything the services read from it; a step whose result exists is left as it is.

    The root is marked as the federation's own, with the declared project's name, when up makes it or finds it empty.
    A root that holds anything without that mark is someone else's, or another layout's, and is refused: down would
    delete what it holds."""
    spec, owned = layout.spec, layout.owned
    if layout.outer_roots or layout.inner_roots:
        raise RuntimeError(
            "the root lies inside another federation's root, or holds one; refused, since down empties a root whole: "
            "give each federation a root beside the others"
        )
    if layout.root.exists() and not owned and any(layout.root.iterdir()):
        raise RuntimeError(
            f"the root holds files and is not marked as this federation's ({ROOT_MARKER} naming {spec.project}); "
            "refused"
        )
    private_directory(layout.root)
    if not owned:
        layout.marker.write_text(spec.project + "\n", encoding="utf-8")
    private_directory(layout.secrets)
    private_directory(layout.users)
    layout.ca_pem.parent.mkdir(exist_ok=True)
    layout.journal.mkdir(exist_ok=True)
    make_ca(layout.ca_pem, layout.ca_key, f"{spec.project} test CA")
    work = private_directory(layout.secrets / "work")
    for service in spec.services:
        key, cert = layout.secrets / f"{service.name}.key", layout.ca_pem.parent / f"{service.name}.pem"
        make_leaf(
            layout.ca_pem,
            layout.ca_key,
            key,
            cert,
            service.domain,
            subject_alt_names(service.domain, NODE, [LOOPBACK]),
            work,
        )
        verify_leaf(layout.ca_pem, cert, service.domain)
        verify_leaf(layout.ca_pem, cert, LOOPBACK)
        passphrase = ensure_secret(layout.passphrase(service.name))
        if not layout.env_file(service.name).exists():
            write_secret(layout.env_file(service.name), f"ONEPANEL_EMERGENCY_PASSPHRASE={passphrase}\n")
        if not layout.persistence(service.name).exists():
            seed(layout, service, key, cert)
        storage = layout.storage(service.name)
        if service.name != ZONE and not storage.exists():
            # The provider writes as the users it maps the space's members to, so its storage is open to them; the
            # root above it is 0700, so no other user of the host can reach it.
            storage.mkdir()
            storage.chmod(0o777)
    work.rmdir()


# -- the commands ----------------------------------------------------------------------------------------------------


def bring_up(layout: Layout, boot: Bootstrap, timer: Timer) -> None:
    """Start the zone, bootstrap it, start the providers, and settle registration, spaces, support and members."""
    spec = layout.spec
    compose(layout, "up", "--detach", spec.zone.name)
    boot.wait_zone(ZONE_TIMEOUT_S)
    timer.mark("zone")
    boot.ensure_admin_token()
    boot.ensure_users()
    boot.ensure_registration_tokens()
    compose(layout, "up", "--detach", *(p.name for p in spec.providers))
    boot.wait_providers(PROVIDER_TIMEOUT_S)
    timer.mark("providers")
    boot.ensure_spaces()
    boot.ensure_support(SUPPORT_TIMEOUT_S)
    for space in spec.spaces:
        if spec.members(space.name):
            boot.ensure_members(space.name)
    timer.mark("spaces")


def up(layout: Layout) -> str:
    timer = Timer()
    require_images()
    containers = project_containers(layout.spec.project)
    require_own(layout, containers, project_networks(layout.spec.project))
    left = baseline_left_behind(layout)
    if left:
        raise RuntimeError(
            f"the layout now declares baseline = false for {', '.join(left)}, brought up with baseline files; up on a "
            "federation that is up writes and removes no baseline file, so they would stay where an agent can read "
            "them: run reset --clean, which empties a baseline-off space, then up again"
        )
    fresh = not containers
    if fresh:
        require_ports_free(layout.spec)
        require_memory_fits(layout.spec)
    prepare(layout)
    timer.mark("prepare")
    rest = Rest(layout.ca_pem)
    try:
        boot = Bootstrap(rest, layout)
        bring_up(layout, boot, timer)
        if not layout.facts.exists():
            Content(boot).reset(TRANSFER_TIMEOUT_S)
            timer.mark("baseline")
        written = write_facts(layout, boot)
        links = check_after_up(layout, dict(boot.ids.spaces))  # last: not ready until every pair transfers (#221)
        timer.mark("links")
    finally:
        rest.close()
    return timer.summary("up" if fresh else "up (already up: checked)") + f"\n{written}\n{links}"


def require_up(layout: Layout) -> None:
    containers = project_containers(layout.spec.project)
    require_own(layout, containers, project_networks(layout.spec.project))
    if not containers or not layout.facts.exists():
        raise RuntimeError("the federation is not up; run up first")


def reset_clean(layout: Layout) -> str:
    """Back to clean: every space emptied and its baseline written again, whatever a run or a study ingested."""
    timer = Timer()
    require_up(layout)
    rest = Rest(layout.ca_pem)
    try:
        boot = Bootstrap(rest, layout)
        bring_up(layout, boot, timer)
        removed = Content(boot).reset(TRANSFER_TIMEOUT_S)
        timer.mark(f"baseline (removed {sum(removed.values())} entries)")
        for leftover in layout.journal.iterdir():
            leftover.unlink()
        write_facts(layout, boot)
    finally:
        rest.close()
    return timer.summary("reset --clean")


def reset_replicas(layout: Layout) -> str:
    """Between runs: every owner keeps its data, and every mirror's replicas are evicted and verified gone."""
    timer = Timer()
    require_up(layout)
    rest = Rest(layout.ca_pem)
    try:
        boot = Bootstrap(rest, layout)
        bring_up(layout, boot, timer)
        evicted = evict_replicas(boot, TRANSFER_TIMEOUT_S)
        timer.mark("replicas")
    finally:
        rest.close()
    return "\n".join([timer.summary("reset --replicas"), *(f"{space}: {done}" for space, done in evicted.items())])


def down(layout: Layout) -> str:
    """Stop and remove the project, then empty and remove the root; refuse first, touching nothing, when the project's
    containers or networks bind another root or the root lacks this layout's mark.

    Compose runs only over a root that exists and carries the mark naming this project: with the root gone, a compose
    down under the project's name could only remove what another federation of that name left, its network included.
    """
    timer = Timer()
    project = layout.spec.project
    containers, networks = project_containers(project), project_networks(project)
    require_own(layout, containers, networks)
    if not layout.root.exists():
        if containers or networks:
            raise RuntimeError(
                f"the root is missing while {len(containers)} container(s) and {len(networks)} network(s) of the "
                f"project {project} name it; nothing stopped, since compose runs only over a marked root"
            )
        return timer.summary("down (already down)")
    if not layout.owned:
        raise RuntimeError(
            f"the root is not marked as this federation's ({ROOT_MARKER} naming {project}); nothing stopped or deleted"
        )
    if layout.inner_roots:
        raise RuntimeError("the root holds another federation's root; nothing stopped or deleted")
    peaks = memory_usage(containers)
    # `--volumes`: any anonymous or project volume goes with the containers. The federation's data is all bind-mounted
    # under its root, so nothing it needs is lost.
    compose(layout, "down", "--volumes", "--remove-orphans", "--timeout", "120")
    timer.mark("containers")
    compose(layout, "--profile", "janitor", "run", "--rm", "--no-deps", "janitor")
    layout.root.rmdir()
    timer.mark("data")
    left = project_containers(project) + project_networks(project)
    volumes = project_volumes(project)
    if volumes:
        raise RuntimeError(
            f"down left {len(volumes)} volume(s) labelled with the project {project} behind ({', '.join(volumes)}); "
            "docker compose down --volumes did not remove them, and nothing else removes them"
        )
    if left or layout.root.exists():
        raise RuntimeError(f"down left {len(left)} container(s) or network(s), or the root, behind")
    summary = timer.summary("down")
    if peaks:
        summary += "\npeak memory: " + ", ".join(f"{name} {gib(read['peak'])}" for name, read in peaks.items())
    return summary


def status(layout: Layout) -> str:
    lines = [str(c) for c in project_containers(layout.spec.project)] or ["no container of the project"]
    parent = root_parent(layout)
    lines.append(f"layout: {layout.spec.source} (project {layout.spec.project})")
    lines.append(f"root parent: {parent['path']} ({parent['set_by']})")
    lines.append(f"facts: {'present' if layout.facts.exists() else 'absent'}")
    return "\n".join(lines)


def memory(layout: Layout) -> str:
    containers = project_containers(layout.spec.project)
    require_own(layout, containers, project_networks(layout.spec.project))
    return "\n".join(memory_lines(layout.spec, memory_usage(containers)))


RESET_CHOICE = (
    "say which reset: 'reset --clean' empties every space and writes its baseline again, removing whatever was "
    "ingested, to bring the deployment back to clean; 'reset --replicas' keeps every owner's data and evicts only the "
    "replicas on the providers that mirror a space, between runs"
)


def main(argv: list[str] | None = None) -> int:
    commands = {
        "up": up,
        "reset --clean": reset_clean,
        "reset --replicas": reset_replicas,
        "down": down,
        "status": status,
        "memory": memory,
        "describe": describe,
    }
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--layout", type=Path, help="the layout file (default: federation.toml beside this script)")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--clean", action="store_true", help="reset: back to the baseline, ingested data removed")
    which.add_argument("--replicas", action="store_true", help="reset: evict the mirrors' replicas, keep all data")
    parser.add_argument("command", choices=("up", "reset", "down", "status", "memory", "describe", "compose"))
    args = parser.parse_args(argv)
    command = args.command
    if command == "reset" and not (args.clean or args.replicas):
        print(f"error: reset: {RESET_CHOICE}", file=sys.stderr)  # nobody wipes ingested data by accident
        return 2
    if command != "reset" and (args.clean or args.replicas):
        print(f"error: {command}: --clean and --replicas belong to reset", file=sys.stderr)
        return 2
    if command == "reset":
        command = "reset --clean" if args.clean else "reset --replicas"
    try:
        spec: Spec = load_spec(args.layout)
    except LayoutError as error:
        print(f"error: {command}: {error}", file=sys.stderr)
        return 2
    if command == "compose":
        print(render_compose(spec), end="")
        return 0
    try:
        layout = Layout.from_environment(spec=spec)  # before anything is touched: a root elsewhere is refused
    except ValueError as error:
        print(f"error: {command}: {error}", file=sys.stderr)
        return 2
    try:
        print(commands[command](layout))
    except (RuntimeError, TimeoutError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

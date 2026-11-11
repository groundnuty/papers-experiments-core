"""One command each to bring the throwaway federation up, reset it to its baseline, and take it down.

    uv run python deploy/compose-federation/federation.py up
    uv run python deploy/compose-federation/federation.py reset
    uv run python deploy/compose-federation/federation.py down
    uv run python deploy/compose-federation/federation.py status

- up: from nothing, make the root, the CA, the certificates and the secrets, start the zone, bootstrap it, start the
  providers, register and support, add the fixed users, write the baseline files, and write facts.json (and
  platform.toml, from platform.toml.template). On a federation that is already up it starts what is stopped and checks
  every step again, and leaves the files as they are.
- reset: start whatever is stopped, check every step again, and bring the spaces' content back to the baseline. The ids
  of the providers, the spaces and the users stay, so platform.toml stays valid.
- down: remove this project's containers and network, then the root with everything in it, the CA and the secrets
  included. Nothing of the federation is left on disk, and the next up makes everything anew.

Only the Compose project `expcore-fed` is ever named: no other container, network or image is touched, nothing is
pulled, and nothing is pruned.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from fedboot import Bootstrap
from fedcontent import Content
from fedlayout import (
    COMPOSE_FILE,
    CONTENT_FILE,
    HOLDER,
    IMAGES,
    LISTING_DIR,
    LISTING_ENTRIES,
    LOOPBACK,
    NODE,
    PRINCIPALS,
    PROJECT,
    PROVIDERS,
    ROOT_MARKER,
    SERVICES,
    SPACES,
    STUDY_SPACE,
    TEMPLATE,
    ZONE,
    Layout,
    Service,
)
from fedpki import (
    ensure_secret,
    make_ca,
    make_leaf,
    private_directory,
    subject_alt_names,
    verify_leaf,
    write_secret,
)
from fedrest import Rest

ZONE_TIMEOUT_S = 1500.0
PROVIDER_TIMEOUT_S = 1800.0
SUPPORT_TIMEOUT_S = 600.0
TRANSFER_TIMEOUT_S = 600.0
PLACEHOLDER = re.compile(r"@([a-z_]+(?:\.[a-z0-9-]+)?)@")
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


# -- docker, for this project only ---------------------------------------------------------------------------------


def compose(layout: Layout, *args: str, capture: bool = False) -> str:
    """Run docker compose for this project's file and name; raise on failure."""
    env = {**os.environ, "EXPCORE_FED_ROOT": str(layout.root)}
    command = ["docker", "compose", "--file", str(COMPOSE_FILE), "--project-name", PROJECT, *args]
    done = subprocess.run(command, env=env, check=False, capture_output=capture, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"docker compose {args[0]} failed with status {done.returncode}")
    return done.stdout if capture else ""


def docker_lines(*args: str) -> list[str]:
    """Run a read-only docker command and return its non-empty output lines."""
    done = subprocess.run(["docker", *args], check=True, capture_output=True, text=True)
    return [line for line in done.stdout.splitlines() if line.strip()]


def project_containers() -> list[str]:
    """Every container of this project, running or not, as 'name state'."""
    label = f"label=com.docker.compose.project={PROJECT}"
    return docker_lines("ps", "--all", "--filter", label, "--format", "{{.Names}} {{.State}}")


def project_networks() -> list[str]:
    return docker_lines(
        "network", "ls", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.Name}}"
    )


def require_images() -> None:
    """Both pinned images must already be present: nothing is ever pulled."""
    for image in IMAGES:
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode != 0:
            raise RuntimeError(f"the image {image} is not present; it is never pulled here")


def require_ports_free() -> None:
    """Before a first start, nothing may listen on the loopback ports the services will publish."""
    for service in SERVICES:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex((LOOPBACK, service.port)) == 0:
                raise RuntimeError(f"something already listens on the loopback port meant for {service.name}")


# -- the root, the CA and the secrets --------------------------------------------------------------------------------


def seed(layout: Layout, service: Service, key: Path, cert: Path) -> None:
    """Put the service's web certificate and the CA where its panel and worker read them, before its first start.

    The image copies its own defaults into the persistence directory only where a file is missing, so these win."""
    zone = service is ZONE
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

    The root is marked as the federation's own when up makes it or finds it empty. A root that holds anything without
    the mark is someone else's, and is refused: down would delete what it holds."""
    owned = layout.marker.is_file() and layout.marker.read_text(encoding="utf-8").strip() == PROJECT
    if layout.root.exists() and not owned and any(layout.root.iterdir()):
        raise RuntimeError(f"the root holds files and is not marked as this federation's ({ROOT_MARKER}); refused")
    private_directory(layout.root)
    if not owned:
        layout.marker.write_text(PROJECT + "\n", encoding="utf-8")
    private_directory(layout.secrets)
    private_directory(layout.users)
    layout.ca_pem.parent.mkdir(exist_ok=True)
    layout.journal.mkdir(exist_ok=True)
    make_ca(layout.ca_pem, layout.ca_key, "expcore-fed test CA")
    work = private_directory(layout.secrets / "work")
    for service in SERVICES:
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
        if service is not ZONE and not storage.exists():
            # The provider writes as the users it maps the space's members to, so its storage is open to them; the
            # root above it is 0700, so no other user of the host can reach it.
            storage.mkdir()
            storage.chmod(0o777)
    work.rmdir()


# -- facts and platform.toml ------------------------------------------------------------------------------------------


def facts_of(boot: Bootstrap) -> dict:
    """What an integrator needs to write platform.toml: addresses, ids, names and where the credentials are.

    It holds no secret: the credentials are named by their files."""
    ids = boot.ids
    return {
        "zone": {"url": ZONE.url, "domain_on_project_network": ZONE.domain},
        "providers": {
            p.name: {"url": p.url, "id": ids.providers[p.name], "domain_on_project_network": p.domain}
            for p in PROVIDERS
        },
        "spaces": {name: {"id": ids.spaces[name], "supported_by": list(by)} for name, by in SPACES.items()},
        "study_space": STUDY_SPACE,
        "data_provider": HOLDER,
        "principals": {
            user: {"user_id": ids.users[user], "privileges": list(rights)} for user, rights in PRINCIPALS.items()
        },
        "credentials": {
            "admin_access_token_file": "secrets/admin.token",
            "user_password_directory": "secrets/users",
            "user_password_file_pattern": "{user}.pass",
        },
        "ca_file": "ca/federation-ca.pem",
        "files": {"content": CONTENT_FILE, "listing_directory": LISTING_DIR, "listing_entries": LISTING_ENTRIES},
        "paths_relative_to": "the directory holding this file",
    }


def placeholders(facts: dict) -> dict[str, str]:
    """The values platform.toml.template names, by placeholder."""
    values = {"zone_url": facts["zone"]["url"], "space_name": facts["study_space"]}
    for name, provider in facts["providers"].items():
        values[f"provider_url.{name}"] = provider["url"]
        values[f"provider_id.{name}"] = provider["id"]
    for name, space in facts["spaces"].items():
        values[f"space_id.{name}"] = space["id"]
    for user, principal in facts["principals"].items():
        values[f"user_id.{user}"] = principal["user_id"]
    return values


def render(template: str, values: dict[str, str]) -> str:
    """Fill every placeholder of the template; refuse one that has no value."""
    missing = sorted({m for m in PLACEHOLDER.findall(template) if m not in values})
    if missing:
        raise RuntimeError(f"platform.toml.template names {len(missing)} placeholder(s) the facts do not hold")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def write_facts(layout: Layout, boot: Bootstrap) -> None:
    facts = facts_of(boot)
    layout.facts.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if TEMPLATE.exists():
        layout.platform.write_text(render(TEMPLATE.read_text(encoding="utf-8"), placeholders(facts)), encoding="utf-8")


# -- the commands ----------------------------------------------------------------------------------------------------


def bring_up(layout: Layout, boot: Bootstrap, timer: Timer) -> None:
    """Start the zone, bootstrap it, start the providers, and settle registration, spaces, support and members."""
    compose(layout, "up", "--detach", ZONE.name)
    boot.wait_zone(ZONE_TIMEOUT_S)
    timer.mark("zone")
    boot.ensure_admin_token()
    boot.ensure_users()
    boot.ensure_registration_tokens()
    compose(layout, "up", "--detach", *(p.name for p in PROVIDERS))
    boot.wait_providers(PROVIDER_TIMEOUT_S)
    timer.mark("providers")
    boot.ensure_spaces()
    boot.ensure_support(SUPPORT_TIMEOUT_S)
    boot.ensure_members(STUDY_SPACE)
    timer.mark("spaces")


def up(layout: Layout) -> str:
    timer = Timer()
    require_images()
    fresh = not project_containers()
    if fresh:
        require_ports_free()
    prepare(layout)
    timer.mark("prepare")
    rest = Rest(layout.ca_pem)
    try:
        boot = Bootstrap(rest, layout)
        bring_up(layout, boot, timer)
        if not layout.facts.exists():
            Content(boot).reset(TRANSFER_TIMEOUT_S)
            timer.mark("baseline")
        write_facts(layout, boot)
    finally:
        rest.close()
    return timer.summary("up" if fresh else "up (already up: checked)")


def reset(layout: Layout) -> str:
    timer = Timer()
    if not project_containers() or not layout.facts.exists():
        raise RuntimeError("the federation is not up; run up first")
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
    return timer.summary("reset")


def down(layout: Layout) -> str:
    timer = Timer()
    had = bool(project_containers()) or layout.root.exists()
    compose(layout, "down", "--remove-orphans", "--timeout", "120")
    timer.mark("containers")
    if layout.root.exists():
        if not (layout.marker.is_file() and layout.marker.read_text(encoding="utf-8").strip() == PROJECT):
            raise RuntimeError(f"the root is not marked as this federation's ({ROOT_MARKER}); nothing deleted")
        compose(layout, "--profile", "janitor", "run", "--rm", "--no-deps", "janitor")
        layout.root.rmdir()
    timer.mark("data")
    left = project_containers() + project_networks()
    if left or layout.root.exists():
        raise RuntimeError(f"down left {len(left)} container(s) or network(s), or the root, behind")
    return timer.summary("down" if had else "down (already down)")


def status(layout: Layout) -> str:
    lines = project_containers() or ["no container of the project"]
    lines.append(f"facts: {'present' if layout.facts.exists() else 'absent'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("up", "reset", "down", "status"))
    command = parser.parse_args(argv).command
    try:
        layout = Layout.from_environment()  # before anything is touched: a root elsewhere is refused
    except ValueError as error:
        print(f"error: {command}: {error}", file=sys.stderr)
        return 2
    try:
        print({"up": up, "reset": reset, "down": down, "status": status}[command](layout))
    except (RuntimeError, TimeoutError, OSError) as error:
        print(f"error: {command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

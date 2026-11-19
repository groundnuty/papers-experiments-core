"""fleetcompose.py: a Compose site of the elastic fleet as the controller reaches it (README.md, "A Compose site of the
elastic fleet"): a host (a group VM) whose federations the operator brought up with `deploy/compose-federation/
federation.py --instance K up`, its static members, driven over ssh through `composesite.py` there.

A `[[site]]` of the sites file with `kind = "compose"` is an ssh site (`host`, `interpreter`, `env_file`, and `deploy`,
here the host's `deploy/compose-federation`) with `fleet` (the site's own 0700 directory on the host, one per campaign),
`channel` (its channel root there, one per campaign) and:

- `members`: a table of the static members, each its label (`[a-z0-9-]`) and its root (absolute, on the host; the
  roots' location is the site's: a host's data volume, or its root disk). A member retired for the campaign's channel
  whose root reads live and linked again joins again as its next generation (`<member>-g2`, ... `-g9`: a new ledger
  label, its own channel, records and copy); the operator may instead add a new member naming the same root, listed
  after it, which takes the root over once the earlier one is retired (members naming one root hold it one at a
  time, in the file's order);
- `study`: the study's directory on the host, which each worker runs a copy of;
- `home`: the workers' HOME on the host, a 0700 directory holding the study's key tree at its models.toml paths, which
  the operator placed there (never copied by anything here);
- `engine` (`docker` by default, `podman` accepted and untested). A member has no walltime: its recorded end is 10
  years from its read, never a setting, so the ledger never proves an end the host does not enforce.

Its `SiteSettings` need none of an HPC site's sizes: `max_federations` and `warm_pool` default to the number of members
(no static member is drained for being idle: a ledger label joins once), `allowance_units` to 0 and
`rate_units_per_hour` to 1 (no node, no add). A join reads the member's identity there (`composesite.py identity`) and
launches its worker (`composesite.py launch`); a retirement stops the worker and proves it gone, collects, pulls, pulls
the places, verifies the copy here, and records the member retired, with no `down`.
"""

import os
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

KIND = "compose"
KEYS = ("members", "study", "home", "engine")
SCRIPT = "composesite.py"
ENGINES = ("docker", "podman")  # `feddocker.ENGINES`
HORIZON_S = 3650 * 86400.0  # `composesite.HORIZON_S`: a member has no walltime, so its end is never reached
MEMBER = re.compile(r"[a-z0-9][a-z0-9-]{0,36}")  # with a generation's `-g9`, the host's 40 (`composesite.MEMBER`)
GENERATION = re.compile(r"(.+)-g([2-9])")  # `composesite.generation`: a member's later generation, its own label
LABEL_MAX = 64  # the ledger's label limit
# The seam for a member's prepare hook (the prepare-hook lane's): argv run on the host after the identity read and
# before the worker's launch, each in order, a failure making the join wait. None is configured in this cut.
PREPARE: Tuple[Tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ComposeSite:
    """What a Compose site adds to an ssh site: its static members (label, root), the study and the workers' HOME on
    the host, the engine, and a member's horizon."""

    members: Tuple[Tuple[str, str], ...]
    study: str
    home: str
    engine: str = "docker"
    horizon_s: float = HORIZON_S

    def root(self, member: str) -> str:
        """A member's root; a later generation's (`<member>-g<k>`, `composesite.chain`) is its member's."""
        found = dict(self.members).get(member)
        later = GENERATION.fullmatch(member)
        if found is None and later is not None:
            found = dict(self.members).get(later.group(1))
        if found is None:
            raise ValueError(f"{member} is no member of the site")
        return found


def _absolute(value: Any, what: str) -> str:
    if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value:
        raise ValueError(f"{what} must be an absolute path")
    return os.path.normpath(value)


def compose_site(label: str, own: Mapping[str, Any]) -> ComposeSite:
    """A Compose site's own keys, checked; ValueError naming the key."""
    members = own.get("members")
    if not isinstance(members, dict) or not members:
        raise ValueError(f"site {label}: members must be a table of at least one member label and its root")
    pairs = []
    for name, root in members.items():
        if not isinstance(name, str) or not MEMBER.fullmatch(name):
            raise ValueError(f"site {label}: members: {name!r} is not a member label ([a-z0-9-])")
        pairs.append((name, _absolute(root, f"site {label}: members.{name}")))
    names = [name for name, _ in pairs]
    for name in names:
        later = GENERATION.fullmatch(name)
        if later is not None and later.group(1) in names:
            raise ValueError(f"site {label}: members: {name} would be a later generation of {later.group(1)}")
        if len(label) + 1 + len(name) + 3 > LABEL_MAX:
            raise ValueError(f"site {label}: members: {name}: its ledger label (and a generation's) would pass "
                             f"{LABEL_MAX} characters")  # fmt: skip
    engine = own.get("engine", "docker")
    if engine not in ENGINES:
        raise ValueError(f"site {label}: engine must be one of {', '.join(ENGINES)}")
    return ComposeSite(tuple(pairs), _absolute(own.get("study"), f"site {label}: study"),
                       _absolute(own.get("home"), f"site {label}: home"), engine)  # fmt: skip


def settings_defaults(entry: dict, compose: ComposeSite) -> None:
    """A Compose site's `SiteSettings` defaults, filled in `entry` where the sites file names none (module docstring).
    Mutates `entry`: it is the sites file's table for this site, read once."""
    count = len(compose.members)
    for key, value in (("max_federations", count), ("warm_pool", count), ("allowance_units", 0.0),
                       ("rate_units_per_hour", 1.0)):  # fmt: skip
        entry.setdefault(key, value)


def script(site: Any, command: str, *words: str) -> List[str]:
    """`composesite.py <command> --engine <the site's> ...` on the host: the command is the fifth word, where the
    executor looks for a retirement's `collect`."""
    return site.script(SCRIPT, command, "--engine", site.compose.engine, *words)


def observe_argv(site: Any) -> List[str]:
    members = [word for name, root in site.compose.members for word in ("--member", f"{name}={root}")]
    return script(site, "observe", "--fleet", str(site.fleet), "--site", site.label, "--channel", str(site.channel),
                  "--horizon-s", repr(site.compose.horizon_s), *members)  # fmt: skip


def channel_of(site: Any, label: str) -> str:
    from expcore.campaign.elastic import channel as ch

    return str(ch.federation_dir(str(site.channel), label))


def launch_steps(action: Any, site: Any) -> Tuple[Tuple[str, ...], ...]:
    """A joining member's steps on the host: the prepare seam's (none yet), then the worker's launch."""
    compose = site.compose
    launch = script(site, "launch", "--fleet", str(site.fleet), "--member", action.member, "--root",
                    compose.root(action.member), "--federation", action.label, "--channel",
                    channel_of(site, action.label), "--study", compose.study, "--home", compose.home)  # fmt: skip
    return (*PREPARE, tuple(launch))


def identity_argv(site: Any, member: str) -> List[str]:
    return script(site, "identity", "--fleet", str(site.fleet), "--root", site.compose.root(member), "--study",
                  site.compose.study, "--home", site.compose.home)  # fmt: skip


def stop_argv(site: Any, member: str) -> List[str]:
    return script(site, "stop", "--fleet", str(site.fleet), "--member", member)


def collect_argv(site: Any, root: str, into: str) -> List[str]:
    return script(site, "collect", "--root", root, "--into", into)


def retired_argv(site: Any, member: str, label: str) -> List[str]:
    return script(site, "retired", "--fleet", str(site.fleet), "--member", member, "--channel",
                  channel_of(site, label))  # fmt: skip


def is_compose(site: Optional[Any]) -> bool:
    return site is not None and getattr(site, "compose", None) is not None

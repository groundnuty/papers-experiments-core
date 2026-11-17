"""What up writes into the root for an integrator, and what the day-0 report records of a deployment.

facts.json holds the addresses, ids and names a platform.toml needs, the declared layout's name, digest and memory
limits, the root's allowed parent, each space's declared supporters beside the ones the zone listed (by layout label,
with the time read) and how each imported space is held (its mode, the name of the variable that gave its source, its
storage and its scan, never the source's path); never a secret, since credentials are named by their files.
platform.toml is rendered from the layout's template with those facts, and with the root's own absolute path where the
template names it (`@hpc_root@`, the HPC render's name; `render_root`). `describe` gives the deployment's
table for the day-0 report.
"""

import json
import os
import re
from pathlib import Path

from fedboot import Bootstrap, Ids
from feddocker import gib
from fedlayout import CONTENT_FILE, LISTING_DIR, LISTING_ENTRIES, Layout, import_storage_name
from fedspec import Space, Spec
from fedtools import check_rendered, require_study_artifacts

PLACEHOLDER = re.compile(r"@([a-z_]+(?:\.[a-z0-9_-]+)?)@")
# The root's placeholder: the name the HPC render fills (`hpcboot.hpc_values`), so one template text names the root on
# either platform. Only a template that names it has the root checked, filled and recorded.
ROOT_PLACEHOLDER = "hpc_root"
TOML_UNSAFE = '"\\'  # a path with one of these, or a non-printable character, cannot stand in a basic string as it is


def root_parent(layout: Layout) -> dict:
    """Where the root was allowed to lie, and which setting said so: the day-0 report records it."""
    return {
        "path": str(layout.parent),
        "set_by": "default" if layout.parent_is_default else "EXPCORE_FED_ROOT_PARENT",
    }


def render_root(layout: Layout) -> str:
    """The federation's root as `@hpc_root@` is filled with it, or a refusal naming the placeholder and the reason,
    never the path. The root must be an absolute path of an existing directory that carries this federation's mark,
    with no symbolic link on any component of its path (its real path is its path, so a copy of the file beside a study
    reads the root's own files and `down`'s root is the one named) and nothing a TOML basic string cannot hold as it
    is: the value is written into the file unescaped, and a quote there would end a string."""
    name, root = f"@{ROOT_PLACEHOLDER}@", layout.root
    text = str(root)
    if not os.path.isabs(text):
        raise RuntimeError(f"{name}: the federation's root must be an absolute path")
    if os.path.realpath(text) != os.path.normpath(text):
        raise RuntimeError(
            f"{name}: the federation's root must not be, or pass through, a symbolic link: set EXPCORE_FED_ROOT and "
            "EXPCORE_FED_ROOT_PARENT to the real path"
        )
    if not root.exists():
        raise RuntimeError(f"{name}: the federation's root does not exist")
    if not root.is_dir():
        raise RuntimeError(f"{name}: the federation's root is not a directory")
    if any(c in text for c in TOML_UNSAFE) or not text.isprintable():
        raise RuntimeError(f"{name}: the federation's root has a character in its path that a TOML string cannot hold")
    if not layout.owned:
        raise RuntimeError(f"{name}: the federation's root does not carry this federation's mark")
    return text


def baseline_files(spec: Spec) -> dict | None:
    """The study space's baseline files a check may read, or None when the study space is declared `baseline = false`:
    it holds none, so a check that reads one has nothing to read there. `spaces.<name>.baseline` says which other
    spaces keep theirs."""
    if not spec.space(spec.study_space).baseline:
        return None
    return {"content": CONTENT_FILE, "listing_directory": LISTING_DIR, "listing_entries": LISTING_ENTRIES}


def baseline_left_behind(layout: Layout) -> list[str]:
    """The spaces the recorded facts show were brought up with baseline files and the layout now declares
    `baseline = false`: up on a federation that is up writes and removes none, so those files would stay where an agent
    can read them. Facts from before the flag was recorded count as having had the files; a space the facts do not list
    had none; no facts.json (a first up) means nothing was written."""
    if not layout.facts.is_file():
        return []
    try:
        recorded = json.loads(layout.facts.read_text(encoding="utf-8"))["spaces"]
        had_files = {name: space.get("baseline", True) for name, space in recorded.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError(
            f"{layout.facts.name} cannot be read as the facts up writes ({type(error).__name__})"
        ) from None
    return [s.name for s in layout.spec.spaces if not s.baseline and had_files.get(s.name, False)]


def import_facts(space: Space, ids: Ids) -> dict | None:
    """How a space is imported, or None: its mode, the name of the variable that gave its source, its storage's name and
    the scan the wait read for it (None before one was read). Never the source's path: it is the host's own."""
    if space.imported is None:
        return None
    return {
        "readonly": space.imported.readonly,
        "source_env": space.imported.source_env,
        "storage": import_storage_name(space.name),
        "scan": ids.scans.get(space.name),
    }


def support_facts(ids: Ids, space: str) -> dict:
    """A space's support set as `verify_support` read it from the zone (O35): its providers by layout label, sorted, and
    the time it was read, beside the declared `supported_by`. Nothing for a space no read was kept for, so facts built
    by a bootstrap that never measured are what they always were."""
    read = getattr(ids, "support", {}).get(space)
    return {} if read is None else {"supported_by_measured": list(read.providers), "support_read_at": read.read_at}


def facts_of(boot: Bootstrap) -> dict:
    """What an integrator needs to write platform.toml: addresses, ids, names and where the credentials are.

    It holds no secret: the credentials are named by their files."""
    spec, ids = boot.layout.spec, boot.ids
    return {
        "layout": {
            "file": spec.source,
            "sha256": spec.digest,
            "project": spec.project,
            "memory_limits": {service.name: service.memory_limit for service in spec.services},
        },
        "zone": {"url": spec.zone.url, "domain_on_project_network": spec.zone.domain},
        "providers": {
            p.name: {"url": p.url, "id": ids.providers[p.name], "domain_on_project_network": p.domain}
            for p in spec.providers
        },
        "spaces": {
            s.name: {
                "id": ids.spaces[s.name],
                "owner": s.owner,
                "mirrors": list(s.mirrors),
                "supported_by": list(s.supporters),
                **support_facts(ids, s.name),
                "agent_visible": s.agent_visible,
                "baseline": s.baseline,
                "import": import_facts(s, ids),
            }
            for s in spec.spaces
        },
        "study_space": spec.study_space,
        "data_provider": spec.holder,
        "principals": {
            p.name: {
                "user_id": ids.users[p.name],
                "privileges": list(p.privileges),
                "spaces": list(p.spaces),
                "agent": p.agent,
            }
            for p in spec.principals
        },
        **slot_user_facts(boot),
        "credentials": {
            "admin_access_token_file": "secrets/admin.token",
            "user_password_directory": "secrets/users",
            "user_password_file_pattern": "{user}.pass",
        },
        "ca_file": "ca/federation-ca.pem",
        "files": baseline_files(spec),
        "paths_relative_to": "the directory holding this file",
        "root_parent": root_parent(boot.layout),
    }


def slot_user_facts(boot: Bootstrap) -> dict:
    """What the bootstrap found of each slot user the platform template declares (O83): `created`, or `matched` (it
    existed, exactly as declared, and nothing was sent for it). Nothing for a layout whose template declares none, so
    its facts are what they always were. The principals' own entries hold each user's id and privileges."""
    found = getattr(boot, "slot_status", None)
    return {"slot_users": dict(sorted(found.items()))} if found else {}


def placeholders(facts: dict) -> dict[str, str]:
    """The values a platform template names, by placeholder."""
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
        raise RuntimeError(f"the platform template names {len(missing)} placeholder(s) the facts do not hold")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def write_facts(layout: Layout, boot: Bootstrap, study_artifacts: str | None = None) -> str:
    """Write facts.json, and platform.toml when the layout names a template; say which were written. A template may name
    `@study_artifacts@`, the directory of the study's own files that `study_artifacts` gives (O83), and `@hpc_root@`,
    the root's absolute path (`render_root`, O35): a root that does not pass is refused before anything is written, and
    facts.json records the `root` it filled, only then. The written file's slots are checked against the layout's
    principals (`fedtools.check_rendered`)."""
    facts = facts_of(boot)
    template = layout.spec.platform_template
    text = None if template is None else template.read_text(encoding="utf-8")
    root = render_root(layout) if text is not None and f"@{ROOT_PLACEHOLDER}@" in text else None
    if root is not None:
        facts = {**facts, "root": root}
    layout.facts.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if text is None:
        return "facts.json written; the layout names no platform_template, so no platform.toml"
    require_study_artifacts(text, study_artifacts)
    values = {
        **placeholders(facts),
        **({"study_artifacts": study_artifacts} if study_artifacts else {}),
        **({ROOT_PLACEHOLDER: root} if root is not None else {}),
    }
    write_private(layout.platform, render(text, values))
    check_rendered(layout, facts)
    return "facts.json and platform.toml written"


def write_private(path: Path, text: str) -> None:
    """Write a file of mode 0600, tightening one that exists, never through a symbolic link: platform.toml says where
    the credentials are."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def import_note(space: Space) -> str:
    """The clause a space's row of the day-0 table gets when it is imported: its mode and the variable, not its path."""
    if space.imported is None:
        return ""
    mode = "read-only" if space.imported.readonly else "writable"
    return f"; imported ({mode}) from ${space.imported.source_env}"


def describe(layout: Layout) -> str:
    """The declared layout and the root, as the rows of the day-0 report's table of the deployment."""
    spec = layout.spec
    parent = root_parent(layout)
    rows = [
        ("Layout", f"`{spec.source}`, SHA-256 `{spec.digest[:16]}`"),
        ("Compose project", f"`{spec.project}`, domain `{spec.domain}`"),
        ("Root parent", f"`{parent['path']}` ({parent['set_by']})"),
        ("Root", f"`{layout.root}`"),
        ("Onezone", f"`{spec.zone.name}` on 127.0.0.1:{spec.zone.port}, memory limit {spec.zone.memory_limit}"),
        *((f"Oneprovider `{p.name}`", f"127.0.0.1:{p.port}, memory limit {p.memory_limit}") for p in spec.providers),
        *(
            (
                f"Space `{s.name}`" + (" (study space)" if s.name == spec.study_space else ""),
                f"owner `{s.owner}`, mirrors {', '.join(f'`{m}`' for m in s.mirrors) or 'none'}, "
                f"support {gib(s.support_bytes)} each"
                + ("" if s.agent_visible else "; day-0 only, no agent user")
                + ("" if s.baseline else "; no baseline files")
                + import_note(s),
            )
            for s in spec.spaces
        ),
        *(
            (
                f"Principal `{p.name}`",
                f"{', '.join(p.privileges)} in {', '.join(f'`{s}`' for s in p.spaces)}"
                + ("" if p.agent else "; not an agent user"),
            )
            for p in spec.principals
        ),
        ("Memory limits together", f"{gib(spec.memory_bytes)} for {len(spec.services)} services"),
    ]
    return "\n".join(["| Fact | Value |", "|---|---|", *(f"| {fact} | {value} |" for fact, value in rows)])

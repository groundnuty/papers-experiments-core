"""What up writes into the root for an integrator, and what the day-0 report records of a deployment.

facts.json holds the addresses, ids and names a platform.toml needs, the declared layout's name, digest and memory
limits, and the root's allowed parent; never a secret, since credentials are named by their files. platform.toml is
rendered from the layout's template with those facts. `describe` gives the deployment's table for the day-0 report.
"""

import json
import re

from fedboot import Bootstrap
from feddocker import gib
from fedlayout import CONTENT_FILE, LISTING_DIR, LISTING_ENTRIES, Layout

PLACEHOLDER = re.compile(r"@([a-z_]+(?:\.[a-z0-9_-]+)?)@")


def root_parent(layout: Layout) -> dict:
    """Where the root was allowed to lie, and which setting said so: the day-0 report records it."""
    return {
        "path": str(layout.parent),
        "set_by": "default" if layout.parent_is_default else "EXPCORE_FED_ROOT_PARENT",
    }


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
            }
            for s in spec.spaces
        },
        "study_space": spec.study_space,
        "data_provider": spec.holder,
        "principals": {
            p.name: {"user_id": ids.users[p.name], "privileges": list(p.privileges), "spaces": list(p.spaces)}
            for p in spec.principals
        },
        "credentials": {
            "admin_access_token_file": "secrets/admin.token",
            "user_password_directory": "secrets/users",
            "user_password_file_pattern": "{user}.pass",
        },
        "ca_file": "ca/federation-ca.pem",
        "files": {"content": CONTENT_FILE, "listing_directory": LISTING_DIR, "listing_entries": LISTING_ENTRIES},
        "paths_relative_to": "the directory holding this file",
        "root_parent": root_parent(boot.layout),
    }


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


def write_facts(layout: Layout, boot: Bootstrap) -> str:
    """Write facts.json, and platform.toml when the layout names a template; say which were written."""
    facts = facts_of(boot)
    layout.facts.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    template = layout.spec.platform_template
    if template is None:
        return "facts.json written; the layout names no platform_template, so no platform.toml"
    layout.platform.write_text(render(template.read_text(encoding="utf-8"), placeholders(facts)), encoding="utf-8")
    return "facts.json and platform.toml written"


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
                f"support {gib(s.support_bytes)} each",
            )
            for s in spec.spaces
        ),
        *(
            (f"Principal `{p.name}`", f"{', '.join(p.privileges)} in {', '.join(f'`{s}`' for s in p.spaces)}")
            for p in spec.principals
        ),
        ("Memory limits together", f"{gib(spec.memory_bytes)} for {len(spec.services)} services"),
    ]
    return "\n".join(["| Fact | Value |", "|---|---|", *(f"| {fact} | {value} |" for fact, value in rows)])

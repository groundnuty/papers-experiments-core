"""A federation's render identity (O74): what `up` rendered, as hashes alone, in `render-identity.json` in the root.

Two federations rendered from one template, with the same pins and the same data, are one platform, and a shard's step
must be able to tell that the federation it runs against is the one a plan expects: the same template, the same pins,
the same platform fingerprint. The record holds, as canonical JSON with sorted keys (mode 0600):

- `version`: `VERSION`, the record's own format.
- `template_sha256`: the SHA-256 of the platform template as the renderer read it, before it was rendered.
- `platform_fingerprint_sha256`: the SHA-256 of the canonical JSON of the rendered file's
  `PlatformConfig.fingerprint()`, hashed as the core hashes a fingerprint (`expcore.campaign.manifest`). It holds the
  platform's declared identity and its spaces' data identity, never an id the federation made, so two federations of
  one template and one set of data have one (C06); it holds the sandbox's pins too, which is why a pin that changed
  changes it as well.
- the SHA-256 of each pin the rendered platform holds (`PINS`): the agent's SIF, the egress shim, the egress collector
  and oneclient's bundle. They are the very values `up` filled in and checked (`hpcpins`: the site's declared hashes,
  each file measured against its own in the preflight, and the bundle's own collector hashed here), never read again.
  A pin the template does not name is not in the record: no entry is ever empty or null.

It holds no address, id, job, node, path, user or secret: only hashes and the version, so it can travel with a
study's results (`shards.py` copies it into `out/study-state/`) and be committed beside a study as the record a plan
expects. `shards.py` runs alone on a node and keeps its own copy of the names below (a test compares the two).
"""

import hashlib
import json
from pathlib import Path
from typing import Dict, Mapping

from fedfacts import write_private

FILE = "render-identity.json"
VERSION = "expcore-render-identity/1"
# The pins, each key with the placeholder of the file it pins and the placeholder of its hash (`hpcpins.PLACEHOLDERS`,
# `hpcpins.OWN`): a pin is in the record when the template names either.
PINS = {
    "sandbox_sif_sha256": ("hpc_agent_sif", "hpc_agent_sif_sha"),
    "egress_shim_sha256": ("hpc_egress_shim", "hpc_egress_shim_sha"),
    "egress_collector_sha256": ("hpc_egress_collector", "hpc_egress_collector_sha"),
    "oneclient_bundle_sha256": ("hpc_oneclient_bundle", "hpc_oneclient_bundle_sha"),
}
KEYS = ("version", "template_sha256", "platform_fingerprint_sha256", *PINS)
HEX_DIGITS = frozenset("0123456789abcdef")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_sha256_of(platform: Path) -> str:
    """The SHA-256 of the canonical fingerprint of the rendered platform file, as the core hashes one (the same
    function, over `load_platform(...).fingerprint()`, which a campaign's own load of the file gives)."""
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore_onedata.config import load_platform

    return fingerprint_sha256(load_platform(platform).fingerprint())


def build(template: str, values: Mapping[str, str], platform: Path) -> Dict[str, str]:
    """The record of the platform file just rendered at `platform` from `template` with `values` (what `write_facts`
    filled: the pins `hpcpins.values` gave). Raises RuntimeError naming a pin whose value is no SHA-256."""
    record = {
        "version": VERSION,
        "template_sha256": sha256_text(template),
        "platform_fingerprint_sha256": fingerprint_sha256_of(platform),
    }
    for key, (file_name, pin_name) in PINS.items():
        if f"@{file_name}@" not in template and f"@{pin_name}@" not in template:
            continue
        value = values.get(pin_name)
        if not isinstance(value, str) or len(value) != 64 or not set(value) <= HEX_DIGITS:
            raise RuntimeError(f"{pin_name}: the template names it and its value is no SHA-256: no render identity")
        record[key] = value
    return record


def canonical(record: Mapping[str, str]) -> str:
    """The record's text: canonical JSON, sorted keys, no spaces, one newline."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def write(root: Path, record: Mapping[str, str]) -> Path:
    """Write the record into the federation's root, mode 0600 and never through a link; its path."""
    path = root / FILE
    write_private(path, canonical(record))
    return path

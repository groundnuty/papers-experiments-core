"""The agent's Apptainer sandbox in a platform template (M5): what `up` fills into its `[sandbox]` from `site.env`, and
the check of every pinned file before the cluster is touched.

A template that declares the Apptainer sandbox names its site values as placeholders (`PLACEHOLDERS`): the agent's SIF
and the egress shim built for it, and oneclient's bundle for a mount level (`oneclient_bundle.py`), each with the
SHA-256 the site declares, and the sandbox's work root on the agent's node's tmpfs. A pin is a declaration, not a
measurement: `check` reads each declared file (on the filesystem the login node sees) and refuses `up` when one is
missing or its SHA-256 is not the one declared, naming the variable and never the path; the rendered file then carries
the declared SHA-256s, which the sandbox checks at every start (O13) and the campaign fingerprint holds
(`PlatformConfig.fingerprint`, `sandbox_backend`), never a path. The egress collector is the
bundle's own, so `values` hashes this checkout's copy.

The template's `[sandbox.apptainer]` also declares where the federation's providers keep their data, `provider_storage
= "@hpc_provider_storage@"` (O57): `values` fills the one word the site's settings give (`provider_storage`: the shared
filesystem when the site has a scratch root, `HPC_STORAGE_ROOT`, else the nodes' tmpfs, never the root's path), and
`check` refuses a template that declares the sandbox without it, since it would give a scratch federation a
fingerprint a tmpfs one has: where a provider stores its data shapes a run's I/O timing, so shards on the two can
never merge. The renderer reads the file it writes back (`hpcboot.check_provider_storage`).
"""

import hashlib
from pathlib import Path
from typing import Dict, Optional

from hpcsettings import Site

TMPFS, SHARED_FILESYSTEM = "tmpfs", "shared_filesystem"  # the platform layer's PROVIDER_STORAGE_KINDS, in this order
STORAGE_KIND = "hpc_provider_storage"  # the placeholder of `[sandbox.apptainer] provider_storage` (O57)
COLLECTOR = Path(__file__).resolve().parent / "bundle" / "egress-collector.py"
CHUNK = 1 << 20
# Each placeholder a template may name, the site variable that fills it and the Site field that holds it. A name holds
# no digit: the renderer's placeholders are lower-case letters and '_' (fedfacts.PLACEHOLDER), so "sha", not "sha256".
PLACEHOLDERS = {
    "hpc_agent_sif": ("HPC_AGENT_SIF", "agent_sif"),
    "hpc_agent_sif_sha": ("HPC_AGENT_SIF_SHA256", "agent_sif_sha256"),
    "hpc_egress_shim": ("HPC_EGRESS_SHIM", "egress_shim"),
    "hpc_egress_shim_sha": ("HPC_EGRESS_SHIM_SHA256", "egress_shim_sha256"),
    "hpc_oneclient_bundle": ("HPC_ONECLIENT_BUNDLE", "oneclient_bundle"),
    "hpc_oneclient_bundle_sha": ("HPC_ONECLIENT_BUNDLE_SHA256", "oneclient_bundle_sha256"),
    "hpc_agent_work_root": ("HPC_AGENT_WORK_ROOT", "agent_work_root"),
}
FILES = {  # file -> its pin
    "hpc_agent_sif": "hpc_agent_sif_sha",
    "hpc_egress_shim": "hpc_egress_shim_sha",
    "hpc_oneclient_bundle": "hpc_oneclient_bundle_sha",
}
OWN = ("hpc_egress_collector", "hpc_egress_collector_sha")  # the bundle's collector: filled from this checkout


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def named(template: str) -> list:
    """The site placeholders a template names, in `PLACEHOLDERS`' order, each with its pin when it names a file."""
    found = {name for name in PLACEHOLDERS if f"@{name}@" in template}
    found |= {FILES[name] for name in found if name in FILES}
    return [name for name in PLACEHOLDERS if name in found]


def provider_storage(site: Site) -> str:
    """Where the federation's providers keep their data, as the platform file declares it: the shared filesystem when
    the site binds their storage onto a scratch root (`HPC_STORAGE_ROOT`), else the nodes' tmpfs. The word, never the
    root."""
    return SHARED_FILESYSTEM if site.storage_root else TMPFS


def check(site: Site, template: Optional[str]) -> None:
    """Refuse a site that leaves a placeholder of the template unfilled, or whose declared file is missing or has
    another SHA-256 than the one declared; the message names the variable, never the path. A template that names the
    agent's sandbox and leaves out the providers' storage kind (`@hpc_provider_storage@`) is refused too (O57)."""
    wanted = named(template or "")
    if wanted and f"@{STORAGE_KIND}@" not in (template or ""):
        raise RuntimeError(
            "the platform template declares the agent's sandbox without the providers' storage kind: add "
            f'provider_storage = "@{STORAGE_KIND}@" to its [sandbox.apptainer] (a federation on scratch and one on '
            "tmpfs would otherwise share a campaign fingerprint)"
        )
    for name in wanted:
        variable, attribute = PLACEHOLDERS[name]
        if not getattr(site, attribute):
            raise RuntimeError(
                f"{variable} is not set: the platform template's [sandbox] names it (site.env, the agent's sandbox)"
            )
    for name, pin in FILES.items():
        if name not in wanted:
            continue
        variable, attribute = PLACEHOLDERS[name]
        try:
            measured = sha256_of(Path(getattr(site, attribute)))
        except OSError:
            raise RuntimeError(f"{variable}: the file it names is missing or cannot be read") from None
        if measured != getattr(site, PLACEHOLDERS[pin][1]):
            raise RuntimeError(f"{PLACEHOLDERS[pin][0]}: the file {variable} names does not have this SHA-256")


def values(site: Site) -> Dict[str, str]:
    """The placeholders' values: the site's, as declared, the providers' storage kind (one word), and the bundle's
    collector with its SHA-256."""
    found = {name: getattr(site, attribute) for name, (_, attribute) in PLACEHOLDERS.items()}
    return {**found, STORAGE_KIND: provider_storage(site), OWN[0]: str(COLLECTOR), OWN[1]: sha256_of(COLLECTOR)}

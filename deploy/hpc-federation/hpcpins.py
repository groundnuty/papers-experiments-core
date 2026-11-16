"""The site's values in a platform template: what `up` fills into the agent's Apptainer sandbox (M5) and the study's
own paths from `site.env`, and the check of every pinned file before the cluster is touched.

A template that declares the Apptainer sandbox names its site values as placeholders (`PLACEHOLDERS`): the agent's SIF
and the egress shim built for it, and oneclient's bundle for a mount level (`oneclient_bundle.py`), each with the
SHA-256 the site declares, and the sandbox's work root on the agent's node's tmpfs. A pin is a declaration, not a
measurement: `check` reads each declared file (on the filesystem the login node sees) and refuses `up` when one is
missing or its SHA-256 is not the one declared, naming the variable and never the path; the rendered file then carries
the declared SHA-256s, which the sandbox checks at every start (O13) and the campaign fingerprint holds
(`PlatformConfig.fingerprint`, `sandbox_backend`), never a path. The egress collector is the
bundle's own, so `values` hashes this checkout's copy.

`@study_artifacts@` (`HPC_STUDY_ARTIFACTS`) is the directory of the study's own artifacts on the nodes (its tool
server's venv, clone and manifests), so a study's template names its paths under it and a public replication package
carries no site path. It is a path, not a pin: a template or a space's `data_manifest` that names it needs the
variable, and an unset one is no value (the renderer then refuses the placeholder, rather than write an empty path).
"""

import hashlib
from pathlib import Path
from typing import Dict, Optional, Sequence

from hpcsettings import Site

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
    "study_artifacts": ("HPC_STUDY_ARTIFACTS", "study_artifacts"),
}
WHY = {  # what names a placeholder, where the default (the template's [sandbox]) is not it
    "study_artifacts": "a platform template or a space's data_manifest names @study_artifacts@ (site.env)",
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


def check(site: Site, template: Optional[str], also: Sequence[str] = ()) -> None:
    """Refuse a site that leaves a placeholder of the template (or of `also`, other text that names them: a space's
    declared `data_manifest`) unfilled, or whose declared file is missing or has another SHA-256 than the one declared;
    the message names the variable, never the path."""
    wanted = named("\n".join([template or "", *also]))
    for name in wanted:
        variable, attribute = PLACEHOLDERS[name]
        if not getattr(site, attribute):
            why = WHY.get(name, "the platform template's [sandbox] names it (site.env, the agent's sandbox)")
            raise RuntimeError(f"{variable} is not set: {why}")
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
    """The placeholders' values: the site's, as declared (one it does not declare is no value, so the renderer refuses a
    placeholder nothing filled), and the bundle's collector with its SHA-256."""
    found = {name: getattr(site, attr) for name, (_, attr) in PLACEHOLDERS.items() if getattr(site, attr)}
    return {**found, OWN[0]: str(COLLECTOR), OWN[1]: sha256_of(COLLECTOR)}

"""The site's values in a platform template: what `up` fills into the agent's Apptainer sandbox (M5) and the study's
own paths from `site.env`, and the check of every pinned file before the cluster is touched.

A template that declares the Apptainer sandbox names its site values as placeholders (`PLACEHOLDERS`): the agent's SIF
and the egress shim built for it, oneclient's bundle for a mount level (`oneclient_bundle.py`) and the 443 mapper
(`bundle/gateport.c`, built for the SIF) for a REST level, each with the SHA-256 the site declares, and the sandbox's
work root on the agent's node's tmpfs. A pin is a declaration, not a
measurement: `check` reads each declared file (on the filesystem the login node sees) and refuses `up` when one is
missing or its SHA-256 is not the one declared, naming the variable and never the path; the rendered file then carries
the declared SHA-256s, which the sandbox checks at every start (O13) and the campaign fingerprint holds
(`PlatformConfig.fingerprint`, `sandbox_backend`), never a path. The egress collector is the
bundle's own, so `values` hashes this checkout's copy.

`@study_artifacts@` (`HPC_STUDY_ARTIFACTS`) is the directory of the study's own artifacts on the nodes (its tool
server's venv, clone and manifests), so a study's template names its paths under it and a public replication package
carries no site path. It is a path, not a pin: a template or a space's `data_manifest` that names it needs the
variable, and an unset one is no value (the renderer then refuses the placeholder, rather than write an empty path).

The template's `[sandbox.apptainer]` also declares where the federation's providers keep their data, `provider_storage
= "@hpc_provider_storage@"` (O57): `values` fills the one word the site's settings give (`provider_storage`: the shared
filesystem when the site has a scratch root, `HPC_STORAGE_ROOT`, else the nodes' tmpfs, never the root's path), and
`check` refuses a template that declares the sandbox without it, or with it only in a comment, since it would give a
scratch federation the fingerprint a tmpfs one has: where a provider stores its data shapes a run's I/O timing, so
shards on the two can never merge. It reads the template as TOML, so a comment, another table or a hard-coded other word
declares nothing; the renderer reads the file it writes back for the same (`hpcboot.check_provider_storage`).
"""

import hashlib
import re
from pathlib import Path
from typing import Dict, Optional, Sequence

import tomllib
from hpcsettings import Site

TMPFS, SHARED_FILESYSTEM = "tmpfs", "shared_filesystem"  # the platform layer's PROVIDER_STORAGE_KINDS, in this order
STORAGE_KIND = "hpc_provider_storage"  # the placeholder of `[sandbox.apptainer] provider_storage` (O57)
ANY_PLACEHOLDER = re.compile(r"@[A-Za-z0-9_.-]+@")  # a name of `fedfacts.placeholders` may hold dots and dashes
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
    "hpc_gateport": ("HPC_GATEPORT", "gateport"),
    "hpc_gateport_sha": ("HPC_GATEPORT_SHA256", "gateport_sha256"),
    "hpc_agent_work_root": ("HPC_AGENT_WORK_ROOT", "agent_work_root"),
    "study_artifacts": ("HPC_STUDY_ARTIFACTS", "study_artifacts"),
}
WHY = {  # what names a placeholder, where the default (the template's [sandbox]) is not it
    "study_artifacts": (
        "a platform template or a space's data_manifest names @study_artifacts@, the study's own files (site.env)"
    ),
}
FILES = {  # file -> its pin
    "hpc_agent_sif": "hpc_agent_sif_sha",
    "hpc_egress_shim": "hpc_egress_shim_sha",
    "hpc_oneclient_bundle": "hpc_oneclient_bundle_sha",
    "hpc_gateport": "hpc_gateport_sha",
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


def check_declared_kind(document: str, kind: Optional[str]) -> None:
    """Refuse a platform file, rendered or a template filled in, whose `[sandbox.apptainer]` does not declare
    `provider_storage` as `kind` (O57): the key is in the campaign fingerprint, so a file that leaves it out, names it
    in a comment or another table, or hard-codes another word would let shards on tmpfs and on scratch merge. It reads
    the document as TOML, so every spelling of a declaration counts (a literal string, a dotted key, an inline table)
    and none that is not one. A document with no Apptainer sandbox has no run to fingerprint and is not asked. Names
    the key, never a path."""
    table = tomllib.loads(document).get("sandbox", {}).get("apptainer")
    if table is None:
        return
    if kind is None or table.get("provider_storage") != kind:
        raise RuntimeError(
            "the platform file's [sandbox.apptainer] must declare provider_storage as the site has it "
            f'("{kind}"): add provider_storage = "@{STORAGE_KIND}@" to the template (a federation on scratch and one '
            "on tmpfs are two experiments)"
        )


def filled_in(template: str, kind: str) -> str:
    """A template with its storage placeholder as the site's kind and every other placeholder as `0`, which a TOML
    value of the renderer's templates takes as it is (an integer where the placeholder stands bare, a character in a
    string where it stands in one): the template as a document, for `check_declared_kind`."""
    word = f"@{STORAGE_KIND}@"
    return ANY_PLACEHOLDER.sub(lambda found: kind if found.group(0) == word else "0", template)


def check_storage_kind(site: Site, template: str) -> None:
    """The refusal before anything is submitted (O57): the template, read as TOML with its placeholders filled in
    (`filled_in`), declares the providers' storage kind as the site has it, or declares no Apptainer sandbox. A template
    that is no TOML once filled in (a placeholder in a key, say) is left to the renderer, which reads the file it writes
    back for the same (`hpcboot.check_provider_storage`) before it writes it."""
    kind = provider_storage(site)
    try:
        tomllib.loads(filled_in(template, kind))
    except tomllib.TOMLDecodeError:
        return
    check_declared_kind(filled_in(template, kind), kind)


def check(site: Site, template: Optional[str], also: Sequence[str] = ()) -> None:
    """Refuse a site that leaves a placeholder of the template (or of `also`, other text that names them: a space's
    declared `data_manifest`) unfilled, or whose declared file is missing or has another SHA-256 than the one declared;
    the message names the variable, never the path. A template that names the agent's sandbox and does not declare the
    providers' storage kind (`check_storage_kind`) is refused too (O57)."""
    wanted = named("\n".join([template or "", *also]))
    check_storage_kind(site, template or "")
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
    placeholder nothing filled), the providers' storage kind (one word), and the bundle's collector with its SHA-256."""
    found = {name: getattr(site, attr) for name, (_, attr) in PLACEHOLDERS.items() if getattr(site, attr)}
    return {**found, STORAGE_KIND: provider_storage(site), OWN[0]: str(COLLECTOR), OWN[1]: sha256_of(COLLECTOR)}

"""A Compose federation's render identity (O74): `render-identity.json` in its root, as an HPC federation's `up`
writes it (`deploy/hpc-federation/hpcidentity.py`, whose `build`, `tree_sha256` and `write` this uses as they are).

The record holds `version`, `template_sha256` (the layout's platform template, as `up` read it) and
`platform_fingerprint_sha256` (the rendered `platform.toml`'s fingerprint, hashed as the core hashes one), and, when
`EXPCORE_FED_STUDY_ARTIFACTS` names the study's directory, `study_artifacts_tree_sha256`, the tree digest HPC takes of
`HPC_STUDY_ARTIFACTS`. What differs from HPC: no pin is ever in it, since a Compose federation has no agent SIF,
egress shim or collector, oneclient bundle or 443 mapper to pin (the sandbox's images are pinned by id in `[sandbox]`,
which the fingerprint already holds). A template that names an HPC pin's placeholder is no Compose template, and
`build` refuses it. Two instances of one layout (`fedspec.instance`) on one host's shared CA have one record: the
fingerprint holds no name, port, address or id, and the CA's digest is the shared one's.

The record is written only for a layout with a platform template, on the host the federation runs on, where a
Compose site's controller reads it (`composesite.py observe`).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

HPC_DIR = Path(__file__).resolve().parents[1] / "hpc-federation"


def hpcidentity() -> ModuleType:
    """`hpcidentity.py` by its path, without putting the HPC directory on `sys.path` (its `fedfacts` import is this
    directory's own module, already importable here)."""
    loaded = sys.modules.get("hpcidentity")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location("hpcidentity", HPC_DIR / "hpcidentity.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def record_of(template: Path, platform: Path, study_artifacts: str | None) -> dict[str, str]:
    """The record of `platform`, rendered from `template`; with the study's artifacts directory, its tree digest."""
    module = hpcidentity()
    values = {}
    if study_artifacts:
        values[module.ARTIFACTS] = module.tree_sha256(Path(study_artifacts))
    return module.build(template.read_text(encoding="utf-8"), values, platform)


def write_render_identity(layout, study_artifacts: str | None) -> str:
    """Write the root's record after `up` rendered `platform.toml`; what was written, for up's summary."""
    template = layout.spec.platform_template
    if template is None or not layout.platform.is_file():
        return ""
    record = record_of(template, layout.platform, study_artifacts)
    hpcidentity().write(layout.root, record)
    return "; render-identity.json written"

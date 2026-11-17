# Provenance: new
"""The core's build hook: writes the core's identity into its wheel and its sdist (`expcore/_build_identity.json`).

The identity is the commit, the git tree, whether the checkout is dirty and a digest of the Python source, read the way
`expcore.provenance.core_identity` reads it at run time, so one commit gives one identity however it is installed. A
wheel built from an sdist takes the identity the sdist carries; a build with neither git nor that file records an
unknown, dirty identity. An editable install gets none: it reads its checkout live. The module is loaded by its path,
since nothing is installed yet when the hook runs.
"""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

IDENTITY_MODULE = "src/expcore/provenance/core_identity.py"


def _identity_module(root: Path):
    """The core's identity module, loaded from the tree being built without importing the package."""
    spec = importlib.util.spec_from_file_location("_expcore_build_identity", root / IDENTITY_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {IDENTITY_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while the class is made
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


class CoreIdentityHook(BuildHookInterface):
    """Force-include the build identity at `expcore/` in the wheel and at `src/expcore/` in the sdist."""

    def initialize(self, version, build_data):
        self._scratch = None
        if self.target_name == "wheel" and version == "editable":
            return
        if self.target_name not in ("wheel", "sdist"):
            return
        root = Path(self.root)
        module = _identity_module(root)
        self._scratch = Path(tempfile.mkdtemp(prefix="expcore-build-identity-"))
        written = self._scratch / module.BUILD_FILE
        written.write_bytes(module.build_bytes(module.build_identity(root)))
        place = "expcore" if self.target_name == "wheel" else "src/expcore"
        build_data["force_include"][str(written)] = f"{place}/{module.BUILD_FILE}"

    def finalize(self, version, build_data, artifact_path):
        if getattr(self, "_scratch", None) is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)

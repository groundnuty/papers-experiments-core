# Provenance: new
"""Which version a plugin's backend factory must declare, by who owns its code (C06).

The campaign fingerprint holds a backend factory's name and declared `version`, so a resume under another is refused.
For code outside the study that version is all the fingerprint knows, so it is held to the code's owner, found from
the file of the factory's module (the module of its class, for a callable instance):
- a file among the files the code identity covers (`[study] code`, the plugin file when it is listed there) is the
  study's own code: the identity covers it, and the version is taken as declared;
- otherwise the installed distribution that owns the file decides: the one whose RECORD lists it, or, for an editable
  install, the one whose checkout holds it (below a directory its `.pth` puts on the path, else its project). Not any
  distribution that shares the module's top-level name: a namespace package can have several. The declared version
  must equal the owner's as a version (`packaging`: `1.0+Build_1` is `1.0+build.1`); a version that does not parse
  must equal it as written. The refusal names both;
- a file nothing owns and the identity does not cover (a module found on the path, a module without a file) has
  nothing behind its version, and is refused, naming the module.
"""

import json
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath

from packaging.version import InvalidVersion, Version

from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import covered_files
from expcore.provenance.environment import editable_roots, installed
from expcore.study.config import StudyConfig
from expcore.study.plugin import StudyPlugin, factory_name


def _module_file(module: str) -> Path | None:
    found = getattr(sys.modules.get(module), "__file__", None)
    return Path(found).resolve() if isinstance(found, str) else None


def _covered(study: StudyConfig, file: Path) -> bool:
    """Whether a file is one the study's code identity covers."""
    root = study.root.resolve()
    try:
        relative = file.relative_to(root).as_posix()
    except ValueError:
        return False
    return relative in set(covered_files(root, study.scope))


def _editable(dist: metadata.Distribution) -> bool:
    try:
        data = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        return False
    info = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(info, dict) and info.get("editable") is True


def _owns(dist: metadata.Distribution, file: Path) -> bool:
    """Whether an installed distribution's code holds the file: listed in its RECORD, or in its editable checkout."""
    if _editable(dist):
        return any(root.resolve() in file.parents for root in editable_roots(dist))
    site = Path(str(dist.locate_file(""))).resolve()
    try:
        relative = PurePosixPath(file.relative_to(site).as_posix())
    except ValueError:
        return False
    return any(PurePosixPath(item.as_posix()) == relative for item in dist.files or [])


def _same_version(declared: str, owned: str) -> bool:
    try:
        return Version(declared) == Version(owned)
    except InvalidVersion:
        return declared == owned


def check_factory_version(plugin: StudyPlugin, study: StudyConfig) -> None:
    """Refuse a backend factory whose declared version nothing vouches for, as described above."""
    factory = plugin.backend_factory
    if factory is None:
        return
    module, qualname = factory_name(factory)
    where = f"{study.plugin_file}: the plugin's backend_factory {module}.{qualname}"
    file = _module_file(module)
    if file is not None and _covered(study, file):
        return  # the study's own code: the code identity covers it
    owners = [dist for dist in installed().values() if _owns(dist, file)] if file is not None else []
    if not owners:
        raise ConfigError(
            f"{where} comes from module {module}, which no installed distribution owns and the study's code identity "
            "does not cover: install it as a package, whose version the factory then declares, or list its file in "
            "[study] code"
        )
    found = sorted((str(dist.metadata["Name"]), str(dist.version)) for dist in owners)
    if not all(_same_version(str(factory.version), version) for _, version in found):
        raise ConfigError(
            f"{where} declares version {factory.version!r}, but its module belongs to "
            f"{', '.join(f'{name} {version}' for name, version in found)}: declare the installed version, so a change "
            "to the backend's code is a change to the campaign fingerprint"
        )


__all__ = ["check_factory_version"]

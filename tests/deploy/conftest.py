"""The deploy scripts of deploy/compose-federation, imported by their plain names as the scripts import one another,
and the layouts every check of a generated Compose file runs over."""

from pathlib import Path

import pytest
from federation_support import DEPLOY, EXAMPLE, MINIMAL, STORAGE_IMPORT, WITH_WRITABLE_IMPORT


@pytest.fixture(scope="session")
def fed():
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(DEPLOY))
        import fedboot
        import fedcompose
        import feddocker
        import federation
        import fedfacts
        import fedimport
        import fedlayout
        import fedpki
        import fedspec
        import make_import_tree

        yield type(
            "Fed",
            (),
            {
                "layout": fedlayout,
                "pki": fedpki,
                "cli": federation,
                "spec": fedspec,
                "compose": fedcompose,
                "docker": feddocker,
                "boot": fedboot,
                "facts": fedfacts,
                "importing": fedimport,
                "tree": make_import_tree,
            },
        )


@pytest.fixture(scope="session")
def minimal_layout(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("layout") / "minimal.toml"
    path.write_text(MINIMAL, encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def imports_layout(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("layout") / "imports.toml"
    path.write_text(WITH_WRITABLE_IMPORT, encoding="utf-8")
    return path


@pytest.fixture(params=["default", "five-providers", "minimal", "imports", "storage-import"])
def any_spec(request, fed, minimal_layout, imports_layout):
    """Every layout a check of the generated Compose file must hold for."""
    path = {
        "default": None,
        "five-providers": EXAMPLE,
        "minimal": minimal_layout,
        "imports": imports_layout,
        "storage-import": STORAGE_IMPORT,
    }
    return fed.spec.load_spec(path[request.param])

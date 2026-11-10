"""An anonymised package withholds the provenance headers of the code it vendors, touching nothing but comments."""

import ast
import io
import zipfile

import pytest
from package_support import wheel_of

from expcore.package import withhold as withhold_module
from expcore.package.spec import PackageError
from expcore.package.wheels import Wheel
from expcore.package.withhold import NEUTRAL, withhold_headers

IMPORTED = (
    "# Provenance:\n"
    f"#   source: quillon-old-study @ {'ab12' * 10}, old/tool.py, blob {'cd34' * 10}\n"
    f"#   import: sanitised with study-token map {'ef56' * 16}\n"
    "#   merged: [S1] varek-pilot @ 1a2b3c4, pilot/run.py:10-20\n"
    '"""A tool."""\n\nVALUE = 1  # a comment that stays\n'
)
NEW = '# Provenance: new\n#   merged: varek-pilot @ 5d6e7f8, pilot/x.py:1\n"""Another."""\n\nX = 2\n'


def wheel(files: dict[str, bytes]) -> Wheel:
    name, data = wheel_of("toy-platform", "2.0.0", files)
    return Wheel("toy-platform", "2.0.0", name, data, from_index=False)


def members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_k11_every_provenance_header_is_replaced_by_one_neutral_line_and_what_it_named_is_collected():
    """K11: an import header and a new module's merged lines alike become one neutral comment; the code is the same
    syntax tree, the repositories and the ids the headers named are collected for the final check, and RECORD is
    written anew for the changed members."""
    record = (
        "toy_platform/tool.py,sha256=old,1\ntoy_platform/other.py,sha256=old,1\ntoy_platform-2.0.0.dist-info/RECORD,,\n"
    )
    original = wheel(
        {
            "toy_platform/tool.py": IMPORTED.encode(),
            "toy_platform/other.py": NEW.encode(),
            "toy_platform/plain.py": b"Y = 3\n",
            "toy_platform-2.0.0.dist-info/RECORD": record.encode(),
        }
    )
    [rewritten], withheld = withhold_headers([original])
    found = members(rewritten.data)
    for name, before in (("toy_platform/tool.py", IMPORTED), ("toy_platform/other.py", NEW)):
        text = found[name].decode()
        assert text.startswith(NEUTRAL)
        assert "Provenance" not in text
        assert ast.dump(ast.parse(text)) == ast.dump(ast.parse(before))
    assert "# a comment that stays" in found["toy_platform/tool.py"].decode()
    assert found["toy_platform/plain.py"] == b"Y = 3\n"
    assert withheld.repositories == {"quillon-old-study", "varek-pilot"}
    assert {"ab12" * 10, "cd34" * 10, "ef56" * 16, "1a2b3c4", "5d6e7f8"} <= withheld.ids
    assert "sha256=old" not in found["toy_platform-2.0.0.dist-info/RECORD"].decode()
    assert (
        withhold_headers([wheel({"toy_platform/plain.py": b"Y = 3\n"})])[0][0].data
        == wheel({"toy_platform/plain.py": b"Y = 3\n"}).data
    )  # a wheel without a header is left as it was


def test_k11_a_rewrite_that_would_change_the_code_or_a_header_that_does_not_parse_is_refused(monkeypatch):
    """K11: the rewrite is proved to touch comments only, by the syntax tree; and a header whose names could not be
    collected is not withheld silently."""
    broken = "# Provenance:\n#   source: not a source line\nX = 1\n"
    with pytest.raises(PackageError, match=r"tool\.py: a provenance header that cannot be read"):
        withhold_headers([wheel({"toy_platform/tool.py": broken.encode()})])
    monkeypatch.setattr(withhold_module, "NEUTRAL", "VALUE = 2\n")
    with pytest.raises(PackageError, match=r"tool\.py: withholding its provenance header would change its code"):
        withhold_headers([wheel({"toy_platform/tool.py": IMPORTED.encode()})])

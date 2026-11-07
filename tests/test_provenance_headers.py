"""Provenance headers: every module under src/ has one, and the parser reads the documented forms."""

from pathlib import Path

import pytest

from expcore.provenance.headers import (
    HeaderError,
    blank_header,
    format_import_header,
    insert_header,
    read_header,
    split_lines,
    strip_header,
)

SRC = Path(__file__).resolve().parents[1] / "src"
BLOB = "a" * 40
MAP_SHA = "b" * 64
IMPORTED = (
    "# Provenance:\n"
    f"#   source: fake-study-repo @ 0123abcd, pkg/capping.py, blob {BLOB}\n"
    f"#   import: sanitised with study-token map {MAP_SHA}\n"
)


def test_every_module_under_src_has_a_header():
    modules = sorted(SRC.rglob("*.py"))
    assert modules
    missing = [str(p.relative_to(SRC)) for p in modules if read_header(p.read_text(encoding="utf-8")) is None]
    assert not missing, f"modules without a '# Provenance:' header: {missing}"


def test_modules_written_fresh_say_so_on_their_first_line():
    # A module that starts from earlier code is allowed to carry a source header instead.
    for path in sorted(SRC.rglob("*.py")):
        header = read_header(path.read_text(encoding="utf-8"))
        assert header is not None
        if header.new:
            assert path.read_text(encoding="utf-8").startswith("# Provenance: new\n"), path


def test_new_header():
    header = read_header('# Provenance: new\n"""Doc."""\n')
    assert header is not None
    assert header.new
    assert header.source_repo is None


def test_new_header_with_merged_and_changes_lines():
    text = "# Provenance: new\n#   merged: other-repo @ 46a5828d, tools.py:121-141\n#   changes: rewritten\nx = 1\n"
    header = read_header(text)
    assert header is not None
    assert header.new
    assert header.merged == ("other-repo @ 46a5828d, tools.py:121-141",)
    assert header.changes == "rewritten"


def test_source_header_fields():
    header = read_header(IMPORTED + "x = 1\n")
    assert header is not None
    assert not header.new
    assert (header.source_repo, header.commit, header.path, header.blob, header.map_sha256) == (
        "fake-study-repo",
        "0123abcd",
        "pkg/capping.py",
        BLOB,
        MAP_SHA,
    )
    assert header.merged == ()
    assert header.changes is None


def test_source_header_with_merged_and_changes():
    text = IMPORTED + "#   merged: second-repo @ 46a5828d, harness/tools.py:121-141\n#   changes: see git history\n"
    header = read_header(text)
    assert header is not None
    assert header.merged == ("second-repo @ 46a5828d, harness/tools.py:121-141",)
    assert header.changes == "see git history"


def test_header_after_shebang_and_encoding_lines():
    text = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n" + IMPORTED + "x = 1\n"
    header = read_header(text)
    assert header is not None
    assert header.source_repo == "fake-study-repo"


def test_no_header_gives_none():
    assert read_header("x = 1\n") is None
    assert read_header("") is None
    assert read_header("import os\n# Provenance: new\n") is None


@pytest.mark.parametrize(
    "text",
    [
        "# Provenance: old\n",
        "# Provenance:\n",
        "# Provenance:\n#   source: fake-study-repo @ 0123abcd, a.py, blob " + BLOB + "\n",
        IMPORTED.replace(" @ 0123abcd", ""),
        f"# Provenance:\n#   source: r @ 0123abcd, a.py, blob {BLOB}\n#   import: sanitised with study-token map x\n",
        f"# Provenance: new\n#   source: r @ 0123abcd, a.py, blob {BLOB}\n",
    ],
)
def test_malformed_headers_are_refused(text):
    with pytest.raises(HeaderError):
        read_header(text)


def test_a_comment_after_the_header_is_not_part_of_it():
    text = "# Provenance: new\n# an ordinary comment\n"
    assert strip_header(text) == "# an ordinary comment\n"


def test_format_insert_and_strip_round_trip():
    body = '#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"""Doc."""\nx = 1\n'
    header = format_import_header("fake-study-repo", "0123abcd", "pkg/capping.py", BLOB, MAP_SHA)
    assert header == IMPORTED
    combined = insert_header(body, header)
    assert combined.startswith("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n# Provenance:\n")
    assert read_header(combined) is not None
    assert strip_header(combined) == body


def test_insert_header_into_a_file_without_preamble():
    assert insert_header("x = 1\n", IMPORTED) == IMPORTED + "x = 1\n"


def test_blank_header_keeps_every_other_line_number():
    text = "#!/bin/sh\n" + IMPORTED + "x = 1\n"
    blanked = blank_header(text)
    assert blanked.count("\n") == text.count("\n")
    assert blanked.endswith("x = 1\n")
    assert "Provenance" not in blanked


def test_split_lines_splits_on_newline_only():
    assert split_lines("a\r\nb\x0cc\nd") == ["a\r\n", "b\x0cc\n", "d"]

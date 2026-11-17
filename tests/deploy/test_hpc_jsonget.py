"""jsonget.py of the HPC bundle, the JSON reader its scripts use for an id, a domain or a token (O58).

A JSON null is no value: it must never come out as the word `null`, which a caller would compare, record or deliver
as if it were one (live: a restart's identity check compared the word with the recorded domain). Run with this host's
interpreter; the bundle runs it as python3.11 -I.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

JSONGET = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle" / "jsonget.py"
MISSING, NULL = 2, 3  # the exit statuses: a field that is not there or a file that is not JSON; a field that is null


def jsonget(tmp_path, doc, *args):
    path = tmp_path / "doc.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(JSONGET), str(path), *args], capture_output=True, text=True, check=False
    )


def test_o58_a_null_field_prints_nothing_and_exits_with_its_own_status(tmp_path):
    """O58: no default, a null field: nothing on stdout (never the word null), status 3, stderr names the field."""
    done = jsonget(tmp_path, {"domain": None}, "domain")
    assert done.stdout == ""
    assert done.returncode == NULL
    assert "domain is null" in done.stderr


def test_o58_a_null_status_is_not_the_missing_status(tmp_path):
    """O58: a caller can tell a null field (3) from one that is not there (2): a missing field never reads as null."""
    assert jsonget(tmp_path, {"domain": None}, "domain").returncode == NULL
    assert jsonget(tmp_path, {"other": 1}, "domain").returncode == MISSING


def test_o58_a_null_field_with_a_default_gives_the_default(tmp_path):
    """O58: a null is no value, as a missing field is: the default stands in, and the word null is never printed."""
    done = jsonget(tmp_path, {"domain": None}, "domain", "--default", "")
    assert (done.returncode, done.stdout) == (0, "\n")
    done = jsonget(tmp_path, {"domain": None}, "domain", "--default", "fallback.example.invalid")
    assert (done.returncode, done.stdout) == (0, "fallback.example.invalid\n")


def test_o58_a_null_inside_a_list_or_under_a_null_is_no_value_either(tmp_path):
    """O58: ids.0 null (a list entry), and a path through a null."""
    assert jsonget(tmp_path, {"ids": [None]}, "ids.0").returncode == NULL
    assert jsonget(tmp_path, {"a": None}, "a.b").returncode == MISSING


@pytest.mark.parametrize(
    ("doc", "field", "printed"),
    [
        ({"domain": "provider-a.example.invalid"}, "domain", "provider-a.example.invalid\n"),
        ({"domain": ""}, "domain", "\n"),  # an empty string is a value: the caller decides what it means
        ({"domain": "null"}, "domain", "null\n"),  # the string null is a value too, only the JSON null is not
        ({"n": 0}, "n", "0\n"),
        ({"flag": False}, "flag", "false\n"),
        ({"ids": ["storage-1"]}, "ids.0", "storage-1\n"),
    ],
)
def test_o58_a_value_prints_as_it_always_did(tmp_path, doc, field, printed):
    """O58: strings as they are, other JSON as JSON, status 0: only the JSON null changed."""
    done = jsonget(tmp_path, doc, field)
    assert (done.returncode, done.stdout) == (0, printed)


def test_o58_a_missing_field_and_an_unreadable_file_still_exit_2(tmp_path):
    """O58: unchanged: a missing field, a file that is not JSON; and a default covers only the missing field."""
    assert jsonget(tmp_path, {"a": 1}, "b").returncode == MISSING
    assert jsonget(tmp_path, "not json", "b").returncode == MISSING
    assert jsonget(tmp_path, "not json", "b", "--default", "x").returncode == MISSING
    done = jsonget(tmp_path, {"a": 1}, "b", "--default", "x")
    assert (done.returncode, done.stdout) == (0, "x\n")

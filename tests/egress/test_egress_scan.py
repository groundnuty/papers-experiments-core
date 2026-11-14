"""The static scan of the agent's code cells: URLs off the platform, downloads and network calls."""

import pytest
from egress_logs import CODE_TOOL, record

from expcore.egress import code_cells, declaration, scan_cells

DECLARED = declaration([("127.0.0.1", 41234)], hosts=["provider-a.test"])
REST = declaration([("127.0.0.1", 41234)], hosts=["provider-a.test"], network_calls=True)


def hits(code: str, declared=DECLARED) -> list[tuple[str, str, bool]]:
    return [(h["kind"], h["what"], h["flag"]) for h in scan_cells([code], declared)["hits"]]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("data = fetch('https://pypi.org/simple/numpy/')", ("url", "pypi.org", True)),
        ("u = 'ftp://mirror.example.net/pub'", ("url", "mirror.example.net", True)),
        ("os.system('curl -s http://x')", ("download", "curl", True)),
        ("subprocess.run(['wget', u])", ("download", "wget", True)),
        ("subprocess.run(['pip', 'install', 'pandas'])", ("download", "pip install", True)),
        ("os.system('python -m pip install pandas')", ("download", "pip install", True)),
        ("!git clone https://example.org/r.git", ("download", "git clone", True)),
        ("os.system('conda install -y scipy')", ("download", "conda install", True)),
        ("import requests\nrequests.get(u)", ("network_call", "requests", True)),
        ("from urllib.request import urlopen", ("network_call", "urllib", True)),
        ("import http.client", ("network_call", "http.client", True)),
        ("import os, socket", ("network_call", "socket", True)),
    ],
)
def test_each_kind_of_hit_is_found_and_flags(code, expected):
    assert expected in hits(code)


def test_a_url_is_recorded_by_its_host_only_never_its_path():
    found = scan_cells(["get('https://pypi.org/simple/secret-path?q=1')"], DECLARED)["hits"]
    assert [h["what"] for h in found] == ["pypi.org"]
    assert "secret-path" not in repr(found)


@pytest.mark.parametrize(
    "code",
    [
        "open('https://provider-a.test/api/v3/oneprovider/data/x/content')",
        "url = 'https://127.0.0.1:41234/x'",
        "url = 'http://localhost:8080/'",
        "import socketserver\nimport pandas as pd\nprint(pd.__version__)",
        "# the word 'curly' and 'pipeline install' are not commands",
    ],
)
def test_the_platforms_own_urls_and_ordinary_code_are_no_hit(code):
    assert hits(code) == []


def test_a_rest_data_path_keeps_network_calls_as_evidence_without_a_flag():
    """The agent's own HTTP to the gate is the REST data path's: recorded, not flagged; a URL elsewhere still is."""
    assert hits("import requests", REST) == [("network_call", "requests", False)]
    assert scan_cells(["import requests"], REST)["flagged"] is False
    assert scan_cells(["import requests\nrequests.get('https://pypi.org/')"], REST)["flagged"] is True


def test_a_cell_without_a_hit_does_not_flag_the_run():
    assert scan_cells(["print(1 + 1)"], DECLARED) == {
        "version": 1,
        "cells": 1,
        "hits": [],
        "hits_cut": False,
        "flagged": False,
    }


def test_each_hit_names_its_cell():
    found = scan_cells(["print(1)", "import socket"], DECLARED)["hits"]
    assert [h["cell"] for h in found] == [1]


def test_the_code_cells_are_the_code_tools_calls_and_no_other_tools():
    """A cell is the `code` argument of a tool whose input schema has a string `code` property."""
    other = {"name": "read_file", "description": "", "input_schema": {"properties": {"path": {"type": "string"}}}}
    run = record(cells=["import socket"])
    run.tools = [CODE_TOOL, other]
    run.tool_calls.append({"name": "read_file", "arguments": {"path": "https://pypi.org/"}, "status": "ok"})
    assert code_cells(run) == ["import socket"]


def test_more_hits_than_the_cap_are_cut_and_say_so(monkeypatch):
    from expcore.egress import scan as scan_module

    monkeypatch.setattr(scan_module, "MAX_HITS", 1)
    done = scan_cells(["import socket\nimport requests"], DECLARED)
    assert (len(done["hits"]), done["hits_cut"], done["flagged"]) == (1, True, True)

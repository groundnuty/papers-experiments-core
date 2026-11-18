"""A node whose `hostname -f` gives one label (no dot) is named `<hostname -s>.<HPC_NODE_DOMAIN>` by every step that
names the node: `bundle/nodefacts.py` (the facts step, which also reads the address) and `lib.sh`'s node_fqdn (the
configure and finish steps) make the same name by the same rule. A dotted name is used as it is, with the setting or
without; a one-label one with no setting is refused, naming the setting. Every name here is invented: `example`
domains, and the documentation range for addresses."""

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hpcop_node_tests", HERE / "hpcnode.py")
node = sys.modules.get("hpcop_node_tests") or importlib.util.module_from_spec(spec)
sys.modules["hpcop_node_tests"] = node
spec.loader.exec_module(node)

# `hostname -f` and `hostname -s` as the node gives them, the domain the site sets (None: none), and what a step makes
# of them: the FQDN (None: refused) and the name the address is read by.
DOTTED, ONE_LABEL = "n01.corp.example", "n01"
CASES = [
    pytest.param(DOTTED, "n01", None, DOTTED, DOTTED, id="dotted-without-the-setting"),
    pytest.param(DOTTED, "n01", "nodes.example", DOTTED, DOTTED, id="dotted-with-the-setting-is-unchanged"),
    pytest.param(ONE_LABEL, "n01", "nodes.example", "n01.nodes.example", "n01", id="one-label-with-the-setting"),
    pytest.param(ONE_LABEL, "n01", None, None, None, id="one-label-without-the-setting"),
    pytest.param("n01-", "n01-", "nodes.example", None, None, id="one-label-short-name-that-is-no-dns-label"),
]
ADDRESS = "192.0.2.7"
FAKE_HOSTNAME = """#!/bin/bash
case ${1:-} in -f) echo "$FAKE_HOSTNAME_F" ;; -s) echo "$FAKE_HOSTNAME_S" ;; *) exit 2 ;; esac
"""


@pytest.fixture
def fake_hostname(tmp_path):
    """A `hostname` that says what FAKE_HOSTNAME_F and FAKE_HOSTNAME_S hold, in a directory to put first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hostname").write_text(FAKE_HOSTNAME, encoding="utf-8")
    (bin_dir / "hostname").chmod(0o755)
    return bin_dir


@pytest.fixture(scope="module")
def nodefacts():
    loaded = importlib.util.spec_from_file_location("nodefacts_names_under_test", node.BUNDLE / "nodefacts.py")
    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    return module


def the_node(monkeypatch, fake_hostname, full, short, domain):
    """This process as the node: the fake `hostname` first on PATH, saying `full` and `short`, and HPC_NODE_DOMAIN."""
    monkeypatch.setenv("PATH", f"{fake_hostname}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HOSTNAME_F", full)
    monkeypatch.setenv("FAKE_HOSTNAME_S", short)
    if domain is None:
        monkeypatch.delenv("HPC_NODE_DOMAIN", raising=False)
    else:
        monkeypatch.setenv("HPC_NODE_DOMAIN", domain)


@pytest.mark.parametrize(("full", "short", "domain", "fqdn", "lookup"), CASES)
def test_the_facts_step_names_a_one_label_node_from_its_short_name_and_the_domain(
    nodefacts, monkeypatch, capsys, fake_hostname, full, short, domain, fqdn, lookup
):
    """The facts step prints the FQDN and the address that the rule gives, and reads the address by the short name
    (the made-up FQDN resolves nowhere); with no domain for a one-label name it refuses, naming HPC_NODE_DOMAIN."""
    the_node(monkeypatch, fake_hostname, full, short, domain)
    asked = []

    def resolve(name, port, family, kind):
        asked.append(name)
        if name != lookup:
            raise socket.gaierror(socket.EAI_NONAME, "no such name")
        return [(socket.AF_INET, kind, 6, "", (ADDRESS, 0))]

    monkeypatch.setattr(nodefacts.socket, "getaddrinfo", resolve)
    code = nodefacts.main()
    out, err = capsys.readouterr()
    if fqdn is None:
        assert code == 1
        assert "HPC_NODE_DOMAIN" in err
        assert out == ""  # nothing printed
        assert asked == []  # and no address read
        return
    assert code == 0, err
    facts = json.loads(out)
    assert (facts["fqdn"], facts["address"]) == (fqdn, ADDRESS)
    assert asked == [lookup]


@pytest.mark.parametrize(("full", "short", "domain", "fqdn", "lookup"), CASES)
def test_node_fqdn_of_the_bundle_gives_the_name_the_facts_step_gives(
    nodefacts, monkeypatch, tmp_path, fake_hostname, full, short, domain, fqdn, lookup
):
    """lib.sh's node_fqdn, which the configure and finish steps run, makes the same name as the facts step in each of
    the cases, and refuses the one-label name with no domain, naming HPC_NODE_DOMAIN."""
    holder = tmp_path / "tmpfs"
    holder.mkdir(mode=0o700)
    extra = {} if domain is None else {"HPC_NODE_DOMAIN": domain}
    path = f"{fake_hostname}{os.pathsep}{os.environ['PATH']}"
    env = node.node_env(holder, PATH=path, FAKE_HOSTNAME_F=full, FAKE_HOSTNAME_S=short, **extra)
    if domain is None:
        env.pop("HPC_NODE_DOMAIN", None)
    done = node.lib("node_fqdn", env=env)
    if fqdn is None:
        assert done.returncode != 0
        assert "HPC_NODE_DOMAIN" in done.stderr
        return
    assert done.returncode == 0, done.stderr
    the_node(monkeypatch, fake_hostname, full, short, domain)
    assert done.stdout.strip() == fqdn == nodefacts.node_name()[0]


def test_the_facts_step_refuses_a_short_name_the_resolver_reads_as_an_address(
    nodefacts, monkeypatch, capsys, fake_hostname
):
    """A node named 42 would have its address read by the name 42, which glibc takes for the literal 0.0.0.42: one
    address, and the wrong one. The facts step refuses, recording none."""
    the_node(monkeypatch, fake_hostname, "42", "42", "nodes.example")
    monkeypatch.setattr(
        nodefacts.socket,
        "getaddrinfo",
        lambda name, port, family, kind: [(socket.AF_INET, kind, 6, "", ("0.0.0.42", 0))],
    )
    assert nodefacts.main() == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "IPv4 address" in err

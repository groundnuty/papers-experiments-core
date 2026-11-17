"""O77: a federation whose services' names resolve nowhere is reached through an address map: the URL, the handshake,
the certificate check and `Host` carry the declared name, and only the connection goes to the node's address.

Names are invented (`.test` resolves nowhere, the documentation ranges are no site's); a real TLS service on the
loopback stands in for a node, with a certificate that names the declared name alone.
"""

import json
import os
import re

import pytest
from mcp_node_names import server_reads
from tls_peer import TlsPeer

from expcore.config.loading import ConfigError
from expcore_onedata import addresses
from expcore_onedata.addresses import AddressMap, AddressMapError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform

ZONE, ALPHA, BETA = "zone.layout.test", "alpha.layout.test", "beta.layout.test"
NODE_A, NODE_B = "node-a.cluster.example.org", "node-b.cluster.example.org"
ADDRESS_A, ADDRESS_B, V6 = "192.0.2.10", "192.0.2.11", "2001:db8::2"
DOCUMENT = {
    "format": addresses.FORMAT,
    "hosts": {
        ZONE: {"address": ADDRESS_A, "nodes": [NODE_A, "node-a"]},
        ALPHA: {"address": ADDRESS_B, "nodes": [NODE_B, "node-b"]},
        BETA: {"address": V6},
    },
}


@pytest.fixture(autouse=True)
def _admin_credential(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")


def _write(path, document, mode=0o600):
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(mode)
    return path


def _document(**hosts):
    return {"format": addresses.FORMAT, "hosts": hosts}


def test_o77_a_private_map_resolves_each_declared_name_and_leaves_other_hosts_alone(tmp_path):
    """O77: the map answers the address of a declared name, in any case, and a host it does not hold is its own."""
    found = AddressMap.load(_write(tmp_path / "map.json", DOCUMENT))
    assert found.dial("ZONE.layout.test") == ADDRESS_A
    assert found.dial(BETA) == V6
    assert found.dial("127.0.0.1") == "127.0.0.1"
    assert found.address_of("elsewhere.test") is None
    assert "192.0.2" not in repr(found)  # a repr shows the count and no name or address


def test_o77_the_map_file_is_written_private_and_read_back(tmp_path):
    """O77: `write` makes a 0600 file, replaces the one a failed `up` left, and `load` reads it back."""
    path = tmp_path / "map.json"
    written = AddressMap.from_document(DOCUMENT)
    written.write(path)
    path.chmod(0o600)
    written.write(path)  # again: replaced
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert not [p for p in tmp_path.iterdir() if p.name != "map.json"]
    assert AddressMap.load(path).to_document() == written.to_document()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_o77_a_map_other_users_can_read_is_refused(tmp_path, mode):
    """O77: the file holds the nodes' addresses and is mode 0600 or it is refused."""
    with pytest.raises(AddressMapError, match="private"):
        AddressMap.load(_write(tmp_path / "map.json", DOCUMENT, mode))


def test_o77_a_link_is_never_the_map(tmp_path):
    """O77: a symbolic link, even to a good private file, is no map: it could be swapped for another."""
    real = _write(tmp_path / "real.json", DOCUMENT)
    (tmp_path / "map.json").symlink_to(real)
    with pytest.raises(AddressMapError, match="never a link"):
        AddressMap.load(tmp_path / "map.json")


@pytest.mark.parametrize(
    ("document", "why"),
    [
        ({"format": "other/1", "hosts": {}}, "not an"),
        ({"format": addresses.FORMAT}, "not an"),
        (_document(), "at least one"),
        (_document(**{"nodot": {"address": ADDRESS_A}}), "at least two labels"),
        (_document(**{ZONE: {"address": "node-a"}}), "address"),
        (_document(**{ZONE: {"address": "0.0.0.0"}}), "address"),
        (_document(**{ZONE: {"address": ADDRESS_A, "port": 8443}}), "only address and nodes"),
        (_document(**{ADDRESS_A: {"address": ADDRESS_B}}), "not an address"),
        (_document(**{ZONE: {"address": ADDRESS_A}, ALPHA: {"address": ADDRESS_A}}), "one declared name only"),
        (
            _document(
                **{ZONE: {"address": ADDRESS_A, "nodes": [NODE_A]}, ALPHA: {"address": ADDRESS_B, "nodes": [NODE_A]}}
            ),
            "one declared name only",
        ),
        (_document(**{ZONE: {"address": ADDRESS_A}, ALPHA: {"address": ADDRESS_B, "nodes": [ZONE]}}), "own name"),
    ],
)
def test_o77_a_map_that_cannot_be_used_is_refused_without_a_name_or_an_address(tmp_path, document, why):
    """O77: a refusal names what is wrong with the file, never a name or an address of it, and a host is one declared
    name's: two names for one address, or one node name, are refused."""
    with pytest.raises(AddressMapError, match=why) as refused:
        AddressMap.load(_write(tmp_path / "map.json", document))
    assert not re.search(r"layout\.test|192\.0\.2|cluster\.example", str(refused.value))


def test_o77_the_node_names_map_every_real_name_and_address_with_and_without_a_port_to_its_declared_name():
    """O77: `ONEDATA_MCP_NODE_NAMES` holds each node's names and addresses, bare and with the port, an IPv6 address
    bare and in brackets, each under its declared name, and a declared name under itself so a port after it goes."""
    found = AddressMap.from_document(DOCUMENT).node_names(ports=(8443, 443))
    assert found[ADDRESS_A] == found[f"{ADDRESS_A}:8443"] == found[f"{ADDRESS_A}:443"] == ZONE
    assert found[NODE_A] == found[f"{NODE_A}:8443"] == found["node-a"] == found["node-a:8443"] == ZONE
    assert found[NODE_B] == found[f"{ADDRESS_B}:8443"] == ALPHA
    assert found[V6] == found[f"[{V6}]:8443"] == BETA
    assert found[ZONE] == found[f"{ZONE}:8443"] == ZONE
    assert server_reads(found)  # valid by the server's own rules, and no host under two names
    assert AddressMap.from_document(DOCUMENT).node_names() == {  # no port asked: bare names alone
        k: v for k, v in found.items() if ":" not in k or k == V6
    }


def _platform(tmp_path, peer, *, with_map=True, extra=None, ca=True, hosts=None):
    """A platform whose zone is the peer's declared name, mapped to the loopback (or to `hosts`), trusting the peer's
    CA."""
    if with_map:
        _write(tmp_path / "map.json", _document(**(hosts or {peer.name: {"address": "127.0.0.1", "nodes": [NODE_A]}})))

    def edit(data):
        data["zone"]["url"] = peer.url
        if ca:
            data["tls"] = {"ca_file": str(peer.ca)}
        if with_map:
            data["addresses"] = {"file": "map.json"}
        if extra:
            extra(data)

    return load_platform(write_platform(tmp_path, edit))


def _scope(client):
    return client.query("/tokens/infer_access_token_scope", {"token": "tok-0123456789"})


def test_o77_the_client_connects_to_the_mapped_address_and_asks_tls_and_host_for_the_declared_name(tmp_path):
    """O77: the URL holds the declared name, which resolves nowhere, and the call is answered: the connection went to
    the mapped address, the handshake asked for the declared name, the certificate (which names it alone) verified, and
    the request's `Host` is the declared name with the port."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        client = OnezoneClient(_platform(tmp_path, peer), admin_token="admin-token-0123456789")
        try:
            assert _scope(client) == {}
        finally:
            client.close()
        assert peer.sni == [ZONE]
        assert peer.requests == [
            {
                "method": "POST",
                "path": "/api/v3/onezone/tokens/infer_access_token_scope",
                "host": f"{ZONE}:{peer.port}",
            }
        ]


def test_o77_without_the_map_a_declared_name_reaches_nothing(tmp_path):
    """O77: the same platform with no `[addresses]` cannot connect: it is the map that dials the name."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        client = OnezoneClient(_platform(tmp_path, peer, with_map=False), admin_token="admin-token-0123456789")
        try:
            with pytest.raises(PlatformError):
                _scope(client)
        finally:
            client.close()
        assert peer.requests == []


def test_o77_the_certificate_is_still_checked_against_the_name_in_the_url(tmp_path):
    """O77: the map moves the connection, not the identity: a name the certificate does not hold is refused even
    when the map sends it to the same node."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        other = peer.url.replace(ZONE, "other.layout.test")

        def edit(data):
            data["zone"]["url"] = other

        config = _platform(tmp_path, peer, extra=edit, hosts={"other.layout.test": {"address": "127.0.0.1"}})
        client = OnezoneClient(config, admin_token="admin-token-0123456789")
        try:
            with pytest.raises(PlatformError):
                _scope(client)
        finally:
            client.close()
        assert peer.requests == []


def test_o77_a_failed_call_names_neither_the_address_nor_the_name(tmp_path):
    """O77: the error of a call to an address that refuses holds no address, no declared name and no port."""
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        config = _platform(tmp_path, peer)
    client = OnezoneClient(config, admin_token="admin-token-0123456789")  # the peer is gone: nothing listens
    try:
        with pytest.raises(PlatformError) as failed:
            _scope(client)
    finally:
        client.close()
    text = str(failed.value) + repr(failed.value.__cause__) + repr(failed.value.__context__)
    assert not re.search(rf"127\.0\.0\.1|layout\.test|{peer.port}", text)


def test_o77_the_platform_loads_the_map_and_never_shows_it(tmp_path):
    """O77: `[addresses]` loads the map beside the platform file, a platform without it has none, and neither the
    fingerprint nor the repr of the platform holds a name or an address: the addresses change at every `up`."""
    (tmp_path / "bare").mkdir()
    with TlsPeer(tmp_path / "pki", ZONE) as peer:
        mapped = _platform(tmp_path, peer)
        bare = _platform(tmp_path / "bare", peer, with_map=False)
    assert mapped.addresses is not None
    assert mapped.addresses.dial(ZONE) == "127.0.0.1"
    assert bare.addresses is None
    assert mapped.fingerprint() == bare.fingerprint()
    assert "127.0.0.1" not in repr(mapped)


@pytest.mark.parametrize(
    ("edit", "why"),
    [
        (lambda d: d.update(addresses={"file": "map.json", "extra": 1}), "extra"),
        (lambda d: d.update(addresses={"file": "absent.json"}), "file"),
    ],
)
def test_o77_a_platform_whose_map_cannot_be_used_is_refused_naming_the_key(tmp_path, edit, why):
    """O77: the platform's refusal names the `[addresses]` key and never the file's path or its content."""
    _write(tmp_path / "map.json", DOCUMENT)
    with pytest.raises(ConfigError, match=why) as refused:
        load_platform(write_platform(tmp_path, edit))
    assert not re.search(r"layout\.test|192\.0\.2|map\.json|absent\.json", str(refused.value))

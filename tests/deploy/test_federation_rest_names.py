"""O77: the bootstrap's REST client reaches a service by its declared name through the address map: the URL, the
handshake, the certificate check and `Host` carry the name, and the connection goes to the mapped address."""

import re

import pytest
from federation_support import DEPLOY
from tls_peer import TlsPeer

from expcore_onedata.addresses import build

NAME = "zone.layout.test"
CONFIGURATION = "/api/v3/onezone/configuration"


@pytest.fixture
def rest():
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(DEPLOY))
        import fedrest

        yield fedrest


def test_o77_the_bootstrap_dials_the_mapped_address_for_the_declared_name_in_the_url(rest, tmp_path):
    """O77: the call is answered by a service that resolves nowhere: the handshake asked for the declared name, which
    the certificate holds alone, and `Host` is the declared name with the port."""
    with TlsPeer(tmp_path, NAME) as peer:
        client = rest.Rest(peer.ca, addresses=build({NAME: ("127.0.0.1", ["node-a.cluster.example.org"])}))
        try:
            assert client.json("GET", peer.url + CONFIGURATION, label="zone configuration") == {}
        finally:
            client.close()
        assert peer.sni == [NAME]
        assert peer.requests == [{"method": "GET", "path": CONFIGURATION, "host": f"{NAME}:{peer.port}"}]


def test_o77_without_the_map_the_bootstrap_cannot_reach_a_declared_name(rest, tmp_path):
    """O77: it is the map that dials the name, and the failure names no host."""
    with TlsPeer(tmp_path, NAME) as peer:
        client = rest.Rest(peer.ca)
        try:
            with pytest.raises(rest.RestError, match="no answer") as refused:
                client.call("GET", peer.url + CONFIGURATION, label="zone configuration")
        finally:
            client.close()
        assert peer.requests == []
        assert not re.search(r"layout\.test|127\.0\.0\.1", str(refused.value))


def test_o77_the_bootstrap_still_checks_the_name_the_certificate_holds(rest, tmp_path):
    """O77: a declared name the certificate does not name is refused, though the map sends it to the same node."""
    with TlsPeer(tmp_path, NAME) as peer:
        client = rest.Rest(peer.ca, addresses=build({"other.layout.test": ("127.0.0.1", [])}))
        try:
            with pytest.raises(rest.RestError, match="no answer"):
                client.call("GET", f"https://other.layout.test:{peer.port}{CONFIGURATION}", label="zone configuration")
        finally:
            client.close()
        assert peer.requests == []

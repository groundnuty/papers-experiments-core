"""O73: `hpcboot.service_client`, the one place the login node's client of a federation's services is built.

On a federation of declared names every service's URL carries a name that resolves nowhere, so a client built with no
address map reaches none of them (F-B2-1, found live on 17 Nov 2026: `verify-data`'s client was built with none). The
constructor therefore refuses a layout of declared names whose root holds no usable map, and never falls back to a
client with none; a layout whose services are at loopback ports (a Compose federation's) needs no map and keeps none.
The factory is a stand-in that records what it is given, so no connection is made."""

import os
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, hpc_on_path

from expcore_onedata.addresses import AddressMap, AddressMapError

DOMAIN = "expcore-hpc-pilot.test"  # the pilot layout's `domain`
MAP_FILE = "secrets/address-map.json"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedlayout
        import fedspec

        modules.layout, modules.spec = fedlayout, fedspec
        yield modules


def layout_of(hpc, tmp_path, *, names: bool):
    """The pilot layout over an empty root: at its declared names (as `placed_layout` makes it) or at the loopback
    ports the Compose federation uses."""
    spec = hpc.spec.load_spec(LAYOUT)
    if names:
        spec = hpc.boot.hpc_spec(spec, {s.name: f"{s.name}.{DOMAIN}" for s in spec.services})
    (tmp_path / "root" / "secrets").mkdir(parents=True)
    return hpc.layout.Layout(tmp_path / "root", tmp_path, spec)


def good_map(layout) -> AddressMap:
    hosts = {
        f"{s.name}.{DOMAIN}": {"address": f"10.0.0.{i + 1}", "nodes": [f"node{i + 1:02d}.cluster.example.org"]}
        for i, s in enumerate(layout.spec.services)
    }
    return AddressMap.from_document({"format": "expcore-address-map/1", "hosts": hosts})


class Factory:
    """The REST client's constructor, recording each call's arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, dict]] = []

    def __call__(self, ca_file, **kwargs):
        self.calls.append((ca_file, kwargs))
        return SimpleNamespace(close=lambda: None)


def test_o73_a_layout_of_declared_names_is_given_the_map_its_root_holds(hpc, tmp_path):
    """O73: the factory is given the CA and, as `addresses`, the map the root holds, as `up` wrote it."""
    layout = layout_of(hpc, tmp_path, names=True)
    good_map(layout).write(layout.address_map)
    factory = Factory()
    hpc.boot.service_client(layout, factory)
    ((ca_file, kwargs),) = factory.calls
    assert ca_file == layout.ca_pem
    assert list(kwargs) == ["addresses"]
    assert kwargs["addresses"].to_document() == good_map(layout).to_document()


def test_o73_a_layout_of_declared_names_with_no_map_in_its_root_is_refused_naming_the_map(hpc, tmp_path):
    """O73: no map is no client: with every service at a name that resolves nowhere, a client with none could reach
    no service, so the constructor refuses, names the missing file, and builds nothing."""
    layout = layout_of(hpc, tmp_path, names=True)
    factory = Factory()
    with pytest.raises(AddressMapError, match=rf"{MAP_FILE} is missing") as raised:
        hpc.boot.service_client(layout, factory)
    assert factory.calls == []
    assert str(tmp_path) not in str(raised.value)  # the file by its place in the root, never a path of the host


def break_with_junk(layout) -> None:
    layout.address_map.write_text("not a map", encoding="utf-8")
    layout.address_map.chmod(0o600)


def break_with_a_public_mode(layout) -> None:
    good_map(layout).write(layout.address_map)
    layout.address_map.chmod(0o644)


def break_with_a_link(layout) -> None:
    good_map(layout).write(layout.address_map.with_name("elsewhere.json"))
    os.symlink("elsewhere.json", layout.address_map)


@pytest.mark.parametrize("break_it", [break_with_junk, break_with_a_public_mode, break_with_a_link])
def test_o73_a_layout_of_declared_names_with_an_unreadable_map_is_refused_naming_the_map(hpc, tmp_path, break_it):
    """O73: a map that is no document, is not private, or is a link is as good as none: refused with the file named
    and the map's own reason, before any client is made, and the text holds no declared name or address."""
    layout = layout_of(hpc, tmp_path, names=True)
    break_it(layout)
    factory = Factory()
    with pytest.raises(AddressMapError, match=rf"{MAP_FILE} cannot be used") as raised:
        hpc.boot.service_client(layout, factory)
    assert factory.calls == []
    text = str(raised.value)
    assert not any(held in text for held in (DOMAIN, "10.0.0.", str(tmp_path)))


def test_o73_a_layout_with_no_declared_names_builds_its_client_with_no_map(hpc, tmp_path):
    """O73: a federation whose services are at loopback ports (the Compose one) has no map and needs none: the
    factory is given the CA alone, as it always was."""
    layout = layout_of(hpc, tmp_path, names=False)
    assert not layout.address_map.exists()
    factory = Factory()
    hpc.boot.service_client(layout, factory)
    assert factory.calls == [(layout.ca_pem, {})]

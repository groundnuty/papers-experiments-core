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


def a_map_of(layout, services) -> AddressMap:
    """A usable map that holds the declared names of `services` alone, each at an address and node name of its own."""
    hosts = {
        f"{s.name}.{DOMAIN}": {"address": f"10.0.1.{n + 1}", "nodes": [f"host{n + 1:02d}.cluster.example.org"]}
        for n, s in enumerate(services)
    }
    return AddressMap.from_document({"format": "expcore-address-map/1", "hosts": hosts})


def test_o73_a_map_that_lacks_a_service_is_refused_naming_the_services_it_lacks_and_no_client_is_built(hpc, tmp_path):
    """O73 (Codex, 17 Nov 2026): the map was checked only to load, so one holding the zone alone built a client whose
    first dial of a provider went to DNS (the declared name resolves nowhere) and failed at connect. The constructor
    checks the map covers every service of the layout first: refused naming the file, how many are missing and which
    services (the layout's own labels), never a declared name, an address or a path; nothing is built."""
    layout = layout_of(hpc, tmp_path, names=True)
    zone, *providers = layout.spec.services
    a_map_of(layout, [zone]).write(layout.address_map)
    factory = Factory()
    with pytest.raises(
        AddressMapError,
        match=rf"{MAP_FILE} holds no entry for {len(providers)} of "
        rf"{len(layout.spec.services)} services",
    ) as raised:
        hpc.boot.service_client(layout, factory)
    assert factory.calls == []
    text = str(raised.value)
    assert all(p.name in text for p in providers)
    assert not any(held in text for held in (DOMAIN, "10.0.1.", "cluster.example.org", str(tmp_path)))


def test_o73_a_map_that_lacks_one_provider_is_refused_naming_that_one_alone(hpc, tmp_path):
    """O73: one missing provider is named and counted, and the services the map does hold are not."""
    layout = layout_of(hpc, tmp_path, names=True)
    *kept, lost = layout.spec.services
    a_map_of(layout, kept).write(layout.address_map)
    factory = Factory()
    with pytest.raises(
        AddressMapError, match=rf"holds no entry for 1 of {len(layout.spec.services)} services"
    ) as raised:
        hpc.boot.service_client(layout, factory)
    assert lost.name in str(raised.value)
    assert not any(s.name in str(raised.value) for s in kept if s.name not in lost.name and lost.name not in s.name)
    assert factory.calls == []


def test_o73_a_map_that_holds_every_service_and_more_is_taken_and_a_name_is_matched_in_any_case(hpc, tmp_path):
    """O73: the check refuses a missing service and nothing else: a map with an entry the layout does not declare is as
    usable as before, and the declared name is matched as the map matches it (case aside)."""
    layout = layout_of(hpc, tmp_path, names=True)
    extra = SimpleNamespace(name="extra")
    a_map_of(layout, [*layout.spec.services, extra]).write(layout.address_map)
    factory = Factory()
    hpc.boot.service_client(layout, factory)
    assert len(factory.calls) == 1
    document = good_map(layout).to_document()
    document["hosts"] = {name.upper(): entry for name, entry in document["hosts"].items()}
    AddressMap.from_document(document).write(layout.address_map)
    hpc.boot.service_client(layout, factory)
    assert len(factory.calls) == 2


def test_o73_the_refusal_comes_before_the_client_is_made_for_the_core_phase_and_verify_data_alike(hpc, tmp_path):
    """O73: both clients of the services go through the constructor, so a map that lacks a service refuses the core's
    phase and `verify-data` before either asks the zone anything."""
    layout = layout_of(hpc, tmp_path, names=True)
    (tmp_path / "root" / "ca").mkdir()
    layout.ca_pem.write_text("not read: no client is made", encoding="utf-8")
    layout.passphrase(layout.spec.zone.name).write_text("onlyletters", encoding="utf-8")
    layout.passphrase(layout.spec.zone.name).chmod(0o600)
    a_map_of(layout, layout.spec.services[:1]).write(layout.address_map)
    factory = Factory()
    with pytest.raises(AddressMapError, match=r"holds no entry for"):
        hpc.boot.core_phase(layout, (5001, 5001), {}, rest=factory)
    layout.facts.write_text('{"spaces": {}}', encoding="utf-8")
    with pytest.raises(AddressMapError, match=r"holds no entry for"):
        hpc.boot.verify_ingested(layout, (5001, 5001), {}, rest=factory)
    assert factory.calls == []

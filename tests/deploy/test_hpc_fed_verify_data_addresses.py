"""O73: `hpcfed.py verify-data` reaches each owner by its declared name through the root's address map.

On an HPC federation every service's URL carries its declared name, which resolves nowhere on the site: the login
node's clients dial it at the address the root's map holds for it. `core_phase` built its client that way, and
`verify_ingested` (what `verify-data` runs, in a fresh process) built its own without the map, so it could reach no
owner (F-B2-1). These tests use the real REST client and the real mapped transport over a network that resolves only
the addresses of the map, never a name; Slurm is the fake of hpc_fed_support and the space reads are stand-ins."""

import functools
from types import SimpleNamespace

import httpx
import pytest
from hpc_fed_support import hpc_on_path
from hpc_ingested_support import ONE, artifacts, ingested_state, study_layout, tree_of, up

from expcore_onedata.addresses import AddressMap, AddressMapError


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcnames

        modules.names = hpcnames
        yield modules


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


def only_addresses_resolve(monkeypatch: pytest.MonkeyPatch, addresses: set[str]) -> list[str]:
    """The site's network: a request to an address of the map is answered, a request to any other host (a declared
    name, which resolves nowhere) fails to connect. Returns the hosts dialled, in order."""
    dialled: list[str] = []

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        dialled.append(request.url.host)
        if request.url.host not in addresses:
            raise httpx.ConnectError("the name resolves nowhere")
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return dialled


def reads_the_owner(boot: SimpleNamespace):
    """A space's tree as its owner serves it, after one REST call to the owner at its URL as every service's is."""

    def read(space: str):
        owner = boot.spec.provider(boot.spec.space(space).owner)
        url = f"https://{boot.names[owner.name]}:8443/api/v3/oneprovider/configuration"
        boot.rest.call("GET", url, label=f"{owner.name} configuration")
        return tree_of(ONE)

    return read


def verify_over_real_rest(hpc, fed: SimpleNamespace, names: dict[str, str]) -> None:
    """`verify-data`'s read, through `verify_ingested`'s own client factory (the real REST client): only the bootstrap
    and the space reads are stand-ins."""

    def bootstrap(client, layout, owner):
        return SimpleNamespace(
            layout=layout, spec=layout.spec, ids=SimpleNamespace(spaces={}), rest=client, names=names
        )

    fed.driver.verify = functools.partial(hpc.boot.verify_ingested, bootstrap=bootstrap, data=reads_the_owner)


def federation(hpc, tmp_path, ca):
    """A federation brought up over the fake cluster, with its address map in the root and its ingested space's
    manifest declared, and the declared name each service answers to."""
    fed = up(hpc, tmp_path / "fed", study_layout(tmp_path / "layout"), ca, artifacts_dir=artifacts(tmp_path / "art"))
    layout = fed.driver.placed_layout(fed.driver.store.read())
    names = hpc.names.declared_names(layout.spec)
    verify_over_real_rest(hpc, fed, names)
    return fed, layout, names


def test_o73_verify_data_reaches_the_owner_through_the_address_map_and_records_its_marker(
    hpc, tmp_path, ca, monkeypatch
):
    """O73: with every service at a declared name that resolves nowhere, `verify-data` dials the owner at the address
    the root's map holds for its declared name, and the pass leaves the space's verification marker (the one
    `shards.py plan` requires)."""
    fed, layout, names = federation(hpc, tmp_path, ca)
    amap = AddressMap.load(layout.address_map)
    owner = layout.spec.provider(layout.spec.space("delta-space").owner).name
    dialled = only_addresses_resolve(monkeypatch, {amap.address_of(name) for name in names.values()})
    said = fed.driver.verify_data()
    assert said.startswith("data: every ingested space holds the data its manifest declares")
    assert dialled == [amap.address_of(names[owner])]
    assert ingested_state(fed)["delta-space"]["verified"]["manifest"] == ONE.digest()


def test_o73_verify_data_with_no_map_in_the_root_cannot_reach_the_owner_and_leaves_no_marker(
    hpc, tmp_path, ca, monkeypatch
):
    """O73: the client is the core phase's: with no map in the root the declared name is dialled as it is, which
    resolves nowhere, so the read fails naming no address and the space has no marker."""
    fed, layout, names = federation(hpc, tmp_path, ca)
    amap = AddressMap.load(layout.address_map)
    addresses = {amap.address_of(name) for name in names.values()}
    layout.address_map.unlink()
    dialled = only_addresses_resolve(monkeypatch, addresses)
    with pytest.raises(hpc.boot.RestError, match=r"no answer \(ConnectError\)") as raised:
        fed.driver.verify_data()
    assert not addresses & set(dialled)
    assert not any(address in str(raised.value) for address in addresses)
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_o73_verify_ingested_passes_the_clients_factory_the_map_the_core_phase_loads(hpc, tmp_path, ca):
    """O73: the factory is given the root's map as `addresses`, as `core_phase`'s is, and a map the root holds but
    cannot be read is refused before any client is made."""
    _, layout, _ = federation(hpc, tmp_path, ca)
    given: list[dict] = []

    def rest(ca_file, **kwargs):
        given.append(kwargs)
        return SimpleNamespace(close=lambda: None)

    def bootstrap(client, lay, owner):
        return SimpleNamespace(layout=lay, spec=lay.spec, ids=SimpleNamespace(spaces={}))

    verify = functools.partial(
        hpc.boot.verify_ingested, rest=rest, bootstrap=bootstrap, data=lambda boot: lambda space: tree_of(ONE)
    )
    assert verify(layout, (5001, 5001), {"delta-space": ONE}) == {"delta-space": ""}
    assert [list(kwargs) for kwargs in given] == [["addresses"]]
    assert given[0]["addresses"].to_document() == AddressMap.load(layout.address_map).to_document()
    layout.address_map.write_text("not a map", encoding="utf-8")
    with pytest.raises(AddressMapError):
        verify(layout, (5001, 5001), {"delta-space": ONE})
    assert len(given) == 1

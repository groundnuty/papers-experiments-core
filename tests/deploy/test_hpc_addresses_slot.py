"""O73: the platform template carries the address map's line, and the render adds nothing to the template.

`up` once put an `addresses = { file, resolver }` line first in the rendered platform file whenever the root held an
address map, so a render was its template plus a line the template did not have, and a study's conformance check (a
render equals its template with only the slots filled) refused it. The line is now the template's own, a top-level key
before the first table whose value is the slot `@hpc_addresses@`; the render fills the slot with the table and adds
nothing. `up` refuses, before it submits a job, a template without the slot (it writes the map into every HPC
federation's root), and the render refuses a template that names the slot when the root holds no map, or that holds a
map the template has no slot for.
"""

import re
import tomllib

import pytest
from hpc_fed_support import HPC, LAYOUT, PILOT_TEMPLATE, SITE, FakeSlurm, address_values, agent_site, hpc_on_path

TEMPLATES = sorted((HPC / "layouts").glob("*.platform.toml.template"))
TEMPLATE = PILOT_TEMPLATE.read_text(encoding="utf-8")
LINE = "addresses = @hpc_addresses@"
SLOT = re.compile(r"@([a-z_]+(?:\.[a-z0-9_-]+)?)@")  # the renderer's own pattern, written out here
BASE = 'zone = { name = "zone", url = "https://zone.invented.test:8443" }\n'


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcnames

        modules.names = hpcnames
        yield modules


def code_of(line: str) -> str:
    return line.split("#", 1)[0].strip()


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda path: path.name)
def test_o73_every_shipped_hpc_template_carries_the_one_address_line_before_its_first_table(path):
    """O73: each template this repository ships for the HPC federation has the same one line, `addresses =
    @hpc_addresses@`, among its top-level keys: a key after a table header would belong to the table."""
    lines = path.read_text(encoding="utf-8").splitlines()
    (at,) = [n for n, line in enumerate(lines) if code_of(line).startswith("addresses")]
    assert code_of(lines[at]) == LINE
    assert not [line for line in lines[:at] if code_of(line).startswith("[")]
    pilot = [line for line in TEMPLATE.splitlines() if code_of(line) == LINE]
    assert lines[at] == pilot[0], "the five templates carry the line alike"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda path: path.name)
def test_o73_a_render_is_its_template_with_only_the_slots_filled(hpc, tmp_path, path):
    """O73: the text `rendered` gives is the template with each `@slot@` replaced and nothing else: no line added, none
    moved, the `addresses` line where the template has it. A study's conformance check holds a render to this."""
    template = path.read_text(encoding="utf-8")
    filled = {name: str(1_000_001 + n) for n, name in enumerate(dict.fromkeys(SLOT.findall(template)))}
    filled.update(address_values(tmp_path))
    expected = SLOT.sub(lambda found: filled[found.group(1)], template)
    text = hpc.boot.rendered(template, filled)
    assert text == expected
    assert text.count("\n") == template.count("\n")
    assert [n for n, line in enumerate(text.splitlines()) if line.startswith("addresses")] == [
        n for n, line in enumerate(template.splitlines()) if line.startswith("addresses")
    ]


@pytest.mark.parametrize("resolver", [True, False], ids=["with the launcher", "without it"])
def test_o73_a_template_with_the_slot_and_a_map_renders_the_table(hpc, tmp_path, resolver):
    """O73: the slot's value is the table the driver used to put first, `{ file = "<the map>" }` with the launcher's
    path as `resolver` once the root holds the launcher, in TOML where the template's key is: the platform layer reads
    it as the `[addresses]` table."""
    given = address_values(tmp_path, resolver)
    text = hpc.boot.rendered(f"{LINE}\n{BASE}[tokens]\nmargin_s = 5\n", given)
    table = tomllib.loads(text)["addresses"]
    assert table["file"] == str(tmp_path / "secrets" / "address-map.json")
    assert table.get("resolver") == (str(tmp_path / "toolshim" / "resolve_launch.py") if resolver else None)
    assert text.splitlines()[0] == f"addresses = {given['hpc_addresses']}"
    assert (
        hpc.names.addresses_value(
            tmp_path / "secrets" / "address-map.json", tmp_path / "toolshim" / "resolve_launch.py" if resolver else None
        )
        == given["hpc_addresses"]
    )


def test_o73_a_render_with_no_map_and_no_slot_is_as_it_always_was(hpc):
    """O73: a root without a map (a Compose federation's) and a template without the slot: the text is the template."""
    assert hpc.boot.rendered(BASE, {}) == BASE


def test_o73_a_template_that_names_the_slot_with_no_map_is_refused_naming_the_slot(hpc):
    """O73: nothing would fill the slot, and the renderer's own refusal does not say which one."""
    with pytest.raises(RuntimeError, match=r"@hpc_addresses@.*no address map"):
        hpc.boot.rendered(f"{LINE}\n{BASE}", {})


def test_o73_a_map_with_a_template_that_has_no_slot_is_refused_and_nothing_is_put_first(hpc, tmp_path):
    """O73: the driver no longer adds the line to a template that lacks it: the render refuses, naming the slot."""
    with pytest.raises(RuntimeError, match=r"@hpc_addresses@"):
        hpc.boot.rendered(BASE, address_values(tmp_path))


MISPLACED = {
    "in a comment only": f"# {LINE}\n{BASE}",
    "as another key": f"address_map = @hpc_addresses@\n{BASE}",
    "under a table": f"{BASE}[tokens]\n{LINE}\n",
    "beside an addresses line of its own": f'addresses = {{ file = "x" }}\n{BASE}[tokens]\nother = @hpc_addresses@\n',
}


@pytest.mark.parametrize("text", MISPLACED.values(), ids=MISPLACED.keys())
def test_o73_a_slot_that_is_not_the_value_of_the_top_level_addresses_key_is_refused(hpc, tmp_path, text):
    """O73: a slot in a comment fills no key, one under a table would be that table's key (`tokens.addresses`), and
    the platform layer would read no map either way; the render refuses each, naming the slot."""
    with pytest.raises(RuntimeError, match=r"@hpc_addresses@"):
        hpc.boot.rendered(text, address_values(tmp_path))


# -- up: the preflight, before anything is submitted ----------------------------------------------------------------


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


def rig(hpc, tmp_path, edit=lambda text: text):
    """A driver over the fake Slurm for the pilot's layout whose template is `edit` of the shipped one."""
    folder = tmp_path / "layout"
    folder.mkdir()
    (folder / LAYOUT.name).write_text(LAYOUT.read_text(encoding="utf-8"), encoding="utf-8")
    (folder / PILOT_TEMPLATE.name).write_text(edit(TEMPLATE), encoding="utf-8")
    fake = FakeSlurm()
    root = tmp_path / "fed" / "root"
    return hpc.cli.make_driver(folder / LAYOUT.name, str(root), runner=fake), fake, root


def test_o73_the_preflight_takes_the_shipped_template(hpc, tmp_path, site):
    """O73: the control of the refusals below: the pilot's template, as shipped, passes the preflight."""
    driver, _, _ = rig(hpc, tmp_path)
    driver.preflight(hpc.settings.site_from_environment(site))


REFUSED = {
    "no slot": lambda text: text.replace(LINE, ""),
    "the slot in a comment only": lambda text: text.replace(LINE, f"# {LINE}"),
    "the slot as another key": lambda text: text.replace(LINE, "address_map = @hpc_addresses@"),
    "the slot under a table": lambda text: text.replace(LINE, "").replace("\n[tokens]\n", f"\n[tokens]\n{LINE}\n", 1),
}


@pytest.mark.parametrize("edit", REFUSED.values(), ids=REFUSED.keys())
def test_o73_up_refuses_a_template_without_the_slot_before_anything_is_submitted(hpc, tmp_path, site, edit):
    """O73: `up` writes the address map into every HPC federation's root, so the template must carry the slot that
    names it; the render would refuse it after the federation is up and billing. `up` reads the template first, names
    the slot, and submits and writes nothing."""
    assert LINE in TEMPLATE
    driver, fake, root = rig(hpc, tmp_path, edit)
    with pytest.raises(RuntimeError, match=r"@hpc_addresses@"):
        driver.up(hpc.settings.site_from_environment(site))
    assert not [call for call in fake.calls if call[0] == "sbatch"]
    assert not (root / "hpc-federation.json").exists()

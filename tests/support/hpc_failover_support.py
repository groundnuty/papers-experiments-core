"""A root, a layout and a stand-in bootstrap for the core's phase of the HPC federation, shared by the tests of the
failover-order admission (O98: deploy/hpc-federation/hpcfailover.py, hpcboot.py). Every name and id is invented.

The layout is the pilot-shape one: alpha-space is held by `larch` and supported by `aspen` (the agent's local provider)
and `rowan`, beta-space by `rowan` and `aspen`. The platform template the layout renders declares `rowan` the failover
holder of alpha-space, the one space of the layout whose owner has a supporter other than the local provider. The
providers' ids are the test's own, so the order is too. Nothing here reaches a cluster."""

import os
from types import SimpleNamespace

from hpc_fed_support import LAYOUT, PILOT_TEMPLATE, data_stand_in, pin_values, write_address_map

FILE = "failover-order.json"
DECLARED = 'outage_targets = ["larch"]'
# larch holds alpha-space (supported by aspen, rowan); every other provider's id is below or above it as the case says
LARCH_FIRST = {"larch": "id-01", "aspen": "id-02", "rowan": "id-03", "alder": "id-04", "hazel": "id-05"}
MIRROR_FIRST = {"larch": "id-03", "aspen": "id-02", "rowan": "id-01", "alder": "id-04", "hazel": "id-05"}
ONE_MIRROR_BELOW = {"larch": "id-02", "aspen": "id-03", "rowan": "id-01", "alder": "id-04", "hazel": "id-05"}
# aspen (the agent's local provider, which supports alpha-space and holds none of its blocks) sorts below the owner and
# rowan, the declared failover holder, above it: the counterexample of the review of #548
LOCAL_FIRST = {"larch": "id-02", "aspen": "id-01", "rowan": "id-03", "alder": "id-04", "hazel": "id-05"}


# the line the platform template's alpha-space entry declares by default: `rowan` holds its whole replica, and `aspen`,
# the agent's local provider, supports it and holds none of its blocks
FAILOVER = 'failover = ["rowan"]'
ALPHA_HOLDER = 'holder = "larch"'


def layout_with(folder, line, replace=(), failover=FAILOVER):
    """The pilot-shape layout with `line` added to its `[hpc]` table (none: as shipped), beside its template, whose
    alpha-space entry gets the line `failover` after its `holder` (none: the entry declares no failover holder).
    `replace` is pairs `(old, new)` of text the layout holds once, replaced in it."""
    folder.mkdir(parents=True, exist_ok=True)
    text = LAYOUT.read_text(encoding="utf-8")
    if line is not None:
        assert text.count("\n[hpc]\n") == 1
        text = text.replace("\n[hpc]\n", f"\n[hpc]\n{line}\n", 1)
    for old, new in replace:
        assert text.count(old) == 1, old
        text = text.replace(old, new, 1)
    (folder / LAYOUT.name).write_text(text, encoding="utf-8")
    template = PILOT_TEMPLATE.read_text(encoding="utf-8")
    if failover is not None:
        assert template.count(f"{ALPHA_HOLDER}\n") == 1
        template = template.replace(f"{ALPHA_HOLDER}\n", f"{ALPHA_HOLDER}\n{failover}\n", 1)
    (folder / PILOT_TEMPLATE.name).write_text(template, encoding="utf-8")
    return folder / LAYOUT.name


def layout_over(hpc, root, folder, line, replace=(), failover=FAILOVER):
    """The layout declaring `line` over the root `root`, with the address map the core's phase needs in it."""
    import fedlayout
    import fedspec

    spec = fedspec.load_spec(layout_with(folder, line, replace, failover))
    hosts = {s.name: f"{s.name}.cluster.example.org" for s in spec.services}
    layout = fedlayout.Layout(root, folder.parent, hpc.boot.hpc_spec(spec, hosts))
    write_address_map(layout)
    return layout


def core_root(hpc, tmp_path, make_ca, line, replace=(), failover=FAILOVER):
    """A root holding what the core's phase needs of the services' bring-up, and the layout over it."""
    import fedspec

    root = tmp_path / "root"
    (root / "secrets" / "users").mkdir(parents=True)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(make_ca(tmp_path / "ca").ca.read_bytes())
    spec = fedspec.load_spec(layout_with(tmp_path / "layout", line, replace, failover))
    files = {"secrets/zone.passphrase": "A1b2C3d4E5f6G7h8", "secrets/admin.token": "an-admin-token"}
    files.update({f"secrets/users/{p.name}.pass": "a-password" for p in spec.principals})
    for name, value in files.items():
        (root / name).write_text(value, encoding="utf-8")
        os.chmod(root / name, 0o600)
    return root, layout_over(hpc, root, tmp_path / "layout", line, replace, failover)


def boot_with(layout, provider_ids):
    ids = SimpleNamespace(
        admin="uid-admin",
        providers={p.name: provider_ids[p.name] for p in layout.spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in layout.spec.spaces},
        users={p.name: f"uid-{p.name}" for p in layout.spec.principals},
        scans={},
        storages={},
    )
    return SimpleNamespace(layout=layout, ids=ids, spec=layout.spec)


def run_core(hpc, layout, provider_ids, events):
    options = {
        "environ": {},
        "rest": lambda ca, **kwargs: SimpleNamespace(close=lambda: None),
        "bootstrap": lambda rest, lay, owner: boot_with(lay, provider_ids),
        "steps": lambda boot: events.append("steps"),
        "content": lambda boot: SimpleNamespace(reset=lambda timeout: None),
        "links": lambda lay, spaces: events.append("links") or "link check passed",
        "data": data_stand_in(),
    }
    values = {
        **hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, layout.root),
        **pin_values(layout.root / "agent"),
    }
    return hpc.boot.core_phase(layout, (5001, 5002), values, **options)

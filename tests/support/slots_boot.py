"""Both platforms' bootstraps over one layout and a fake zone each (O83): the Compose bootstrap and the HPC one, which
extends it, each in a root of its own, for the tests that compare what they provision."""

from fake_zone import FakeZone
from slots_support import LAYOUT, WITH_SLOTS, layout_with_template, secret_of


def modules(hpc):
    """The federation modules a test of provisioning needs, imported by their plain names while the driver's modules are
    on the path (`hpc_on_path`)."""
    import fedboot
    import fedfacts
    import fedlayout
    import fedpki
    import fedrest
    import fedspec
    import fedtools
    import hpcnames

    for name, module in (
        ("fedboot", fedboot), ("fedfacts", fedfacts), ("fedlayout", fedlayout), ("fedpki", fedpki),
        ("fedrest", fedrest), ("fedspec", fedspec), ("fedtools", fedtools), ("names", hpcnames),
    ):  # fmt: skip
        setattr(hpc, name, module)
    return hpc


def boots(hpc, tmp_path, template=WITH_SLOTS, layout=LAYOUT):
    """`[(platform, zone, boot, layout)]` for Compose and for HPC: the same layout file, a root and a fake zone of each
    one's own, the admin's token in place, and the ids the bootstrap holds before it makes any user."""
    spec = hpc.fedspec.load_spec(layout_with_template(template, tmp_path, layout))
    found = []
    for name, make in (
        ("compose", lambda zone, lay: hpc.fedboot.Bootstrap(zone, lay)),
        ("hpc", lambda zone, lay: hpc.boot.HpcBootstrap(zone, lay, (5001, 5001))),
    ):
        root = hpc.fedlayout.Layout(tmp_path / name, spec=spec)
        hpc.fedpki.private_directory(root.secrets)
        hpc.fedpki.private_directory(root.users)
        root.admin_token.write_text("an-admin-token-for-the-scan", encoding="utf-8")
        root.admin_token.chmod(0o600)
        zone = FakeZone(spec)
        boot = make(zone, root)
        boot.zone = zone.zone
        boot.ids.spaces = {s.name: f"sid-{s.name}" for s in spec.spaces}
        boot.ids.providers = {p.name: f"pid-{p.name}" for p in spec.providers}
        found.append((name, zone, boot, root))
    return found


def provision(boot, spec):
    """The bootstrap's user and member steps and the slot audit, as both platforms' `bring_up` take them."""
    boot.ensure_users()
    for space in spec.spaces:
        if spec.members(space.name):
            boot.ensure_members(space.name)
    return boot.audit_slot_users()


def made_by_hand(zone, root, spec, *, only=None):
    """Users that exist before the bootstrap runs, each as the layout declares it: its zone user, its password file (a
    private file the zone's user opens) and its membership of each declared space with exactly its privileges."""
    for principal in spec.principals:
        if only is not None and principal.name not in only:
            continue
        zone.add_user(principal.name, secret_of(principal.name))
        path = root.password(principal.name)
        path.write_text(secret_of(principal.name), encoding="utf-8")
        path.chmod(0o600)
        for space in principal.spaces:
            zone.join(space, principal.name, principal.privileges)

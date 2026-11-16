"""hpcimport.py: what the HPC federation's `up` needs of a shared imported tree (O56), from the layout (the Compose
federation's: `fedspec.py`, `fedlayout.py`). Plain functions on a loaded `fedlayout.Layout`, so the driver imports them
and a test calls them; stdlib only, parseable by the oldest Python the bundle's tests parse with.

A space declared `import = { readonly = true, shared = true, ... }` is imported by its owner from a directory of the
cluster's shared filesystem that other federations import too. The tree is never written: the owner's service binds it
read-only (`bundle/importstorage.py`), its storage is read-only on the panel (the layout's declaration, which the
platform file repeats and the core checks), and the mirrors keep node-local storage and mount nothing of it.

The hooks, in the order `up` meets them (DESIGN.md, section 8):

0. `check_support`, in the preflight: the support the layout gives a space is no smaller than its tree's manifest.
1. `provider_import_args`: appended to a provider's `provider.sbatch` command, so its service mounts the trees it owns.
2. `import_storage_extra`: returned by the HPC bootstrap's `Bootstrap.import_storage_extra`, so an imported storage is
   made for a provider that runs as the job's user.
3. `luma_requests`: the panel calls that map the space and its members to the job's user on a storage, sent for the
   imported storage as for the provider's regular one (`Bootstrap.storage_for` names the right one).
4. `fedimport.verify_imports`, after the platform file is rendered: the core's check that the space holds its manifest.
5. `mount_check_argv`: the control step on the owner's node that proves, from the mounts the service holds, that each
   tree is read-only.
6. `direct_storages`: the host directories a direct mount reads, for the platform file's `[sandbox.mount.storages]`.
"""

import os


class HpcImportRefused(ValueError):
    """An import the HPC federation will not run; the message names the space and the variable, never a path."""


def shared_imports_of(layout, provider):
    """The shared imports a provider owns, as spaces of the layout. Any other import it owns is refused: a writable
    import copies its source into the federation's root, which on HPC is the shared filesystem's, and a read-only one
    that is not marked shared would leave the sharing out of the platform file the core checks."""
    owned = layout.owned_imports(provider)
    for space in owned:
        if not space.imported.shared:
            raise HpcImportRefused(
                f"spaces.{space.name}.import must be shared = true on HPC: its tree is on the shared filesystem, "
                "which other federations read, so it is read-only and verified against a manifest"
            )
    return owned


def provider_import_args(layout, provider, environ):
    """The `--import=SPACE=PATH` arguments of a provider's `provider.sbatch` command, one per shared import it owns, in
    the layout's order; none for a mirror or a provider that owns no import. Each path is the variable's value as the
    layout checks it (`Layout.import_source`: absolute, no `..`, no link on the way, an existing directory outside the
    federation's root); the node checks it again where it is bound."""
    return [
        "--import=%s=%s" % (space.name, layout.import_source(space.name, environ))
        for space in shared_imports_of(layout, provider)
    ]


def check_support(layout, environ):
    """Refuse a shared import whose manifest holds more bytes than the support the layout gives its space
    (`support_bytes`, 10 GiB by default), before the scan finds it out half way; return each shared space's manifest
    bytes by its name. The message names the space and the two figures, never a path; a manifest that cannot be used is
    refused by `Layout.load_manifest`, naming its variable. Needs the core, so it runs in the driver, not on a node."""
    found = {}
    for space in layout.shared_imports():
        wanted = layout.load_manifest(space.name, environ).bytes
        if wanted > space.support_bytes:
            raise HpcImportRefused(
                f"spaces.{space.name}: the tree holds {wanted} bytes and its support is {space.support_bytes}: "
                "raise support_bytes"
            )
        found[space.name] = wanted
    return found


def import_storage_extra(uid, gid):
    """What the HPC bootstrap adds to the creation of an imported storage (`Bootstrap.import_storage_extra`): the job
    user's ids, the local LUMA feed the mappings below need, and the canonical path type an imported storage keeps its
    files under. They are the settings the provider's regular posix storage is made with (`bundle/finish.sh`)."""
    for name, value in (("uid", uid), ("gid", gid)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HpcImportRefused(f"the job user's {name} must be a non-negative integer")
    return {"rootUid": uid, "rootGid": gid, "lumaFeed": "local", "storagePathType": "canonical"}


def luma_requests(storage_id, space_id, user_ids, uid, gid):
    """The panel calls, as (method, path, body) with the path under `/provider`, that map a space and its members to
    the job's user on one storage: the space's default credentials, then each member. They are the calls
    `bundle/onboard.py` makes for the provider's regular storage, here for any storage that supports a space."""
    base = f"/provider/storages/{storage_id}/luma/local_feed/storage_access"
    calls = [("PUT", f"{base}/posix_compatible/default_credentials/{space_id}", {"uid": uid, "gid": gid})]
    for user in user_ids:
        mapping = {
            "onedataUser": {"mappingScheme": "onedataUser", "onedataUserId": user},
            "storageUser": {"storageCredentials": {"type": "posix", "uid": str(uid)}, "displayUid": uid},
        }
        calls.append(("POST", f"{base}/all/onedata_user_to_credentials", mapping))
    return calls


def mount_check_argv(bundle, workdir, python="python3.11"):
    """The argv of the control step, on a provider's node, that reads the mounts its service holds and exits 0 only
    when every listed tree is held read-only (`importstorage.py mounts`)."""
    return [python, "-I", os.path.join(bundle, "importstorage.py"), "mounts", "--workdir", workdir]


def direct_storages(layout, storage_ids, environ):
    """The storages a direct mount reads, as the platform file's `[sandbox.mount.storages]` maps them: each declared
    `direct_io` import's storage id (`storage_ids`, by (owner, space), as the bootstrap gave them out) to the host
    directory of its tree. The sandbox binds each of them read-only into its sidecar alone, never into the agent's
    container, which is why only a read-only import may declare it. A direct mount reads the storage of the agent's own
    provider (`local` in the platform file), so `direct_io` is meaningful only on a space that provider owns: the layout
    has no `local` to check it against, and the platform template must put the two together. The paths are the host's
    own: the platform file that holds them is the federation's root's, never committed."""
    mapping = {}
    for space in layout.shared_imports():
        if not space.imported.direct_io:
            continue
        found = storage_ids.get((space.owner, space.name))
        if not found:
            raise HpcImportRefused(f"no storage was made for spaces.{space.name}, so a direct mount has none to read")
        mapping[found] = str(layout.import_source(space.name, environ))
    return mapping

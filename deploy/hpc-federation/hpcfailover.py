"""The failover order of a federation's owner-outage spaces, admitted at the end of `up` (O98).

Taking down the provider that holds a space's data (an owner outage) fails the space's reads over to a mirror only when
some mirror's provider id sorts below the owner's: op-worker asks the holders of a file's blocks in the order of their
ids and does not retry another after a disconnected one (`expcore_onedata.failover`). Ids are random at registration,
so it is a property of one member, found out only once its providers have registered. `up` therefore checks it where the
ids first exist, in the core's phase, right after `platform.toml` is written and before the link check, and a member
where it does not hold fails `up`, so that the fleet replaces it.

**Scope.** Only the providers a layout declares an owner outage for: `[hpc] outage_targets = ["<provider>", ...]`, the
providers the study's `provider_outage` conditions take down, by the names the layout gives them. The spaces in scope
are those the rendered platform file holds with one of them as the data provider (`holder`) and another provider as a
supporter. A layout that declares none, or whose declared providers hold no space with a mirror, has nothing in scope:
`admit` then returns None, writes nothing and says nothing, and `up` is what it was.

**Which providers are mirrors.** The layout says: a space's `mirrors` (`fedspec.Space.mirrors`) are the providers the
study designates to hold its replicas, and the order is told over the owner and those alone. Another provider the
rendered file lists as supporting the space (the agent's local provider, say) is ignored, since it may hold no block of
the space's files and is then no replica to read from: counting it would let an owner whose id sorts above such a
provider pass, and op-worker, choosing between the owner and the one mirror that holds the data, would ask the stopped
owner first. The declared mirrors are matched to the file's providers through the bootstrap's ids, as the rest is
(`declared_mirrors`); a space the layout gives no mirror, or does not know, is out of scope, as `holders_differ` skips
it, and is never ordered over every supporter.

**What is written.** When something is in scope and every space passes, `failover-order.json` in the root
(`expcore_onedata.failover`): per space in scope its position label, its owner's and declared mirrors' provider labels,
the labels in the order of their ids, and `mirror_first: true`. Labels only: no provider's name, no space's name, no id.
It is evidence like `render-identity.json` (`hpcevidence.UP_FILES`), and the identity read reports it
(`fleetidentity.py`); the fault injector reads it too, taking each space's mirrors from it (`failover.admit_stop`). A
space that fails refuses the member, naming the spaces and providers by label, and removes a record an earlier run of
the phase left. The record of an earlier run is removed first thing in the core's phase (`invalidate`, called by
`hpcboot.core_phase` before anything that can raise, with an outage declared or not), so a run that fails at any point
before it reaches the admission (a declared manifest it cannot load, say), or whose layout no longer declares the
outage, never leaves a record the identity read would report as passed. The record is published by a private draft in
the root, fsynced and renamed over the file (`publish_private`), so a full filesystem leaves no partial record.

The scope is read from the rendered file's data provider (`holder`) of each space, which nothing else ties to the
layout's owner, so the file is tied to the layout through the ids the bootstrap gave out, as the support check
(`fedsupport.check_rendered_support`) ties its sets: a space of the file is the layout's by its id, and a provider of
the file is the layout's by its id, whatever the template calls either. A space of a declared owner whose file names
another provider, or one whose file names a declared provider and the layout another owner (`holders_differ`), and a
space of a declared owner that the file does not hold at all, are refused, never left out of scope in silence. The one
exception is the link check's probe space (`spec.probe_space`), which the file holds under `[link_check]` and not as a
`[[spaces]]` entry.
"""

import contextlib
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import hpcsettings

# The record's name, kept here and not taken from `expcore_onedata.failover`, so that `invalidate` needs no import of
# the core where no outage is declared; a test holds it equal to the core's and to the evidence list's.
FILE = "failover-order.json"
REFUSAL = "refuse this member"


def declared(spec: Any) -> Tuple[str, ...]:
    """The providers the layout declares an owner outage for (`[hpc] outage_targets`), checked against the layout's own
    providers; SettingsError when the key is malformed or names one the layout lacks. Empty: no outage declared."""
    return hpcsettings.outage_targets(spec.hpc, [provider.name for provider in spec.providers])


def invalidate(root: Any) -> None:
    """Remove the failover record of the root, if there is one: whatever it said was of an earlier run's platform file.
    `hpcboot.core_phase` calls it first, before it loads a manifest or writes a file, whether this layout declares an
    outage or not, so the record is gone on every way the run can go on to fail or to end without an admission. OSError
    when it cannot be removed: a record that might outlive the file it is of is no state to go on from."""
    (Path(root) / FILE).unlink(missing_ok=True)


def publish_private(path: Path, text: str) -> None:
    """Write `text` to `path` whole or not at all, mode 0600: a draft of this process's own beside it (never through a
    link, never an existing file), flushed and fsynced, renamed over `path` (a link there is replaced, not followed). A
    failure, a full filesystem included, removes the draft and leaves `path` as it was. A draft that already exists is
    some other writer's: creating it fails, and it is left alone, since only a draft this call made is its to remove."""
    draft = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)  # nothing of ours yet
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)  # whatever the umask says
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, path)
    except BaseException:
        with contextlib.suppress(OSError):  # the draft is ours: it may be gone already, as after the rename
            draft.unlink()
        raise


def holders_differ(spec: Any, config: Any, ids: Any, owners: Tuple[str, ...]) -> Tuple[List[str], int]:
    """The spaces whose data provider in the rendered platform file is not the owner the layout declares, among those an
    owner outage reaches by either (the layout's owner is declared, or the file's data provider is): their position
    labels in the file, and how many spaces of a declared owner the file does not hold at all.

    The failover check reads the file's `holder` and the layout says who owns the space, and nothing else ties the two,
    so a space where they differ is refused rather than left out of scope with no word. The two are matched through
    `ids` (the bootstrap's: `providers` and `spaces`, each by the layout's name), never through names, which a
    template may give as it likes: a file space is the layout's when it has its id, and a file provider is the layout's
    owner when it has its id. A layout space with no mirror, or the link check's probe space (the one `[link_check]`
    holds and no `[[spaces]]` entry does), is skipped; every other layout space of a declared owner that no entry of
    the file carries is counted."""
    holders = config.space_holders()
    provider_id = {config.provider_label(name): provider.id for name, provider in config.providers.items()}
    entries = {entry.space.id: entry for entry in config.spaces}
    targets = {ids.providers[name] for name in owners}
    differing, omitted = [], 0
    for space in spec.spaces:
        if space.name == spec.probe_space or not space.mirrors:
            continue
        owner_id = ids.providers[space.owner]
        entry = entries.get(ids.spaces[space.name])
        if entry is None:
            omitted += owner_id in targets
            continue
        held_id = provider_id.get(holders.get(entry.space.name))
        if (owner_id in targets or held_id in targets) and held_id != owner_id:
            differing.append(entry.position)
    return differing, omitted


def declared_mirrors(spec: Any, config: Any, ids: Any, owners: Tuple[str, ...]) -> Dict[str, Tuple[str, ...]]:
    """The mirrors the layout declares for every space of the rendered file that a declared owner holds, by the file's
    position label of the space to the file's labels of its providers, for `failover.orders`' `declared`: the
    order is over the owner and these alone, and a provider that merely supports the space is ignored. A file space
    whose layout space has no mirror, or that is no layout space at all, is listed with none (out of scope: nothing is
    designated to serve it, as `holders_differ` skips a layout space with no mirror), so that no space a declared
    owner holds is ever ordered over every supporter. The probe space, which the file does not hold as a `[[spaces]]`
    entry, is not in it. The file's spaces and providers are matched to the layout's through the bootstrap's ids,
    never names. FailoverError, naming the space by label, when a declared mirror is no provider of the file."""
    from expcore_onedata import failover

    label_of = {provider.id: config.provider_label(name) for name, provider in config.providers.items()}
    targets = {label_of[ids.providers[name]] for name in owners if ids.providers[name] in label_of}
    layout_space = {ids.spaces[space.name]: space for space in spec.spaces if space.name != spec.probe_space}
    holders = config.space_holders()
    found: Dict[str, Tuple[str, ...]] = {}
    for entry in config.spaces:
        if holders.get(entry.space.name) not in targets:
            continue
        space = layout_space.get(entry.space.id)
        mirrors = space.mirrors if space is not None else ()
        labels = tuple(label_of[ids.providers[name]] for name in mirrors if ids.providers[name] in label_of)
        if len(labels) != len(mirrors):
            raise failover.FailoverError(
                f"failover order: {entry.position}: {len(mirrors) - len(labels)} declared mirror(s) are no "
                "provider of the platform file, so no order is told"
            )
        found[entry.position] = labels
    return found


def admit(layout: Any, ids: Any) -> Optional[str]:
    """The failover-order admission of the member `layout` describes, once its platform file is rendered: None when no
    space is in scope (nothing is read, written or said), else the sentence `up` prints after writing the record. `ids`
    are the bootstrap's (`providers` and `spaces`, by the layout's names), through which the file is tied to the layout.
    RuntimeError, naming spaces and providers by label, when a space's owner would be asked before every mirror, or
    when an id is not known, or when the file's spaces and owners are not the layout's, or when owners are declared and
    the layout has no platform file to read the ids from."""
    owners = declared(layout.spec)
    if not owners:
        return None
    from expcore_onedata import failover  # only where an outage is declared: up and its other commands stay as light
    from expcore_onedata.config import load_platform

    if layout.spec.platform_template is None:
        raise RuntimeError(
            "failover order: the layout declares outage targets and no platform file to read the ids from"
        )
    config = load_platform(layout.platform)
    differing, omitted = holders_differ(layout.spec, config, ids, owners)
    if differing or omitted:
        invalidate(layout.root)
        clauses = [f"{label}: the platform file's data provider is not the layout's owner" for label in differing]
        if omitted:
            clauses.append(f"{omitted} space(s) the layout gives an outage target are not in the platform file")
        where = "; ".join(clauses)
        raise RuntimeError(f"failover order: {where}, so the order of the owner's outage cannot be checked; {REFUSAL}")
    named = {provider.id: name for name, provider in config.providers.items()}  # the file's names, by id
    try:
        sought = tuple(named[ids.providers[name]] for name in owners if ids.providers[name] in named)  # the owners'
        found = failover.admit(config, sought, declared_mirrors(layout.spec, config, ids, owners))
    except failover.FailoverError as error:
        invalidate(layout.root)  # a record of an earlier run of the phase is no longer true of this one
        raise RuntimeError(f"{error}; {REFUSAL}") from None
    if not found:
        invalidate(layout.root)
        return None
    publish_private(Path(layout.root) / FILE, failover.record_text(found))
    return f"{FILE} written: a mirror is asked before the owner in each of {len(found)} space(s) in scope"

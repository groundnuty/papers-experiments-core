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

**What is written.** When something is in scope and every space passes, `failover-order.json` in the root
(`expcore_onedata.failover`): per space in scope its position label, its owner's and mirrors' provider labels, the
labels in the order of their ids, and `mirror_first: true`. Labels only: no provider's name, no space's name, no id. It
is evidence like `render-identity.json` (`hpcevidence.UP_FILES`), and the identity read reports it (`fleetidentity.py`).
A space that fails refuses the member, naming the spaces and providers by label, and removes a record an earlier run of
the phase left. The scope is read from the rendered file's data provider (`holder`) of each space, which nothing else
ties to the layout's owner, so a space of a declared owner whose file names another provider, or one whose file names a
declared provider and the layout another owner, is refused too (`holders_differ`), never left out of scope in silence.
"""

from pathlib import Path
from typing import Any, List, Optional, Tuple

import hpcsettings
from fedfacts import write_private

REFUSAL = "refuse this member"


def declared(spec: Any) -> Tuple[str, ...]:
    """The providers the layout declares an owner outage for (`[hpc] outage_targets`), checked against the layout's own
    providers; SettingsError when the key is malformed or names one the layout lacks. Empty: no outage declared."""
    return hpcsettings.outage_targets(spec.hpc, [provider.name for provider in spec.providers])


def holders_differ(spec: Any, config: Any, owners: Tuple[str, ...]) -> List[str]:
    """The position labels of the spaces whose data provider in the rendered platform file is not the owner the layout
    declares, among those an owner outage reaches by either: the layout's owner is declared, or the file's data provider
    is. The failover check reads the file's `holder`, the layout says who owns the space, and nothing else ties the two,
    so a space where they differ is refused rather than left out of scope with no word. A layout space the file does not
    hold (the link check's probe space) or one with no mirror is skipped."""
    holders = config.space_holders()
    targets = {config.provider_label(name) for name in owners if name in config.providers}
    entries = {entry.space.name: entry for entry in config.spaces}
    found = []
    for space in spec.spaces:
        entry = entries.get(space.name)
        if entry is None or not space.mirrors:
            continue
        held = holders.get(space.name)
        owner = config.provider_label(space.owner) if space.owner in config.providers else None
        if (space.owner in owners or held in targets) and held != owner:
            found.append(entry.position)
    return found


def admit(layout: Any) -> Optional[str]:
    """The failover-order admission of the member `layout` describes, once its platform file is rendered: None when no
    space is in scope (nothing is read, written or said), else the sentence `up` prints after writing the record.
    RuntimeError, naming spaces and providers by label, when a space's owner would be asked before every mirror, or
    when an id is not known, or when owners are declared and the layout has no platform file to read the ids from."""
    owners = declared(layout.spec)
    if not owners:
        return None
    from expcore_onedata import failover  # only where an outage is declared: up and its other commands stay as light
    from expcore_onedata.config import load_platform

    path = Path(layout.root) / failover.FILE
    if layout.spec.platform_template is None:
        raise RuntimeError(
            "failover order: the layout declares outage targets and no platform file to read the ids from"
        )
    config = load_platform(layout.platform)
    differing = holders_differ(layout.spec, config, owners)
    if differing:
        path.unlink(missing_ok=True)
        where = "; ".join(
            f"{label}: the platform file's data provider is not the layout's owner" for label in differing
        )
        raise RuntimeError(f"failover order: {where}, so the order of the owner's outage cannot be checked; {REFUSAL}")
    try:
        found = failover.admit(config, owners)
    except failover.FailoverError as error:
        path.unlink(missing_ok=True)  # a record of an earlier run of the phase is no longer true of this one
        raise RuntimeError(f"{error}; {REFUSAL}") from None
    if not found:
        path.unlink(missing_ok=True)
        return None
    write_private(path, failover.record_text(found))
    return f"{failover.FILE} written: a mirror is asked before the owner in each of {len(found)} space(s) in scope"

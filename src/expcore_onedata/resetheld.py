# Provenance: new
"""When the reset's quiet check runs, and the files it reads beyond a removed requirement's (O33, #209).

A removed requirement names the files a QoS replication may still be placing, but an agent that may manage QoS can add
a requirement and remove it before the run ends. Its replication creates no transfer document and no requirement is
left, so at reset time nothing shows that one is landing, and it can land after the eviction. So the quiet check does
not wait for a requirement to be found. **It runs, and why is recorded**, in this order:

- `requirements`: the reset removed a requirement;
- `rights`: the caller says (`qos_rights=True`) that a run since the last verified reset could have set one;
- `default`: the caller did not say (`qos_rights=None`), which fails safe.

It is skipped only for a caller that knows no run since the last verified reset could have set a requirement
(`qos_rights=False`), when the reset found none. The reason is judged at reset time, and the record gives it with the
quiet stage's seconds.

**What `qos_rights` means.** A study's reset runs at the start of each run and clears what earlier runs left. So the
question is whether **any run since the last verified reset** could have set a QoS requirement, never whether the run
about to start can: keyed on the upcoming condition, a read-only run that follows a writing run would skip the check
exactly when it is needed. `True` or `None` runs the check and is the safe choice; `False` only when the caller knows
that no run since the last verified reset had the right to manage QoS. `config.Condition.manages_qos` describes one
condition, so a caller that passes it combines it over every run since the last verified reset.

**What it covers.** The block maps of the files a removed requirement applied to, and of **the eviction plan's own
files**: the files the reset is about to evict from a provider other than the data provider, the ones a file path of
the plan names and every file the eviction itself lists under a directory path (`SOURCE`). They are read after the
eviction and again after the wait, since the eviction empties them and a replication that puts one back shows in its
map. No block map of the rest of the space is read, so the cost is bounded by the plan, one read per planned file on
each provider, twice, and not by the size of the space. **The occupancy is read again after the wait whenever the
stage runs**, a plan with no file included, and set against the occupancy stage's: a replication that lands on a file
that is not in the plan has no map read here, and only the occupancy shows it.

**How long it waits.** Twice the occupancy's lag (`resetqos.quiet_wait_s`) for the deepest file a late replication
could reach: the deepest regular file of the space, which the QoS scan already saw and costs no read of its own, since
a replication nothing names could reach any file; or, for a caller that knows no run could have set a requirement
(`qos_rights=False`), the deepest file a removed requirement applied to. The wait is 20 s for files directly under the
root, 30 s one directory down, and 10 s more for each further level.
"""

from collections.abc import Mapping

REQUIREMENTS, RIGHTS, DEFAULT = "requirements", "rights", "default"  # the reasons the quiet check runs, in order
SOURCE = "eviction plan"  # where the files whose block maps the quiet check reads come from, as the record says


def quiet_reason(removed: int, qos_rights: bool | None) -> str | None:
    """Why the quiet check runs, or None when it is skipped: `removed` requirements were removed at the reset, and the
    caller said whether any run since the last verified reset could have set one (`None`: it did not say)."""
    if removed:
        return REQUIREMENTS
    if qos_rights is None:
        return DEFAULT
    return RIGHTS if qos_rights else None


def quiet_depth(affected: Mapping[str, int], deepest: int, qos_rights: bool | None) -> int:
    """The depth the quiet wait is for: the deepest regular file of the space (`deepest`, from the scan), which a
    replication nothing names could reach, unless the caller said no run could have set a requirement, and then the
    deepest affected file."""
    depths = [*affected.values(), *([deepest] if qos_rights is not False else [])]
    return max(depths, default=1)


__all__ = ["DEFAULT", "REQUIREMENTS", "RIGHTS", "SOURCE", "quiet_depth", "quiet_reason"]

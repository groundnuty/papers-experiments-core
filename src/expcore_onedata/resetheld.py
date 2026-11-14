# Provenance: new
"""When the reset's quiet check runs, and the files it reads beyond a removed requirement's (O33, #209).

A removed requirement names the files a QoS replication may still be placing, but an agent that may manage QoS can add
a requirement and remove it before the run ends, and a cancel that missed leaves one file replicating whole
(`resetremove.py`, the residual). That replication creates no transfer document and no requirement is left, so at
reset time nothing shows that it is landing, and it can land after the eviction: at most one file's size, and rare. So
the quiet check does not wait for a requirement to be found. **It runs, and why is recorded**, in this order:

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
the plan names and every file the eviction itself lists under a directory path (`SOURCE`). They are read once, at one
instant T after the eviction: each (file, provider) pair the eviction emptied must still show nothing, and each file a
removed requirement applied to must show no more than the settle saw. No block map of the rest of the space is read, so
the cost is bounded by the plan, one read per evicted pair, and not by the size of the space. **The occupancy is read
whenever the stage runs**, a plan with no file included, and set against the occupancy stage's: a replication that
lands on a file that is not in the plan has no map read here, and only the occupancy shows it.

**What it proves, exactly: no replica of the plan's files, and no occupancy growth, had landed by T.** The occupancy
is read at T + the lag + a margin, when every placement that landed by T shows in it. A landing after T is not
certified: it may not show in the occupancy yet, and no map is read again.

**How long it waits.** From T, the occupancy's lag (`resetqos.occupancy_lag_s`) plus a margin
(`resetqos.QUIET_MARGIN_S`, 5 s) for the deepest file a late replication could reach: the deepest regular file of the
space, which the QoS scan already saw and costs no read of its own, since a replication nothing names could reach any
file; or, for a caller that knows no run could have set a requirement (`qos_rights=False`), the deepest file a removed
requirement applied to. The wait is 15 s for files directly under the root, 20 s one directory down, and 5 s more for
each further level. **The figure it holds the occupancy to is read after a wait of its own** (O45): the occupancy
stage waits the same lag and margin from the eviction stage's end, for the deepest regular file of the space whatever
`qos_rights` says, since an eviction's change lags as a placement's does (`resetqos`, the 14 Nov live slot). Without
it the figure could still count bytes already evicted, and a growth up to their size would pass here.
"""

from collections.abc import Iterable, Mapping

from expcore_onedata.placement import Range

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


def expected_maps(
    settled: Mapping[tuple[str, str], tuple[Range, ...]], evicted: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], tuple[Range, ...]]:
    """The block maps the quiet stage holds the files to, by (file id, provider): what the settle ended on for a file
    a removed requirement applied to, and nothing for each pair the eviction verified empty."""
    return {**settled, **dict.fromkeys(evicted, ())}


def quiet_depth(affected: Mapping[str, int], deepest: int, qos_rights: bool | None) -> int:
    """The depth the quiet wait is for: the deepest regular file of the space (`deepest`, from the scan), which a
    replication nothing names could reach, unless the caller said no run could have set a requirement, and then the
    deepest affected file."""
    depths = [*affected.values(), *([deepest] if qos_rights is not False else [])]
    return max(depths, default=1)


__all__ = ["DEFAULT", "REQUIREMENTS", "RIGHTS", "SOURCE", "expected_maps", "quiet_depth", "quiet_reason"]

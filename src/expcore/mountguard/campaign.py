# Provenance: new
"""The data path of each level of a study's data-path factor, as the campaign's own fingerprint records it (O75).

A run of a campaign that mixes data paths carries the study's word for its path (its data-path factor's level, such as
`rest-arm`; the factor is `data_path` unless `[study] data_path_factor` names another), and only the platform's file
maps the word to a kind: `mount`, `rest` or `none`. The platform puts the
mapping in the campaign fingerprint (`[sandbox.data_paths]`, `data_paths` under `platform`, each level's `kind`), which
a campaign records once, verifies at every resume and every merge, and the analysis reads back. That is the campaign's
own record of which of its runs had a mount, and the one place a run that has no mount-guard snapshot can be shown to
have had no mount from. `recorded_data_paths(fingerprint)` returns the kind of each level the fingerprint records, or
None when it records none; an entry that is not one of the three kinds is left out, so its level is unknown.
"""

from collections.abc import Mapping
from typing import Any

from expcore.mountguard.instrument import MOUNT_PATH, NO_MOUNT_PATHS

KINDS = (MOUNT_PATH, *NO_MOUNT_PATHS)


def recorded_data_paths(fingerprint: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The kind (`mount`, `rest` or `none`) of each level of the data-path factor the campaign fingerprint records;
    None when it records no data paths."""
    platform = None if fingerprint is None else fingerprint.get("platform")
    paths = platform.get("data_paths") if isinstance(platform, Mapping) else None
    if not isinstance(paths, Mapping):
        return None
    found = {}
    for level, entry in paths.items():
        kind = entry.get("kind") if isinstance(entry, Mapping) else None
        if isinstance(level, str) and kind in KINDS:
            found[level] = kind
    return found

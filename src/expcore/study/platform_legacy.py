# Provenance: new
"""The one case in which a start presents a fingerprint other than its campaign's (C06): a store that recorded the
platform's half in an earlier form of the same settings (`StudyPlugin.platform_legacy`).

The Onedata layer's spaces were fingerprinted by their Onezone id until a space could declare its data
(`data_manifest`); a space that declares it is fingerprinted by its declared identity and its data's content hash
instead, so two federations holding the same data are one experimental condition. A store started before its spaces
declared their data recorded the id-bearing form, and resumes under that recorded fingerprint, unchanged, when it is
exactly the campaign's with the platform's half in that form. Nothing else is accepted: any other difference is the
store's to refuse, as at every resume, and a merge compares the shards' recorded fingerprints as they are, so a store
of the earlier form never merges with one of the later.
"""

import sys
from collections.abc import Mapping
from typing import Any

from expcore.campaign.manifest import compare_fingerprints


def legacy_fingerprint_of(
    recorded: Mapping[str, Any] | None, fingerprint: dict[str, Any], legacy: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The fingerprint a start presents: the campaign's, or the recorded one when it is exactly the campaign's with
    the platform's half in its earlier form `legacy`."""
    if recorded is None or legacy is None:
        return fingerprint
    earlier = {**fingerprint, "platform": dict(legacy)}
    if compare_fingerprints(recorded, fingerprint).same or not compare_fingerprints(recorded, earlier).same:
        return fingerprint
    print("run: the store recorded its spaces in their earlier form; it resumes under that form", file=sys.stderr)
    return earlier


__all__ = ["legacy_fingerprint_of"]

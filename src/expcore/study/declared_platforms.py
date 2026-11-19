# Provenance: new
"""The platforms a campaign declares its members may run on (C50).

One campaign's members may run on federations whose platforms differ in what shapes a run: a Docker and an Apptainer
sandbox above all, since a site's backend is a site attribute recorded per run, not the experiment's identity (the
study's ruling of 19 Nov). Such a campaign declares the set it accepts in campaign.toml, `platform_fingerprints`, each
entry the SHA-256 of one platform's fingerprint (`StudyPlugin.platform`, hashed as `manifest.fingerprint_sha256` hashes
a fingerprint; for an HPC federation, its render identity's `platform_fingerprint_sha256`).

- The campaign fingerprint holds the sorted set in the platform's place (`campaign_platform`), so every member has one
  campaign fingerprint whichever declared platform it runs on, and a store, a lease and a merge compare it as before.
- A start on a platform the set does not declare is refused before any run (`require_declared`).
- A member's own platform is `own_sha256`: an elastic federation joins with it, and the ledger keeps it as the identity
  each of its runs ran under.

A campaign that declares none is what it was: the platform's own fingerprint is the section, byte for byte.
"""

from collections.abc import Mapping
from typing import Any

from expcore.campaign.manifest import fingerprint_sha256
from expcore.config.loading import Section

KEY = "platform_fingerprints"  # campaign.toml's
DECLARED = "declared_sha256"  # the campaign fingerprint's platform section, when a set is declared


def own_sha256(platform: Mapping[str, Any]) -> str:
    """The SHA-256 of a platform's own fingerprint, as campaign.toml declares it."""
    return fingerprint_sha256(platform)


def campaign_platform(declared: tuple[str, ...], platform: Mapping[str, Any]) -> Mapping[str, Any]:
    """The platform's section of the campaign fingerprint: the declared set, sorted, or with none the platform's own."""
    return {DECLARED: sorted(declared)} if declared else platform


def require_declared(declared: tuple[str, ...], platform: Mapping[str, Any], source: str) -> None:
    """Refuse, naming campaign.toml's key, a platform whose fingerprint the declared set does not hold; nothing when
    the campaign declares none."""
    if declared and own_sha256(platform) not in declared:
        raise Section({}, source, "top level").fail(
            KEY,
            f"does not declare this federation's platform fingerprint {own_sha256(platform)}: declare it before the "
            "campaign is created, or run on a declared platform",
        )

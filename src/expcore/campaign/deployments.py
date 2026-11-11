# Provenance: new
"""The deployment shares of the models that declare accepted fingerprints (B18), for the export and the report.

A load-balanced endpoint may serve a model from several deployments the study declared the same model
(`expcore.campaign.identity`). Each attempt of such a model records the turns each fingerprint served; this counts,
over the analysed runs, the turns and the runs each fingerprint served and their fractions, and says which declared
fingerprint the admission probe or a run confirmed and which no reply has shown. A reader can then see how much of the
data each deployment produced, and whether a deployment the study declared was ever seen at all.
"""

import collections
import json
from collections.abc import Iterable, Mapping
from typing import Any

from expcore.models.served import MIXED_FINGERPRINTS, is_mixed

ACCEPTED_META = "accepted_fingerprints"  # the store's meta key of the declared sets and their confirmations


class DeploymentsUnreadable(ValueError):
    """The store's record of the declared sets, or an attempt's deployment mix, is not what the core writes."""


def deployment_shares(declared: Mapping[str, Any], analysed: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
    """Return one entry per model that declares a set, in model order.

    `declared` is the store's record of the sets (model: {"accepted": [...], "confirmed": {fingerprint: witness}}),
    `analysed` the model and the analysed attempt row of each analysed run. A fraction whose denominator is 0 is None.
    """
    by_model: dict[str, list[Any]] = collections.defaultdict(list)
    for model, attempt in analysed:
        by_model[model].append(attempt)
    try:
        return [_model_shares(model, entry, by_model[model]) for model, entry in sorted(declared.items())]
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise DeploymentsUnreadable(f"the deployment record cannot be read ({type(error).__name__}: {error})") from None


def _model_shares(model: str, entry: Mapping[str, Any], attempts: list[Any]) -> dict[str, Any]:
    accepted, confirmed = list(entry["accepted"]), dict(entry["confirmed"])
    mixes = [json.loads(a["deployments"]) for a in attempts if a["deployments"] is not None]
    turns: collections.Counter[str | None] = collections.Counter()
    runs: collections.Counter[str | None] = collections.Counter()
    mixed = 0
    for pairs in mixes:
        reported = [fingerprint for fingerprint, _ in pairs if fingerprint is not None]
        mixed += len(reported) > 1 or any(is_mixed(f) for f in reported)
        for fingerprint, served in pairs:
            turns[fingerprint] += int(served)
            runs[fingerprint] += 1
    total = sum(turns.values())
    names = sorted(set(accepted) | set(turns), key=lambda f: (f is None, f not in accepted, f or ""))
    return {
        "model": model,
        "accepted_fingerprints": accepted,
        "analysed_runs": len(attempts),
        "runs_without_mix": len(attempts) - len(mixes),
        "mixed_runs": mixed,
        "turns": total,
        "fingerprints": [
            {
                "fingerprint": name,
                "accepted": name is not None and all(p in accepted for p in name.split(MIXED_FINGERPRINTS)),
                "confirmed_by": confirmed.get(name, {}).get("by"),
                "confirming_run": confirmed.get(name, {}).get("run"),
                "turns": turns[name],
                "turn_share": turns[name] / total if total else None,
                "runs": runs[name],
                "run_share": runs[name] / len(mixes) if mixes else None,
            }
            for name in names
        ],
    }

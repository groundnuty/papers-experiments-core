# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/health.py, blob d841c16205d7b5b545c0186eec9788b9965880e6
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The campaign's Onedata health check (`health_check() -> (ok, detail)`), run before and after every run.

Read-only, with a scoped short-lived token and short timeouts. It runs on the HOST with its own client (never through a
run's gate, sidecar or network namespace), and the scheduler calls it outside `execute_run`, so its time and bytes can
never enter a run's wall time, byte counter or time cap. The file it reads (a 1982 day) lies outside every instance's
required extent, so it cannot warm anything a run will read (asserted against `dataset_76.instances` in the tests). It checks, in order: (1) the study space is supported by
fr ONLY (Onezone's view of the token's scope), (2) fr answers, (3) one known S1 file read back through fr has the
sha256 of #3's manifest. Any failure, including an exception, is `(False, reason)`: infra by the #16 rule, never a crash.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import required_token_ttl_s
from .real_sandbox import ONEZONE_HOST, PROVIDER_HOST, S1_DIR, SPACE_ID, SPACE_NAME, mint_token

REPO = Path(__file__).resolve().parents[2]
FR_PROVIDER_ID = json.loads((REPO / "data" / "upload_record.json").read_text())["provider_id"]
MANIFEST = REPO / "data" / "manifest" / "s1_sha256.txt"
KNOWN_FILE = "dataset-97.nc"           # the first S1 day, about 190 kB: a whole-file read is cheap
TIMEOUT_S = 15.0
TOKEN_CAP_S = 300.0                       # the check's own wall budget; the token outlives it plus the margin

_cache: dict[str, Any] = {"token": None, "exp": 0.0}
_lock = threading.Lock()


def _token(mint: Callable) -> str:
    with _lock:
        if _cache["token"] is None or time.time() > _cache["exp"] - 120:
            _cache["token"] = mint(oneclient=False, time_cap_s=TOKEN_CAP_S, service=True)[0]      # default lifetime: cap + margin
            _cache["exp"] = time.time() + required_token_ttl_s(TOKEN_CAP_S)
        return _cache["token"]


def expected_sha(name: str = KNOWN_FILE, manifest: Path = MANIFEST) -> str:
    for line in manifest.read_text().splitlines():
        h, _, n = line.partition("  ")
        if n.strip() == name:
            return h
    raise KeyError(f"{name} not in {manifest}")


def health_check(*, post: Callable | None = None, get: Callable | None = None,
                 mint: Callable | None = None, manifest: Path = MANIFEST) -> tuple[bool, str]:
    t0 = time.monotonic()
    try:
        import requests
        post, get, mint = post or requests.post, get or requests.get, mint or mint_token
        tok = _token(mint)
        h = {"x-auth-token": tok}
        r = post(f"https://{ONEZONE_HOST}/api/v3/onezone/tokens/infer_access_token_scope", json={"token": tok}, timeout=TIMEOUT_S)
        if r.status_code != 200:
            return False, f"onezone scope inference HTTP {r.status_code}"
        spaces = r.json()["dataAccessScope"]["spaces"]
        sp = spaces.get(SPACE_ID)
        if not sp or sp.get("name") != SPACE_NAME:
            return False, f"study space {SPACE_NAME} not in the token's scope"
        if sorted(sp.get("supports", [])) != [FR_PROVIDER_ID]:
            return False, f"study space supported by {sorted(sp.get('supports', []))}, expected only fr ({FR_PROVIDER_ID})"
        base = f"https://{PROVIDER_HOST}/api/v3/oneprovider"
        r = post(f"{base}/lookup-file-id/{SPACE_NAME}/{S1_DIR}/{KNOWN_FILE}", headers=h, timeout=TIMEOUT_S)
        if r.status_code != 200:
            return False, f"fr lookup of {KNOWN_FILE} HTTP {r.status_code}"
        fid = r.json()["fileId"]
        r = get(f"{base}/data/{fid}/content", headers={**h, "Range": "bytes=0-"}, timeout=TIMEOUT_S)
        if r.status_code not in (200, 206):
            return False, f"fr read of {KNOWN_FILE} HTTP {r.status_code}"
        got = hashlib.sha256(r.content).hexdigest()
        if got != expected_sha(manifest=manifest):
            return False, f"{KNOWN_FILE} sha256 differs from the manifest"
        return True, f"ok: space supported by fr only, {KNOWN_FILE} sha256 ok ({time.monotonic() - t0:.1f}s)"
    except Exception as e:  # noqa: BLE001 -- a check that cannot run is a failed check
        return False, f"health check raised {type(e).__name__}: {str(e)[:200]}"

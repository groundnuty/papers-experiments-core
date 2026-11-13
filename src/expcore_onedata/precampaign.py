# Provenance: new
"""What is checked once before a campaign admits its first run: the content type the data gate streams on (O05, O11).
The provider links (O35, `linkcheck.py`) are checked at the same first call, but by `health.PlatformHealth` itself and
not kept here: a stuck link is remedied while the campaign waits, so its check runs again until it passes.

The data gate streams only a file's content, an answer of 200 or 206 from the content endpoint typed
`application/octet-stream` (`sandbox/gateboundary.py`); every other answer it buffers whole, up to 4 MiB, and refuses
above that. Onedata 25.0 answers a file's content with that type (`[L1]` in `client.py`), but a deployment of another
release may not: if it does not, every read over 4 MiB in a REST condition is refused mid-campaign and its run set
aside, so the failures cluster in one condition. So before the first run the type is measured, and a campaign whose
REST reads would be cut is refused instead of run.

`content_type_check` reads the provisioned file `[sandbox] content_check_file` through the agent's local provider, the
one the gate forwards to: whole, which must be answered 200, and its first byte by range, which must be answered 206,
both typed as the gate requires. Only the heads are read (`OnezoneClient.content_answer`), never the body. With no data
path over REST the gate streams nothing, so nothing is read and the campaign is admitted. A type that differs, or one
that cannot be measured (the file is missing, the provider fails), refuses the campaign: unknown is not a pass. The
detail names the problem, the provider by its position and the type measured, never a configured value.
`measure_content_type` says besides whether a type was measured at all, so the day-0 check can report a check that
measured nothing as unknown, never as passed, while the campaign is still admitted when nothing streams.

Where it runs. The core has no hook at a campaign's start, and a study's `build` must have no side effects, so the
check runs at the first call of the health gate, which the core makes before any attempt starts. The platform's
standard health check composes it itself (`health.PlatformHealth`), with no opt-out: `PreCampaignHealth` wraps the
space check, runs the checks once, and from then on either answers as the space check does or, when a check refused,
answers that refusal at every call, without measuring again. The core then holds every run
(a closed health gate, C11) and records the refusal as the outage's detail; the attempt whose pre-run check found it is
recorded as that outage's, as for any failed pre-run check. The same check is the day-0 check `content_type`, which an
operator runs before starting a campaign, and which says failed, passed or unknown.

Its time. The first call makes a few requests in sequence: the space's root, a listing page per part of the file's
path, the gate's walk from the file up to the root before the first read (the root as the gate reads it, and one parent
link per level of the path; the second read finds it kept), the space's name before each read, and each read's head.
Each is bounded by the client's timeout (15 s by default). The core's `health_timeout_s` (60 s by default) should exceed
their sum, and the link check's bounds when platform.toml has a `[link_check]` (`linkcheck.py`, "Where it runs"); a
first call that outlives it reads unhealthy once, closing the gate, and the verdict it keeps when it ends answers every
call after it.
"""

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from expcore.campaign.healthgate import Health, HealthCheck, read_health
from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.provision import LISTING_ATTRIBUTES, LISTING_LIMIT, listing_entry, next_page_token
from expcore_onedata.rangemap import safe_path
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.gateboundary import DATA_CONTENT_TYPE, streams

CHECK_KEY = "[sandbox] content_check_file"
REFUSED = "the campaign is refused before any run starts"
NOTHING_STREAMS = (
    "no data path is REST (no [sandbox], or none of its levels is rest), so nothing was measured: the data gate "
    "streams nothing, so there is no content type to check"
)
_SHOWN_TYPE = re.compile(r"[^A-Za-z0-9/.+;=_ -]")


def _shown(content_type: str | None) -> str:
    """A measured content type as a message may hold it: plain characters, short; 'none' for no header."""
    return "none" if content_type is None else _SHOWN_TYPE.sub("?", content_type)[:80]


def _child(client: OnezoneClient, provider: str, directory: str, name: str) -> str:
    """The file id of an entry of a directory, following its pages; LookupError when it is not there."""
    params: dict[str, Any] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
    while True:
        page = client.get(f"/data/{directory}/children", provider=provider, params=params, retry=ONE_ATTEMPT)
        token = next_page_token(page)  # a page without its last-page marker is refused, never the end (O26)
        for item in page["children"]:
            entry = listing_entry(item)
            if entry.name == name:
                return entry.file_id
        if token is None:
            raise LookupError("the check file is not in the space")
        params = {**params, "token": token}


@dataclass(frozen=True)
class ContentTypeVerdict:
    """What the content-type check found: whether it measured a type at all, whether a campaign may start, and why.

    `measured` is False when nothing was read (no REST data path, no check file) or the reads failed; `ok` is True
    only when a type was measured and is the one the gate streams on, or when there is nothing for the gate to stream.
    """

    measured: bool
    ok: bool
    detail: str

    @property
    def health(self) -> Health:
        """The verdict as the health gate reads it."""
        return Health(self.ok, self.detail)


def measure_content_type(config: PlatformConfig, client: OnezoneClient) -> ContentTypeVerdict:
    """Measure the content type of a file's content, whole and by range, on the local provider; never raise."""
    sandbox = config.sandbox
    if sandbox is None or DataPathMode.REST not in sandbox.data_paths.values():
        return ContentTypeVerdict(False, True, NOTHING_STREAMS)
    if sandbox.content_check_file is None:
        return ContentTypeVerdict(
            False, False, f"{REFUSED}: {CHECK_KEY} is not set, so the content type the gate streams on is unknown"
        )
    # The file lies under the root of the first space the data path's token reaches: the only space, or with several
    # (`[[spaces]]`) the first entry whose `tokens` hold the data path, read through the agent's one local provider.
    local, space_id = config.local_provider.name, config.space_ids("data_path")[0]
    label = client.provider_label(local)
    try:
        node = consistency.space_root(client, local, space_id, tries=1, retry=ONE_ATTEMPT)
        for part in safe_path(sandbox.content_check_file):
            node = _child(client, local, node, part)
        whole = client.content_answer(f"/data/{node}/content", provider=local, first_byte=False)
        part = client.content_answer(f"/data/{node}/content", provider=local, first_byte=True)
    except (PlatformError, LookupError, ValueError) as error:  # their messages hold labels and positions, no value
        return ContentTypeVerdict(
            False, False, f"{REFUSED}: the content type of {CHECK_KEY} on {label} could not be measured ({error})"
        )
    except Exception as error:
        return ContentTypeVerdict(
            False, False, f"{REFUSED}: measuring the content type on {label} raised {type(error).__name__}"
        )
    problems = [
        f"a {what} was answered {status} typed {_shown(content_type)}"
        for what, expected, (status, content_type) in (("whole read", 200, whole), ("read by range", 206, part))
        if status != expected or not streams(status, "content", content_type)
    ]
    if problems:
        return ContentTypeVerdict(
            True,
            False,
            f"{REFUSED}: on {label}, {'; '.join(problems)}, not {DATA_CONTENT_TYPE} with 200 and 206, the only "
            "answer the data gate streams, so every REST read over 4 MiB would be refused mid-campaign",
        )
    return ContentTypeVerdict(
        True, True, f"a file's content on {label} is {DATA_CONTENT_TYPE}, whole (200) and by range (206)"
    )


def content_type_check(config: PlatformConfig, client: OnezoneClient) -> Health:
    """The pre-campaign check as the health gate reads it (`measure_content_type`); never raise."""
    return measure_content_type(config, client).health


class PreCampaignHealth:
    """A `HealthCheck` that runs the pre-campaign checks at its first call and then the platform's own.

    The checks run once, in order, until one refuses; their verdict is kept for the campaign. While it is a refusal
    every call answers it and the platform's check is not asked; otherwise every call answers as the platform's check.
    """

    def __init__(self, health: HealthCheck, *checks: Callable[[], Health]) -> None:
        self._health, self._checks = health, checks
        self._verdict: Health | None = None
        self._lock = threading.Lock()

    def _first(self) -> Health:
        for check in self._checks:
            verdict = read_health(check)  # a check that raises or answers nonsense is a refusal, never a pass
            if not verdict.ok:
                return verdict
        return Health(True, "the pre-campaign checks passed")

    def __call__(self) -> Health:
        with self._lock:
            if self._verdict is None:
                self._verdict = self._first()
            verdict = self._verdict
        if not verdict.ok:
            return Health(False, verdict.detail if REFUSED in verdict.detail else f"{REFUSED}: {verdict.detail}")
        return self._health()


__all__ = ["CHECK_KEY", "ContentTypeVerdict", "PreCampaignHealth", "content_type_check", "measure_content_type"]

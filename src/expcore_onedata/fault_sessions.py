# Provenance: new
"""The session gate of a restore: the restored provider's peers can reach it before its links are checked (O35).

After a long outage the peers' sessions toward a restored provider renew on a backoff that reaches 180 s (op-worker
f118ed9, outgoing_connection_manager.erl; inference from source, not measured), and until a peer's outgoing session is
up, a fetch by that peer from the restored provider raises in the rtransfer callback `get_connection_secret`, which can
crash the peer's link and strand a third pair. So a restore checks the links only once this gate passes, polled by
`fault_recovery.Recovery` every `poll_s` for at most `[faults] gate_timeout_s`, failing closed (`sessions_not_ready`).
One reading (`sessions`) passes when both hold:

1. **Onezone shows every provider of the probe space online** (`GET /providers/{id}` with the admin credential, its
   `online` true; `[link_check].providers`).
2. **The block map of the gate's probe file, read through each peer, has a success entry for the restored provider.**
   A provider answers `/data/{id}/distribution` by asking every provider of the space through its outgoing session to
   it (data_distribution.erl, provider_rpc.erl) and keeps each one's error as that provider's entry, `success` false
   with an `error`. That session is the one `get_connection_secret` uses, so the entry is a REST-only proof that the
   peer's fetch from the restored provider will not raise; it is also passive (finding the session connects nothing).
   An entry that is missing, not a table, has `success` false or an `error`, or lacks its storage table is not ready.
   The read through the restored provider itself is not made: it starts with no sessions and opens them as it sends.

**The probe file predates the injection.** A file made during the outage may be unknown to the restored provider, and
its entry would then be an error for dbsync's lag, not for the session; the error does not tell the two apart, so the
file must. `arm`, which the injector calls before it journals and stops anything, finds or writes one small file of the
probe space, `GATE_PROBE_NAME` (no other check writes or deletes it), through the local provider, and waits until every
provider of `[link_check].providers` sees it at its size; its id goes into the injection's journal line, so a later
process settling the injection reads the same file. An injection journalled without one fails the gate closed at once
(`GateUnarmed`): no later file can stand in for it. Without `[link_check]` no provider has a peer to wait for, and the
gate passes with nothing read.
"""

import math
from collections.abc import Callable, Mapping
from typing import Any

from expcore.config.loading import ConfigError
from expcore_onedata import consistency
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import PlatformConfig
from expcore_onedata.provision import LISTING_ATTRIBUTES, LISTING_LIMIT, Entry, listing_entry, next_page_token
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.streamed import OCTET_STREAM

GATE_PROBE_NAME = "expcore-session-probe"
GATE_PROBE_BYTES = b"expcore session gate probe\n"
DISTRIBUTION_TIMEOUT_S = 45.0  # above a peer's 30 s wait on a provider that never answers: its error entry is read
ZONE_TIMEOUT_S = 10.0


class GateUnarmed(ConfigError):
    """The injection has no gate probe file journalled, so no reading can show its provider's peers reaching it."""


def await_sessions(
    driver: Any,
    service: str,
    gate_probe: str | None,
    timeout_s: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Poll the driver's session gate (`ComposeDriver.sessions_ready`) for a service every `poll_s` until it passes or
    `timeout_s` is over; return the reading for the snapshot: `ok`, `how` (`ready`, `not_ready`, `unarmed`, or `none`
    for a driver with no gate), `waited_s` and the last `detail`. A reading that raises is no pass: it is read again,
    and its type is the detail; `GateUnarmed` ends the wait at once, closed."""
    read = getattr(driver, "sessions_ready", None)
    if read is None:
        return {"ok": True, "how": "none", "waited_s": 0.0, "detail": None}
    started, detail = clock(), None
    polls = max(1, math.ceil(timeout_s / poll_s)) + 1
    for poll in range(polls):
        if poll:
            sleep(poll_s)
        try:
            detail = read(service, gate_probe)
        except GateUnarmed as error:
            return {"ok": False, "how": "unarmed", "waited_s": round(clock() - started, 3), "detail": str(error)}
        except Exception as error:  # a reading that cannot answer is no pass, never a pass
            detail = f"the gate's reading raised {type(error).__name__}"
        else:
            if detail is None:
                return {"ok": True, "how": "ready", "waited_s": round(clock() - started, 3), "detail": None}
        if clock() - started >= timeout_s:
            break
    return {"ok": False, "how": "not_ready", "waited_s": round(clock() - started, 3), "detail": detail}


def _success(entry: Any) -> bool:
    """Whether a provider's entry of a block map is an answer, not the error a peer kept for it."""
    return (
        isinstance(entry, Mapping)
        and entry.get("success") is not False
        and "error" not in entry
        and isinstance(entry.get("distributionPerStorageBackend"), Mapping)
    )


class SessionGate:
    """The session gate's readings through the platform's admin client (see the module docstring)."""

    def __init__(self, config: PlatformConfig, client: OnezoneClient) -> None:
        self._config, self._client = config, client

    def _label(self, name: str) -> str:
        return self._config.provider_label(name)

    def _root(self, provider: str, space_id: str) -> str:
        return consistency.space_root(self._client, provider, space_id, tries=3, retry=ONE_ATTEMPT)

    def _find(self, provider: str, root: str) -> Entry | None:
        """The gate's probe file as `provider` lists the probe space's root, or None when it lists none."""
        params: dict[str, object] = {"limit": LISTING_LIMIT, "attribute": LISTING_ATTRIBUTES}
        while True:
            page = self._client.get(f"/data/{root}/children", provider=provider, params=params, retry=ONE_ATTEMPT)
            token = next_page_token(page)
            for item in page["children"]:
                entry = listing_entry(item)
                if entry.name == GATE_PROBE_NAME:
                    return entry
            if token is None:
                return None
            params = {**params, "token": token}

    def arm(self) -> str | None:
        """Find or write the gate's probe file through the local provider and wait until every provider of the probe
        space sees it at its size; return its id, or None without `[link_check]`. Raise PlatformError (or a wait's
        timeout) when it cannot: the injector then stops nothing."""
        settings = self._config.link_check
        if settings is None:
            return None
        space, local = settings.space, self._config.spaces[0].support.local
        root = self._root(local, space.id)
        found = self._find(local, root)
        file_id, size = (found.file_id, found.size) if found is not None else (None, 0)
        if file_id is None:
            made = self._client.mutate(
                "POST",
                f"/data/{root}/children",
                provider=local,
                params={"name": GATE_PROBE_NAME, "type": "REG"},
                space=space,
                gate_retry=ONE_ATTEMPT,
            )
            file_id = made.get("fileId") if isinstance(made, dict) else None
            if not isinstance(file_id, str) or not file_id:
                raise PlatformError(f"{self._label(local)} answered the gate probe's creation with no file id")
        if size == 0:  # made now, or made by an arming that died before its write: the file is the gate's alone
            self._client.mutate(
                "PUT",
                f"/data/{file_id}/content",
                provider=local,
                params={"offset": 0},
                content=GATE_PROBE_BYTES,
                content_type=OCTET_STREAM,
                space=space,
                gate_retry=ONE_ATTEMPT,
            )
            size = len(GATE_PROBE_BYTES)
        for provider in settings.providers:
            consistency.wait_visible(
                self._client,
                provider,
                file_id,
                size=size,
                timeout_s=settings.visible_timeout_s,
                retry=ONE_ATTEMPT,
            )
        return file_id

    def sessions(self, provider: str, gate_probe: str | None) -> str | None:
        """One reading of the gate for `provider`, restored: None when it passes, else why not, naming labels only.
        Raise GateUnarmed when the injection journalled no probe file, which no wait changes."""
        settings = self._config.link_check
        if settings is None:
            return None
        if gate_probe is None:
            raise GateUnarmed("the injection journalled no gate probe file, so its peers' sessions cannot be shown")
        for name in settings.providers:
            provider_id = self._config.providers[name].id
            if not provider_id:
                return f"{self._label(name)} has no provider id declared, so Onezone's view of it cannot be read"
            try:
                seen = self._client.get(f"/providers/{provider_id}", timeout_s=ZONE_TIMEOUT_S, retry=ONE_ATTEMPT)
            except PlatformError as error:
                return f"Onezone's view of {self._label(name)} could not be read ({error})"
            if not isinstance(seen, Mapping) or seen.get("online") is not True:
                return f"Onezone does not show {self._label(name)} online"
        restored_id = self._config.providers[provider].id
        for peer in (name for name in settings.providers if name != provider):
            try:
                answer = self._client.get(
                    f"/data/{gate_probe}/distribution",
                    provider=peer,
                    timeout_s=DISTRIBUTION_TIMEOUT_S,
                    retry=ONE_ATTEMPT,
                )
            except PlatformError as error:
                return f"the gate probe's block map through {self._label(peer)} could not be read ({error})"
            table = answer.get("distributionPerProvider") if isinstance(answer, Mapping) else None
            entry = table.get(restored_id) if isinstance(table, Mapping) else None
            if not _success(entry):
                return f"{self._label(peer)} holds an error, or no entry, for {self._label(provider)} in a block map"
        return None

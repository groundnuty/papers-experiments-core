"""Test support for placement: a clock that advances by sleeping, the `[placement]` table, and a seeded data file.

Every name is a test name (`.test` hosts, `alpha` and `beta`), never a federation's.
"""

from pathlib import Path
from typing import Any

import httpx
from fake_data import PROVIDER
from platform_support import ALPHA_ID, BETA_ID, TOOLS, write_platform

from expcore_onedata.client import AUTH_HEADER

LATER = 2_000_000_000  # a time caveat far in the future
SETTINGS = {
    "holder": "alpha",
    "poll_s": 1.0,
    "transfer_timeout_s": 30.0,
    "settle_timeout_s": 20.0,
    "consecutive_polls": 2,
    "request_timeout_s": 30.0,
    "chunk_bytes": 1024,
    "probe_attempts": 3,
    "probe_backoff_s": 0.0,
}
DATA = bytes(range(256)) * 16  # 4096 bytes, so every offset has its own value


__all__ = [
    "ALPHA_ID",
    "BETA_ID",
    "DATA",
    "SETTINGS",
    "Ticker",
    "tool_plane_replicates",
    "with_placement",
    "write_with_placement",
]


def tool_plane_replicates(fake: Any, client: Any, path: str, target_id: str) -> str:
    """The agent's replication through the tool plane: the tool server posts the job to the provider with its own
    user's token, as schedule_file_replication does, and nothing of the harness sends it. Return the job's id."""
    token = client.mint_own_token(TOOLS, [{"type": "time", "validUntil": LATER}])
    request = httpx.Request(
        "POST",
        f"https://alpha.test{PROVIDER}/transfers",
        headers={AUTH_HEADER: token},
        json={
            "type": "replication",
            "replicatingProviderId": target_id,
            "dataSourceType": "file",
            "fileId": fake.id_of(path),
        },
    )
    answer = fake.respond(request)
    assert answer.status_code == 201
    return answer.json()["transferId"]


class Ticker:
    """A clock that only moves when something sleeps, so a bounded wait runs instantly and exactly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def with_placement(data: dict[str, Any], **override: Any) -> None:
    """Edit a platform document: add a `[placement]` table with the test settings."""
    data["placement"] = {**SETTINGS, **override}


def write_with_placement(tmp_path: Path, rows: str = "", **override: Any) -> Path:
    """Write a platform.toml with a `[placement]` table and, after it, raw TOML rows (such as `[[placement.map]]`)."""
    path = write_platform(tmp_path, lambda d: with_placement(d, **override))
    if rows:
        path.write_text(path.read_text(encoding="utf-8") + rows, encoding="utf-8")
    return path

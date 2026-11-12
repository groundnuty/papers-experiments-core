# Provenance: new
"""The test world around the fakes: one study's names, its `platform.toml`, a clock and the federation that answers.

Every name here is invented (`.test` hosts, `login-*` users, the space `study-a-data`, the providers `alpha` and
`beta`), never a federation's. `write_platform` renders the default `platform.toml`, lets a test edit the parsed
dictionary, and writes it as TOML, so a test changes one key and sees one error. `make_federation` builds the
federation whose ids, hosts, users and privileges that document names, and `Ticker` is the clock the platform layer's
waits and probes take, which moves only when something sleeps.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from expcore_onedata.client import AUTH_HEADER, OnezoneClient
from expcore_onedata.credentials import Principal
from expcore_onedata.testing.fake_data import ERRORS_25_0, PROVIDER, FakeData
from expcore_onedata.testing.fake_federation import FakeFederation

NOW = 1_800_000_000.0  # the issuer's clock in tests
CEILING_S = 600.0  # a run's whole-time ceiling in tests
ADMIN_ENV = "PLATFORM_TEST_ADMIN"  # the variable `platform.toml` names for the admin credential: `fake.admin_token`
SPACE_NAME = "study-a-data"
SPACE_PREFIX = "study-a-"
ALPHA_ID, BETA_ID = "prmock0001", "pr-beta"  # the ids the fake federation reports for the two providers
BASELINE = ["space_view", "space_read_data"]
MANAGE = [*BASELINE, "space_manage_qos"]
# The test users: login name -> (password, Onezone user id). The password files are written by `write_platform`.
USERS = {
    "login-data": ("pw-data-0001", "u-data"),
    "login-tools": ("pw-tools-0001", "u-tools"),
    "login-svc": ("pw-svc-0001", "u-svc"),
}
DATA_PATH_USER, TOOL_SERVER_USER, SERVICE_USER = (
    Principal(name, USERS[name][1]) for name in ("login-data", "login-tools", "login-svc")
)
CREDENTIALS_DIR = "credentials"
TOOL_SERVER_ARGS = ["-m", "expcore_onedata.testing.fake_onedata_mcp"]  # with `sys.executable`: the stand-in server
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


class Ticker:
    """A clock that only moves when something sleeps, so a bounded wait runs instantly and exactly.

    Models: one epoch clock (`clock`, from 1000.0) and the sleep that advances it (`sleep`, which records the
    seconds in `slept`); give both to what takes `now=` and `sleep=`, and to `FakeData.clock`, so a job's times, a
    provider's `Date` header and the probes' window agree. Does not model: real time, other threads (it is not
    synchronised) or a clock that moves by itself.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def add_users(federation: FakeFederation) -> None:
    """Register the three users of `USERS` in a federation, each with the baseline privileges in the study space."""
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)


def make_federation(
    *,
    clock: Callable[[], float] | None = None,
    error_shape: str = ERRORS_25_0,
    visibility_delay_s: float = 0.0,
    delete_delay_s: float = 0.0,
) -> FakeData:
    """Return the federation the default `platform.toml` describes, as a `FakeData`.

    It has the study space, the data provider `alpha` that supports it, a second provider `beta` that Onezone knows
    and that does not support it yet, and the three users, who can log in and read. Its jobs' times and its providers'
    `Date` headers come from `clock` (a `Ticker`'s `clock`, say), and so does the visibility delay: a new file shows on
    the provider it was not made on after `visibility_delay_s` seconds of it. `error_shape` is the data API's error
    shape (`ERRORS_25_0`, or `ERRORS_LEGACY` for the older 404 shape, which no live version is known to give).
    `delete_delay_s` keeps a delete under way for that many seconds of `clock` after it is answered (`FakeData`,
    "Pending deletes"); 0, the default, finishes it at once, and above 0 it needs a `clock` that moves (a `Ticker`'s):
    the first delete that would pend without one raises `ValueError`. The platform layer reads the admin credential
    from the environment variable `ADMIN_ENV`: a test sets it to `fake.admin_token`.
    """
    federation = FakeData(
        space_name=SPACE_NAME,
        error_shape=error_shape,
        visibility_delay_s=visibility_delay_s,
        delete_delay_s=delete_delay_s,
    )
    if clock is not None:
        federation.clock = clock
    federation.add_provider(BETA_ID, "beta.test", supports=False)
    add_users(federation)
    return federation


def platform_data(directory: Path) -> dict[str, Any]:
    """Return the default platform document as a dictionary; the files it names exist (empty manifests, passwords)."""
    for name in ("read", "manage"):
        manifest = directory / f"{name}.manifest.json"
        if not manifest.exists():  # a test that froze a real manifest first keeps it
            manifest.write_text("{}", encoding="utf-8")
    credentials = directory / CREDENTIALS_DIR
    credentials.mkdir(exist_ok=True)
    for login, (password, _) in USERS.items():
        file = credentials / f"{login}.pass"
        if not file.exists():
            file.write_text(password + "\n", encoding="utf-8")
            os.chmod(file, 0o600)
    return platform_document(directory)


def platform_document(directory: Path) -> dict[str, Any]:
    """Return the default platform document without writing anything: the files it names are not created."""
    manifests = {name: directory / f"{name}.manifest.json" for name in ("read", "manage")}
    return {
        "zone": {"name": "zone", "url": "https://zone.test"},
        "providers": {
            "alpha": {"url": "https://alpha.test", "id": ALPHA_ID},
            "beta": {"url": "https://beta.test", "id": BETA_ID},
        },
        "space": {"name": SPACE_NAME, "id": "spmock0001", "prefixes": [SPACE_PREFIX]},
        "support": {"providers": ["alpha"], "local": "alpha"},
        "credentials": {"admin": {"env": ADMIN_ENV}, "users": {"directory": CREDENTIALS_DIR}},
        "principals": {
            "data_path": {"username": DATA_PATH_USER.username, "user_id": DATA_PATH_USER.user_id},
            "toolserver": {"username": TOOL_SERVER_USER.username, "user_id": TOOL_SERVER_USER.user_id},
            "service": {"username": SERVICE_USER.username, "user_id": SERVICE_USER.user_id},
        },
        "toolserver": {
            "command": sys.executable,
            "args": list(TOOL_SERVER_ARGS),
            "start_timeout_s": 30,
            "conditions": {
                "read": {"allow": ["echo", "add"], "rights": BASELINE, "manifest": manifests["read"].name},
                "manage": {"allow": ["echo", "add", "fail"], "rights": MANAGE, "manifest": manifests["manage"].name},
            },
        },
    }


def _value(value: Any) -> str:
    """Render a scalar or a list of scalars as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    return json.dumps(value)


def _tables(value: Any) -> bool:
    """Whether a value is an array of tables (rendered `[[key]]`), as `[[spaces]]` is."""
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def render(data: dict[str, Any]) -> str:
    """Render a nested dictionary of tables, arrays of tables, scalars and lists as TOML."""
    lines: list[str] = []

    def emit(prefix: str, table: dict[str, Any], *, array: bool = False) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict) and not _tables(v)}
        if array:
            lines.append(f"[[{prefix}]]")
        elif prefix and (scalars or not any(isinstance(v, dict) or _tables(v) for v in table.values())):
            lines.append(f"[{prefix}]")
        lines.extend(f"{k} = {_value(v)}" for k, v in scalars.items())
        for key, sub in table.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(sub, dict):
                emit(name, sub)
            elif _tables(sub):
                for entry in sub:
                    emit(name, entry, array=True)

    emit("", data)
    return "\n".join(lines) + "\n"


SPACE_IDS = ("spmock0001", "sp-second-0002", "sp-third-0003")  # the fake's study space first, then two invented ones
SPACE_NAMES = (SPACE_NAME, "study-a-mirror", "study-b-owned")
SPACE_LABELS = ("lakeside", "hillcrest", "riverbend")


def with_spaces(data: dict[str, Any], count: int = 2, **override: Any) -> None:
    """Edit a platform document into the several-space form: `count` `[[spaces]]` entries in place of `[space]` and
    `[support]`, the first being the fake's study space; `override` updates every entry.

    The entries are `SPACE_LABELS`, `SPACE_NAMES` and `SPACE_IDS` in order (at most three); each is supported by
    `alpha` and `beta`, with `alpha` local and holding. The federation answers for the others only after
    `FakeFederation.add_space` has added them.
    """
    del data["space"], data["support"]
    data["spaces"] = [
        {
            "label": SPACE_LABELS[i],
            "name": SPACE_NAMES[i],
            "id": SPACE_IDS[i],
            "prefixes": ["study-b-" if SPACE_NAMES[i].startswith("study-b-") else SPACE_PREFIX],
            "providers": ["alpha", "beta"],
            "local": "alpha",
            "holder": "alpha",
            **override,
        }
        for i in range(count)
    ]


def write_platform(directory: Path, edit: Callable[[dict[str, Any]], None] | None = None) -> Path:
    """Write `platform.toml` into a directory after letting `edit` change the default document; return its path.

    The tool-server table launches `TOOL_SERVER_ARGS` with this interpreter: the stand-in server, not a real one. The
    document has no `[placement]` table; `write_with_placement` adds one.
    """
    data = platform_data(directory)
    if edit is not None:
        edit(data)
    path = directory / "platform.toml"
    path.write_text(render(data), encoding="utf-8")
    return path


def with_placement(data: dict[str, Any], **override: Any) -> None:
    """Edit a platform document: add a `[placement]` table with the test settings (`SETTINGS`, then `override`)."""
    data["placement"] = {**SETTINGS, **override}


def write_with_placement(directory: Path, rows: str = "", **override: Any) -> Path:
    """Write a platform.toml with a `[placement]` table and, after it, raw TOML rows (such as `[[placement.map]]`)."""
    path = write_platform(directory, lambda d: with_placement(d, **override))
    if rows:
        path.write_text(path.read_text(encoding="utf-8") + rows, encoding="utf-8")
    return path


def tool_plane_replicates(fake: FakeData, client: OnezoneClient, path: str, target_id: str) -> str:
    """The agent's replication through the tool plane: the tool server posts the job to the provider with its own
    user's token, as schedule_file_replication does, and nothing of the harness sends it. Return the job's id.

    It is the way a test makes a job the harness did not schedule, so that a probe finds it in the space's listing.
    """
    token = client.mint_own_token(TOOL_SERVER_USER, [{"type": "time", "validUntil": LATER}])
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
    if answer.status_code != 201:
        raise AssertionError(f"the fake refused the tool plane's replication: HTTP {answer.status_code}")
    return answer.json()["transferId"]


__all__ = [
    "ADMIN_ENV",
    "ALPHA_ID",
    "BASELINE",
    "BETA_ID",
    "CEILING_S",
    "CREDENTIALS_DIR",
    "DATA",
    "DATA_PATH_USER",
    "MANAGE",
    "NOW",
    "SERVICE_USER",
    "SETTINGS",
    "SPACE_NAME",
    "SPACE_PREFIX",
    "TOOL_SERVER_ARGS",
    "TOOL_SERVER_USER",
    "USERS",
    "Ticker",
    "add_users",
    "make_federation",
    "platform_document",
    "tool_plane_replicates",
    "with_placement",
    "write_platform",
    "write_with_placement",
]

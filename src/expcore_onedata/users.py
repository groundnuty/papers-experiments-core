# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/slot_users.py, blob 6bd4dcbb3acbafbf3070773537c37b460f817ba9
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Per-slot Onedata users for run attribution under concurrency (#11, dataset_76/faults/ATTRIBUTION.md).

One user per concurrency slot, one option per slot. Each user is a member of the study data space ONLY,
with exactly {space_view, space_read_data}; nothing is copied per slot. The audit log's `user` field is
the Onezone user id, so the slot map is {slot: {"user": <user id>, "option": <option>}}.

Safety rules, all enforced here:
- names are `slot-user-23-<option>-<n>`; nothing else is ever created, changed or deleted;
- teardown deletes exactly the ids recorded in the change log (never a scan by name), and refuses any user
  whose live username does not start with `slot-user-23-`;
- every mutation is appended to the change log (no password or token value, ever);
- passwords are generated here, written FIRST to a 0600 file in a 0700 directory (O_CREAT|O_EXCL) outside
  the repo, and removed again if the create call fails;
- `plan` is a read-only preflight; `apply` re-reads the privileges and refuses a membership that is not exact.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Callable

ONEZONE = "https://zone.example.org"
STUDY_SPACE_ID = "a0000000000000000000000000000003ch0000"
STUDY_SPACE_NAME = "study_space_15"
ADMIN_PASS_FILE = Path("/home/ubuntu/.secrets/dataset-80/onedata.pass")
SECRETS_DIR = Path("/home/ubuntu/.secrets/dataset-80/slot-users")
PREFIX = "slot-user-23-"
PRIVILEGES = ("space_view", "space_read_data")          # the minimum to read data: no write, invite or management
OPTIONS = ("T", "condition_a", "D", "M")
_NAME = re.compile(rf"^{re.escape(PREFIX)}(T|condition_a|D|M|M\+)-(\d+)$")


class SlotUserError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status               # the HTTP status when the error is an Onezone answer


def slot_name(option: str, n: int) -> str:
    if n < 1:
        raise ValueError("slot index starts at 1")
    name = f"{PREFIX}{option}-{n}"
    if not _NAME.match(name):
        raise ValueError(f"not a valid slot name: {name}")
    return name


def slot_key(option: str, n: int) -> str:
    return f"{option}-{n}"


def token_caveats(option: str, ttl_s: int, space_id: str = STUDY_SPACE_ID, now: float | None = None) -> list[dict]:
    """The run-shape token scope: time, read-only, data.path = the study space (+ interface for oneclient).
    The same shape dataset_76.harness.oracle_real.mint_token uses."""
    c = [{"type": "time", "validUntil": int((now if now is not None else time.time()) + ttl_s)},
         {"type": "data.readonly"},
         {"type": "data.path", "whitelist": [base64.b64encode(f"/{space_id}".encode()).decode()]}]
    if option in ("M", "condition_b"):
        c.append({"type": "interface", "interface": "oneclient"})
    return c


def describe_caveats(caveats: list[dict]) -> list[str]:
    """Human-readable scope for the log: never contains a token."""
    out = []
    for c in caveats:
        if c["type"] == "time":
            out.append("time")
        elif c["type"] == "data.path":
            out.append("data.path=" + ",".join(base64.b64decode(w).decode() for w in c["whitelist"]))
        elif c["type"] == "interface":
            out.append(f"interface={c['interface']}")
        else:
            out.append(c["type"])
    return out


def _error_id(r) -> str:
    """The Onedata error id only (e.g. badValueUsername); a raw body could echo a password or token."""
    try:
        eid = r.json().get("error", {}).get("id")
    except Exception:  # noqa: BLE001 - any non-JSON body
        return ""
    return f" ({eid})" if isinstance(eid, str) and re.fullmatch(r"[A-Za-z0-9_]{1,60}", eid) else ""


class Client:
    """Thin Onezone REST client; `session` is injectable for tests."""

    def __init__(self, session=None, admin_pass_file: Path = ADMIN_PASS_FILE, zone: str = ONEZONE):
        import requests
        self.s = session or requests.Session()
        self.zone = zone
        self._pf = admin_pass_file

    def _admin_auth(self) -> dict:
        try:
            pw = Path(self._pf).read_text().strip()
        except OSError as e:
            raise SlotUserError(f"admin password file unreadable: {self._pf} ({type(e).__name__})") from None
        if not pw:
            raise SlotUserError(f"admin password file empty: {self._pf}")
        return {"authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}

    def admin_token(self, ttl_s: int = 3600) -> str:
        r = self.s.post(f"{self.zone}/api/v3/onezone/user/tokens/temporary", headers=self._admin_auth(), timeout=30,
                        json={"type": {"accessToken": {}}, "caveats": [{"type": "time", "validUntil": int(time.time()) + ttl_s}]})
        if r.status_code >= 300:
            raise SlotUserError(f"admin token mint failed: HTTP {r.status_code}")
        return r.json()["token"]

    def oz(self, method: str, path: str, token: str, expect=(200, 201, 204), **kw):
        r = self.s.request(method, f"{self.zone}/api/v3/onezone{path}", headers={"x-auth-token": token}, timeout=60, **kw)
        if expect and r.status_code not in expect:
            raise SlotUserError(f"{method} {path} -> HTTP {r.status_code}{_error_id(r)}", status=r.status_code)
        return r


def log_change(log: Path, action: str, **fields) -> None:
    for k in fields:
        if k in ("password", "token", "secret"):
            raise ValueError(f"refusing to log a field named {k}")
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, **fields}) + "\n")


def read_log(log: Path) -> list[dict]:
    p = Path(log)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def logged_users(log: Path) -> dict[str, dict]:
    """name -> {user_id, ...} for users created and not yet deleted, from the log alone."""
    live: dict[str, dict] = {}
    for e in read_log(log):
        if e["action"] == "user_create":
            live[e["username"]] = {"user_id": e["user_id"], "option": e["option"], "slot": e["slot"]}
        elif e["action"] == "user_delete":
            live.pop(e["username"], None)
    return live


def unresolved_intents(log: Path) -> dict[str, dict]:
    """Intents with no later user_create, user_create_failed or user_delete for the same username."""
    out: dict[str, dict] = {}
    for e in read_log(log):
        if e["action"] == "user_create_intent":
            out[e["username"]] = e
        elif e["action"] in ("user_create", "user_create_failed", "user_delete"):
            out.pop(e["username"], None)
    return out


def _secret_path(dirpath: Path, username: str) -> Path:
    return Path(dirpath) / f"{username}.pass"


REPO = Path(__file__).resolve().parents[2]


def _check_outside_repo(dirpath: Path) -> Path:
    d = Path(dirpath).expanduser().resolve()
    if d == REPO or REPO in d.parents:
        raise SlotUserError(f"credentials directory {d} is inside the repository")
    return d


def _write_secret_first(dirpath: Path, username: str, password: str) -> Path:
    d = _check_outside_repo(dirpath)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = _secret_path(d, username)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)      # exists already -> refused, never overwritten
    with os.fdopen(fd, "w") as f:
        f.write(password)
    return p


def read_slot_password(username: str, dirpath: Path = SECRETS_DIR) -> str:
    p = _secret_path(dirpath, username)
    if not p.is_file():
        raise SlotUserError(f"no credential file for {username}: {p}")
    if p.stat().st_mode & 0o077:
        raise SlotUserError(f"{p} is readable by others (mode {oct(p.stat().st_mode & 0o777)})")
    return p.read_text().strip()


def slot_user_token(client: Client, username: str, caveats: list[dict], dirpath: Path = SECRETS_DIR) -> str:
    """A temporary access token of the slot user (basic auth), with the given caveats. In memory only."""
    pw = read_slot_password(username, dirpath)
    r = client.s.post(f"{client.zone}/api/v3/onezone/user/tokens/temporary", timeout=30,
                      headers={"authorization": "Basic " + base64.b64encode(f"{username}:{pw}".encode()).decode()},
                      json={"type": {"accessToken": {}}, "caveats": caveats})
    if r.status_code >= 300:
        raise SlotUserError(f"token mint for {username} failed: HTTP {r.status_code}")
    return r.json()["token"]


def mint_run_token(client: Client, username: str, option: str, time_cap_s: float,
                   dirpath: Path = SECRETS_DIR, space_id: str = STUDY_SPACE_ID) -> tuple[str, list[dict]]:
    """The token one run uses as this slot's user: run-shape scope, lifetime = required_token_ttl_s(time_cap_s)
    of the run's stated cap (never a constant). Returns (token, caveats); the token is for memory only."""
    from .config import check_token_ttl, required_token_ttl_s
    ttl = required_token_ttl_s(time_cap_s)
    check_token_ttl(ttl, time_cap_s, f"slot token of {username}")
    caveats = token_caveats(option, ttl, space_id)
    return slot_user_token(client, username, caveats, dirpath), caveats


@dataclasses.dataclass
class Slot:
    option: str
    n: int

    @property
    def username(self) -> str:
        return slot_name(self.option, self.n)

    @property
    def key(self) -> str:
        return slot_key(self.option, self.n)


def wanted(options=OPTIONS, per_option: int = 1) -> list[Slot]:
    """The slots to have: `per_option` per option. Adding more later is one call with a bigger number."""
    bad = [o for o in options if o not in ("T", "condition_a", "D", "M", "condition_b")]
    if bad:
        raise ValueError(f"unknown options {bad}")
    return [Slot(o, n) for o in options for n in range(1, per_option + 1)]


def _existing_by_name(client: Client, tok: str) -> dict[str, str]:
    """username -> id of every existing user whose name starts with the prefix (read-only)."""
    out = {}
    for uid in client.oz("GET", "/users", tok).json()["users"]:
        u = client.oz("GET", f"/users/{uid}", tok, expect=None)
        if u.status_code == 404:
            continue                                      # deleted while we scanned
        if u.status_code != 200:
            raise SlotUserError(f"user scan incomplete: GET /users/<id> -> HTTP {u.status_code}", status=u.status_code)
        if str(u.json().get("username", "")).startswith(PREFIX):
            out[u.json()["username"]] = uid
    return out


def plan(client: Client, slots: list[Slot], log: Path) -> dict:
    """Read-only preflight: what exists, what would be created, exact privileges and caveats. No values."""
    tok = client.admin_token()
    me = client.oz("GET", "/user", tok).json()
    eff = set(client.oz("GET", f"/users/{me['userId']}/effective_privileges", tok).json()["privileges"])
    need = {"oz_users_create", "oz_users_delete", "oz_users_manage_passwords", "oz_spaces_add_relationships",
            "oz_users_add_relationships", "oz_spaces_set_privileges"}
    members = client.oz("GET", f"/spaces/{STUDY_SPACE_ID}/users", tok).json()["users"]
    existing, logged = _existing_by_name(client, tok), logged_users(log)
    acts = []
    for s in slots:
        have = existing.get(s.username)
        acts.append({"slot": s.key, "username": s.username, "option": s.option,
                     "action": "skip (exists, in the log)" if have and s.username in logged else
                               "REFUSE: exists but was not created by this log" if have else "create",
                     "privileges": list(PRIVILEGES), "run_token_scope": describe_caveats(token_caveats(s.option, 3600))})
    return {"admin_has_needed_privileges": sorted(need - eff) == [], "missing_admin_privileges": sorted(need - eff),
            "study_space": STUDY_SPACE_NAME, "space_member_count": len(members), "actions": acts}


def apply(client: Client, slots: list[Slot], log: Path, secrets_dir: Path = SECRETS_DIR,
          note: Callable[[str], None] = print) -> dict[str, dict]:
    """Create what is missing; idempotent. Returns the slot map {slot: {"user", "option"}} (ids, no secrets)."""
    _check_outside_repo(secrets_dir)
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a"):                                  # the log must be writable BEFORE anything is created
        pass
    tok = client.admin_token()
    logged = logged_users(log)
    intents = unresolved_intents(log)
    existing = _existing_by_name(client, tok)
    for s in slots:
        have = existing.get(s.username)
        if have and s.username not in logged and s.username in intents:
            # a previous run logged its intent and lost the outcome (timeout, crash). Adopt only if the saved
            # password still opens the account: that proves this log's POST created it, not someone else.
            try:
                slot_user_token(client, s.username, [{"type": "time", "validUntil": int(time.time()) + 60}], secrets_dir)
            except Exception:  # noqa: BLE001
                raise SlotUserError(f"{s.username} exists but the saved password does not open it (or basic auth is "
                                    "off): not adopting it; resolve by hand") from None
            log_change(log, "user_create", username=s.username, user_id=have, option=s.option, slot=s.key,
                       credential_file=str(_secret_path(secrets_dir, s.username)), recovered_from_intent=True,
                       where="onezone POST /users (outcome recovered)")
            logged = logged_users(log)
        if have and s.username not in logged:
            raise SlotUserError(f"{s.username} exists (id {have[:6]}...) but this log did not create it: refusing to adopt it")
        if s.username in logged:
            uid = logged[s.username]["user_id"]
            if existing.get(s.username) != uid:
                raise SlotUserError(f"{s.username}: the log says {uid[:6]}... but Onezone has {str(existing.get(s.username))[:6]}...")
        else:
            password = secrets.token_urlsafe(24)
            path = _write_secret_first(secrets_dir, s.username, password)
            log_change(log, "user_create_intent", username=s.username, option=s.option, slot=s.key,
                       credential_file=str(path), where="before onezone POST /users")
            try:
                r = client.oz("POST", "/users", tok, json={"username": s.username, "password": password,
                                                           "fullName": f"dataset-80 slot {s.key}"})
            except BaseException:
                # The outcome may be ambiguous (a timeout after the server created it). Keep the credential
                # unless Onezone confirms no such user exists.
                # Only a definite refusal by Onezone (an HTTP 4xx answer) proves nothing was created. A timeout,
                # transport error or 5xx may still complete server-side: keep the credential and the open intent.
                st = getattr(sys.exc_info()[1], "status", None)
                if st is not None and 400 <= st < 500:
                    path.unlink(missing_ok=True)
                    log_change(log, "user_create_failed", username=s.username, http=st)
                raise
            uid = r.headers["Location"].rstrip("/").split("/")[-1]
            log_change(log, "user_create", username=s.username, user_id=uid, option=s.option, slot=s.key,
                       credential_file=str(path), where="onezone POST /users")
            note(f"created {s.username}")
        ba = client.oz("GET", f"/users/{uid}", tok).json()
        if ba.get("basicAuthEnabled") is not True:
            client.oz("PATCH", f"/users/{uid}/basic_auth", tok, json={"basicAuthEnabled": True})
            log_change(log, "user_basic_auth", username=s.username, user_id=uid, enabled=True,
                       where=f"onezone PATCH /users/{{id}}/basic_auth")
        _ensure_membership(client, tok, log, s, uid)
    return slot_map(log, slots)


def _ensure_membership(client: Client, tok: str, log: Path, s: Slot, uid: str) -> None:
    cur = client.oz("GET", f"/spaces/{STUDY_SPACE_ID}/users", tok).json()["users"]
    if uid not in cur:
        client.oz("PUT", f"/spaces/{STUDY_SPACE_ID}/users/{uid}", tok, json={"privileges": list(PRIVILEGES)})
        log_change(log, "space_member_add", username=s.username, user_id=uid, space_id=STUDY_SPACE_ID,
                   privileges_requested=list(PRIVILEGES), where="onezone PUT /spaces/{id}/users/{uid}")
    got = sorted(client.oz("GET", f"/spaces/{STUDY_SPACE_ID}/users/{uid}/privileges", tok).json()["privileges"])
    log_change(log, "privileges_readback", username=s.username, user_id=uid, space_id=STUDY_SPACE_ID, privileges=got,
               exact=got == sorted(PRIVILEGES), where="onezone GET /spaces/{id}/users/{uid}/privileges")
    eff = sorted(client.oz("GET", f"/spaces/{STUDY_SPACE_ID}/effective_users/{uid}/privileges", tok).json()["privileges"])
    log_change(log, "effective_privileges_readback", username=s.username, user_id=uid, space_id=STUDY_SPACE_ID,
               privileges=eff, exact=eff == sorted(PRIVILEGES),
               where="onezone GET /spaces/{id}/effective_users/{uid}/privileges")
    if got != sorted(PRIVILEGES) or eff != sorted(PRIVILEGES):
        raise SlotUserError(f"{s.username}: direct {got}, effective {eff}; expected exactly {sorted(PRIVILEGES)}")


def slot_map(log: Path, slots: list[Slot] | None = None) -> dict[str, dict]:
    """{slot: {"user": id, "option": option}} from the log, in the shape dataset_76.faults.matcher.load_slot_map reads."""
    live = logged_users(log)
    names = {s.username for s in slots} if slots is not None else set(live)
    return {v["slot"]: {"user": v["user_id"], "option": v["option"]} for k, v in sorted(live.items()) if k in names}


def teardown(client: Client, log: Path, secrets_dir: Path = SECRETS_DIR, dry_run: bool = False) -> list[str]:
    """Delete exactly the users the log says were created (and their credential files); nothing else."""
    tok = client.admin_token()
    done = []
    for name, v in logged_users(log).items():
        if not name.startswith(PREFIX) or not _NAME.match(name):
            raise SlotUserError(f"refusing to delete {name!r}: not a slot name")
        r = client.oz("GET", f"/users/{v['user_id']}", tok, expect=None)
        if r.status_code == 404:
            gone = True
        elif r.status_code == 200:
            if r.json().get("username") != name:
                raise SlotUserError(f"{v['user_id'][:6]}... is {r.json().get('username')!r}, not {name!r}: refusing to delete")
            gone = False
        else:
            raise SlotUserError(f"GET user {name} -> HTTP {r.status_code}")
        if dry_run:
            done.append(f"would delete {name}")
            continue
        if not gone:
            client.oz("DELETE", f"/users/{v['user_id']}", tok)
        log_change(log, "user_delete", username=name, user_id=v["user_id"], already_gone=gone, where="onezone DELETE /users/{id}")
        _secret_path(secrets_dir, name).unlink(missing_ok=True)
        done.append(f"deleted {name}")
    return done


# ---------------------------------------------------------------- command line
DEFAULT_LOG = Path(__file__).resolve().parents[2] / "day0" / "evidence" / "slot_users_changes.jsonl"
DEFAULT_MAP = Path(__file__).resolve().parents[2] / "config" / "slot_map.json"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Per-slot Onedata users (read-only members of the study space)")
    ap.add_argument("command", choices=["plan", "apply", "teardown"])
    ap.add_argument("--options", nargs="+", default=list(OPTIONS))
    ap.add_argument("--per-option", type=int, default=1, help="slots per option; raise it to add more")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--map", type=Path, default=DEFAULT_MAP, help="slot map written by apply")
    ap.add_argument("--secrets-dir", type=Path, default=SECRETS_DIR)
    ap.add_argument("--yes", action="store_true", help="really change Onezone (apply and teardown are dry runs without it)")
    a = ap.parse_args(argv)
    cl = Client()
    slots = wanted(tuple(a.options), a.per_option)
    if a.command == "plan" or (a.command == "apply" and not a.yes):
        print(json.dumps(plan(cl, slots, a.log), indent=1))
        if a.command == "apply":
            print("dry run: nothing changed; add --yes to apply")
        return 0
    if a.command == "apply":
        m = apply(cl, slots, a.log, a.secrets_dir)
        a.map.parent.mkdir(parents=True, exist_ok=True)
        a.map.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
        print(f"slot map: {a.map}")
        return 0
    for line in teardown(cl, a.log, a.secrets_dir, dry_run=not a.yes):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Helpers of the root-evidence tests of `deploy/hpc-federation/shards.py collect` and `hpcdown.py` (O63): a
federation's root as `up` and a campaign leave it, with the files the study's post-check and the facts need (the
evidence) and the files that must never leave it (secrets, live state). Every name, id and token here is invented."""

import json
import os
from pathlib import Path

TOKEN = "tok-3f9a1c77d2b84e65a0c1d9e7"  # planted in secrets and elsewhere: it must never reach an archive
PASSPHRASE = "pass-5b2e8d41c07f93a6e1b4"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
TAKEN_ID = "fedcba9876543210fedcba9876543210"

# What a collect archives from a root: every file below, by this name (the allow-list), and nothing else.
EVIDENCE = {
    "hpc-federation.json": b'{"format": "expcore-hpc-federation/1", "phase": "linked", "site": {"queue": "batch"}}\n',
    "facts.json": b'{"layout": {"file": "federation.toml"}, "zone": {"url": "https://zone.example:8443"}}\n',
    "platform.toml": b'[space]\nname = "s"\ndata_manifest = "m.json"\n[tokens]\nfile = "secrets/admin.token"\n',
    "link-check.json": b'{"format": "expcore-link-check/1", "ok": true}\n',
    "render-identity.json": b'{"version": "expcore-render-identity/1", "template_sha256": "' + b"a" * 64 + b'"}\n',
    "day0-wiring.toml": b'[wiring]\nzone = "zone"\n',
    "fault-journal/injections.jsonl": b'{"event": "injecting", "target": "aspen"}\n{"event": "cleared"}\n',
    "fault-journal/shared-holds.jsonl": b'{"event": "held", "id": "h1"}\n',
    "fault-journal/reset-holds.jsonl": b'{"event": "reset"}\n',
    "fault-journal/link-epoch.json": b'{"epoch": 3}\n',
    "fault-journal/federation.intent": b'{"intent": "reset"}\n',
    "fault-journal/federation.lock": b"",  # the heal script flocks it: archived as it is, never locked
    "fault-journal/journal.lock": b"",
    "fault-journal/aspen.lock": b"",
    "fault-journal/federation.holder": b'{"pid": 1}\n',
    "fault-journal/federation.drain": b"0\n",
    "fault-journal/old/one.jsonl": b'{"event": "earlier"}\n',
    "shard-claim.json": b'{"held_by": "the pre-step guard of shard s"}\n',
    "shard-claim.lock": b"",
    "fault-relay/late-steps.jsonl": b'{"verb": "stop", "late": []}\n',
    f"fault-relay/requests/{REQUEST_ID}.json": b'{"verb": "status", "service": "aspen"}\n',
    f"fault-relay/taken/{REQUEST_ID}.json": b'{"verb": "status", "service": "aspen"}\n',
    f"fault-relay/answers/{REQUEST_ID}.json": b'{"status": "ok", "stdout": "up"}\n',
    f"fault-relay/answers/{TAKEN_ID}.json": b'{"status": "timed_out"}\n',
    "data-manifests/alpha-space.json": b'{"version": 1, "files": [], "directories": []}\n',
    "shard-steps/shard.json": b'{"format": "expcore-hpc-shard-step-record/1", "shard": {"label": "s"}, "starts": 1}\n',
}

# What a root holds that is not evidence: secrets, live state and what no read of the study needs. None is archived.
NOT_EVIDENCE = {
    "secrets/admin.token": TOKEN.encode(),
    "secrets/aspen.passphrase": PASSPHRASE.encode(),
    "secrets/users/alice.pass": b"alice-pass-91c4e07b2d\n",
    "secrets/ca.key": b"-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkq\n-----END PRIVATE KEY-----\n",
    "notes.token": TOKEN.encode(),  # a file of the root outside the allow-list that holds the planted token
    "fault-relay/heartbeat.json": b'{"nonce": "n1", "beat": 7}\n',
    "fault-relay/relay.lock": b"",
    "ca/federation-ca.pem": b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
    "toolshim/resolve_launch.py": b"print('shim')\n",
    "aspen/persistence/data.db": b"provider data\n",
}


def make_root(root: Path, evidence=None, extras=None) -> Path:
    """A federation's root with `evidence` (name -> bytes, default `EVIDENCE`) and `extras` (default `NOT_EVIDENCE`)."""
    for name, content in {
        **(EVIDENCE if evidence is None else evidence),
        **(NOT_EVIDENCE if extras is None else extras),
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600 if name.startswith(("secrets/", "fault-relay/")) or name == "render-identity.json" else 0o644)
    return root


def record_step(root: Path, label: str, starts: int = 1) -> Path:
    """The record a shard's step leaves in the root when it starts (`shards.step`): a step ran against this root."""
    directory = root / "shard-steps"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{label}.json"
    record = {"format": "expcore-hpc-shard-step-record/1", "shard": {"label": label}, "starts": starts}
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    return path


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o777

# Provenance:
#   source: www-2027-experiments @ 46a5828ddb3276ba9582e4b4a6c2ae0f217e9da7, harness/agent/record.py, blob 7d033daa1e8228d4a997fc5f66203f66039620bc
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The run record: one JSON file per attempt, written atomically and read back byte for byte (R01).

A record holds what is needed to audit, re-score and replay an attempt offline: who it was (run key, factor values,
attempt), the route and the settings each request carried, the prompts and tools the model was given, the agent
configuration as resolved for the run, the code stamp, the transcript turn by turn with the neutral and the native
message, every tool call with the exact text shown and the raw result, the clock readings, the outcome, the instrument
outputs under their namespaces, and the scores, kept apart. The sections and their fields are in `schema.py`.

The file name is the SHA-256 of the run key and the attempt number. The text is JSON with an indent of one, in the
order the schema lists the fields, with no trailing newline: loading a record and writing it back gives its exact
bytes, so a check can compare them. A field a later schema version added is never written into a record of an older
version. The writer redacts secrets (R12) and warns when it did; numbers are never changed, and NaN is refused.
"""

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.loop.outcomes import SET_ASIDE_OUTCOMES
from expcore.records.redact import redact_structure
from expcore.records.schema import (
    CURRENT,
    SCHEMAS,
    RecordError,
    SchemaVersion,
    added_after,
    validate,
)

_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")  # in a str only unpaired (a valid pair decodes to one character)


def record_filename(run_key: str, attempt: int) -> str:
    """Return the file name of an attempt: the SHA-256 of its run key, a dot, the attempt number and `.json`."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise RecordError(f"the attempt is a non-negative integer, not {attempt!r}")
    return f"{hashlib.sha256(run_key.encode('utf-8')).hexdigest()}.{attempt}.json"


@dataclass(frozen=True)
class RunRecord:
    """One attempt, as plain values. Sections are described in `expcore.records.schema`."""

    run_key: str
    factors: dict[str, Any]
    attempt: int
    route: dict[str, Any]
    prompt: dict[str, Any]
    tools: list[dict[str, Any]]
    agent_config: dict[str, Any]
    agent_config_sha256: str
    code: dict[str, Any]
    core_version: str | None
    transcript: list[dict[str, Any]]
    failed_request: dict[str, Any] | None
    tool_calls: list[dict[str, Any]]
    clock: list[list[Any]]
    outcome: dict[str, Any]
    totals: dict[str, Any]
    instruments: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, Any] = field(default_factory=dict)
    schema_version: str = CURRENT

    @property
    def filename(self) -> str:
        """The file name of this attempt."""
        return record_filename(self.run_key, self.attempt)

    @property
    def set_aside(self) -> bool:
        """Tell whether the attempt is set aside and re-run instead of scored: a partial record (R09)."""
        return self.outcome["name"] in SET_ASIDE_OUTCOMES

    def with_instrument(self, namespace: str, data: dict[str, Any]) -> "RunRecord":
        """Return a copy that also holds an instrument's output under its namespace."""
        return dataclasses.replace(self, instruments={**self.instruments, namespace: data})

    def to_json(self, schemas: tuple[SchemaVersion, ...] = SCHEMAS) -> dict[str, Any]:
        """Return the record as a dictionary in file order, without the fields a later schema added.

        A later-added field is dropped when it holds its default; one that holds data cannot be written into a record
        of an earlier version, and raises.
        """
        data = dataclasses.asdict(self)
        ordered = {"schema_version": data.pop("schema_version"), **data}
        for name in sorted(added_after(ordered["schema_version"], "record", schemas)):
            default = next(f for f in dataclasses.fields(self) if f.name == name)
            empty = default.default_factory() if default.default_factory is not dataclasses.MISSING else default.default
            if ordered[name] != empty:
                raise RecordError(f"{name} holds data but {ordered['schema_version']} predates the field")
            del ordered[name]
        return ordered

    @classmethod
    def from_json(cls, data: Any, schemas: tuple[SchemaVersion, ...] = SCHEMAS) -> "RunRecord":
        """Validate a decoded record against its own schema version and build it; raise RecordError when it is not."""
        validate(data, schemas)
        return cls(**data)

    def dumps(self, schemas: tuple[SchemaVersion, ...] = SCHEMAS, environ: dict[str, str] | None = None) -> str:
        """Return the text of the record: validated, secrets redacted, an unpaired surrogate as its \\u escape.

        A model's reply can carry an unpaired surrogate through JSON's \\u escapes. UTF-8 cannot encode one, so it is
        written as its escape, and reads back to the same value and the same text.
        """
        data = self.to_json(schemas)
        validate(data, schemas)
        data, count = redact_structure(data, environ)
        try:
            text = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)
        except ValueError as exc:
            raise RecordError(f"the record cannot be written as JSON: {exc}") from None
        text = _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
        if count:
            warnings.warn(
                f"record {self.filename[:12]}: redacted {count} secret value(s) before writing",
                RuntimeWarning,
                stacklevel=2,
            )
        return text

    def write(
        self,
        out_dir: str | os.PathLike[str],
        schemas: tuple[SchemaVersion, ...] = SCHEMAS,
        environ: dict[str, str] | None = None,
    ) -> Path:
        """Write the record to `<out_dir>/<filename>` atomically and return the path.

        The text goes to a temporary file in the same directory, is flushed and fsynced, and then renamed over the
        target, so a reader sees the old record or the whole new one and a crash leaves at most a `.tmp` file (R01).
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        target = out / self.filename
        text = self.dumps(schemas, environ)
        fd, tmp = tempfile.mkstemp(dir=out, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return target


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object from its pairs, refusing a key that appears twice: the last would win and hide the first."""
    keys = [k for k, _ in pairs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise RecordError(f"duplicate key(s) {repeated}")
    return dict(pairs)


def _refuse_constant(name: str) -> Any:
    """Refuse NaN and the infinities, which are not JSON."""
    raise RecordError(f"{name} is not a JSON number")


def decode(raw: bytes | str) -> Any:
    """Decode the text of a record to plain values, refusing bad UTF-8, duplicate keys and non-JSON numbers."""
    try:
        text = raw if isinstance(raw, str) else raw.decode("utf-8")
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_refuse_constant)
    except RecordError:
        raise
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        raise RecordError(f"not a JSON document: {type(exc).__name__}") from None


def load_record(path: str | os.PathLike[str], schemas: tuple[SchemaVersion, ...] = SCHEMAS) -> RunRecord:
    """Read and validate a record file; the error names the file."""
    try:
        return RunRecord.from_json(decode(Path(path).read_bytes()), schemas)
    except RecordError as exc:
        raise RecordError(f"{path}: {exc}") from None

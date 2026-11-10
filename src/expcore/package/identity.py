# Provenance: new
"""The code identity of an anonymised package: the tree hash alone, and no commit id or source URL anywhere (#47).

A commit id in a double-blind package is a lookup key to its authors, in any public repository that holds it, and so is
a source URL. An anonymised build therefore replaces every commit id with the first 40 hexadecimal digits of the tree
hash of the same code, which a reader can recompute, and drops every source:

- first, every file of the batch must be exactly the bytes the original replay check hashed, and no other input may
  have appeared: a record changed after the check would otherwise be given a pseudo-commit and pass under the
  original check's time;
- each record's `code.commit`, and the head of the commit in `code.why`, become that record's pseudo-commit. The
  field is free text under the format and no seal covers it (`records.seal` pins prompts, configuration, tools and
  settings), so no record format changes, and every digest a record holds stays valid;
- the campaign snapshot's fingerprint and the replay check lose the environment's `sources`;
- the replay check is made again over the rewritten records, so the claim that they replay is a fresh one, keeping
  only the time of the original check, since the archive must not depend on when it was built (K06);
- the tables and the report are derived again from the rewritten records and must be exactly the author's, with the
  commit replaced: a table the records do not give is refused here as it is everywhere;
- the code stamp names the pseudo-commit, and `PACKAGE.json` keeps the environment's names and versions only.

The mapping from pseudo-commit back to commit is the local campaign itself, which never leaves the author's machine.
The core's own identity follows the same rule: the only trace of it in a record is `core_version`, a plain version;
its commit and URL live in the environment's `sources`, which are dropped. A version with a local label (`1.2+g1a2b3c`)
can spell a commit, so an anonymised build refuses one.

What an anonymised package still shows: the core's distribution name and its code, which the reader must run. So its
wheel is searched for the terms of the list and for the identifying URLs like every other file: the core's own
sources name no author.

`find_identity` is the safety net behind the rewriting: every stream of every file the checks read is searched for each
commit of the study's repository, of the records and of the environment (by its first 12 digits, which also finds
longer prefixes) and for each source URL of the environment and each remote of the study's repository, ignoring case
and the scheme. A finding names the file and line, never the value (K05).
"""

import dataclasses
import fnmatch
import hashlib
import json
import re
import sqlite3
import tempfile
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.snapshot import SNAPSHOT_FILE
from expcore.package.spec import PackageError
from expcore.package.views import hidden_name, streams, views
from expcore.provenance.codestamp import STAMP, CodeScope, covered_files, is_sha, write_stamp
from expcore.provenance.environment import without_sources
from expcore.provenance.gitobjects import GitError, run_git
from expcore.records.record import RunRecord, decode
from expcore.replay.check import CHECK_FILE, inputs_sha256, verify_batch, write_check
from expcore.study.pipeline import OUT_DIR, OUTPUT_FILES, RUNS_DIR, Refused, analyse_study, output_bytes

PREFIX_DIGITS = 12
_HEX_RUN = re.compile(rf"[0-9a-fA-F]{{{PREFIX_DIGITS},}}")
FOUND_COMMIT = "a commit id of the study's repository or its environment"
FOUND_URL = "a source URL of the study's repository or its environment"
BATCH = f"{OUT_DIR}/{RUNS_DIR}"


def pseudo_commit(tree_sha256: str) -> str:
    """The stand-in for a commit: the first 40 hexadecimal digits of the tree hash of the same code."""
    return tree_sha256[:40]


def lay_out(stage: Path, files: Mapping[str, bytes]) -> None:
    """Write the files under `stage`, making their directories."""
    for path, data in files.items():
        target = stage / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def stamp_bytes(
    files: Mapping[str, bytes], root: Path, scope: CodeScope, head: str, created_utc: str, parent: Path | None
) -> bytes:
    """The bytes of the code stamp of the files the identity covers, naming `head`, dated `created_utc`."""
    with tempfile.TemporaryDirectory(prefix=".stamp-", dir=parent) as scratch:
        lay_out(Path(scratch), {p: d for p, d in files.items() if p in covered_files(root, scope)})
        write_stamp(scratch, head, scope, created_utc=created_utc)
        return (Path(scratch) / STAMP).read_bytes()


def verify_inputs(files: Mapping[str, bytes], check: Mapping[str, Any]) -> None:
    """Refuse a batch whose files are not exactly the inputs the replay check hashed, naming the first that differs.

    An input is what the check counts as one: a `.json` file other than the check, or a file its hashed-only patterns
    name, never a name starting with a dot.
    """
    recorded = check.get("inputs")
    if not isinstance(recorded, dict) or check.get("inputs_sha256") != inputs_sha256(recorded):
        raise PackageError(f"{BATCH}/{CHECK_FILE}: its list of inputs was altered; run `python -m expcore check` again")
    patterns = check.get("hashed_only_patterns") or ()
    current = {}
    for path, data in files.items():
        rel = path[len(BATCH) + 1 :] if path.startswith(f"{BATCH}/") else None
        if rel is None or rel == CHECK_FILE or Path(rel).name.startswith("."):
            continue
        if rel.endswith(".json") or any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns):
            current[rel] = hashlib.sha256(data).hexdigest()
    for rel in sorted(set(current) | set(recorded)):
        if current.get(rel) != recorded.get(rel):
            state = "is not" if rel not in recorded else "is missing" if rel not in current else "differs from"
            raise PackageError(
                f"{BATCH}/{rel} {state} what the replay check hashed; run `python -m expcore check` again"
            )


def replayed_records(check: Mapping[str, Any]) -> list[str]:
    """The records the check replayed, relative to the batch; a check that does not list them cannot be anonymised."""
    replayed = check.get("replayed")
    if not isinstance(replayed, list) or not replayed:
        raise PackageError(f"{BATCH}/{CHECK_FILE}: lists no replayed record; run `python -m expcore check` again")
    return replayed


def record_commits(files: Mapping[str, bytes], replayed: Iterable[str]) -> set[str]:
    """Every commit the replayed records name."""
    return {
        commit
        for rel in replayed
        if is_sha(commit := RunRecord.from_json(decode(files[f"{BATCH}/{rel}"])).code.get("commit"))
    }


def _anonymised_record(raw: bytes) -> bytes:
    """The record with its commit replaced by the pseudo-commit of its own tree, here and in `why`."""
    record = RunRecord.from_json(decode(raw))
    code = dict(record.code)
    commit, tree = code.get("commit"), code.get("tree_sha256")
    if not is_sha(commit):
        return raw  # an unknown version names no commit
    if not isinstance(tree, str):
        raise PackageError("a record names a commit but no tree hash, so it cannot be anonymised")
    pseudo = pseudo_commit(tree)
    code["commit"] = pseudo
    if isinstance(code.get("why"), str):
        code["why"] = code["why"].replace(commit, pseudo).replace(commit[:PREFIX_DIGITS], pseudo[:PREFIX_DIGITS])
    return dataclasses.replace(record, code=code).dumps(environ={}).encode("utf-8")


def _anonymised_snapshot(data: bytes, scratch: Path) -> bytes:
    """The snapshot with no source in its fingerprint's environment, rebuilt so that no old copy of it remains."""
    path = scratch / SNAPSHOT_FILE
    path.write_bytes(data)
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (FINGERPRINT_KEY,)).fetchone()
        fingerprint = json.loads(row[0]) if row is not None else {}
        if "environment" not in fingerprint:
            raise PackageError(f"{BATCH}/{SNAPSHOT_FILE}: records no environment; run the campaign with this core")
        fingerprint["environment"] = without_sources(fingerprint["environment"])
        text = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), allow_nan=False)
        connection.execute("UPDATE meta SET value=? WHERE key=?", (text, FINGERPRINT_KEY))
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return path.read_bytes()


def _anonymised_check(
    files: Mapping[str, bytes], check: Mapping[str, Any], code: Mapping[str, Any], env: Mapping[str, Any], parent: Path
) -> bytes:
    """Make the replay check again over the rewritten batch; the original's time is kept so the archive is stable."""
    batch = parent / "batch"
    lay_out(batch, {path[len(BATCH) + 1 :]: data for path, data in files.items() if path.startswith(f"{BATCH}/")})
    (batch / CHECK_FILE).unlink()
    made = verify_batch(batch, code=code, hashed_only=check.get("hashed_only_patterns") or (), environment=env)
    if made["failures"] or made["n_ok"] != check.get("n_ok") or made["n_records"] != check.get("n_records"):
        raise PackageError(f"{BATCH}/{CHECK_FILE}: the anonymised records do not pass the check the originals passed")
    made["checked_at"] = check.get("checked_at")
    if "snapshot" in check:
        made["snapshot"] = check["snapshot"]
    write_check(batch, made)
    return (batch / CHECK_FILE).read_bytes()


def _outputs(files: dict[str, bytes], commit: str, pseudo: str, stage: Path) -> None:
    """Derive the outputs from the rewritten batch; refuse any that is not the author's with the commit replaced."""
    lay_out(stage, files)
    try:
        made = output_bytes(analyse_study(stage, stamped=True))
    except Refused as error:
        raise PackageError(f"the anonymised records cannot be analysed: {error}") from None
    for name in OUTPUT_FILES:
        path = f"{OUT_DIR}/{name}"
        if files[path].replace(commit.encode(), pseudo.encode()) != made[name]:
            raise PackageError(f"{path}: differs from what the records give")
        files[path] = made[name]


def refuse_labelled_versions(env: Mapping[str, Any]) -> None:
    """A local version label (`1.2+g1a2b3c`) can spell a commit, and the version is kept, so it is refused."""
    labelled = sorted(name for name, version in env["distributions"].items() if "+" in version)
    if labelled:
        raise PackageError(
            f"{labelled[0]} has a local version label, which can name a commit and an anonymised package keeps every "
            "version; install a release"
        )


def anonymise_identity(
    files: Mapping[str, bytes],
    *,
    root: Path,
    scope: CodeScope,
    check: Mapping[str, Any],
    env: Mapping[str, Any],
    parent: Path | None,
) -> tuple[dict[str, bytes], dict[str, Any], Mapping[str, Any]]:
    """Return the files with a tree-hash identity and no source, the new code identity and the environment kept."""
    refuse_labelled_versions(env)
    verify_inputs(files, check)
    original = dict(check["code"])
    code = {**original, "commit": pseudo_commit(original["tree_sha256"])}
    kept = without_sources(env)
    result = dict(files)
    for rel in replayed_records(check):
        result[f"{BATCH}/{rel}"] = _anonymised_record(files[f"{BATCH}/{rel}"])
    with tempfile.TemporaryDirectory(prefix=".anonymise-", dir=parent) as scratch:
        where = Path(scratch)
        snapshot = f"{BATCH}/{SNAPSHOT_FILE}"
        result[snapshot] = _anonymised_snapshot(files[snapshot], where)
        result[f"{BATCH}/{CHECK_FILE}"] = _anonymised_check(result, check, code, kept, where)
        result[STAMP] = stamp_bytes(result, root, scope, code["commit"], str(check.get("checked_at")), where)
        _outputs(result, str(original["commit"]), code["commit"], where / "stage")
    del result[STAMP]  # written with every package's generated files
    return result, code, kept


def study_commits(root: Path) -> set[str]:
    """Every commit of the repository the study lies in; empty when it lies in none."""
    try:
        return set(run_git(root, "rev-list", "--all").decode("ascii").split())
    except (GitError, OSError):
        return set()


def study_remotes(root: Path) -> set[str]:
    """Every remote URL of the repository the study lies in; empty when it lies in none."""
    try:
        lines = run_git(root, "remote", "-v").decode("utf-8", errors="replace").splitlines()
    except (GitError, OSError):
        return set()
    return {line.split()[1] for line in lines if len(line.split()) >= 2}


def _url_forms(url: str) -> set[str]:
    """The forms a URL can be written in, lower case: its host and path, and the path alone when it has two parts."""
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", url) if "://" not in url else None
    parts = urlsplit(url)
    host, path = (scp.group(1), scp.group(2)) if scp else (parts.hostname or "", parts.path)
    path = re.sub(r"\.git$", "", path.strip("/"))
    if not path:
        return set()
    return {f"{host}/{path}".lower()} | ({path.lower()} if path.count("/") >= 1 else set())


def hidden_identity(
    root: Path, code: Mapping[str, Any], env: Mapping[str, Any], records: Collection[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """What an anonymised package may not hold: commit prefixes, and URLs in the forms `find_identity` looks for."""
    sources = env.get("sources", {})
    commits = {*records, *study_commits(root), code["commit"]}
    commits |= {s["commit"] for s in sources.values() if is_sha(s.get("commit"))}
    urls = {s["url"] for s in sources.values() if isinstance(s.get("url"), str)} | study_remotes(root)
    forms = frozenset(form for url in urls for form in _url_forms(url))
    return frozenset(commit[:PREFIX_DIGITS] for commit in commits), forms


def commit_lines(text: str, prefixes: frozenset[str]) -> set[int]:
    lines = set()
    for run in _HEX_RUN.finditer(text):
        digits = run.group(0).lower()
        if any(digits[i : i + PREFIX_DIGITS] in prefixes for i in range(len(digits) - PREFIX_DIGITS + 1)):
            lines.add(text.count("\n", 0, run.start()) + 1)
    return lines


def url_lines(text: str, forms: frozenset[str]) -> set[int]:
    lowered = text.lower()
    return {lowered.count("\n", 0, m.start()) + 1 for form in forms for m in re.finditer(re.escape(form), lowered)}


def find_identity(
    files: Mapping[str, bytes], prefixes: frozenset[str], forms: frozenset[str], *, opaque: Collection[str]
) -> list[str]:
    """One finding per stream and line that holds a hidden commit or URL, never the value; empty when clean."""
    findings: set[str] = set()
    for position, name in enumerate(sorted(files), start=1):
        if name in opaque:
            continue
        for stream in streams(name, files[name])[0]:
            held = commit_lines(stream.label, prefixes) or url_lines(stream.label, forms)
            shown = hidden_name(position, stream.label) if held else stream.label
            if held:
                findings.add(f"{shown}: the path holds {FOUND_COMMIT} or URL")
            for _, text in views(stream):
                findings |= {f"{shown}:{line}: {FOUND_COMMIT}" for line in commit_lines(text, prefixes)}
                findings |= {f"{shown}:{line}: {FOUND_URL}" for line in url_lines(text, forms)}
    return sorted(findings)

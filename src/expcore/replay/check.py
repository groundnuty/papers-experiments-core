# Provenance:
#   source: www-2027-experiments @ 46a5828ddb3276ba9582e4b4a6c2ae0f217e9da7, harness/agent/replay_check.py, blob c1f933ca96b49b028d88085daaa334529f4b7400
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The replay check: the standing gate a batch of run records passes before any analysis.

Every record of a batch (an OUT directory of the pilot runner, or of the closed-book probe
runner) must load, write back to exactly its stored bytes, and replay offline: an agent run
through the real loop (replay.py), a probe record through the probe's replay (harness/probe). The result
is written beside the records as ``OUT/replay-check.json``:

    {"schema": "replay-check/2", "checked_at", "code": {git_commit, git_dirty, source, tree_sha256},
     "n_records", "n_ok", "failures": [{"file", "stage", "error"}], "hashed_only": [paths],
     "decisions_recomputed", "held", "held_records": [{"file", "run_id", "error", "sha256", "question_id",
     "model", "repetition", "arm", "phase", "code", "tree", "online", "offline"}], "halted",
     "divergence": {"rule", "groups": [...]}, "halt_inputs": [{"phase", "model", "code", "tree", "state", "n"}],
     "set_aside": {arm: {"set_aside", "checker_divergence", "rerun"}},
     "records_sha256"}

``stage`` is ``load`` (not JSON, or not a readable run record), ``roundtrip`` (writing the
loaded record gives other bytes), ``replay`` or ``checker``. A record that holds an online checker
decision has it decided again offline (dress rehearsal F3): the loop's replay serves the decision
back from the record (``RecordedChecker``), so it shows the loop agrees with the decision, never
that the decision was right, and the rehearsal's records, whose checker had failed on every query,
passed it. ``verify_batch(out, recompute=...)`` (harness/checkloop/recompute.py: from the recorded
store responses, with no store and no model) recomputes each such decision; one that diverges
makes the batch HELD (``held``; the diverging records in ``held_records``, each also a ``checker``
failure), and one that cannot be recomputed (no sources given, or not the arm's) is a ``checker``
failure: a checker decision is never passed unverified. ``decisions_recomputed`` counts those that
recomputed to themselves. A record set aside (in ``harness/`` with its sidecar) is not recomputed;
``divergence`` applies the halt rule (see ``divergence_groups``) and ``set_aside`` counts the set-aside
records and their re-runs per arm. ``records_sha256`` is the sha256 of the
compact JSON list of ``[path, sha256 of the file]`` pairs, sorted by path, over every input
of the analysis: the replayed records and the files that are only hashed (``hashed_only``).
Paths are relative to OUT (``records/<run_id>.json``), so a batch extracted from its archive
elsewhere has the same digest.

Replayed: the ``*.json`` files in ``records/``, ``infrastructure/``, ``harness/`` and
``aborted/``. Hashed only: the set-aside decisions (``harness/*.setaside.json``) and the
runner's ``concurrency-events.jsonl``, which the summary reads but which are not records.

Analysis accepts a batch only while (``require_current_check``) its check passed and it is not
held, its inputs
still have that digest (a record or decision changed, added or removed invalidates it), and
the check was made under the code in use, unchanged: the same commit and the same tree digest,
with the code clean. Otherwise verify_batch.py must run again.

The code (``CODE_PATHS``: every file under ``harness/`` and ``scripts/``, ``__pycache__`` and
compiled files left out, ``pyproject.toml``, ``uv.lock``, and the data the harness reads from its
own tree at run time to decide what it does, F1 review item 9: ``data/vocab/`` (the W3C term list
the compiler's rule 36 and the fault bank's conformance read), ``data/tokenizers/`` (the generated
source's token counts), ``stores/`` (the stores' pinned identities the fault bank's replays
read) and ``data/baselines/condition_au/`` (SPARQL-LLM's vendored validator and the metadata indexes
and prefix maps its cell loads by hash, issue 18: a saved check is invalid once one of them changes);
not the prompt blocks, each content-addressed, refused on load when its bytes are not its
name, and named in every record) is identified in one of two ways:

* a git checkout: HEAD, clean when git shows no change or untracked file in the code paths;
* a deployed copy without git: the files ``COMMIT`` and ``TREE`` at the code root, written in
  the clean checkout it was copied from, and clean only while the tree digest recomputed from
  the files on disk is the one ``TREE`` names and ``COMMIT`` is the commit ``TREE`` was written
  for (``TREE`` binds the two: ``{"schema": "code-tree/1", "commit", "tree_sha256"}``, F1 review
  item 10; a ``TREE`` of the earlier form, the digest alone, binds nothing and is refused).
  ``COMMIT`` alone is not enough: then git decides.

The tree digest is the sha256 of the compact JSON list of ``[path, sha256 of the file]`` pairs,
sorted by path, over the code files. Deploying to the run host:

    uv run python scripts/write_tree_digest.py          # in a clean checkout: writes COMMIT, TREE
    rsync -a --delete --exclude .git --exclude .venv --exclude '__pycache__' --exclude '*.pyc' \\
        --exclude results --exclude runs ./ HOST:~/dataset-84/code/

write_tree_digest.py refuses a checkout with uncommitted changes to the code paths. COMMIT and
TREE are git-ignored; in a checkout that has them they take precedence over git, so a later
commit needs them written again (until then a changed file fails the tree check, never passes).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.agent.record import RunRecord, redact_secrets, utc_now
from harness.agent.replay import replay_attempt

CHECK_SCHEMA = "replay-check/2"
CHECK_FILE = "replay-check.json"
BATCH_DIRS = ("records", "infrastructure", "harness", "aborted")
SETASIDE_SUFFIX = ".setaside.json"
HASHED_FILES = ("concurrency-events.jsonl",)
CODE_PATHS = ("harness", "scripts", "pyproject.toml", "uv.lock", "data/vocab", "data/tokenizers", "stores",
              "data/baselines/condition_au")
_COMPILED = (".pyc", ".pyo")
ERROR_LIMIT = 500
CODE_ROOT = Path(__file__).resolve().parent.parent.parent


class BatchNotVerified(Exception):
    """A batch has no passing, current replay check; the message says why and what to run."""


def input_kind(rel: str) -> str | None:
    """For a path relative to OUT: "replay" (a run record), "hashed" (an analysis input that is not
    a record) or None (not an input of the analysis)."""
    if rel in HASHED_FILES:
        return "hashed"
    sub, _, name = rel.partition("/")
    if sub not in BATCH_DIRS or "/" in name or not name.endswith(".json"):
        return None
    if name.endswith(SETASIDE_SUFFIX):
        return "hashed" if sub == "harness" else None
    return "replay"


def batch_inputs(out: Path) -> list[str]:
    """Every analysis input of a batch, as sorted paths relative to it."""
    found = [name for name in HASHED_FILES if (out / name).is_file()]
    for sub in BATCH_DIRS:
        found += [f"{sub}/{p.name}" for p in (out / sub).glob("*.json") if p.is_file()]
    return sorted(rel for rel in found if input_kind(rel))


def batch_files(out: Path) -> list[str]:
    """The run records of a batch (the inputs that are replayed), as sorted paths relative to it."""
    return [rel for rel in batch_inputs(out) if input_kind(rel) == "replay"]


def records_sha256(file_sha256: dict[str, str]) -> str:
    pairs = sorted([path, digest] for path, digest in file_sha256.items())
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def current_records_sha256(out: Path) -> str:
    return records_sha256({rel: hashlib.sha256((out / rel).read_bytes()).hexdigest() for rel in batch_inputs(out)})


def code_files(root: Path) -> list[str]:
    """The code files under ``root`` (``CODE_PATHS``), as sorted paths relative to it."""
    found = []
    for name in CODE_PATHS:
        path = root / name
        if path.is_file():
            found.append(name)
        elif path.is_dir():
            found += [f.relative_to(root).as_posix() for f in path.rglob("*")
                      if f.is_file() and "__pycache__" not in f.parts and f.suffix not in _COMPILED]
    return sorted(found)


def tree_sha256(root: Path) -> str:
    return records_sha256({rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in code_files(root)})


def _stat(path: str) -> tuple:
    # plain strings and os.stat: this runs before every record is written, so it stays far below a millisecond a file
    try:
        st = os.stat(path)
    except OSError:
        return (path, None, None)
    return (path, st.st_size, st.st_mtime_ns)


def _git_dir(root: Path) -> Path | None:
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():  # a worktree: "gitdir: <path>"
        text = dot.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            path = Path(text.partition(":")[2].strip())
            return path if path.is_absolute() else root / path
    return None


def code_stat(root: Path) -> tuple:
    """A cheap fingerprint of the code on disk, no file read (F1 review, item 8): each code file's path, size and
    modification time, the same of COMMIT and TREE, and of git's HEAD, its reflog and the index, so an edit, another
    deploy or a commit changes it. ``record.harness_commit`` recomputes the identity whenever it changes."""
    root = Path(root)
    entries = []
    for name in CODE_PATHS:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            entries.append(_stat(path))
        elif os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                entries += [_stat(os.path.join(dirpath, f)) for f in filenames if not f.endswith(_COMPILED)]
    entries += [_stat(os.path.join(root, name)) for name in ("COMMIT", "TREE")]
    git_dir = _git_dir(root)
    if git_dir is not None:
        entries += [_stat(os.path.join(git_dir, name)) for name in ("HEAD", "logs/HEAD", "index")]
    return tuple(sorted(entries, key=lambda e: e[0]))


TREE_SCHEMA = "code-tree/1"


def tree_file_text(commit: str, tree: str) -> str:
    """The TREE file write_tree_digest.py writes: the tree digest bound to the commit it was written for (F1 review,
    item 10), as one JSON object ``{"schema": "code-tree/1", "commit", "tree_sha256"}``."""
    return json.dumps({"schema": TREE_SCHEMA, "commit": commit, "tree_sha256": tree}) + "\n"


def read_tree_file(path: Path) -> dict[str, str] | None:
    """A TREE file's ``{"commit", "tree_sha256"}``, or None when it binds no commit (the earlier form, the digest
    alone, or anything that is not a code-tree/1 object)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != TREE_SCHEMA:
        return None
    if not isinstance(data.get("commit"), str) or not isinstance(data.get("tree_sha256"), str):
        return None
    return {"commit": data["commit"], "tree_sha256": data["tree_sha256"]}


def code_commit(root: Path | None = None) -> dict[str, Any]:
    """The code in use: ``{git_commit, git_dirty, source, tree_sha256}``, from ``COMMIT`` and ``TREE``
    at the code root when both are there (clean only while the files still have the tree digest TREE
    names and COMMIT is the commit TREE was written for; None when TREE binds no commit), else from
    git (clean when the code paths have no uncommitted change or untracked file; None when git cannot
    say). ``tree_sha256`` is always recomputed from the files on disk."""
    root = CODE_ROOT if root is None else Path(root)
    tree = tree_sha256(root)
    commit_file, tree_file = root / "COMMIT", root / "TREE"
    if commit_file.is_file() and tree_file.is_file():
        commit = commit_file.read_text(encoding="utf-8").strip() or None
        bound = read_tree_file(tree_file)
        dirty = None if bound is None else (bound["tree_sha256"] != tree or bound["commit"] != commit)
        return {"git_commit": commit, "git_dirty": dirty, "source": "COMMIT", "tree_sha256": tree}

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    inside = git("rev-parse", "--show-toplevel")
    if inside is None or Path(inside).resolve() != root.resolve():  # not a checkout of this code
        return {"git_commit": None, "git_dirty": None, "source": "git", "tree_sha256": tree}
    status = git("status", "--porcelain", "--untracked-files=all", "--", *CODE_PATHS)
    return {"git_commit": git("rev-parse", "HEAD"), "git_dirty": None if status is None else bool(status),
            "source": "git", "tree_sha256": tree}


class CodeNotIdentified(Exception):
    """The running code cannot be named by a commit and a tree digest; the message says why and what to do."""


def require_identified_code(code: dict[str, Any] | None = None, root: Path | None = None) -> dict[str, Any]:
    """``code`` (by default ``code_commit(root)``) when it names the code that runs; else CodeNotIdentified: a
    deployed copy whose tree digest, recomputed from its files, is not its ``TREE`` (it changed after it was
    deployed), or code with no commit (neither a git checkout of it nor a deploy with ``COMMIT`` and ``TREE``), whose
    records would name nothing (dress rehearsal F1). A git checkout with uncommitted changes passes: its records say
    so (``git_dirty``), and the grid launcher refuses it (run_pilot ``check_running_code``)."""
    root = CODE_ROOT if root is None else Path(root)
    code = code_commit(root) if code is None else code
    if code.get("source") == "COMMIT" and code.get("git_dirty") is not False:
        again = "deploy it again (scripts/write_tree_digest.py in the clean checkout it is copied from)"
        bound = read_tree_file(root / "TREE")
        if bound is None:
            raise CodeNotIdentified(f"the deployed code at {root}: its TREE file names no commit it was written for "
                                    f"(the earlier form, or not written by scripts/write_tree_digest.py), so its COMMIT "
                                    f"is bound to nothing; {again}")
        if bound["commit"] != code.get("git_commit"):
            raise CodeNotIdentified(f"the deployed code at {root}: COMMIT names {str(code.get('git_commit'))[:12]}, but "
                                    f"its TREE was written for {bound['commit'][:12]}, so its records would name the "
                                    f"wrong commit; {again}")
        raise CodeNotIdentified(
            f"the deployed code at {root} has tree digest {str(code.get('tree_sha256'))[:12]} (recomputed from its "
            f"files), not its TREE file's {bound['tree_sha256'][:12]}: the code changed after it was deployed, so no "
            f"record could name it; {again}")
    if not code.get("git_commit"):
        raise CodeNotIdentified(
            f"the code at {root} names no commit: it is neither a git checkout of the code nor a deploy with COMMIT and "
            f"TREE files, so its records would name no commit; write them with scripts/write_tree_digest.py in the "
            f"clean checkout it is copied from (harness/agent/replay_check.py)")
    return code


def _code_problem(code: dict[str, Any], what: str) -> str | None:
    if not code.get("git_commit"):
        return f"{what} has no commit"
    if code.get("git_dirty") is not False:
        changed = "differs from its TREE" if code.get("source") == "COMMIT" else "has uncommitted changes"
        return f"{what} ({str(code['git_commit'])[:7]}) {changed}, or it cannot be told"
    return None


def _error(exc: BaseException) -> str:
    return redact_secrets(f"{type(exc).__name__}: {exc}")[0][:ERROR_LIMIT]


def check_record(raw: bytes) -> tuple[str, str] | None:
    """None when the stored bytes load, write back to themselves and replay; else (stage, error).
    A closed-book probe record (schema ``closed-book-probe/*``, harness/probe) replays through
    the probe's own replay; every other record through the agent loop's."""
    from harness.probe.record import ProbeRecord, is_probe_record
    from harness.probe.replay import replay_probe

    try:
        data = json.loads(raw.decode("utf-8"))
        probe = is_probe_record(data)
        record = ProbeRecord.from_json(data) if probe else RunRecord.from_json(data)
    except Exception as exc:  # noqa: BLE001 - every record is checked and every failure reported
        return "load", _error(exc)
    try:
        written = record.dumps().encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return "roundtrip", _error(exc)
    if written != raw:
        return "roundtrip", "writing the loaded record does not give back its stored bytes"
    try:
        (replay_probe if probe else replay_attempt)(json.loads(written))
    except Exception as exc:  # noqa: BLE001
        return "replay", _error(exc)
    return None


NOT_RECOMPUTED = ("its online checker decision was not recomputed, so it is not verified: give the replay check "
                  "the sources (scripts/verify_batch.py --sources-dir DIR --hierarchy FILE; the runner passes its arms)")


def check_decision(data: dict, recompute: Callable[[dict], bool] | None) -> bool | tuple | None:
    """None when the record holds no checked online decision; True when its decision recomputes to itself; else
    (error, held, divergence): held when the recomputed decision diverges from the recorded one, not held when it
    could not be recomputed (``recompute`` None, or the arm's sources not given); ``divergence`` (held only, else
    None) is {"online", "offline"}: the recorded decision and the recomputed one (online.decision_summary; offline
    None when the recompute stopped before deciding)."""
    from harness.agent.replay import ReplayDivergence
    from harness.checkloop.online import decision_summary

    chk = data.get("checker")
    if not chk or (chk.get("initial") or {}).get("submission_index") is None:
        return None
    if recompute is None:
        return NOT_RECOMPUTED, False, None
    try:
        recompute(data)
    except ReplayDivergence as exc:
        divergence = {"online": decision_summary(chk["initial"], chk.get("retry") is not None),
                      "offline": getattr(exc, "recomputed", None)}
        return f"its online checker decision diverges from the offline recompute: {_error(exc)}", True, divergence
    except Exception as exc:  # noqa: BLE001 - every record is checked and every failure reported
        return f"its online checker decision could not be recomputed: {_error(exc)}", False, None
    return True


# -- releasing a held batch (science ruling, issue 3, 20 Oct 07:26) -------------------------------------------------
# A record whose online decision diverges from the offline recompute may be set aside as a harness defect on the
# divergence alone (scripts/relabel_harness_failure.py --diverged: classification checker_divergence, its score never
# read). Set aside, it is out of the recompute and of the analysis, and its attempt is re-run. But when more than 1%
# of a phase's records diverge, or divergences occur in more than one model, the checker code is suspect: the phase
# HALTS. Its records are no longer set aside one by one and the runner starts none of its attempts until the cause is
# fixed (relabel --checker-fix: under a check made by the fixed code, every divergent record of the phase is set
# aside with the fix recorded, and the phase re-runs them under that code). The count is per phase, over every record
# of the phase in the batch (set-aside ones included); a divergence is a held record or a checker_divergence set-aside
# not yet stamped with a fix, so setting records aside one at a time never lowers it, and a fix stops its
# divergences counting.

CHECKER_DIVERGENCE = "checker_divergence"  # a set-aside sidecar's classification (harness_defect)
PLAIN_PHASE = "run"  # the one phase of a run without the grid
HALT_SHARE = 100  # more than one in HALT_SHARE of a phase's records diverging halts it (integers: 100 * k > n)


def _grid_manifest(out: Path) -> dict | None:
    """A grid OUT's manifest (OUT/manifest.json), or None for a run without the grid."""
    from harness.checkloop.grid import GRID_MANIFEST

    path = Path(out) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except ValueError:
        return None
    return manifest if isinstance(manifest, dict) and manifest.get("schema") == GRID_MANIFEST else None


def cell_phases(out: Path) -> dict[str, str] | None:
    """A grid OUT's phase of each cell (its manifest's fingerprint.phases.<phase>.arms); None for a run without the
    grid, whose records are all of PLAIN_PHASE."""
    manifest = _grid_manifest(out)
    if manifest is None:
        return None
    phases = (manifest.get("fingerprint") or {}).get("phases") or {}
    return {cell: phase for phase, pinned in phases.items() if isinstance(pinned, dict)
            for cell in (pinned.get("arms") or {})}


def record_facts(data: dict, phases: dict[str, str] | None) -> dict[str, Any]:
    """What the halt rule and the set-aside counts read of a record: its attempt, its phase and the code that ran
    it, by commit and by tree digest (never its outcome or score)."""
    from harness.agent.loop import AgentConfig

    cell = (data.get("load") or {}).get("arm")
    harness = data.get("harness") or {}
    return {"run_id": data.get("run_id"), "question_id": data.get("question_id"), "model": data.get("model"),
            "repetition": data.get("repetition"), "arm": cell or data.get("condition") or AgentConfig().condition,
            "phase": PLAIN_PHASE if phases is None else phases.get(cell, f"unpinned:{cell}"),
            "code": harness.get("git_commit"), "tree": harness.get("tree_sha256")}


def standing_halts(out: Path, sidecars: dict[str, dict]) -> dict[str, str]:
    """The phases a periodic check halted while they ran (the grid manifest's ``periodic_checks``, written by
    scripts/run_pilot.py: science, issue 3, 20 Oct 10:39), each with its reason, while no checker fix of the phase was
    recorded after the halt (a checker_divergence sidecar of it whose fix is later: ``sidecars``, read_sidecars). The
    phase halts until the cause is fixed, so records finished after the halt never dilute it away."""
    found: dict[str, str] = {}
    for entry in (_grid_manifest(out) or {}).get("periodic_checks") or []:
        if not isinstance(entry, dict) or not entry.get("halted"):
            continue
        phase, at = entry.get("phase"), str(entry.get("at"))
        if any(s.get("classification") == CHECKER_DIVERGENCE and s.get("phase") == phase
               and str((s.get("fix") or {}).get("fixed_at") or "") > at for s in sidecars.values()):
            continue
        found[str(phase)] = (f"the periodic check at {at} halted it while it ran ("
                             f"{' and '.join(entry.get('reasons') or [])}), until a checker fix of it is recorded")
    return found


def divergence_groups(records: list[dict[str, Any]], standing: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The halt rule over a batch's records, each {"phase", "code", "tree", "model", "state"}, state "held" (its
    decision diverges now), "set_aside" (set aside as checker_divergence, no fix stamped), "fixed" (set aside with a
    fix) or "ok"; a row with ``n`` stands for n records (``halt_inputs``). Per phase: its records, its divergences
    (held and set_aside), their models and the code that made them, by commit (``codes``) and by tree digest
    (``trees``: a fix must be other code, not another commit id of the same code), and ``halted`` with the reasons:
    more than 1 in HALT_SHARE of its records diverging, or divergences in more than one model, or a halt that stands
    (``standing``: standing_halts)."""
    groups: dict[str, dict[str, Any]] = {}
    for r in records:
        n = r.get("n", 1)  # a row of the check's halt_inputs stands for n records alike
        g = groups.setdefault(r["phase"], {"phase": r["phase"], "records": 0, "held": 0, "set_aside": 0, "fixed": 0,
                                           "models": set(), "codes": set(), "trees": set()})
        g["records"] += n
        if r["state"] in ("held", "set_aside", "fixed"):
            g[r["state"]] += n
        if r["state"] in ("held", "set_aside"):
            g["models"].add(r["model"])
            g["codes"].add(r["code"])
            g["trees"].add(r.get("tree"))
    for phase in standing or {}:
        groups.setdefault(phase, {"phase": phase, "records": 0, "held": 0, "set_aside": 0, "fixed": 0,
                                  "models": set(), "codes": set(), "trees": set()})
    out = []
    for phase in sorted(groups, key=str):
        g = groups[phase]
        diverged = g["held"] + g["set_aside"]
        models, codes = sorted(g.pop("models"), key=str), sorted(g.pop("codes"), key=str)
        trees = sorted(g.pop("trees"), key=str)
        reasons = []
        if HALT_SHARE * diverged > g["records"]:
            reasons.append(f"{diverged} of its {g['records']} records diverge, more than 1 in {HALT_SHARE}")
        if len(models) > 1:
            reasons.append(f"divergences in {len(models)} models ({', '.join(map(str, models))})")
        if phase in (standing or {}):
            reasons.append(standing[phase])
        out.append({**g, "diverged": diverged, "models": models, "codes": codes, "trees": trees,
                    "halted": bool(reasons), "reasons": reasons})
    return out


def halt_message(out: Path, check: dict[str, Any], phase: str | None = None) -> str | None:
    """The refusal for a HALTED phase (``phase``; any phase when None) of the batch, or None."""
    halted = [g for g in (check.get("divergence") or {}).get("groups") or []
              if g.get("halted") and (phase is None or g.get("phase") == phase)]
    if not halted:
        return None
    listed = "; ".join(f"phase {g['phase']}: {' and '.join(g['reasons'])}" for g in halted)
    return (f"{out} is HALTED, its checker code suspect ({listed}): no attempt of the phase starts and no record of it "
            f"is set aside one by one until the cause is fixed; then verify the batch under the fixed code and set "
            f"the phase's divergent records aside with scripts/relabel_harness_failure.py --checker-fix PHASE")


HALT_KEYS = ("phase", "model", "code", "tree", "state")  # what the halt rule reads of each record


def halt_inputs(facts_of: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The halt rule's inputs over a batch, as the check stores them: one row per distinct (phase, model, code, tree,
    state) with its count ``n``, so the rule can be applied again to the check alone (``check_groups``)."""
    counted: dict[tuple, int] = {}
    for r in facts_of:
        key = tuple(r.get(k) for k in HALT_KEYS)
        counted[key] = counted.get(key, 0) + 1
    return [{**dict(zip(HALT_KEYS, key, strict=True)), "n": n}
            for key, n in sorted(counted.items(), key=lambda kv: [str(x) for x in kv[0]])]


def check_groups(check: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The halt rule applied again to a stored check's per-record inputs (``halt_inputs``), never its stored verdict;
    None for a check made before it stored them."""
    rows = check.get("halt_inputs")
    return None if not isinstance(rows, list) else divergence_groups(rows, check.get("standing_halts"))


SETASIDE_SCHEMA = "setaside/1"  # a set-aside sidecar's schema (scripts/relabel_harness_failure.py)
OPERATOR_FIELDS = frozenset({"defect", "ruling", "set_aside_at"})  # a divergence sidecar's fields the operator gives


def divergence_sidecar(check: dict[str, Any], entry: dict[str, Any], *, grid: bool) -> dict[str, Any]:
    """What a divergence set-aside's sidecar (scripts/relabel_harness_failure.py --diverged) takes from the replay
    ``check`` and its held record ``entry``: the record it decides (run id, file name and the sha256 of its bytes), its
    attempt (with its cell in a grid) and phase, the classification, the online and the offline decision, the code
    that made the online one, and the check it was made from. The sidecar holds these and the OPERATOR_FIELDS, and a
    fix once --checker-fix stamps one."""
    return {"schema": SETASIDE_SCHEMA, "run_id": entry.get("run_id"), "record": str(entry.get("file")).partition("/")[2],
            "sha256": entry.get("sha256"), "question_id": entry.get("question_id"), "model": entry.get("model"),
            "repetition": entry.get("repetition"), **({"arm": entry.get("arm")} if grid else {}),
            "label": "failed:harness", "failure_type": "harness_defect", "classification": CHECKER_DIVERGENCE,
            "divergence": {"online": entry.get("online"), "offline": entry.get("offline"), "error": entry.get("error")},
            "phase": entry.get("phase"), "online_code": entry.get("code"), "online_tree": entry.get("tree"),
            "check": {"checked_at": check.get("checked_at"), "code": check.get("code"),
                      "records_sha256": check.get("records_sha256")}}


def own_setaside(sidecar: Any, check: dict[str, Any], entry: dict[str, Any], *, grid: bool) -> bool:
    """Whether ``sidecar`` is the set-aside --diverged made from ``check`` for its held record ``entry``: each field
    divergence_sidecar takes from them is there and the same, and it holds nothing else but the operator's fields. A
    sidecar with a fix is not (release re-review, item 2): a fix stops its divergence counting, and one added by hand
    looks like one --checker-fix stamped, after which the batch is verified again anyway."""
    if not isinstance(sidecar, dict):
        return False
    expected = divergence_sidecar(check, entry, grid=grid)
    return set(sidecar) <= set(expected) | OPERATOR_FIELDS and all(
        k in sidecar and sidecar[k] == v for k, v in expected.items())


def check_covers(out: Path, check: dict[str, Any]) -> str | None:
    """None when the stored ``check`` covers every analysis input of the batch as it is now, by count and by hash; else
    why not (release review, item 6: a classification one record at a time must stand on a count of every record now
    in the batch). The one change allowed is the check's own divergence set-asides
    (scripts/relabel_harness_failure.py): a record it held, moved unchanged from records/ to harness/, with or without
    (an interrupted move) the sidecar made from this check; each is counted where the check found it, so several
    divergences below the halt are set aside from one check, and anything else needs the batch verified again. A
    sidecar is the check's own only when it says what the check held (``own_setaside``: release re-review, item 2),
    never by the check stamp it copied: one edited since, its classification above all, makes the check stale."""
    out = Path(out)
    entries = {str(h.get("file")): h for h in check.get("held_records") or []}
    held = {rel: h.get("sha256") for rel, h in entries.items()}
    grid = _grid_manifest(out) is not None
    stamp = check.get("records_sha256")
    found: dict[str, str] = {}
    not_own: list[str] = []
    for rel in batch_inputs(out):
        sha = hashlib.sha256((out / rel).read_bytes()).hexdigest()
        sub, _, name = rel.partition("/")
        if sub == "harness" and name.endswith(SETASIDE_SUFFIX):
            record = name.removesuffix(SETASIDE_SUFFIX) + ".json"
            entry = entries.get(f"records/{record}") or entries.get(f"harness/{record}")
            if entry is not None:
                try:
                    sidecar = json.loads((out / rel).read_text(encoding="utf-8"))
                except ValueError:
                    sidecar = None
                if own_setaside(sidecar, check, entry, grid=grid):
                    continue  # this check's own set-aside decision
                not_own.append(rel)  # hashed as any other input: it is not in the check's stamp
        if sub == "harness" and held.get(f"records/{name}") == sha and not (out / "records" / name).exists():
            rel = f"records/{name}"  # a record this check held, moved unchanged by a set-aside from it
        found[rel] = sha
    n_now = sum(input_kind(rel) == "replay" for rel in found)
    if n_now != check.get("n_records"):
        return (f"the batch has {n_now} records now, the replay check covers {check.get('n_records')}; verify the "
                f"batch again (scripts/verify_batch.py {out})")
    if records_sha256(found) != stamp:
        edited = (f"; {', '.join(not_own)} decide(s) a record the check held but not as the set-aside made from it"
                  if not_own else "")
        return (f"the batch's inputs changed since the replay check (records_sha256 differs{edited}); verify the batch "
                f"again (scripts/verify_batch.py {out})")
    return None


def read_sidecars(out: Path) -> dict[str, dict]:
    """The finished set-asides of a batch: each sidecar in harness/, by the name of the record it decides
    (``<name>.json`` for ``<name>.setaside.json``); an unreadable one as {} (its record stays set aside)."""
    found = {}
    for p in sorted((Path(out) / "harness").glob(f"*{SETASIDE_SUFFIX}")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        found[p.name.removesuffix(SETASIDE_SUFFIX) + ".json"] = data if isinstance(data, dict) else {}
    return found


def setaside_counts(set_aside: list[tuple[dict, dict]], recorded: set[tuple]) -> dict[str, dict[str, int]]:
    """Per arm, the set-aside records (``set_aside``: (record facts, sidecar) pairs), how many were set aside as
    checker divergences, and how many of their attempts were re-run (``recorded``: the attempts with a record in
    records/ now)."""
    counts: dict[str, dict[str, int]] = {}
    for facts, sidecar in set_aside:
        c = counts.setdefault(str(facts["arm"]), {"set_aside": 0, CHECKER_DIVERGENCE: 0, "rerun": 0})
        c["set_aside"] += 1
        c[CHECKER_DIVERGENCE] += sidecar.get("classification") == CHECKER_DIVERGENCE
        c["rerun"] += _attempt(facts) in recorded
    return dict(sorted(counts.items()))


def setaside_lines(check: dict[str, Any]) -> list[str]:
    """The set-aside counts of a check, one line per arm, for the runner's summary and verify_batch.py."""
    return [f"set aside in arm {arm}: {c['set_aside']} records ({c[CHECKER_DIVERGENCE]} checker divergences), "
            f"{c['rerun']} re-run" for arm, c in (check.get("set_aside") or {}).items()]


def _attempt(facts: dict[str, Any]) -> tuple:
    return (facts["arm"], facts["question_id"], facts["model"], facts["repetition"])


def divergence_so_far(out: Path, decisions: dict[str, Any], phase: str) -> dict[str, Any]:
    """The halt rule over phase ``phase``'s records completed so far, for the periodic check while the phase runs
    (scripts/run_pilot.py): every record ``decisions`` decided (``recompute_decisions``, made in a fresh interpreter;
    a record completed after it looked waits for the next check) and every set-aside record, by its facts, never its
    score. Counts and the halt verdict only."""
    out = Path(out)
    phases, sidecars = cell_phases(out), read_sidecars(out)
    rows, recomputed, not_recomputed = [], 0, 0
    for rel in batch_files(out):
        sidecar = _sidecar_of(rel, sidecars)
        if sidecar is None and rel not in decisions:
            continue
        try:
            facts = record_facts(json.loads((out / rel).read_bytes().decode("utf-8")), phases)
        except ValueError:
            continue
        if facts["phase"] != phase:
            continue
        if sidecar is not None:
            state = "ok"
            if sidecar.get("classification") == CHECKER_DIVERGENCE:
                state = "fixed" if sidecar.get("fix") else "set_aside"
        else:
            decided = decisions[rel]
            held = isinstance(decided, list) and len(decided) > 1 and decided[1] is True
            recomputed += decided is True
            not_recomputed += isinstance(decided, list) and not held
            state = "held" if held else "ok"
        rows.append({**facts, "state": state})
    g = next(iter(divergence_groups(rows)), None) or {}
    return {"records": g.get("records", 0), "decisions_recomputed": recomputed, "not_recomputed": not_recomputed,
            "held": g.get("held", 0), "diverged": g.get("diverged", 0), "models": g.get("models", []),
            "halted": bool(g.get("halted")), "reasons": g.get("reasons", [])}


def recompute_decisions(out: Path, recompute: Callable[[dict], bool] | None,
                        progress: Path | None = None) -> dict[str, Any]:
    """``check_decision`` for every run record of the batch that reads as JSON, as JSON: {path relative to OUT: None |
    true | [error, held]}. scripts/verify_batch.py --decisions-json writes it from a fresh interpreter, for the runner
    (``verify_batch(out, decisions=...)``). With ``progress``, each decision is also appended there as it is made, one
    JSON line ``{"file", "decided"}`` flushed at once, so a recompute stopped part way (the periodic check at the
    phase's end, entry 125) leaves the decisions it completed."""
    out = Path(out)
    sidecars = read_sidecars(out)
    found: dict[str, Any] = {}
    sink = Path(progress).open("a", encoding="utf-8") if progress is not None else None
    try:
        for rel in batch_files(out):
            if _sidecar_of(rel, sidecars) is not None:
                continue  # set aside: out of the recompute
            try:
                data = json.loads((out / rel).read_bytes().decode("utf-8"))
            except ValueError:
                continue  # the replay check reports it at its load stage
            decided = check_decision(data, recompute)
            found[rel] = list(decided) if isinstance(decided, tuple) else decided
            if sink is not None:
                sink.write(json.dumps({"file": rel, "decided": found[rel]}, ensure_ascii=False) + "\n")
                sink.flush()
    finally:
        if sink is not None:
            sink.close()
    return found


def read_progress(path: Path) -> dict[str, Any]:
    """The decisions a stopped ``recompute_decisions(..., progress=path)`` completed, by file: every whole line (a
    line cut by the stop is left out)."""
    found: dict[str, Any] = {}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return found
    # whole lines only: what follows the last newline is a line the stop cut (its bytes may end inside a character)
    for line in raw.split(b"\n")[:-1]:
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            found[entry["file"]] = entry.get("decided")
    return found


def _sidecar_of(rel: str, sidecars: dict[str, dict]) -> dict | None:
    """The set-aside decision of the record at ``rel`` (harness/ only), or None when it was not set aside."""
    sub, _, name = rel.partition("/")
    return sidecars.get(name) if sub == "harness" else None


STUDY_VAR_187 = "condition_au-audit-problems.json"
STUDY_VAR_186 = {"file": "records", "stage": "condition_au_audit",
                           "error": f"the SPARQL-LLM cell's records differ from their cell or their parents "
                                    f"({STUDY_VAR_187}, mode 600, read it only as the owner)"}


def _condition_au_manifest(out: Path, claimed: bool) -> tuple[Any, bool]:
    """(the OUT's parsed manifest or None, whether the audit applies to this OUT). It applies when the manifest NAMES the cell by
    its structure (a pinned branch arm, or a cell of the mode among a built OUT's baseline cells), or when a record CLAIMS the
    cell (``claimed``) though the manifest does not name it: then the records have no pin to be held to, which the audit reports.
    A manifest that does not parse but contains the word is refused as the audit's failure: no reader parses it either. Nothing
    of the cell is imported unless the audit applies (harness/baselines/condition_au/scope.py imports none)."""
    try:
        text = (Path(out) / "manifest.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None, claimed
    try:
        manifest = json.loads(text)
    except ValueError:
        return None, claimed or "condition_au" in text
    from harness.baselines.condition_au.scope import names_cell

    return manifest, claimed or bool(names_cell(manifest))


def _claims_condition_au(data: Any) -> bool:
    checker = data.get("checker") if isinstance(data, dict) else None
    settings = checker.get("settings") if isinstance(checker, dict) else None
    return isinstance(settings, dict) and settings.get("mode") == "condition_au"


def condition_au_audit(out: Path, claimed: bool = False) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """(a failure of the check or None, the audit's record for the check or None): the batch audit of the SPARQL-LLM cell's records
    (harness/baselines/condition_au/audit.py), run for an OUT whose manifest names the cell and inert for any other. Each record
    replays from itself, which cannot show that the index it names is the one its cell pins, that the index was built under its
    PARENT's store, or that its initial phase is its parent's; without this the analysis gate (which needs a passing check) would
    accept a batch the coverage check rejects. The pins are the manifest's (the runner wrote them from the spec's cell); the
    record holds their digest, so a stored check is current only while the manifest still pins the same indexes. A problem can say
    which way a record's retry went, so the failure names no record and counts none and the details go to a mode-600 file in the
    OUT (entry 191)."""
    out = Path(out)
    manifest, applies = _condition_au_manifest(out, claimed)
    if not applies:
        return None, None
    from harness.baselines.condition_au.audit import audit_dir, pins_from_manifest, pins_sha256

    pins = pins_from_manifest(manifest)
    record = {"pins_sha256": pins_sha256(pins)}
    problems = audit_dir(out / "records", pins) if manifest is not None else ["manifest.json is missing or does not parse"]
    detail = out / STUDY_VAR_187
    if not problems:
        detail.unlink(missing_ok=True)  # an earlier check's, now answered
        return None, record
    fd = os.open(detail, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # before the first byte: a file that existed keeps its old mode through os.open
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(problems, indent=1) + "\n")
    return dict(STUDY_VAR_186), record


def _condition_au_stale(out: Path, check: dict[str, Any]) -> str | None:
    """Why a stored check does not cover the OUT's SPARQL-LLM cell as its manifest pins it now (None: it does, or neither the
    check nor the manifest has such a cell): the check was made before the audit existed, the manifest's index pins changed after
    it, or the manifest no longer names the cell the check audited."""
    manifest, applies = _condition_au_manifest(out, False)
    audited = check.get("condition_au_audit")
    if not applies:
        return ("the manifest no longer names the SPARQL-LLM cell the check audited" if isinstance(audited, dict) else None)
    from harness.baselines.condition_au.scope import pins_from_manifest, pins_sha256

    if not isinstance(audited, dict):
        return "the check holds no SPARQL-LLM audit of this OUT's cell (made before it, or by other code)"
    if audited.get("pins_sha256") != pins_sha256(pins_from_manifest(manifest)):
        return "the manifest pins other SPARQL-LLM indexes than the check audited the records against"
    return None


def verify_batch(out: Path, recompute: Callable[[dict], bool] | None = None,
                 decisions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check every record of the batch, recomputing every online checker decision with ``recompute``
    (harness.checkloop.recompute.DecisionRecompute), or taking each from ``decisions`` (``recompute_decisions``, made
    in another process; a record it lacks is not recomputed); returns the check (``write_check`` stores it)."""
    out = Path(out)
    phases, sidecars = cell_phases(out), read_sidecars(out)
    shas: dict[str, str] = {}
    failures, hashed_only, held, n_records, recomputed = [], [], [], 0, 0
    condition_au_claimed = False  # whether any record claims to be a SPARQL-LLM cell's (the manifest need not say so)
    facts_of: list[dict[str, Any]] = []  # every loaded record, with its state for the halt rule
    set_aside: list[tuple[dict, dict]] = []
    recorded: set[tuple] = set()  # the attempts with a record in records/
    for rel in batch_inputs(out):
        raw = (out / rel).read_bytes()  # the bytes hashed are the bytes checked
        shas[rel] = hashlib.sha256(raw).hexdigest()
        if input_kind(rel) == "hashed":
            hashed_only.append(rel)
            continue
        n_records += 1
        failed = check_record(raw)
        if failed is not None:
            failures.append({"file": rel, "stage": failed[0], "error": failed[1]})
            continue
        data = json.loads(raw.decode("utf-8"))  # it loaded: check_record passed
        condition_au_claimed = condition_au_claimed or _claims_condition_au(data)
        facts = record_facts(data, phases)
        sidecar = _sidecar_of(rel, sidecars)
        if sidecar is not None:  # set aside (scripts/relabel_harness_failure.py): out of the recompute and analysis
            set_aside.append((facts, sidecar))
            state = "ok"
            if sidecar.get("classification") == CHECKER_DIVERGENCE:
                state = "fixed" if sidecar.get("fix") else "set_aside"
            facts_of.append({**facts, "state": state})
            continue
        if rel.startswith("records/"):
            recorded.add(_attempt(facts))
        if decisions is not None and rel in decisions:
            decided = tuple(decisions[rel]) if isinstance(decisions[rel], list) else decisions[rel]
        else:
            decided = check_decision(data, None if decisions is not None else recompute)
        if decided is None or decided is True:
            recomputed += decided is True
            facts_of.append({**facts, "state": "ok"})
            continue
        error, diverged = decided[0], decided[1]
        divergence = (decided[2] if len(decided) > 2 else None) or {}
        failures.append({"file": rel, "stage": "checker", "error": error})
        facts_of.append({**facts, "state": "held" if diverged else "ok"})
        if diverged:  # with what a set-aside needs without opening the record: its bytes' sha256 and its attempt
            held.append({"file": rel, "run_id": data.get("run_id"), "error": error, "sha256": shas[rel],
                         **{k: facts[k] for k in ("question_id", "model", "repetition", "arm", "phase", "code",
                                                  "tree")},
                         "online": divergence.get("online"), "offline": divergence.get("offline")})
    audit_failure, audit_record = condition_au_audit(out, condition_au_claimed)
    if audit_failure is not None:
        failures.append(audit_failure)
    standing = standing_halts(out, sidecars)
    groups = divergence_groups(facts_of, standing)
    check = {"schema": CHECK_SCHEMA, "checked_at": utc_now(), "code": code_commit(), "n_records": n_records,
            "n_ok": n_records - len(failures), "failures": failures, "hashed_only": hashed_only,
            "decisions_recomputed": recomputed, "held": bool(held), "held_records": held,
            "halted": any(g["halted"] for g in groups),
            "divergence": {"rule": f"a phase halts when more than 1 in {HALT_SHARE} of its records diverge, or "
                                   f"divergences occur in more than one model (a divergence: a held record, or one "
                                   f"set aside as {CHECKER_DIVERGENCE} with no fix recorded); a phase a periodic "
                                   f"check halted while it ran stays halted until a checker fix of it is recorded",
                           "groups": groups},
            "halt_inputs": halt_inputs(facts_of), "standing_halts": standing,
            "set_aside": setaside_counts(set_aside, recorded),
            "records_sha256": records_sha256(shas)}
    if audit_record is not None:  # only an OUT with the SPARQL-LLM cell: the check of any other keeps its shape
        check["condition_au_audit"] = audit_record
    return check


def held_message(out: Path, check: dict[str, Any]) -> str | None:
    """The refusal for a HELD batch (its online checker decisions diverge from the offline recompute), or None."""
    held = check.get("held_records") or []
    if not check.get("held") and not held:
        return None
    listed = "; ".join(f"{h.get('file')} (run {h.get('run_id')})" for h in held[:20])
    more = f" and {len(held) - 20} more" if len(held) > 20 else ""
    return (f"{out} is HELD: the online checker decisions of {len(held)} record(s) diverge from their offline "
            f"recompute ({CHECK_FILE}): {listed}{more}")


def write_check(out: Path, check: dict[str, Any]) -> Path:
    target = Path(out) / CHECK_FILE
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(check, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


def require_current_check(out: Path) -> dict[str, Any]:
    """The batch's stored check, when it passed and its files are unchanged; else BatchNotVerified."""
    out = Path(out)
    rerun = f"run `uv run python scripts/verify_batch.py {out}` first"
    path = out / CHECK_FILE
    if not path.is_file():
        raise BatchNotVerified(f"{out}: no {CHECK_FILE}, so its records were never shown to replay; {rerun}")
    try:
        check = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BatchNotVerified(f"{path}: not readable ({exc}); {rerun}") from None
    if check.get("schema") != CHECK_SCHEMA:
        raise BatchNotVerified(f"{path}: schema {check.get('schema')!r}, not {CHECK_SCHEMA!r} (an earlier check "
                               f"covers fewer inputs); {rerun}")
    held = held_message(out, check)
    if held:
        raise BatchNotVerified(f"{held}; re-run or set aside those records, then {rerun}")
    halted = halt_message(out, check)
    if halted:
        raise BatchNotVerified(f"{halted}, then {rerun}")
    if check.get("n_ok") != check.get("n_records") or check.get("failures"):
        raise BatchNotVerified(f"{out}: the replay check failed for {len(check.get('failures') or [])} of "
                               f"{check.get('n_records')} records (see {CHECK_FILE}); fix them, then {rerun}")
    if check.get("records_sha256") != current_records_sha256(out):
        raise BatchNotVerified(f"{out}: records or other inputs changed since the replay check (records_sha256 "
                               f"differs); {rerun}")
    stale = _condition_au_stale(out, check)
    if stale:
        raise BatchNotVerified(f"{out}: {stale}; {rerun}")
    # the check must have been made under the code in use, both committed (runner review)
    current, made = code_commit(), check.get("code") or {}
    problem = _code_problem(current, "the current code") or _code_problem(made, "the code the check was made under")
    if problem is None and made["git_commit"] != current["git_commit"]:
        problem = (f"the check was made under code {str(made['git_commit'])[:7]}; the current code is "
                   f"{str(current['git_commit'])[:7]}")
    if problem is None and made.get("tree_sha256") != current.get("tree_sha256"):
        problem = (f"the check was made under code tree {str(made.get('tree_sha256'))[:12]}; the current tree "
                   f"is {str(current.get('tree_sha256'))[:12]}")
    if problem:
        raise BatchNotVerified(f"{out}: {problem}; commit the code, then {rerun}")
    return check

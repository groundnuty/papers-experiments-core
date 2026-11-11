# Provenance: new
"""Build a study's replication package (`python -m expcore package`) and verify one (`python -m expcore package-check`).

`build_package` goes through these steps, and writes nothing to the output directory unless every one passes:
1. select the files the inclusion list names (`spec`); nothing else goes in;
2. require what the package's own check needs: the study's configuration and plugin, every file the code identity
   covers, every input of the replay check (records, outcome markers, the snapshot of the store, and the side files
   study.toml declares) with the check itself, and the four outputs (`export.json`, `scores.json`, `analysis.json`,
   `report.md`);
3. refuse a check made under code that is not a clean commit, outputs made by another core version, and a check that
   records no environment or another one than the build's (`provenance.environment`); warn about every other included
   file no commit holds and list it, and refuse one in an anonymised build unless the author allows it (`tracking`);
4. in every build, write each key location of models.toml (`key_file`, `key_env`) as its fixed placeholder: where a key
   lives is outside the code identity and the campaign fingerprint, so the file still verifies (`config.key_locations`,
   K13). With an anonymisation list (`anonymise`), also rewrite the prose and metadata files it names, give the
   package a code identity by tree hash alone: no commit id and no source URL anywhere (`identity`), and withhold the
   provenance headers of the vendored code (`withhold`);
5. add the generated files: `.code-stamp.json` (the commit and tree hash the check names, dated by the check), one
   wheel per distribution of the check's environment under `wheels/` with `wheels/requirements.txt` pinning each by
   hash (`wheels`), a `Makefile` whose `check` target installs those wheels into a fresh environment, offline, and
   runs `python -m expcore package-check .` with it, `PACKAGE.json` (what was built from what, under which core and
   environment, from which wheels, and the study's declaration of side files when it has one) and `SHA256SUMS`
   (`archive`);
6. refuse the package if a path or content of any file matches a study token, holds a term of the anonymisation
   list, or names a key location the study's models declare, or if its models.toml still holds a key location other
   than the placeholder; another file named models.toml is refused before that. The token check is
   `expcore.provenance.study_tokens`' own scanner with the hashes of names that are private everywhere (hosts,
   addresses, users, spaces, providers: the `all` scope): a study's own condition, dataset or model names belong in
   its own package. Both checks fail closed: a file goes in only if they can read all of it (text,
   SQLite databases and archives of them, `views`); anything else is refused, unless package.toml attests that exact
   file by its SHA-256 with a reason (`[[opaque]]`), and then it goes in unread. Every text member of every wheel is
   read too, and every member that is not text is attested by its hash and listed in PACKAGE.json (`wheelscan`); a
   wheel whose bytes the study's `uv.lock` records from pypi.org skips the token list alone (`lockfile`), and
   PACKAGE.json lists it with the lock's hash;
7. lay the package out in a scratch directory and run its check there, with this interpreter: every file against
   `SHA256SUMS`, the environment against the check's, the declaration of side files in `PACKAGE.json` against
   study.toml's, then the four outputs derived anew from the records and compared byte for byte (K07), which pairs
   every side file with its record again (R14);
8. write the deterministic archive `<out>/<name>.tar.gz` (K06).

Nothing in the package depends on when or where it was built: the only time in it comes from the replay check, and no
path of the build machine is added. The campaign snapshot keeps the attempt directories the store recorded, as the
replay check hashed them; the token check reads it like any file.
"""

import contextlib
import dataclasses
import hashlib
import json
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

from expcore.campaign.snapshot import SNAPSHOT_FILE
from expcore.config.key_locations import location_finders, withhold_key_locations
from expcore.config.loading import ConfigError
from expcore.models.route import sdk_version
from expcore.package.anonymise import Anonymisation, holds_term, load_anonymisation, refuse_models, rewrite
from expcore.package.archive import SUMS_FILE, render_sums, verify_sums, write_archive
from expcore.package.identity import (
    PREFIX_DIGITS,
    anonymise_identity,
    hidden_identity,
    lay_out,
    record_commits,
    replayed_records,
    stamp_bytes,
    verify_inputs,
)
from expcore.package.leaks import default_hashes, findings_in, private_hashes, scan_tokens
from expcore.package.lockfile import PYPI, lock_hashes, token_exempt
from expcore.package.spec import (
    GENERATED,
    SPEC_FILE,
    WHEELS_DIR,
    PackageError,
    PackageSpec,
    load_spec,
    select,
)
from expcore.package.tracking import Tracking, note_tracking
from expcore.package.wheels import REQUIREMENTS, Vendor, Wheel, makefile, requirements, vendor_wheels
from expcore.package.wheelscan import scan_wheels
from expcore.package.withhold import withhold_headers
from expcore.provenance.codestamp import STAMP, CodeVersionUnknown, covered_files, is_sha
from expcore.provenance.environment import EnvironmentUnknown, canonical, differences, environment, is_environment
from expcore.provenance.gitobjects import GitError, run_git
from expcore.provenance.hashfile import Hashes
from expcore.provenance.redact import Redactor, stop_message
from expcore.provenance.study_tokens import count_hashes
from expcore.records.build import CORE_DISTRIBUTION
from expcore.replay.check import CHECK_FILE, BatchNotVerified
from expcore.replay.side_files import declared
from expcore.study.config import CONFIG_FILES, MODELS_FILE, StudyConfig, load_study
from expcore.study.pipeline import ANALYSIS_FILE, OUT_DIR, OUTPUT_FILES, RUNS_DIR, Refused, regenerate

PACKAGE_FILE = "PACKAGE.json"
PACKAGE_FORMAT = "expcore-package/1"
SIDE_FILES_KEY = "side_files"  # the key of PACKAGE.json that records the study's declaration of side files
# The refusals a build's steps raise, whose message is shown as it is (after the redactor); any other exception is
# shown after its kind.
_REFUSALS = (PackageError, ConfigError, Refused, BatchNotVerified, EnvironmentUnknown, CodeVersionUnknown)


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def _read_json(files: Mapping[str, bytes], path: str, what: str) -> dict:
    try:
        data = json.loads(files[path])
    except (KeyError, ValueError):
        raise PackageError(f"{path}: {what} is not in the package or cannot be read") from None
    if not isinstance(data, dict):
        raise PackageError(f"{path}: {what} is not a JSON object")
    return data


def _required(study: StudyConfig, check: Mapping[str, object]) -> list[str]:
    """Every file the package's check needs: configuration, plugin, code, the check's inputs and the outputs."""
    root = study.root.resolve()
    try:
        plugin = study.plugin_file.resolve().relative_to(root).as_posix()
    except ValueError:
        raise PackageError(f"{study.plugin_file}: the plugin lies outside the study; no package can hold it") from None
    batch = f"{OUT_DIR}/{RUNS_DIR}"
    inputs = check.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise PackageError(f"{batch}/{CHECK_FILE}: names no input; run `python -m expcore check {batch}` first")
    return sorted(
        {*CONFIG_FILES, plugin, *covered_files(root, study.scope), f"{batch}/{CHECK_FILE}", f"{batch}/{SNAPSHOT_FILE}"}
        | {f"{batch}/{name}" for name in inputs}
        | {f"{OUT_DIR}/{name}" for name in OUTPUT_FILES}
    )


def _clean_code(check: Mapping[str, object], where: str) -> Mapping[str, object]:
    """The code identity of the check; refuse one that is not a clean commit with a tree hash."""
    code = check.get("code")
    ok = isinstance(code, dict) and is_sha(code.get("commit")) and isinstance(code.get("tree_sha256"), str)
    if not ok or code.get("dirty") or code.get("stale"):
        raise PackageError(
            f"{where}: the replay check was made under code that is not a clean commit; commit the study, then run "
            "check, analyze and report again: a package ships the code of one commit"
        )
    return code


def _environment(study: StudyConfig, check: Mapping[str, object], where: str) -> Mapping[str, Any]:
    """The environment the check ran under; refuse a check that records none, or a build under another one."""
    recorded = check.get("environment")
    if not is_environment(recorded):
        raise PackageError(f"{where}: records no environment; run `python -m expcore check` again with this core")
    try:
        now = environment(study.requires)
    except EnvironmentUnknown as error:
        raise PackageError(str(error)) from None
    changed = differences(recorded, now)
    if changed:
        raise PackageError(
            f"{where}: the check ran under another environment than this build's; build the package in the "
            "campaign's environment:\n  " + "\n  ".join(changed)
        )
    return recorded  # type: ignore[return-value]


def _own_distributions(env: Mapping[str, Any]) -> frozenset[str]:
    """The distributions vendored as the author's own code, which the key-location search reads (K13): those installed
    from a local directory, a checkout on the author's machine, and never the core, whatever its source. A distribution
    from an index, a URL or a repository is published code that cannot hold where the author's key lives, and may well
    name a generic variable a study also uses as its key_env (the core's own redaction names API_KEY and TOKEN)."""
    sources = env.get("sources") or {}
    core = canonical(CORE_DISTRIBUTION)
    return frozenset(
        canonical(name)
        for name, source in sources.items()
        if isinstance(source, Mapping) and source.get("kind") == "directory" and canonical(name) != core
    )


def _wheels(env: Mapping[str, Any], vendor: Vendor | None, supplied: Path | None) -> list[Wheel]:
    """One wheel per distribution of the environment (`wheels`); refuse a set that is not exactly that."""
    wheels = list((vendor or vendor_wheels)(env["distributions"], supplied))
    named = {(wheel.name, wheel.version) for wheel in wheels}
    if named != set(env["distributions"].items()) or len(named) != len(wheels):
        raise PackageError("the wheels to vendor are not exactly one per distribution of the check's environment")
    return wheels


def _package_json(
    spec: PackageSpec,
    study: StudyConfig,
    code: Mapping,
    check: Mapping,
    rewritten: Sequence[str],
    env: Mapping[str, Any],
    wheels: Sequence[Wheel],
    state: Tracking,
    *,
    rewritten_identity: bool,
    binaries: Mapping[str, list[dict[str, str]]],
    exempt: Mapping[str, str],
) -> bytes:
    data = {
        "format": PACKAGE_FORMAT,
        "name": spec.name,
        "study": study.name,
        "core_version": sdk_version(CORE_DISTRIBUTION),
        "code": {"commit": code["commit"], "tree_sha256": code["tree_sha256"]},
        "code_identity": (
            "tree hash only: every commit id is the first 40 hexadecimal digits of its tree hash, and no source is kept"
            if rewritten_identity
            else "commit and tree hash"
        ),
        "key_locations": (
            f"withheld: every key_file and key_env of {MODELS_FILE} is a fixed placeholder; where a key lives is not "
            "part of the code identity or the campaign fingerprint, so the file verifies as it is"
        ),
        "checked_at": check.get("checked_at"),
        "environment": env,
        "wheels": [
            {"file": w.path, "name": w.name, "version": w.version, "sha256": w.sha256}
            | {"read": "as it is" if w.from_index or w.path in exempt else "every view", "binaries": binaries[w.path]}
            | ({"uncompared": list(w.uncompared)} if w.uncompared else {})
            for w in sorted(wheels, key=lambda w: w.name)
        ],
        "study_tokens_exempt": [
            {"file": w.path, "name": w.name, "version": w.version, "lock_sha256": exempt[w.path], "lock_source": PYPI}
            for w in sorted(wheels, key=lambda w: w.name)
            if w.path in exempt
        ],
        "include": list(spec.include),
        "outputs": [f"{OUT_DIR}/{name}" for name in OUTPUT_FILES],
        "anonymised": list(rewritten),
        "vcs": state.vcs,
        "uncommitted": state.entries(),
        "opaque": [dataclasses.asdict(spec.opaque[path]) for path in sorted(spec.opaque)],
        "check": (
            "make -f Makefile check: the wheels in wheels/ into a fresh environment, offline; then each file against "
            "SHA256SUMS, the environment against the check's, and each output anew from the records"
        ),
    }
    if study.side_files:  # recorded only when declared, so the package of a study that declares none is as before
        data[SIDE_FILES_KEY] = declared(study.side_files)
    return (json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _attested(spec: PackageSpec, files: Mapping[str, bytes], redactor: Redactor) -> frozenset[str]:
    """The files package.toml attests by hash, each checked to be exactly the attested bytes."""
    for path, entry in sorted(spec.opaque.items()):
        if path.startswith(f"{WHEELS_DIR}/"):
            continue  # a member of a vendored wheel, checked when the wheels are read (`wheelscan`)
        if hashlib.sha256(files[path]).hexdigest() != entry.sha256:
            raise PackageError(
                f"{redactor.name(path)}: its SHA-256 is not the one [[opaque]] attests in {SPEC_FILE}; inspect the "
                "file again and attest its new hash, or leave it out"
            )
    return frozenset(path for path in spec.opaque if not path.startswith(f"{WHEELS_DIR}/"))


def collect(
    study_root: Path,
    *,
    hashes: Path | None = None,
    anonymise: Path | None = None,
    out_dir: Path | None = None,
    vendor: Vendor | None = None,
    wheels: Path | None = None,
    allow_uncommitted: bool = False,
    warn: Callable[[str], None] | None = None,
    note: Callable[[str], None] | None = None,
    hidden: dict[str, str] | None = None,
) -> tuple[PackageSpec, dict[str, bytes]]:
    """Select, check and complete the files of a package, without writing anything; return the spec and the files.

    `vendor` makes the wheels (`wheels.vendor_wheels` by default, which takes the author's own from the directory
    `wheels` first). `warn` receives each warning and `note` each line of information, such as how many study-token
    hashes were loaded (both standard error by default); `allow_uncommitted` lets an anonymised package include files
    no commit holds. `hashes` names a study-token hash file to use in place of the core's public one: it replaces that
    list, it does not add to it.

    The hashes load first, then the anonymisation list, so that every message after them, warnings and refusals
    alike, goes through one redactor (`provenance.redact`): nothing that holds a private name or a term of the
    anonymisation list is ever shown, and a hidden path is named by the start of its SHA-256. Every exception after
    the hashes, whatever raised it, leaves as a PackageError whose message went through it (`_guarded`). `hidden`, when
    given, is filled with each hidden name and the path it stands for, on purpose: the command line shows them only
    when the author asks (`--show-private-paths`).
    """
    study_root = Path(study_root)
    tokens, redactor = _token_list(hashes, note)
    with _guarded(redactor, hidden):
        terms = _terms(anonymise, study_root, out_dir, redactor)
        say = _scrubbing(redactor, warn or _warning)
        return _collect(study_root, tokens, terms, redactor, out_dir, vendor, wheels, allow_uncommitted, say)


def _token_list(hashes: Path | None, note: Callable[[str], None] | None) -> tuple[Hashes, Redactor]:
    """The hashes of names private everywhere, and the redactor every later message goes through; says how many."""
    tokens = private_hashes(default_hashes() if hashes is None else Path(hashes), given=hashes is not None)
    origin = "the core's public list" if hashes is None else "the list given with --hashes, which replaces the core's"
    (note or _stderr)(f"study-token check: {count_hashes(tokens)} of names private everywhere, from {origin}")
    return tokens, Redactor(tokens)


def _terms(anonymise: Path | None, study_root: Path, out_dir: Path | None, redactor: Redactor) -> Anonymisation | None:
    """The anonymisation list, if one is given; from then on the redactor hides its terms as well as the tokens."""
    if anonymise is None:
        return None
    terms = load_anonymisation(Path(anonymise), keep_out_of=private_list_guard(study_root, out_dir))
    redactor.also = partial(holds_term, terms)
    return terms


@contextlib.contextmanager
def _guarded(redactor: Redactor, hidden: dict[str, str] | None) -> Iterator[None]:
    """Every exception that leaves becomes a PackageError whose message went through the redactor: a refusal's as it
    is, any other's (a file that cannot be read, a wheel that cannot be opened, a directory that cannot be made) after
    its kind. So no message shows a path or a value that holds a private name or a term, whatever raised it."""
    try:
        yield
    except SystemExit as stop:  # a study's plugin, run during the build's own check, may exit with a message
        message = stop_message(stop)
        if message is None:
            raise  # an exit status goes on unchanged
        raise PackageError(redactor.scrub(message), private=redactor.hidden) from None
    except Exception as error:
        text = str(error) if isinstance(error, _REFUSALS) else f"{type(error).__name__}: {error}"
        raise PackageError(redactor.scrub(text), private=redactor.hidden) from None
    finally:
        if hidden is not None:
            hidden.update(redactor.hidden)


def _scrubbing(redactor: Redactor, say: Callable[[str], None]) -> Callable[[str], None]:
    """A warning callback that scrubs each line before it is said."""
    return lambda line: say(redactor.scrub(line))


def _warning(line: str) -> None:
    print(f"warning: {line}", file=sys.stderr)


def _collect(
    study_root: Path,
    tokens: Hashes,
    terms: Anonymisation | None,
    redactor: Redactor,
    out_dir: Path | None,
    vendor: Vendor | None,
    wheels: Path | None,
    allow_uncommitted: bool,
    warn: Callable[[str], None],
) -> tuple[PackageSpec, dict[str, bytes]]:
    study, spec, files, check = _selection(study_root)
    files[MODELS_FILE] = withhold_key_locations(files[MODELS_FILE])  # in every build; the identity agrees (K13)
    other = sorted(path for path in files if PurePosixPath(path).name == MODELS_FILE and path != MODELS_FILE)
    if other:
        raise PackageError(
            f"{other[0]}: a file named {MODELS_FILE} beside the study's own; a package withholds the key locations of "
            f"the study's {MODELS_FILE} only, so leave this one out of [package] include (K13)"
        )
    locations = location_finders(study.models.models.values())  # the values the rewrite withheld, looked for anywhere
    if terms is not None:
        refuse_models(terms, _model_names(study))
    where = f"{OUT_DIR}/{RUNS_DIR}/{CHECK_FILE}"
    opaque = _attested(spec, files, redactor)
    code = _clean_code(check, where)
    env = _environment(study, check, where)
    own = _own_distributions(env)  # before an anonymised build drops the sources
    _require_this_core(files)
    rewritten: list[str] = []
    covered = set(covered_files(study.root, study.scope))
    others = [p for p in files if not p.startswith(f"{OUT_DIR}/") and p not in covered]  # the identity and check vouch
    state = note_tracking(study.root, others, redactor, terms is not None, allow_uncommitted, warn)
    hidden_commits, hidden_urls = frozenset(), frozenset()
    if terms is not None:
        rewritten = list(terms.files)
        verify_inputs(files, check)  # before a record is read: the bytes must be the ones the check hashed
        hidden_commits, hidden_urls = hidden_identity(
            study.root, code, env, record_commits(files, replayed_records(check))
        )
        never = set(covered_files(study.root, study.scope)) | opaque  # code, and files attested unread
        files = rewrite(files, terms, code=never, out_prefix=f"{OUT_DIR}/")
        files, code, env = anonymise_identity(
            files, root=study.root, scope=study.scope, check=check, env=env, parent=out_dir
        )
    vendored = _wheels(env, vendor, wheels)
    if terms is not None:  # an anonymised package withholds the vendored code's provenance headers
        vendored, withheld = withhold_headers(vendored)
        hidden_commits |= {found[:PREFIX_DIGITS] for found in withheld.ids}
        hidden_urls |= {name.lower() for name in withheld.repositories}
    files.update({wheel.path: wheel.data for wheel in vendored})
    files[REQUIREMENTS] = requirements(vendored)
    opaque |= {wheel.path for wheel in vendored}  # read member by member below
    attested = {path: entry for path, entry in spec.opaque.items() if path.startswith(f"{WHEELS_DIR}/")}
    exempt = token_exempt(vendored, lock_hashes(study.root, warn))  # the bytes vendored, after headers withheld
    read = scan_wheels(
        vendored,
        tokens,
        terms=terms,
        hidden=(hidden_commits, hidden_urls),
        attested=attested,
        exempt=exempt,
        redactor=redactor,
        locations=locations,
        own={wheel.path for wheel in vendored if canonical(wheel.name) in own},
    )
    files["Makefile"] = makefile(env)
    files[PACKAGE_FILE] = _package_json(
        spec,
        study,
        code,
        check,
        rewritten,
        env,
        vendored,
        state,
        rewritten_identity=terms is not None,
        binaries=read.binaries,
        exempt=exempt,
    )
    files[STAMP] = stamp_bytes(
        files, study.root, study.scope, str(code["commit"]), str(check.get("checked_at")), out_dir
    )
    if json.loads(files[STAMP])["tree_sha256"] != code["tree_sha256"]:
        raise PackageError(f"{where}: the package's code is not the code the records were checked under")
    files[SUMS_FILE] = render_sums(files)
    generated = {*GENERATED, REQUIREMENTS}
    findings = read.findings + findings_in(
        files,
        tokens,
        terms,
        hidden_commits,
        hidden_urls,
        opaque,
        generated=generated,
        redactor=redactor,
        locations=locations,
    )
    if findings:
        raise PackageError("refusing to build the package:" + "".join(f"\n  {finding}" for finding in findings))
    return spec, files


def _selection(study_root: Path) -> tuple[StudyConfig, PackageSpec, dict[str, bytes], dict]:
    """Load the study, select its files and its replay check, and refuse a selection its check cannot run on."""
    try:
        study = load_study(study_root)
    except ConfigError as error:
        raise PackageError(str(error)) from None
    spec = load_spec(study.root)
    files = {path: (study.root / path).read_bytes() for path in select(study.root, spec)}
    check = _read_json(files, f"{OUT_DIR}/{RUNS_DIR}/{CHECK_FILE}", "the replay check")
    missing = [path for path in _required(study, check) if path not in files]
    if missing:
        more = f" and {len(missing) - 1} more" if len(missing) > 1 else ""
        raise PackageError(f"{SPEC_FILE}: the package's check needs {missing[0]}{more}; add it to [package] include")
    return study, spec, files, check


def _require_this_core(files: Mapping[str, bytes]) -> None:
    made_with = _read_json(files, f"{OUT_DIR}/{ANALYSIS_FILE}", "the analysis").get("core_version")
    if made_with != sdk_version(CORE_DISTRIBUTION):
        raise PackageError(
            f"{OUT_DIR}/{ANALYSIS_FILE}: made with core {made_with}, this is core {sdk_version(CORE_DISTRIBUTION)}; "
            "run analyze and report again with this core"
        )


def private_list_guard(study_root: Path, out_dir: Path | None) -> list[Path]:
    """Where an anonymisation list may not lie (K03): the study's repository, if any, the study and the output."""
    places = [Path(study_root)] + ([] if out_dir is None else [Path(out_dir)])
    try:
        top = run_git(Path(study_root), "rev-parse", "--show-toplevel").decode("utf-8").strip()
    except (GitError, OSError):
        return places
    return [Path(top), *places] if top else places


def _model_names(study: StudyConfig) -> set[str]:
    names = set()
    for key in study.models:
        spec = study.models.resolve(key)
        names |= {key, spec.api_id, *spec.aliases}
    return names


def verify_package(root: Path, *, redactor: Redactor | None = None) -> list[str]:
    """The package's own check: the core version, every file against SHA256SUMS, the environment against the one the
    check ran under (names and versions: the package is installed from its own wheels), then the outputs byte for
    byte. With `redactor`, each problem comes back through it (the build's own, or one of the core's public list for
    `package-check`), so none shows a path or a value it must not."""
    problems = _verify(Path(root))
    return problems if redactor is None else [redactor.scrub(problem) for problem in problems]


def _verify(root: Path) -> list[str]:
    try:
        package = json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))
        expected, recorded = package["core_version"], package["environment"]
    except (OSError, ValueError, KeyError, TypeError):
        return [f"{PACKAGE_FILE}: missing or unreadable, so the package cannot say which core it needs"]
    installed = sdk_version(CORE_DISTRIBUTION)
    if installed != expected:
        return [f"this package was made with core {expected}, but core {installed} is installed; install {expected}"]
    problems = verify_sums(root)
    if problems:
        return problems
    try:
        study = load_study(root)
        changed = differences(recorded, environment(study.requires), sources=False)
        if changed:
            return [
                f"the environment in use is not the one {PACKAGE_FILE} names, which the records were checked under; "
                "`make -f Makefile check` installs it from wheels/: " + "; ".join(changed)
            ]
        if package.get(SIDE_FILES_KEY, []) != declared(study.side_files):
            return [f"{PACKAGE_FILE} records another declaration of side files than the package's study.toml"]
        return regenerate(root, stamped=True)
    except (Refused, BatchNotVerified, ConfigError, EnvironmentUnknown) as error:
        return [str(error)]


def build_package(
    study_root: Path,
    out_dir: Path,
    *,
    hashes: Path | None = None,
    anonymise: Path | None = None,
    vendor: Vendor | None = None,
    wheels: Path | None = None,
    allow_uncommitted: bool = False,
    warn: Callable[[str], None] | None = None,
    note: Callable[[str], None] | None = None,
    hidden: dict[str, str] | None = None,
) -> Path:
    """Build the package of a study into `out_dir/<name>.tar.gz` and return the archive's path (see `collect`)."""
    out_dir, study_root = Path(out_dir), Path(study_root)
    tokens, redactor = _token_list(hashes, note)
    with _guarded(redactor, hidden):  # the output directory, the staging and the archive included
        terms = _terms(anonymise, study_root, out_dir, redactor)
        batch = (study_root / OUT_DIR).resolve()
        if out_dir.resolve() == batch or batch in out_dir.resolve().parents:
            raise PackageError(
                f"the output directory lies inside the study's {OUT_DIR}/, whose files the package is checked against"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        say = _scrubbing(redactor, warn or _warning)
        spec, files = _collect(study_root, tokens, terms, redactor, out_dir, vendor, wheels, allow_uncommitted, say)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=out_dir) as scratch:
            stage = Path(scratch) / spec.name
            lay_out(stage, files)
            problems = verify_package(stage, redactor=redactor)
        if problems:
            raise PackageError("the package fails its own check:\n  " + "\n  ".join(problems))
        return write_archive(out_dir / f"{spec.name}.tar.gz", spec.name, files)


__all__ = [
    "PackageError",
    "build_package",
    "collect",
    "default_hashes",
    "private_hashes",
    "scan_tokens",
    "verify_package",
]

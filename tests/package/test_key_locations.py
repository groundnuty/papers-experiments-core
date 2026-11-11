"""Where a model's key lives never reaches a replication package, and is not part of the experiment (K13).

The example study gains two models its plan does not run, one with its key in a file under a folder named after an
earlier study, one with its key in a variable named after it. Neither name is in the study-token list, so only the
withholding keeps them out of the package. The campaign runs once per module.
"""

import dataclasses
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from study_support import EXAMPLE, HEAD, Campaign, clean_environment, cli, copy_campaign, finish

from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.package import build as package_build
from expcore.package.build import PACKAGE_FILE, build_package, verify_package
from expcore.package.spec import PackageError
from expcore.provenance.codestamp import CodeScope, tree_hash, write_stamp
from expcore.provenance.hashfile import render_hash_file
from expcore.records.record import RunRecord, decode
from expcore.replay.check import CHECK_FILE, inputs_sha256
from expcore.study.config import load_study

NAME = "tiny-study-package"
AUTHOR = "Quillon Varek"  # invented; what the anonymisation list hides
KEY_FILE = "~/vaults/quillfeather-tribunal-2025/gamma-access.key"  # invented: an earlier study's folder
KEY_ENV = "QUILLFEATHER_DELTA_KEY"  # invented: a variable named after it
MOVED_FILE = "/srv/keys/gamma.key"
MOVED_ENV = "DELTA_KEY"
FRAGMENTS = ("quillfeather", "tribunal", "vaults", "gamma-access")  # every part of both locations, lower case
KEYED = f"""
# Two models the plan does not run, each with its key where the author keeps it.
[models.gamma-remote]
backend = "openai_compat"
api_id = "gamma-1"
base_url = "https://llm.example.invalid/v1"
key_file = "{KEY_FILE}"  # read when a request is built
status = "draft"

[models.delta-remote]
backend = "anthropic"
api_id = "delta-1"
base_url = "https://api.example.invalid"
'key_env' = '{KEY_ENV}'
status = "draft"
"""

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module", autouse=True)
def stand_in_wheels():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file(["zarnothwick"]), encoding="utf-8")  # a name the key locations do not hold
    return path


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment) -> Campaign:
    """The example with the two keyed models, stamped, run, checked, analysed and reported."""
    root = tmp_path_factory.mktemp("campaign") / "study"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("out", "__pycache__"))
    with (root / "models.toml").open("a", encoding="utf-8") as table:
        table.write(KEYED)
    write_stamp(root, HEAD, load_study(root).scope)
    campaign = Campaign(root)
    campaign.codes["run"], _ = cli(clean_environment, "run", str(root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def anonymisation(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("list") / "anon.toml"
    path.write_text(f'files = ["README.md"]\n\n[terms]\n"{AUTHOR}" = "Author A"\n', encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def builds(campaign, hashes, anonymisation, tmp_path_factory) -> dict[str, Path]:
    """The plain build and the anonymised build of the one batch."""
    return {
        "plain": build_package(campaign.root, tmp_path_factory.mktemp("plain"), hashes=hashes),
        "anonymised": build_package(
            campaign.root, tmp_path_factory.mktemp("anonymised"), hashes=hashes, anonymise=anonymisation
        ),
    }


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def every_stream(archive: Path) -> dict[str, bytes]:
    """Every member of the archive by name, and every member of each wheel in it, decompressed."""
    found = {}
    with tarfile.open(archive) as members:
        for member in members.getmembers():
            data = members.extractfile(member).read() if member.isfile() else b""
            found[member.name] = data
            if member.name.endswith(".whl"):
                with zipfile.ZipFile(io.BytesIO(data)) as wheel:
                    found |= {f"{member.name}!{inner}": wheel.read(inner) for inner in wheel.namelist()}
    return found


@pytest.mark.parametrize("build", ["plain", "anonymised"])
def test_k13_no_part_of_a_key_location_is_anywhere_in_the_package(builds, campaign, build):
    """K13: no part of the key file's path or of the key variable's name is in any member's name or bytes, the
    records, the store's snapshot, PACKAGE.json and the wheels included; models.toml holds the placeholders, in both
    builds, and PACKAGE.json says so."""
    streams = every_stream(builds[build])
    assert any(name.endswith(".sqlite") for name in streams)
    assert any("/out/runs/" in name and name.endswith(".json") for name in streams)
    for name, data in streams.items():
        for fragment in FRAGMENTS:
            assert fragment not in name.lower(), f"{name} names {fragment}"
            assert fragment.encode() not in data.lower(), f"{name} holds {fragment}"
    shipped = streams[f"{NAME}/models.toml"].decode("utf-8")
    assert 'key_file = "<withheld>"  # read when a request is built' in shipped
    assert "'key_env' = \"KEY_WITHHELD\"" in shipped
    original = (campaign.root / "models.toml").read_text(encoding="utf-8")
    assert shipped == original.replace(KEY_FILE, "<withheld>").replace(f"'{KEY_ENV}'", '"KEY_WITHHELD"')
    assert json.loads(streams[f"{NAME}/{PACKAGE_FILE}"])["key_locations"].startswith("withheld: every key_file")


@pytest.mark.parametrize("build", ["plain", "anonymised"])
def test_k13_the_package_with_its_key_locations_withheld_passes_its_own_check(builds, campaign, build, tmp_path):
    """K13: the shipped models.toml is the very form the code identity hashes, so the tree hash a reader recomputes
    from the package, even hashing every file as it is, is the one its records were checked under; the author's file
    as it is would give another. The package's check passes offline as it is."""
    root = unpack(builds[build], tmp_path)
    assert verify_package(root) == []
    check = json.loads((root / "out" / "runs" / CHECK_FILE).read_text(encoding="utf-8"))
    scope = load_study(root).scope
    as_it_is = CodeScope.of(scope.roots)  # no canonical form: every file hashed as it is, as an earlier core did
    assert tree_hash(root, scope) == tree_hash(root, as_it_is) == check["code"]["tree_sha256"]
    assert tree_hash(campaign.root, as_it_is) != check["code"]["tree_sha256"]


def test_k13_the_plain_packages_make_check_passes(builds, tmp_path, clean_environment):
    """K13: `make check` of the plain package, which keeps the commits, passes with the key locations withheld."""
    root = unpack(builds["plain"], tmp_path)
    assert 'key_file = "<withheld>"' in (root / "models.toml").read_text(encoding="utf-8")
    done = subprocess.run(
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_k13_a_package_whose_models_toml_still_holds_a_key_location_is_refused(campaign, hashes, tmp_path):
    """K13: behind the rewrite, the build's own scan refuses a models.toml that still holds a key location, naming the
    model and the key and never the value."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(package_build, "withhold_key_locations", lambda data: data, raising=False)
        with pytest.raises(PackageError) as caught:
            build_package(campaign.root, tmp_path / "dist", hashes=hashes)
    message = str(caught.value)
    assert "models.toml: [models.gamma-remote]: key 'key_file': a key location" in message
    assert "models.toml: [models.delta-remote]: key 'key_env': a key location" in message
    assert not any(fragment in message.lower() for fragment in FRAGMENTS)
    assert not list((tmp_path / "dist").glob("*.tar.gz"))


def test_k13_any_file_that_names_a_key_location_refuses_the_build(campaign, hashes, tmp_path):
    """K13: the build looks for every key location the study's models declare in every file it packages, under every
    view, here a README naming the key file's path and an encoded copy of the variable's name, and refuses each,
    naming the file and the line and never the value."""
    copy = copy_campaign(campaign, tmp_path, "named")
    readme = copy.root / "README.md"
    escaped = "".join(f"\\u{ord(char):04x}" for char in KEY_ENV)  # the name only an escape-decoding view shows
    readme.write_text(
        readme.read_text(encoding="utf-8") + f"\nOur key lives in {KEY_FILE}.\n\nSet `{escaped}`.\n", encoding="utf-8"
    )
    lines = readme.read_text(encoding="utf-8").count("\n")
    with pytest.raises(PackageError) as caught:
        build_package(copy.root, tmp_path / "dist", hashes=hashes)
    message = str(caught.value)
    assert f"README.md:{lines - 2}: a key location the study's models.toml declares" in message
    assert f"README.md:{lines}: a key location the study's models.toml declares" in message
    assert not any(fragment in message.lower() for fragment in FRAGMENTS)


def test_k13_another_file_named_models_toml_refuses_the_build(campaign, hashes, tmp_path):
    """K13: a package withholds the key locations of the study's own models.toml only, so another file of that name
    that the inclusion list takes, which could hold a key location of its own, refuses the build."""
    copy = copy_campaign(campaign, tmp_path, "second")
    (copy.root / "notes").mkdir()
    (copy.root / "notes" / "models.toml").write_text('[models.old]\nbackend = "stub"\n', encoding="utf-8")
    spec = copy.root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('"README.md",', '"README.md",\n    "notes",'), "utf-8")
    with pytest.raises(PackageError, match=r"notes/models\.toml: a file named models\.toml beside the study's own"):
        build_package(copy.root, tmp_path / "dist", hashes=hashes)


def move_keys(root: Path) -> None:
    table = root / "models.toml"
    text = table.read_text(encoding="utf-8")
    table.write_text(text.replace(KEY_FILE, MOVED_FILE).replace(KEY_ENV, MOVED_ENV), encoding="utf-8")


def test_k13_moving_a_key_leaves_the_campaign_one_experiment(campaign, tmp_path, clean_environment):
    """K13: after both keys move, the code identity, its stamp and the campaign fingerprint are unchanged: the campaign
    resumes, and its replay check is still current, so the analysis runs without a new check."""
    copy = copy_campaign(campaign, tmp_path, "moved")
    recorded = copy.sql("SELECT value FROM meta WHERE key=?", FINGERPRINT_KEY)[0]["value"]
    before = tree_hash(copy.root, load_study(copy.root).scope)
    move_keys(copy.root)
    assert tree_hash(copy.root, load_study(copy.root).scope) == before
    code, errors = cli(clean_environment, "run", str(copy.root))
    assert code == 0, errors
    assert copy.sql("SELECT value FROM meta WHERE key=?", FINGERPRINT_KEY)[0]["value"] == recorded
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 0, errors


def as_an_earlier_core_left_it(copy: Campaign) -> str:
    """Make the copy's campaign what a core that counted key locations made: the store's hash of models.toml, and the
    tree hash in every record and in the check, are those of the file as it is. Return that tree hash."""
    raw = (copy.root / "models.toml").read_bytes()
    connection = sqlite3.connect(copy.root / "out" / "state.sqlite")
    try:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (FINGERPRINT_KEY,)).fetchone()
        fingerprint = json.loads(row[0])
        fingerprint["configs"]["models.toml"] = hashlib.sha256(raw).hexdigest()
        text = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), allow_nan=False)
        connection.execute("UPDATE meta SET value=? WHERE key=?", (text, FINGERPRINT_KEY))
        connection.commit()
    finally:
        connection.close()
    earlier = tree_hash(copy.root, CodeScope.of(load_study(copy.root).scope.roots))
    check_path = copy.runs / CHECK_FILE
    check = json.loads(check_path.read_text(encoding="utf-8"))
    for rel in check["replayed"]:
        path = copy.runs / rel
        record = RunRecord.from_json(decode(path.read_bytes()))
        rewritten = dataclasses.replace(record, code={**record.code, "tree_sha256": earlier})
        path.write_bytes(rewritten.dumps(environ={}).encode("utf-8"))
        check["inputs"][rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    check["inputs_sha256"] = inputs_sha256(check["inputs"])
    check["code"]["tree_sha256"] = earlier
    check_path.write_text(json.dumps(check, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return earlier


def test_k13_a_campaign_an_earlier_core_started_is_refused_a_resume_saying_why_and_checks_again(
    campaign, tmp_path, clean_environment
):
    """K13: a campaign whose fingerprint and records an earlier core made, counting where the keys live, is refused a
    resume by a refusal that names the leaf and why it differs; the analysis asks for a new check, the check replays
    its records, which still name the earlier tree hash, and the analysis then runs."""
    copy = copy_campaign(campaign, tmp_path, "earlier")
    earlier = as_an_earlier_core_left_it(copy)
    code, errors = cli(clean_environment, "run", str(copy.root))
    assert code != 0
    assert "configs.models.toml: recorded with where each key lives, which the core that started the campaign" in errors
    assert "(K13); finish the campaign with that core, or start a new store" in errors
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code != 0
    assert f"the check was made under code tree {earlier[:12]}" in errors
    assert "python -m expcore check" in errors
    for command, target in (("check", copy.runs), ("analyze", copy.root), ("report", copy.root)):
        code, errors = cli(clean_environment, command, str(target))
        assert code == 0, f"{command}: {errors}"
    check = json.loads((copy.runs / CHECK_FILE).read_text(encoding="utf-8"))
    assert check["n_ok"] == check["n_records"]
    assert check["code"]["tree_sha256"] != earlier

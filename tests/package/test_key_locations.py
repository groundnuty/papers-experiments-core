"""Where a model's key lives never reaches a replication package, and is not part of the experiment (K13).

The example study gains two models its plan does not run, one with its key in a file under a folder named after an
earlier study, one with its key in a variable named after it. Neither name is in the study-token list, so only the
withholding keeps them out of the package. The campaign runs once per module.
"""

import base64
import dataclasses
import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from package_support import installed_distribution, stand_in, stand_in_vendor, wheel_of
from study_support import EXAMPLE, HEAD, Campaign, clean_environment, cli, copy_campaign, finish

from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.config.key_locations import location_finders
from expcore.config.loading import ConfigError
from expcore.package import build as package_build
from expcore.package import wheels as wheels_module
from expcore.package.build import PACKAGE_FILE, build_package, private_hashes, verify_package
from expcore.package.leaks import FOUND_COPY, FOUND_LOCATION, find_locations
from expcore.package.spec import Opaque, PackageError
from expcore.package.wheels import Wheel
from expcore.package.wheelscan import scan_wheels
from expcore.provenance.codestamp import CodeScope, tree_hash, write_stamp
from expcore.provenance.environment import CORE_DISTRIBUTION
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


CLEARED = "code you did not write can name a key location by chance"  # how a third-party hit is cleared
GENERIC = b"SECRET_NAME = re.compile(r'(?:^|_)(?:API_KEY|SECRET|TOKEN)$')\n"  # as the core's own redaction names them


THIRD_PARTY = "numpy"  # a distribution of the example's environment, here installed from a repository


def repository_vendor(*names: str):
    """A vendor that installs each distribution in `names` from a repository, so it is read with every view, with a
    module naming the generic variable the study's key_env is; every other distribution comes from an index."""

    def vendor(distributions, supplied=None):
        wheels = []
        for name, version in sorted(distributions.items()):
            if name in names:
                filename, data = wheel_of(name, version, {f"{name.replace('-', '_')}/naming.py": GENERIC})
                wheels.append(Wheel(name, version, filename, data, from_index=False, direct=True))
            else:
                wheels.append(Wheel(name, version, *stand_in(name, version), from_index=True))
        return wheels

    return vendor


def test_k13_a_generic_key_variable_named_by_the_core_installed_from_a_repository_builds(campaign, hashes, tmp_path):
    """K13: the core, however it was installed (here from a repository, so read with every view), is not the author's
    code and is never searched for key locations: a study whose key_env is API_KEY, which the core's own redaction
    names, still builds."""
    copy = copy_campaign(campaign, tmp_path, "generic")
    move_keys(copy.root, env="API_KEY")
    archive = build_package(copy.root, tmp_path / "dist", hashes=hashes, vendor=repository_vendor(CORE_DISTRIBUTION))
    assert "'key_env' = \"KEY_WITHHELD\"" in every_stream(archive)[f"{NAME}/models.toml"].decode("utf-8")


def test_k13_a_key_location_in_a_wheel_from_a_repository_refuses_the_build_until_attested(campaign, hashes, tmp_path):
    """K13: a wheel of any distribution not from an index, the core's aside, can be the author's own code, so it is
    searched: one installed from a repository that names the study's key_env refuses the build, naming the member,
    saying how a distribution the author did not write is cleared, and printing its attestation, never the value. Read
    by hand and attested in [[opaque]], it builds."""
    copy = copy_campaign(campaign, tmp_path, "third-party")
    move_keys(copy.root, env="API_KEY")
    vendor = repository_vendor(CORE_DISTRIBUTION, THIRD_PARTY)
    with pytest.raises(PackageError) as refused:
        build_package(copy.root, tmp_path / "dist", hashes=hashes, vendor=vendor)
    message = str(refused.value)
    member = re.search(rf"wheels/{THIRD_PARTY}-[^/]+\.whl/{THIRD_PARTY}/naming\.py", message)
    assert member is not None, message
    assert f"{member[0]}:1: a key location the study's models.toml declares" in message
    assert f"{member[0]}: {CLEARED}" in message
    digest = hashlib.sha256(GENERIC).hexdigest()
    assert f'[[opaque]] path = "{member[0]}", sha256 = "{digest}"' in message
    assert "API_KEY" not in message
    assert f"{CORE_DISTRIBUTION.replace('-', '_')}/naming.py" not in message
    attestation = f'\n[[opaque]]\npath = "{member[0]}"\nsha256 = "{digest}"\nreason = "read by hand: a generic name"\n'
    spec = copy.root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8") + attestation, encoding="utf-8")
    build_package(copy.root, tmp_path / "dist", hashes=hashes, vendor=vendor)


SOURCES = {
    "a git repository": {"url": "git+file:///srv/src/toy-kit", "vcs_info": {"vcs": "git", "commit_id": "c" * 40}},
    "a private helper over ssh": {
        "url": "git+ssh://git@code.example.org/lab/toy-kit.git",
        "vcs_info": {"vcs": "git", "commit_id": "c" * 40},
    },
    "a local wheel": {
        "url": "file:///srv/src/toy-kit/dist/toy_kit-1.0-py3-none-any.whl",
        "archive_info": {"hashes": {"sha256": "d" * 64}},
    },
    "a URL": {"url": "https://code.example.org/toy_kit-1.0.tar.gz", "archive_info": {}},
    "a directory": {"url": "file:///srv/src/toy-kit", "dir_info": {}},
}


def scanned(tmp_path, monkeypatch, hashes, name: str, direct_url: dict | None, attested=None) -> list[str]:
    """The findings of the wheel scan for one distribution installed as `direct_url` says (None: from an index),
    holding a module that names API_KEY, under models whose key_env is API_KEY."""
    site = tmp_path / "lib" / "site-packages"
    site.mkdir(parents=True)
    module = f"{name.replace('-', '_')}/naming.py"
    shown = None if direct_url is None else json.dumps(direct_url)
    dist = installed_distribution(site, name, "1.0", {module: GENERIC}, direct_url=shown)
    monkeypatch.setattr(wheels_module, "installed", lambda: {name: dist})
    [wheel] = wheels_module.vendor_wheels({name: "1.0"})
    finders = location_finders([SimpleNamespace(key_file=None, key_env="API_KEY")])
    scan = scan_wheels(
        [wheel],
        private_hashes(hashes),
        terms=None,
        hidden=(frozenset(), frozenset()),
        attested=attested or {},
        locations=finders,
    )
    return scan.findings


@pytest.mark.parametrize("source", SOURCES)
def test_k13_every_wheel_not_from_an_index_is_searched_whatever_its_source(tmp_path, monkeypatch, hashes, source):
    """K13: a distribution installed from a repository, a local wheel, a URL or a directory can be the author's own
    code, so its wheel is searched for the key locations the models declare, and refused naming the member and how a
    distribution the author did not write is cleared, never the value; attested once read by hand, it is not."""
    found = scanned(tmp_path, monkeypatch, hashes, "toy-kit", SOURCES[source])
    member = "wheels/toy_kit-1.0-py3-none-any.whl/toy_kit/naming.py"
    assert f"{member}:1: a key location the study's models.toml declares, which no package holds" in found
    assert any(line.startswith(f"{member}: {CLEARED}") for line in found)
    assert not any("API_KEY" in line for line in found)
    digest = hashlib.sha256(GENERIC).hexdigest()
    attested = {member: Opaque(member, digest, "read by hand: a generic name")}
    assert scanned(tmp_path / "again", monkeypatch, hashes, "toy-kit", SOURCES[source], attested) == []


@pytest.mark.parametrize(
    ("name", "direct_url"),
    [("toy-kit", None), (CORE_DISTRIBUTION, SOURCES["a git repository"])],
    ids=["from an index", "the core from a repository"],
)
def test_k13_a_wheel_from_an_index_and_the_cores_are_not_searched(tmp_path, monkeypatch, hashes, name, direct_url):
    """K13: a release from an index, and the core however it was installed, are code the author did not write, and may
    well name a generic variable a study also uses as its key_env: neither is searched for key locations."""
    assert scanned(tmp_path, monkeypatch, hashes, name, direct_url) == []


KEY_BYTES = b"0123456789abcdef0123456789abcdef\n"  # stands in for a key's bytes


def tar_of(member: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


THE_KEY_FILE = [
    ("lab.key", "lab.key", KEY_BYTES),
    ("lab.key", "notes/lab.key", KEY_BYTES),
    ("keys/lab.key", "keys/lab.key", KEY_BYTES),
    ("keys/lab.key", "old/keys/lab.key", KEY_BYTES),
    ("study/keys/lab.key", "keys/lab.key", KEY_BYTES),  # declared from the directory above the study
    ("/srv/study/lab.key", "lab.key", KEY_BYTES),
    ("lab.key", "bundle.tar", tar_of("lab.key", KEY_BYTES)),  # a member of a packaged archive
    ("keys/openai.key", "keys/OpenAI.key", KEY_BYTES),  # a label is compared without regard to case
    ("keys/OpenAI.key", "KEYS/OPENAI.KEY", KEY_BYTES),
    ("lab.key", "bundle.tar", tar_of("notes/LAB.KEY", KEY_BYTES)),
]
NOT_THE_KEY_FILE = [
    ("lab.key", "my-lab.key"),
    ("lab.key", "lab.key.md"),
    ("keys/lab.key", "mykeys/lab.key"),
    ("keys/lab.key", "lab.key/README.md"),
    ("/srv/study/lab.key", "notes/lab.key"),
    ("keys/openai.key", "keys/OpenAI.key.md"),
    ("keys/openai.key", "MYKEYS/OpenAI.key"),
]


@pytest.mark.parametrize(("key_file", "path", "data"), THE_KEY_FILE, ids=[f"{k} as {p}" for k, p, _ in THE_KEY_FILE])
def test_k13_a_packaged_file_that_is_the_key_file_is_refused(key_file, path, data):
    """K13: a packaged file, or a member of a packaged archive, whose path and the declared key file's agree on their
    last parts, the shorter one whole and without regard to case, can be the key itself: it is refused, by its hidden
    name, never the value."""
    finders = location_finders([SimpleNamespace(key_file=key_file, key_env=None)])
    found = find_locations({path: data}, finders)
    assert any(line.endswith(f": the path holds {FOUND_LOCATION}") for line in found), found
    assert not any("lab.key" in line for line in found)


@pytest.mark.parametrize(("key_file", "path"), NOT_THE_KEY_FILE, ids=[f"{k} as {p}" for k, p in NOT_THE_KEY_FILE])
def test_k13_a_packaged_file_with_a_longer_name_is_not_the_key_file(key_file, path):
    """K13: a packaged file is the key file only by whole parts of its path: `my-lab.key` is not `lab.key`."""
    finders = location_finders([SimpleNamespace(key_file=key_file, key_env=None)])
    assert find_locations({path: KEY_BYTES}, finders) == []


OTHER_BYTES = KEY_BYTES.replace(b"0", b"1")  # as long as the key, and not it
BINARY_KEY = bytes(range(32))  # a key that is not text


KEPT = ("vault-of-lanterns", "ember.secret")  # the parts of the path of the key file the tests below keep


def kept_key(directory: Path, data: bytes = KEY_BYTES) -> str:
    """A key file at an absolute path no packaged file's name agrees with, as a model would declare it."""
    path = directory / "vault-of-lanterns" / "ember.secret"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return str(path)


def zip_of(member: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, data)
    return buffer.getvalue()


COPIES = [  # what it is, its path in the package, its bytes, the key file's bytes
    ("a text file", "notes/readme.txt", KEY_BYTES, KEY_BYTES),
    ("a file with no extension", "data", KEY_BYTES, KEY_BYTES),
    ("a binary file", "notes/blob.bin", BINARY_KEY, BINARY_KEY),
    ("a member of a tar", "bundle.tar", tar_of("misc/plain.txt", KEY_BYTES), KEY_BYTES),
    ("a member of a zip", "bundle.zip", zip_of("misc/plain.txt", KEY_BYTES), KEY_BYTES),
]


@pytest.mark.parametrize(("path", "data", "key"), [c[1:] for c in COPIES], ids=[c[0] for c in COPIES])
def test_k13_a_copy_of_the_key_file_is_refused_whatever_it_is_named(tmp_path, path, data, key):
    """K13: a packaged file, or a member of a packaged archive, whose size and SHA-256 are the declared key file's is
    the key itself, however it is named: it is refused, naming its path in the package and never the key file's path
    or the key."""
    finders = location_finders([SimpleNamespace(key_file=kept_key(tmp_path, key), key_env=None)])
    found = find_locations({path: data}, finders)
    assert any(line.startswith(f"{path}") and ": a copy of a key file" in line for line in found), found
    message = "\n".join(found)
    assert not any(part in message for part in KEPT)
    assert KEY_BYTES.decode().strip() not in message


def test_k13_a_file_the_size_of_the_key_file_but_not_it_is_not_refused(tmp_path):
    """K13: the copy is told by the size and the SHA-256 together: a file as long as the key file is not refused."""
    finders = location_finders([SimpleNamespace(key_file=kept_key(tmp_path), key_env=None)])
    assert len(OTHER_BYTES) == len(KEY_BYTES)
    assert find_locations({"notes/readme.txt": OTHER_BYTES}, finders) == []


def test_k13_a_key_file_that_is_not_there_or_holds_nothing_adds_no_search_by_content(tmp_path):
    """K13: a key file that does not exist, is a directory, or is empty gives no refusal and no error at all: an empty
    key file is not matched by every empty file of the package."""
    empty = tmp_path / "empty.secret"
    empty.write_bytes(b"")
    for declared in (str(tmp_path / "absent" / "x.secret"), str(tmp_path), str(empty)):
        finders = location_finders([SimpleNamespace(key_file=declared, key_env=None)])
        assert find_locations({"pkg/__init__.py": b""}, finders) == []


def test_k13_a_key_file_over_a_mebibyte_is_refused_by_a_clear_message_that_names_no_path(tmp_path):
    """K13: a key file is a small secret; one over 1 MiB is implausible and refuses the build, whose message names the
    model and the key, never the file's path."""
    big = tmp_path / "vault-of-lanterns" / "ember.secret"
    big.parent.mkdir()
    big.write_bytes(b"k" * (2**20 + 1))
    with pytest.raises(ConfigError, match=r"\[models\.gamma\]: key 'key_file': .*over 1 MiB") as refused:
        location_finders([SimpleNamespace(key="gamma", key_file=str(big), key_env=None)])
    assert not any(part in str(refused.value) for part in KEPT)
    big.write_bytes(b"k" * 2**20)  # exactly 1 MiB is still a key file
    assert location_finders([SimpleNamespace(key="gamma", key_file=str(big), key_env=None)])


def test_k13_a_binary_member_of_a_wheel_that_is_the_key_file_is_refused(tmp_path, hashes):
    """K13: a member of the author's own wheel that is not text is listed, not read, but if it is the key file, by its
    size and SHA-256, it is refused, by its path and never the key file's."""
    filename, data = wheel_of("toy-kit", "1.0", {"toy_kit/blob.bin": BINARY_KEY})
    wheel = Wheel("toy-kit", "1.0", filename, data, from_index=False, direct=True)
    finders = location_finders([SimpleNamespace(key_file=kept_key(tmp_path, BINARY_KEY), key_env=None)])
    found = scan_wheels(
        [wheel], private_hashes(hashes), terms=None, hidden=(frozenset(), frozenset()), attested={}, locations=finders
    ).findings
    assert any("toy_kit/blob.bin: a copy of a key file" in line for line in found), found
    assert not any(part in line for line in found for part in KEPT), found


def test_k13_a_wheel_member_that_is_the_key_file_is_refused_by_its_hidden_name(hashes):
    """K13: a member of a wheel read with every view that can be the key file itself is refused, and every line about
    it, the attestation that would clear it included, shows it by its hidden name, never its path."""
    filename, data = wheel_of("toy-kit", "1.0", {"toy_kit/lab.key": KEY_BYTES})
    wheel = Wheel("toy-kit", "1.0", filename, data, from_index=False, direct=True)
    finders = location_finders([SimpleNamespace(key_file="lab.key", key_env=None)])
    found = scan_wheels(
        [wheel], private_hashes(hashes), terms=None, hidden=(frozenset(), frozenset()), attested={}, locations=finders
    ).findings
    assert any(line.endswith(f": the path holds {FOUND_LOCATION}") for line in found), found
    assert any("[[opaque]]" in line for line in found), found
    assert not any("lab.key" in line for line in found), found


COPY_FOUND = "a copy of a key file the study's models.toml declares, which no package holds, was found at this path"
WHEEL = "wheels/toy_kit-1.0-py3-none-any.whl"  # where `wheel_of` puts the wheel in a package
LONG_KEY = b"sk-" + bytes(range(65, 91)) * 3 + b"\n"  # long enough for an encoded run of it to be decoded


def scan_one_wheel(hashes, members: dict[str, bytes], finders, attested=None) -> list[str]:
    filename, data = wheel_of("toy-kit", "1.0", members)
    wheel = Wheel("toy-kit", "1.0", filename, data, from_index=False, direct=True)
    return scan_wheels(
        [wheel],
        private_hashes(hashes),
        terms=None,
        hidden=(frozenset(), frozenset()),
        attested=attested or {},
        locations=finders,
    ).findings


WHEEL_COPIES = [  # what it is, the member's path and bytes, the key file's bytes
    ("a text member", "toy_kit/data/settings.txt", KEY_BYTES, KEY_BYTES),
    ("a binary member", "toy_kit/blob.bin", BINARY_KEY, BINARY_KEY),
    (
        "an encoded run in a text member",
        "toy_kit/data/blob.txt",
        b"blob = '" + base64.b64encode(LONG_KEY) + b"'\n",
        LONG_KEY,
    ),
]


@pytest.mark.parametrize(("member", "data", "key"), [c[1:] for c in WHEEL_COPIES], ids=[c[0] for c in WHEEL_COPIES])
def test_k13_a_copy_of_the_key_file_in_a_wheel_is_never_cleared_and_its_digest_never_printed(
    tmp_path, hashes, member, data, key
):
    """K13: a wheel member that is a copy of the key file, or holds one encoded, is refused saying a copy was found at
    its path and must be removed; no line says how a member is attested, none prints the SHA-256 of the key or of the
    member, and attesting the member in [[opaque]] by its hash refuses it still."""
    finders = location_finders([SimpleNamespace(key_file=kept_key(tmp_path, key), key_env=None)])
    label = f"{WHEEL}/{member}"
    digests = {hashlib.sha256(key).hexdigest(), hashlib.sha256(data).hexdigest()}
    for attested in ({}, {label: Opaque(label, hashlib.sha256(data).hexdigest(), "read by hand")}):
        found = scan_one_wheel(hashes, {member: data}, finders, attested)
        assert not any("[[opaque]]" in line or "attest" in line or CLEARED in line for line in found), found
        assert not any(digest in line for line in found for digest in digests), found
        assert found, "attesting the member cleared it"
        assert any(line.startswith(f"{label}") and COPY_FOUND in line for line in found), found


def test_k13_a_key_location_a_wheel_member_names_by_chance_is_still_cleared_by_attesting_it(tmp_path, hashes):
    """K13: only a copy of the key file is beyond attesting: a wheel member that merely names a key location is
    refused with how it is cleared, and attested by its hash it is accepted, a member beside a copy aside."""
    finders = location_finders([SimpleNamespace(key_file=None, key_env="API_KEY")])
    members = {"toy_kit/naming.py": GENERIC}
    label = f"{WHEEL}/toy_kit/naming.py"
    assert any(CLEARED in line for line in scan_one_wheel(hashes, members, finders))
    attested = {label: Opaque(label, hashlib.sha256(GENERIC).hexdigest(), "read by hand: a generic name")}
    assert scan_one_wheel(hashes, members, finders, attested) == []


def test_k13_a_copy_of_the_key_file_in_a_packaged_file_is_not_cleared_by_attesting_it(tmp_path):
    """K13: a packaged file that is a copy of the key file stays refused when package.toml attests it by its hash,
    whether the checks could read it or not; the finding names its path in the package, saying to remove it, and
    never prints the digest."""
    finders = location_finders([SimpleNamespace(key_file=kept_key(tmp_path), key_env=None)])
    found = find_locations({"notes/readme.txt": KEY_BYTES}, finders, opaque={"notes/readme.txt"})
    assert len(found) == 1, "attesting the file cleared it"
    assert found == [f"notes/readme.txt: {FOUND_COPY}"]
    assert FOUND_COPY.startswith(COPY_FOUND)
    assert FOUND_COPY.endswith("must be removed")
    assert not any(hashlib.sha256(KEY_BYTES).hexdigest() in line for line in found)
    binary = location_finders([SimpleNamespace(key_file=kept_key(tmp_path / "again", BINARY_KEY), key_env=None)])
    assert find_locations({"blob.bin": BINARY_KEY}, binary, opaque={"blob.bin"}) == [f"blob.bin: {FOUND_COPY}"]


def test_k13_the_key_file_itself_refuses_the_build(campaign, hashes, tmp_path):
    """K13: a key file kept in the study's directory and taken by the inclusion list would publish the key itself:
    the build refuses it by its hidden name, never its path."""
    copy = copy_campaign(campaign, tmp_path, "key-itself")
    table = copy.root / "models.toml"
    table.write_text(table.read_text(encoding="utf-8").replace(KEY_FILE, "lab.key"), encoding="utf-8")
    (copy.root / "lab.key").write_bytes(KEY_BYTES)
    spec = copy.root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('"README.md",', '"README.md",\n    "lab.key",'), "utf-8")
    with pytest.raises(PackageError) as refused:
        build_package(copy.root, tmp_path / "dist", hashes=hashes)
    message = str(refused.value)
    assert f": the path holds {FOUND_LOCATION}" in message
    assert "lab.key" not in message


def test_k13_a_renamed_copy_of_the_key_file_refuses_the_build(campaign, hashes, tmp_path):
    """K13: the key file exists where models.toml says, and a copy of it under any other name that the inclusion list
    takes would publish the key: the build refuses it by its path in the package, never the key file's path."""
    copy = copy_campaign(campaign, tmp_path, "copy-of-key")
    key = kept_key(tmp_path / "home", KEY_BYTES)
    table = copy.root / "models.toml"
    table.write_text(table.read_text(encoding="utf-8").replace(KEY_FILE, key), encoding="utf-8")
    (copy.root / "notes.txt").write_bytes(KEY_BYTES)
    spec = copy.root / "package.toml"
    spec.write_text(spec.read_text(encoding="utf-8").replace('"README.md",', '"README.md",\n    "notes.txt",'), "utf-8")
    with pytest.raises(PackageError) as refused:
        build_package(copy.root, tmp_path / "dist", hashes=hashes)
    message = str(refused.value)
    assert "notes.txt: a copy of a key file" in message
    assert not any(part in message for part in KEPT)


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


def move_keys(root: Path, *, env: str = MOVED_ENV) -> None:
    table = root / "models.toml"
    text = table.read_text(encoding="utf-8")
    table.write_text(text.replace(KEY_FILE, MOVED_FILE).replace(KEY_ENV, env), encoding="utf-8")


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

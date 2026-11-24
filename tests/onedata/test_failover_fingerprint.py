"""O98: the failover order is in no fingerprint, and the declaration of the failover holders is. The record `up`
writes in a federation's root (`failover-order.json`), and the order it holds, leave the platform's fingerprint
(`PlatformConfig.fingerprint`), its hash of the file, the campaign fingerprint a study takes from it and the identity a
member joins with exactly as they were, as the provider ids are left out today: the order differs from one member to the
next by chance, so a fingerprint that held it would make interchangeable members two experiments. What a space declares
as `failover` is in the platform file and so in the entry's digest, and with it in the campaign fingerprint: another
declaration is another experiment.

Every name and id is invented. The study is a copy of the tiny example whose plugin takes its platform half from the
`platform.toml` beside it, as a study's plugin does on a shard's copy; each copy is stamped after it is made."""

import json
import shutil
from pathlib import Path

import pytest
from study_support import HEAD

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import campaign_member_identity, fingerprint_of
from expcore.study.plugin import StudyContext, load_plugin
from expcore_onedata import failover
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
BUILT = "        lease_keys=lease_keys,\n    )"
OWNER, MIRROR_ONE, MIRROR_TWO = "owner-site", "mirror-one", "mirror-two"
IDS = {OWNER: "id-0300", MIRROR_ONE: "id-0100", MIRROR_TWO: "id-0200"}  # a mirror sorts first: the order holds
READS_PLATFORM = (
    "        lease_keys=lease_keys,\n"
    '        platform=__import__("expcore_onedata.config", fromlist=["load_platform"])'
    '.load_platform(HERE / "platform.toml", require_secrets=False).fingerprint(),\n'
)


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def in_scope_root(directory: Path, ids=IDS, failover=(MIRROR_TWO,)) -> Path:
    """A federation root holding an in-scope platform: one space the owner holds, the local provider and a mirror
    support, and that declares the mirror its failover holder (`failover`; none: the key is left out)."""

    def edit(data):
        data["providers"] = {name: {"url": f"https://{name}.test", "id": value} for name, value in ids.items()}
        del data["space"], data["support"]
        data["spaces"] = [
            {
                "label": "entry-0",
                "name": "invented-0-space",
                "id": "sp-invented-0",
                "prefixes": ["invented-0-"],
                "providers": [OWNER, MIRROR_ONE, MIRROR_TWO],
                "local": MIRROR_ONE,
                "holder": OWNER,
                **({"failover": list(failover)} if failover else {}),
            }
        ]

    directory.mkdir()
    write_platform(directory, edit)
    return directory


def with_record(root: Path) -> Path:
    """`root` with the record `up` writes in it, written as `up` writes it, from the root's own platform file."""
    found = failover.admit(load_platform(root / "platform.toml", require_secrets=False), [OWNER])
    assert found, "the platform is in scope"
    (root / failover.FILE).write_text(failover.record_text(found), encoding="utf-8")
    return root


def study_over(tmp_path: Path, name: str, root: Path) -> Path:
    """A stamped copy of the tiny study with every file of `root` beside it (its platform file, what it names, and the
    record when the root holds one), whose plugin's platform half is that platform file's own fingerprint."""
    copy = tmp_path / name
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    plugin = copy / "plugin.py"
    text = plugin.read_text(encoding="utf-8")
    assert text.count(BUILT) == 1
    plugin.write_text(text.replace(BUILT, READS_PLATFORM + "    )"), encoding="utf-8")
    shutil.copytree(root, copy, dirs_exist_ok=True)
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


def campaign_fingerprint(study_dir: Path) -> dict:
    study = load_study(study_dir)
    return fingerprint_of(study, load_plugin(StudyContext(study, {"commit": HEAD})))


def test_o98_the_platform_fingerprint_and_its_hash_are_the_same_with_and_without_the_record(tmp_path):
    """O98: `PlatformConfig.fingerprint()`, its SHA-256 as the render identity takes it and the hash of the file as
    loaded are identical for the same platform in a root with the record and in one without; the fingerprint holds no
    word of the order, no label of it and no key for it."""
    from expcore.campaign.manifest import fingerprint_sha256

    bare = in_scope_root(tmp_path / "bare")
    recorded = with_record(shutil.copytree(bare, tmp_path / "recorded"))
    assert (recorded / failover.FILE).is_file()
    assert not (bare / failover.FILE).exists()
    one, other = (load_platform(root / "platform.toml", require_secrets=False) for root in (bare, recorded))
    assert one.fingerprint() == other.fingerprint()
    assert fingerprint_sha256(one.fingerprint()) == fingerprint_sha256(other.fingerprint())
    assert one.sha256 == other.sha256
    text = json.dumps(one.fingerprint(), sort_keys=True)
    for absent in ("failover", "mirror_first", "providers[", "id-0100", "id-0200", "id-0300"):
        assert absent not in text


def test_o98_the_order_is_not_in_the_fingerprint_whichever_provider_the_ids_put_first(tmp_path):
    """O98: two federations of one declared platform whose ids give two different orders (a holder asked first, and the
    owner) have one platform fingerprint, as two with other ids always had: the order is in neither."""
    first = in_scope_root(tmp_path / "first", {OWNER: "id-0300", MIRROR_ONE: "id-0100", MIRROR_TWO: "id-0200"})
    second = in_scope_root(tmp_path / "second", {OWNER: "id-0200", MIRROR_ONE: "id-0100", MIRROR_TWO: "id-0300"})
    orders = [
        failover.orders(load_platform(root / "platform.toml", require_secrets=False), [OWNER])[0].order
        for root in (first, second)
    ]
    assert orders[0] != orders[1]
    fingerprints = [
        load_platform(root / "platform.toml", require_secrets=False).fingerprint() for root in (first, second)
    ]
    assert fingerprints[0] == fingerprints[1]


def test_o98_the_campaign_fingerprint_and_the_member_identity_are_the_same_with_and_without_the_record(tmp_path):
    """O98: a study over the root's platform file has one campaign fingerprint, and a member one identity (the
    fingerprint, the environment identity, the platform fingerprint's hash), whether or not the record is beside the
    platform file: the platform half is the file's own fingerprint, which the record is not part of."""
    bare = in_scope_root(tmp_path / "bare")
    recorded = with_record(shutil.copytree(bare, tmp_path / "recorded"))
    plain, noted = study_over(tmp_path, "plain", bare), study_over(tmp_path, "noted", recorded)
    assert (noted / failover.FILE).is_file()
    assert not (plain / failover.FILE).exists()
    assert campaign_fingerprint(plain) == campaign_fingerprint(noted)
    assert (
        campaign_fingerprint(plain)["platform"]
        == load_platform(bare / "platform.toml", require_secrets=False).fingerprint()
    )
    assert campaign_member_identity(plain) == campaign_member_identity(noted)


def test_o98_the_declared_failover_holders_are_in_the_campaign_fingerprint_and_the_member_identity(tmp_path):
    """O98: the declaration is in the platform file, so it is part of the experiment: the same platform with and without
    `failover` on its space has two platform fingerprints, two campaign fingerprints and two member identities."""
    plain = in_scope_root(tmp_path / "plain-root", failover=())
    declared = in_scope_root(tmp_path / "declared-root")
    studies = {name: study_over(tmp_path, name, root) for name, root in (("plain", plain), ("declared", declared))}
    platforms = [load_platform(root / "platform.toml", require_secrets=False) for root in (plain, declared)]
    assert platforms[0].fingerprint() != platforms[1].fingerprint()
    assert campaign_fingerprint(studies["plain"]) != campaign_fingerprint(studies["declared"])
    assert campaign_member_identity(studies["plain"]) != campaign_member_identity(studies["declared"])

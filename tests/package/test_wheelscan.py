"""The wheels' messages name a member through the build's one redactor, whichever message it is."""

from pathlib import Path

from package_support import wheel_of

from expcore.package.anonymise import Anonymisation
from expcore.package.spec import Opaque
from expcore.package.wheels import Wheel
from expcore.package.wheelscan import scan_wheels
from expcore.provenance.hashfile import hashes_from_tokens
from expcore.provenance.redact import Redactor, hidden_name

PRIVATE = "zarnothwick"  # invented; a private name of the study-token list
HASHES = hashes_from_tokens([PRIVATE])


def wheel(name: str, files: dict[str, bytes]) -> Wheel:
    filename, data = wheel_of(name, "1.0", files)
    return Wheel(name, "1.0", filename, data, from_index=False)


def messages(wheels: list[Wheel], *, attested: dict[str, Opaque], terms=None) -> tuple[list[str], Redactor]:
    seen = Redactor(HASHES)
    found = scan_wheels(
        wheels, HASHES, terms=terms, hidden=(frozenset(), frozenset()), attested=attested, redactor=seen
    )
    assert not any(PRIVATE in line for line in found.findings), found.findings
    return found.findings, seen


def test_k05_a_member_path_holding_a_private_name_is_hidden_in_its_finding_and_its_attestation_hint():
    """K05: the finding and the hint that tells how to attest the member name it by its hidden name; the hint gives the
    hash to attest and says where the path is shown, never the path."""
    member = f"tools/{PRIVATE}/notes.txt"
    one = wheel("tools", {member: f"tuned on {PRIVATE}\n".encode()})
    label = f"{one.path}/{member}"
    found, seen = messages([one], attested={})
    assert any(line.startswith(f"{hidden_name(label)}:1: a private name") for line in found)
    assert any(line.startswith(f"to attest {hidden_name(label)} once read by hand") for line in found)
    assert seen.hidden[hidden_name(label)] == label


def test_k05_an_attestation_mismatch_a_direct_url_and_a_stray_attestation_hide_the_member_path():
    """K05: the refusals that name a member without reading it (a changed attested member, a direct_url.json in an
    anonymised build, an attestation of a member no wheel holds) hide a path that holds a private name too."""
    tools = wheel(f"{PRIVATE}-tools", {"pkg/a.txt": b"plain\n", "pkg-1.0.dist-info/direct_url.json": b"{}\n"})
    changed = f"{tools.path}/pkg/a.txt"
    stray = f"wheels/absent-1.0-py3-none-any.whl/{PRIVATE}.py"
    attested = {path: Opaque(path, "0" * 64, "read") for path in (changed, stray)}
    found, _ = messages([tools], attested=attested, terms=Anonymisation(Path("anon.toml"), (), {"Quillon": "A"}))
    assert f"{hidden_name(changed)}: its SHA-256 is not the one [[opaque]] attests; read it again" in found
    direct = f"{tools.path}/pkg-1.0.dist-info/direct_url.json"
    assert any(line.startswith(f"{hidden_name(direct)}: names where the distribution came from") for line in found)
    assert f"{hidden_name(stray)}: [[opaque]] attests a member no vendored wheel holds" in found

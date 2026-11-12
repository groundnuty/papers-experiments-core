"""O05, O06: a token is examined fail-closed against the exact caveat set of its plane before it is used."""

import base64
import json
import re

import pytest

from expcore_onedata.tokencheck import (
    CaveatSpec,
    Examined,
    PrivilegesMismatch,
    TokenRefused,
    require_distinct,
    require_examined,
    require_privileges,
)

NOW = 1_800_000_000
SPACE = "spmock0001"
DATA = CaveatSpec(SPACE, readonly=True, interface="rest")
WRITING = CaveatSpec(SPACE, readonly=False, interface="rest")
MIN_TTL, MAX_TTL = 1500, 2100


def info(spec=DATA, *, lifetime=1800, caveats=None, subject="u-data", token_id="tid-1"):
    """What Onezone says about a token minted from the spec, unless the test replaces the caveats."""
    return {
        "id": token_id,
        "subject": {"type": "user", "id": subject},
        "caveats": spec.caveats(NOW + lifetime) if caveats is None else caveats,
    }


def examine(answer, spec=DATA, subject="u-data"):
    return require_examined(
        "tok", spec, subject=subject, examine=lambda _t: answer, min_ttl_s=MIN_TTL, max_ttl_s=MAX_TTL, now=NOW
    )


def without(type_):
    return [c for c in DATA.caveats(NOW + 1800) if c["type"] != type_]


def path_caveat(path):
    return {"type": "data.path", "whitelist": [base64.b64encode(path.encode()).decode()]}


def replaced(type_, new):
    return [new if c["type"] == type_ else c for c in DATA.caveats(NOW + 1800)]


def test_o05_the_exact_set_passes_and_the_result_holds_ids_and_types_only():
    """O05: a token with exactly the data-path caveats passes; the answer holds no value."""
    got = examine(info())
    assert got == Examined("tid-1", "u-data", NOW + 1800, ("data.path", "data.readonly", "interface", "time"))


REFUSED = {
    "readonly missing": (info(caveats=without("data.readonly")), "data.readonly"),
    "path missing": (info(caveats=without("data.path")), "data.path"),
    "path is another space": (info(caveats=replaced("data.path", path_caveat("/other"))), "study space"),
    "path is the root": (info(caveats=replaced("data.path", path_caveat("/"))), "study space"),
    "path lists two entries": (
        info(
            caveats=replaced("data.path", {"type": "data.path", "whitelist": path_caveat(f"/{SPACE}")["whitelist"] * 2})
        ),
        "study space",
    ),
    "path is not base64": (
        info(caveats=replaced("data.path", {"type": "data.path", "whitelist": ["!!"]})),
        "malformed",
    ),
    "path is not a list": (info(caveats=replaced("data.path", {"type": "data.path", "whitelist": "x"})), "malformed"),
    "extra caveat": (
        info(caveats=[*DATA.caveats(NOW + 1800), {"type": "consumer", "whitelist": ["x"]}]),
        "not asked for",
    ),
    "duplicate readonly": (info(caveats=[*DATA.caveats(NOW + 1800), {"type": "data.readonly"}]), "2 data.readonly"),
    "readonly with a field": (info(caveats=replaced("data.readonly", {"type": "data.readonly", "x": 1})), "fields"),
    "wrong interface": (
        info(caveats=replaced("interface", {"type": "interface", "interface": "oneclient"})),
        "interface",
    ),
    "no time caveat": (info(caveats=without("time")), "no time"),
    "time that is not a number": (info(caveats=replaced("time", {"type": "time", "validUntil": "soon"})), "malformed"),
    "time that is a boolean": (info(caveats=replaced("time", {"type": "time", "validUntil": True})), "malformed"),
    "expired": (info(lifetime=-5), "expired"),
    "expires now": (info(lifetime=0), "expired"),
    "shorter than the run needs": (info(lifetime=MIN_TTL - 1), "shorter"),
    "longer than needed": (info(lifetime=MAX_TTL + 1), "longer"),
    "another principal's": (info(subject="u-other"), "another principal"),
    "not a mapping": ("a string", "parsed"),
    "no id": ({"subject": {"id": "u-data"}, "caveats": []}, "'id'"),
    "caveats not a list": ({"id": "t", "subject": {"id": "u-data"}, "caveats": "x"}, "'caveats'"),
    "a caveat without a type": ({"id": "t", "subject": {"id": "u-data"}, "caveats": [{"x": 1}]}, "'caveats'"),
    "no subject": ({"id": "t", "caveats": []}, "'subject.id'"),
    "a subject that is a string": ({"id": "t", "subject": "u-data", "caveats": []}, "'subject.id'"),
    "a subject with the id under another key": (
        {"id": "t", "subject": {"user": "u-data"}, "caveats": []},
        "'subject.id'",
    ),
    "a subject whose id is a number": ({"id": "t", "subject": {"id": 7}, "caveats": []}, "'subject.id'"),
    "a subject with an empty id": ({"id": "t", "subject": {"id": ""}, "caveats": []}, "'subject.id'"),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_o05_o06_any_deviation_from_the_exact_set_is_refused(name):
    """O05, O06: missing read-only, a wrong path, an extra caveat, an unparseable answer or a bad lifetime refuses."""
    answer, reason = REFUSED[name]
    with pytest.raises(TokenRefused, match=reason):
        examine(answer)


def test_o05_an_examination_that_raises_or_is_empty_refuses_without_echoing_the_error():
    """O05: unknown is not a pass; the cause is named by class only, never by its text."""

    def broken(_token):
        raise RuntimeError("the response held tok-secret-9999")

    with pytest.raises(TokenRefused) as caught:
        require_examined("tok", DATA, subject="u", examine=broken, min_ttl_s=1, max_ttl_s=2, now=NOW)
    assert "RuntimeError" in str(caught.value)
    assert "tok-secret-9999" not in str(caught.value)
    with pytest.raises(TokenRefused, match="empty"):
        require_examined("", DATA, subject="u", examine=lambda t: info(), min_ttl_s=1, max_ttl_s=2, now=NOW)


def test_o05_a_token_that_would_write_carries_no_data_access_caveat_and_each_plane_refuses_the_others():
    """O05: a writable token is not a data-access token, and a read-only one is; each spec refuses the other's token."""
    assert examine(info(WRITING), WRITING) == Examined("tid-1", "u-data", NOW + 1800, ("interface", "time"))
    with pytest.raises(TokenRefused, match="data-access caveat"):
        examine(info(DATA), WRITING)
    with pytest.raises(TokenRefused, match=r"no data\.path"):
        examine(info(WRITING), DATA)


def writing_with(*extra):
    return info(WRITING, caveats=[*WRITING.caveats(NOW + 1800), *extra])


WRITING_REFUSED = {
    "a path whitelist": (writing_with(path_caveat(f"/{SPACE}")), r"data-access caveat \('data\.path'\)"),
    "read-only": (writing_with({"type": "data.readonly"}), r"data-access caveat \('data\.readonly'\)"),
    "an object id whitelist": (
        writing_with({"type": "data.objectid", "whitelist": ["000000000000"]}),
        r"data-access caveat \('data\.objectid'\)",
    ),
    "the oneclient interface": (
        info(
            WRITING,
            caveats=[
                {"type": "interface", "interface": "oneclient"} if c["type"] == "interface" else c
                for c in WRITING.caveats(NOW + 1800)
            ],
        ),
        "interface caveat is not the one",
    ),
    "an api caveat": (writing_with({"type": "api", "whitelist": ["opw/create/op_transfer.*.instance:*"]}), "not asked"),
    "no interface": (info(WRITING, caveats=WRITING.caveats(NOW + 1800)[:1]), "no interface"),
}


@pytest.mark.parametrize("name", sorted(WRITING_REFUSED))
def test_o05_a_writing_token_that_carries_a_data_access_caveat_or_anything_else_is_refused(name):
    """O05: on Onedata 25.0 any of data.path, data.objectid and data.readonly, and the oneclient interface, makes a
    token usable for data access alone, so it could not schedule a transfer; a writing token carrying one is refused,
    as is one with any caveat its spec did not ask for or without one it did."""
    answer, reason = WRITING_REFUSED[name]
    with pytest.raises(TokenRefused, match=reason):
        examine(answer, WRITING)


@pytest.mark.parametrize("missing", ["data.path", "data.readonly"])
def test_o05_a_read_only_token_that_lacks_one_of_its_data_access_caveats_is_refused(missing):
    """O05: the read-only set keeps both data caveats; a token without either is refused, naming the one it lacks."""
    with pytest.raises(TokenRefused, match=f"no {re.escape(missing)} caveat"):
        examine(info(caveats=without(missing)))


def test_o05_a_read_only_token_has_byte_for_byte_the_caveats_it_had_before_writing_tokens_changed():
    """O05: the data path's and a read-only tool server's caveat list is unchanged, in content and in order."""
    assert json.dumps(DATA.caveats(5)) == (
        '[{"type": "time", "validUntil": 5}, {"type": "data.readonly"}, {"type": "interface", "interface": "rest"}, '
        '{"type": "data.path", "whitelist": ["L3NwbW9jazAwMDE="]}]'
    )
    several = CaveatSpec(("spmock0001", "spmock0002"), readonly=True, interface="oneclient")
    assert json.dumps(several.caveats(5)) == (
        '[{"type": "time", "validUntil": 5}, {"type": "data.readonly"}, {"type": "interface", "interface": '
        '"oneclient"}, {"type": "data.path", "whitelist": ["L3NwbW9jazAwMDE=", "L3NwbW9jazAwMDI="]}]'
    )


def test_o05_a_writing_spec_mints_only_its_expiry_and_its_interface_and_is_never_a_oneclient_one():
    """O05: a writable token's caveats are the time and the rest interface, nothing that limits it to data access; a
    writable spec with the oneclient interface, itself a data-access caveat on 25.0, is refused when it is built."""
    assert WRITING.caveats(5) == [{"type": "time", "validUntil": 5}, {"type": "interface", "interface": "rest"}]
    assert (DATA.data_access, WRITING.data_access) == (True, False)
    with pytest.raises(ValueError, match="oneclient"):
        CaveatSpec(SPACE, readonly=False, interface="oneclient")


def test_the_spec_builds_the_caveats_the_examination_accepts():
    assert [c["type"] for c in DATA.caveats(5)] == ["time", "data.readonly", "interface", "data.path"]
    assert [c["type"] for c in WRITING.caveats(5)] == ["time", "interface"]
    with pytest.raises(ValueError, match="interface"):
        CaveatSpec(SPACE, readonly=True, interface="ftp")


def test_o05_privileges_must_equal_the_declared_set_direct_and_effective():
    """O05: the principal holds exactly view and read, read back both ways."""
    declared = ["space_view", "space_read_data"]
    require_privileges(declared, direct=["space_read_data", "space_view"], effective=declared)
    with pytest.raises(TokenRefused, match=r"direct.*lacks space_read_data"):
        require_privileges(declared, direct=["space_view"], effective=declared)
    with pytest.raises(TokenRefused, match=r"effective.*has also space_write_data"):
        require_privileges(declared, direct=declared, effective=[*declared, "space_write_data"])
    for unreadable in (None, "space_view", 5, [1, 2]):
        with pytest.raises(TokenRefused, match="could not be read"):
            require_privileges(declared, direct=unreadable, effective=declared)


@pytest.mark.parametrize(
    "answer",
    [
        {"space_view": True, "space_read_data": True},
        {"space_view": False, "space_read_data": True},
        ("space_view", "space_read_data"),
        {"space_view", "space_read_data"},
        [["space_view"], "space_read_data"],
        [{"space_view": True}],
        [None],
        7.5,
        b"space_view",
    ],
    ids=lambda answer: type(answer).__name__,
)
def test_o05_a_privilege_answer_that_is_not_a_list_of_strings_is_refused_naming_the_field(answer):
    """O05: a dict of booleans iterates as its keys and would read as the rights held, so only a list of strings is
    accepted, for the direct read and for the effective read alike, and the message names the field."""
    declared = ["space_view", "space_read_data"]
    with pytest.raises(TokenRefused, match=r"direct privileges could not be read: 'privileges' is not a list"):
        require_privileges(declared, direct=answer, effective=declared)
    with pytest.raises(TokenRefused, match=r"effective privileges could not be read: 'privileges' is not a list"):
        require_privileges(declared, direct=declared, effective=answer)


def test_the_two_planes_share_neither_a_principal_nor_a_token():
    one = Examined("token-one", "user-one", 1.0, ())
    require_distinct(one, Examined("token-two", "user-two", 1.0, ()))
    with pytest.raises(TokenRefused, match="principal"):
        require_distinct(one, Examined("token-two", "user-one", 1.0, ()))
    with pytest.raises(TokenRefused, match="token"):
        require_distinct(one, Examined("token-one", "user-two", 1.0, ()))


def test_o05_a_path_caveat_in_non_canonical_base64_is_refused():
    """O05: "L3N=" decodes strictly to the same path as "L3M=" but is a different text, so it is refused."""
    spec = CaveatSpec("s", readonly=True, interface="rest")
    canonical, other = "L3M=", "L3N="
    assert base64.b64decode(canonical, validate=True) == base64.b64decode(other, validate=True) == b"/s"
    assert spec.caveats(NOW + 1800)[-1]["whitelist"] == [canonical]

    def examined(text):
        caveats = [
            c if c["type"] != "data.path" else {"type": "data.path", "whitelist": [text]}
            for c in spec.caveats(NOW + 1800)
        ]
        return examine(info(spec, caveats=caveats), spec)

    assert examined(canonical).subject == "u-data"
    with pytest.raises(TokenRefused, match="not canonical"):
        examined(other)


def test_o05_only_a_difference_between_the_privileges_and_the_declared_set_is_a_privileges_mismatch():
    """O05: a caller that tells "the principal holds other rights" from every other refusal can: a mismatch is its own
    class, and an unreadable answer, a missing caveat or an expired token is a TokenRefused that is not one."""
    declared = ["space_view", "space_read_data"]
    for direct, effective in ((["space_view"], declared), (declared, [*declared, "space_manage_qos"])):
        with pytest.raises(PrivilegesMismatch):
            require_privileges(declared, direct=direct, effective=effective)
    others = [
        lambda: require_privileges(declared, direct={"space_view": True}, effective=declared),
        lambda: examine(info(caveats=without("data.readonly"))),
        lambda: examine(info(lifetime=-5)),
        lambda: examine("not an object"),
    ]
    for other in others:
        with pytest.raises(TokenRefused) as caught:
            other()
        assert not isinstance(caught.value, PrivilegesMismatch)

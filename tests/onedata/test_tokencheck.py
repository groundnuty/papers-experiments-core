"""O05, O06: a token is examined fail-closed against the exact caveat set of its plane before it is used."""

import base64

import pytest

from expcore_onedata.tokencheck import (
    CaveatSpec,
    Examined,
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


def test_o05_a_token_that_would_write_has_no_readonly_caveat_and_one_that_may_not_is_refused_for_having_it():
    """O05: the tool-server plane's set differs by read-only alone, and each plane refuses the other's."""
    assert examine(info(WRITING), WRITING)
    with pytest.raises(TokenRefused, match="not asked for"):
        examine(info(DATA), WRITING)
    with pytest.raises(TokenRefused, match=r"no data\.readonly"):
        examine(info(WRITING), DATA)


def test_the_spec_builds_the_caveats_the_examination_accepts():
    assert [c["type"] for c in DATA.caveats(5)] == ["time", "data.readonly", "interface", "data.path"]
    assert [c["type"] for c in WRITING.caveats(5)] == ["time", "interface", "data.path"]
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

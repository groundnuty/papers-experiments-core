"""The client's data calls: query parameters, raw bodies and range reads, behind the file-to-space gate."""

import httpx
import pytest
from placement_support import DATA

from expcore_onedata.client import OnezoneClient, OutOfScope, PlatformError

PATH = "study/data.bin"
OTHER = "/data/fid/content"


def study_path(*parts):
    """What a provider answers as a file's path attribute in the study space."""
    return "/study-a-data/" + "/".join(parts)


def scripted(config, content_answer, *, path=None):
    """A client whose provider answers the path attribute with `path` and the content request with `content_answer`."""
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params.get("attribute") == "path":
            return httpx.Response(200, json={"path": study_path("x") if path is None else path})
        return content_answer(request)

    return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler))), seen


def ranged(status, body, content_range):
    headers = {"Content-Range": content_range} if content_range else {}
    return lambda request: httpx.Response(status, content=body, headers=headers)


def read(client, **kw):
    return client.read_range(OTHER, provider="beta", offset=10, length=5, **kw)


def test_o09_a_range_read_of_a_file_in_another_space_sends_nothing_but_the_path_read(config, seeded, client):
    """O09: a range read can change a provider's block map, so it passes the gate a mutation passes."""
    seeded.space_name = "elsewhere"  # the provider now reports every file as lying in a space outside the prefixes
    file_id = seeded.id_of(PATH)
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.read_range(f"/data/{file_id}/content", provider="beta", offset=0, length=5)
    assert [r for r in seeded.requests if r.range] == []


def test_o09_a_mutation_by_file_id_is_scoped_by_the_files_space(config, seeded, client):
    """O09: a file id in the path resolves to its space through the provider; outside the prefixes nothing is sent."""
    file_id = seeded.id_of(PATH)
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("DELETE", f"/data/{file_id}", provider="alpha")
    assert [c for c in seeded.provider_calls if c[1] == "DELETE"] == []
    assert seeded.exists(PATH)


def test_o09_a_transfer_is_scoped_by_the_file_its_body_names(config, seeded, client):
    """O09: a transfer job has no file in its path, so the gate reads the body's fileId and resolves its space."""
    file_id = seeded.id_of(PATH)
    body = {"type": "replication", "replicatingProviderId": "x", "dataSourceType": "file", "fileId": file_id}
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("POST", "/transfers", provider="beta", json=body)
    assert [c for c in seeded.provider_calls if c[2] == "/transfers"] == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/lookup-file-id/space/path", None),  # nothing maps a path to a space
        ("POST", "/replicas-id/abc", None),
        ("DELETE", "/transfers/abc", None),  # a cancel names no file
        ("POST", "/transfers", {"type": "eviction"}),  # no fileId
        ("POST", "/transfers", {"fileId": 5}),  # not a plain id
        ("PUT", "/data/a%2Fb/content", None),  # an escape the server could read as another path
        ("PUT", "/data/abc/content?offset=1", None),
        ("PUT", "/data/ab c/content", None),
    ],
)
def test_o09_a_mutation_that_names_no_file_and_no_space_is_refused_before_anything_is_read(
    config, seeded, client, method, path, body
):
    with pytest.raises(OutOfScope):
        client.mutate(method, path, provider="alpha", json=body)
    assert seeded.provider_calls == []


def test_o09_a_file_target_needs_a_provider_to_ask(config, seeded, client):
    """O09: Onezone cannot resolve a file, so a file target with no provider named is refused."""
    with pytest.raises(OutOfScope, match="resolves to no space"):
        client.mutate("DELETE", "/data/abc")
    assert seeded.provider_calls == []


def test_the_file_a_provider_does_not_know_resolves_to_no_space(config, seeded, client):
    with pytest.raises(OutOfScope, match="unknown to its provider"):
        client.mutate("DELETE", "/data/nosuchfile", provider="alpha")


def test_an_answer_without_a_path_resolves_to_no_space(config):
    client = OnezoneClient(
        config, http=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    )
    with pytest.raises(OutOfScope, match="holds no path"):
        client.mutate("DELETE", "/data/abc", provider="alpha")


def path_reads(seeded):
    return [r for r in seeded.requests if r.query.get("attribute") == "path"]


def test_o09_every_mutation_by_file_id_reads_the_files_space_again_right_before_it_is_sent(config, seeded, client):
    """O09: nothing is kept between mutations of one file, so three writes cost three path reads, each just before
    its write."""
    file_id = seeded.id_of(PATH)
    for offset in (0, 10, 20):
        client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": offset}, content=b"x")
    sequence = [(r.method, r.query.get("attribute") or "-") for r in seeded.requests]
    assert sequence == [("GET", "path"), ("PUT", "-")] * 3


def test_o09_a_space_renamed_out_of_scope_between_two_mutations_by_file_id_refuses_the_second(config, seeded, client):
    """O09: the name that authorised one write authorises nothing later. The provider reports the file in another
    space for the second write, which is refused and not sent."""
    file_id = seeded.id_of(PATH)
    client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 0}, content=b"x")
    writes_before = [c for c in seeded.provider_calls if c[1] == "PUT"]
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 1}, content=b"y")
    assert [c for c in seeded.provider_calls if c[1] == "PUT"] == writes_before


def test_o09_a_space_renamed_out_of_scope_between_two_range_reads_refuses_the_second(config, seeded, client):
    """O09: a range read re-resolves the file's space as well, so a rename between two reads stops the second."""
    file_id = seeded.id_of(PATH)
    client.read_range(f"/data/{file_id}/content", provider="beta", offset=0, length=5)
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.read_range(f"/data/{file_id}/content", provider="beta", offset=5, length=5)
    assert len(path_reads(seeded)) == 2
    assert len([r for r in seeded.requests if r.range]) == 1


def test_o09_a_space_renamed_out_of_scope_between_two_transfers_refuses_the_second(config, seeded, client):
    """O09: a transfer job names its file in the body, and that file's space is read again for each job."""
    file_id = seeded.id_of(PATH)
    body = {"type": "replication", "replicatingProviderId": "x", "dataSourceType": "file", "fileId": file_id}
    client.mutate("POST", "/transfers", provider="beta", json=body)
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("POST", "/transfers", provider="beta", json=body)
    assert len([c for c in seeded.provider_calls if c[2] == "/transfers" and c[1] == "POST"]) == 1


@pytest.mark.parametrize(
    "answered",
    [
        "/study-a-data/../production/f",  # climbs out of the study's space, past a first segment that passes
        "/study-a-data/./f",
        "/study-a-data/sub/../../production/f",
        "/study-a-data//f",
        "/study-a-data/",  # a trailing slash is an empty segment
        "//study-a-data/f",
        "/study-a-data/%2e%2e/production/f",  # an escape the server may read as another path
        "/study-a-data/a%2Fb",
        "/study-a-data\\..\\production",
        "/..",
        "/",
        "study-a-data/f",  # not absolute
        "",
    ],
)
def test_o09_a_path_that_is_not_canonical_resolves_to_no_space_before_the_prefix_is_checked(config, answered):
    """O09: the first segment of `/study-a-data/../production/f` passes the prefix check, so a path with a dot
    segment, an empty segment, an escape, a backslash or no leading slash is refused before it."""
    client, seen = scripted(config, lambda request: httpx.Response(204), path=answered)
    with pytest.raises(OutOfScope, match=r"not canonical|holds no path"):
        client.mutate("DELETE", "/data/abc", provider="alpha")
    assert [r.method for r in seen] == ["GET"]  # the path read only; the DELETE was not sent


def test_o09_a_canonical_path_resolves_to_the_name_of_its_first_segment(config):
    """O09: a deep path and the space root itself, which has a single segment, both resolve to the space's name."""
    for answered in ("/study-a-data/dir/file.bin", "/study-a-data"):
        client, _ = scripted(config, lambda request: httpx.Response(204), path=answered)
        assert client.file_space_name("abc", "alpha") == "study-a-data"


def test_a_range_read_needs_a_valid_range_a_content_path_and_a_declared_provider(config, seeded, client):
    with pytest.raises(OutOfScope, match="non-negative offset"):
        client.read_range(OTHER, provider="beta", offset=-1, length=5)
    with pytest.raises(OutOfScope, match="content only"):
        client.read_range("/data/fid", provider="beta", offset=0, length=5)
    with pytest.raises(OutOfScope, match="not declared"):
        client.read_range(OTHER, provider="nowhere", offset=0, length=5)
    assert seeded.requests == []


def test_a_range_read_returns_the_bytes_of_a_206_that_matches_the_range(config):
    client, _ = scripted(config, ranged(206, b"abcde", "bytes 10-14/100"))
    assert read(client) == b"abcde"


def test_a_range_read_may_end_short_only_at_the_end_of_the_file(config):
    client, _ = scripted(config, ranged(206, b"abc", "bytes 10-12/13"))
    assert read(client) == b"abc"
    client, _ = scripted(config, ranged(206, b"abc", "bytes 10-12/100"))
    with pytest.raises(PlatformError, match="another range"):
        read(client)


@pytest.mark.parametrize(
    ("status", "body", "content_range"),
    [
        (200, b"abcde", None),  # the whole file, not a range
        (206, b"abcde", None),  # no Content-Range
        (206, b"abcde", "bytes 0-4/100"),  # another offset
        (206, b"abcdef", "bytes 10-15/100"),  # more than asked for
        (206, b"abc", "bytes 10-14/100"),  # fewer bytes than the header says
    ],
)
def test_a_range_read_refuses_an_answer_that_is_not_the_range_asked_for(config, status, body, content_range):
    client, _ = scripted(config, ranged(status, body, content_range))
    with pytest.raises(PlatformError):
        read(client)


def test_the_range_header_is_sent_to_the_named_provider_with_the_admin_credential(config):
    client, seen = scripted(config, ranged(206, b"abcde", "bytes 10-14/100"))
    read(client)
    content = seen[-1]
    assert content.headers["Range"] == "bytes=10-14"
    assert content.url.host == "beta.test"
    assert content.headers["X-Auth-Token"]


def test_mutate_sends_query_parameters_and_a_raw_body(config, seeded, client):
    file_id = seeded.id_of(PATH)
    client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 7}, content=b"\x00\x01")
    sent = [r for r in seeded.requests if r.method == "PUT"]
    assert sent[0].query == {"offset": "7"}
    assert sent[0].body == b"\x00\x01"


def test_get_sends_repeated_query_parameters(config):
    seen = {}

    def handler(request):
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={})

    OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler))).get(
        "/data/x/children", provider="alpha", params={"attribute": ["name", "size"]}
    )
    assert seen["query"] == "attribute=name&attribute=size"


def test_the_content_of_a_range_read_is_the_files_own_bytes(config, seeded, client):
    file_id = seeded.id_of(PATH)
    assert client.read_range(f"/data/{file_id}/content", provider="beta", offset=2000, length=500) == DATA[2000:2500]

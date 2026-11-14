"""The client's data calls: query parameters, raw bodies and range reads, behind the file-to-space gate."""

import httpx
import pytest

from expcore_onedata.client import (
    MAX_KEPT_LINKS,
    MAX_SPACE_DEPTH,
    ZONE_TIME_PATH,
    NotSent,
    OnezoneClient,
    OutOfScope,
    PlatformError,
    ScopeUnread,
    TruncatingWrite,
)
from expcore_onedata.streamed import ReadOutcome
from expcore_onedata.testing import DATA, PROVIDER, ROOT_ID, SPACE_NAME
from expcore_onedata.testing.fake_federation import ZONE

PATH = "study/data.bin"
OTHER = "/data/fid/content"
SCRIPTED_ROOT = "root-s"


def scripted(config, content_answer, *, links=None):
    """A client whose federation answers the gate's reads, and `content_answer` for anything else.

    The provider gives the space's root as `root-s` and each file's parent link from `links` (file id to the answer's
    body; by default `fid` and `abc` lie right under the root), and Onezone gives the space its configured name."""
    links = {"fid": {"parent_id": SCRIPTED_ROOT}, "abc": {"parent_id": SCRIPTED_ROOT}} if links is None else links
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path == f"{ZONE}/spaces/{config.space.id}":
            return httpx.Response(200, json={"name": config.space.name})
        if path == f"{PROVIDER}/spaces/{config.space.id}":
            return httpx.Response(200, json={"fileId": SCRIPTED_ROOT})
        if request.url.params.get("attribute") == "parent_id":
            return httpx.Response(200, json=links[path.rsplit("/", 1)[-1]])
        return content_answer(request)

    return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler))), seen


def ranged(status, body, content_range):
    headers = {"Content-Range": content_range} if content_range else {}
    return lambda request: httpx.Response(status, content=body, headers=headers)


def read(client, **kw):
    return client.read_range(OTHER, provider="beta", offset=10, length=5, **kw)


def parent_reads(fake):
    """The gate's reads of a file's parent link, by the file id each asked about."""
    return [r.path.rsplit("/", 1)[-1] for r in fake.requests if r.query.get("attribute") == "parent_id"]


def root_reads(fake):
    """The gate's reads of the space's root on a provider."""
    return [c for c in fake.provider_calls if c[1] == "GET" and c[2] == f"/spaces/{fake.SPACE_ID}"]


def name_reads(fake):
    """The gate's reads of the space's name from Onezone."""
    return [r for r in fake.requests if r.method == "GET" and r.path == f"{ZONE}/spaces/{fake.SPACE_ID}"]


def gate_reads(fake):
    """What every request the fake received was, in order: a read of the gate's (root, parent, name), or the method."""
    kinds = []
    for r in fake.requests:
        if r.query.get("attribute") == "parent_id":
            kinds.append("parent")
        elif r.method == "GET" and r.path == f"{PROVIDER}/spaces/{fake.SPACE_ID}":
            kinds.append("root")
        elif r.method == "GET" and r.path == f"{ZONE}/spaces/{fake.SPACE_ID}":
            kinds.append("name")
        else:
            kinds.append(r.method)
    return kinds


def test_o09_a_range_read_of_a_file_in_another_space_sends_nothing_but_the_gates_reads(config, seeded, client):
    """O09: a range read can change a provider's block map, so it passes the gate a mutation passes."""
    seeded.space_name = "elsewhere"  # Onezone now names the space outside the prefixes
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
        ("DELETE", "/transfers/a%2Fb", None),  # a cancel is resolved through its record, never one with an escape
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


def test_o09_a_target_25_answers_with_posix_enoent_resolves_to_no_space_and_another_400_is_unread(config, fake, client):
    """O09: Onedata 25.0 answers a file it does not know with 400 posix enoent, which the walk reads as a 404: the
    target resolves to no space. Another 400 is no answer about the file, so the request is refused unread."""
    fake.answer_missing_as_25()
    with pytest.raises(OutOfScope, match="unknown to its provider"):
        client.mutate("DELETE", "/data/nosuchfile", provider="alpha")
    fake.answer_missing_as_25((400, "posix", "eacces"))
    with pytest.raises(ScopeUnread) as unread:
        client.mutate("DELETE", "/data/nosuchfile", provider="alpha")
    assert (unread.value.status, unread.value.code, unread.value.errno) == (400, "posix", "eacces")
    assert "posix: eacces" in str(unread.value)
    assert [c for c in fake.provider_calls if c[1] == "DELETE"] == []


# -- the walk from a file up to its space's root -----------------------------------------------------------------------


def test_o09_a_nested_file_resolves_to_its_space_by_its_parent_links_and_never_by_a_path(config, fake, client):
    """O09: Onedata 25.0 has no path attribute, so the gate walks a file's parent links (`parent_id`) up to the root of
    a space the configuration knows, read from the provider (`GET /spaces/{id}`), and then reads that space's name from
    Onezone. A file four levels down costs one root read, four parent reads and one name read; the path is never asked
    for."""
    file_id = fake.add_file("a/b/c/data.bin", DATA)
    assert client.file_space_name(file_id, "alpha") == SPACE_NAME
    assert parent_reads(fake) == [file_id, fake.id_of("a/b/c"), fake.id_of("a/b"), fake.id_of("a")]
    assert gate_reads(fake) == ["root", "parent", "parent", "parent", "parent", "name"]
    assert not [r for r in fake.requests if "path" in str(r.query.get("attribute"))]
    assert client.file_space_name(ROOT_ID, "alpha") == SPACE_NAME  # the root itself needs no parent read
    assert len(parent_reads(fake)) == 4


def test_o09_the_walk_is_kept_for_the_client_and_the_spaces_name_is_read_again_every_time(config, fake, client):
    """O09: a file's space does not change, so its parent links and the space's root are read once and kept: a second
    resolution of the same file reads nothing from the provider, and a file beside it reads only its own link. The
    space's name is read from Onezone again every time, so a space renamed out of scope stops the next mutation."""
    first = fake.add_file("a/b/one.bin", DATA)
    second = fake.add_file("a/b/two.bin", DATA)
    client.file_space_name(first, "alpha")
    before = len(fake.provider_calls)
    assert client.file_space_name(first, "alpha") == SPACE_NAME
    assert len(fake.provider_calls) == before  # nothing read from the provider again
    client.file_space_name(second, "alpha")
    assert parent_reads(fake) == [first, fake.id_of("a/b"), fake.id_of("a"), second]
    assert len(root_reads(fake)) == 1
    assert len(name_reads(fake)) == 3
    fake.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("DELETE", f"/data/{first}", provider="alpha")
    assert [c for c in fake.provider_calls if c[1] == "DELETE"] == []


def test_o09_the_spaces_root_and_the_parent_links_are_kept_per_provider(config, fake, client):
    """O09: the root and the parent links are read once from each provider the gate asks, and kept as that provider's:
    a file resolved through one provider is walked again through another, then found kept there too."""
    file_id = fake.add_file("a/data.bin", DATA)
    client.file_space_name(file_id, "alpha")
    client.file_space_name(file_id, "beta")
    client.file_space_name(file_id, "beta")
    assert [c[0] for c in root_reads(fake)] == [fake.PROVIDER_ID, fake.provider_of("beta.test")]
    assert parent_reads(fake) == [file_id, fake.id_of("a")] * 2


def test_o09_a_file_as_deep_as_the_bound_resolves_and_one_level_deeper_is_refused(config, fake, client):
    """O09: the walk follows at most `MAX_SPACE_DEPTH` parent links. A file at that depth resolves; one a level deeper
    is refused before its mutation is sent, with a reason that names no path and no space, and nothing of it is kept."""
    deepest = fake.add_file("/".join(["d"] * (MAX_SPACE_DEPTH - 1) + ["at-bound.bin"]), DATA)
    assert client.file_space_name(deepest, "alpha") == SPACE_NAME
    too_deep = fake.add_file("/".join(["e"] * MAX_SPACE_DEPTH + ["past-bound.bin"]), DATA)
    reads_before = len(parent_reads(fake))
    with pytest.raises(OutOfScope, match=f"more than {MAX_SPACE_DEPTH} directories") as refused:
        client.mutate("DELETE", f"/data/{too_deep}", provider="alpha")
    assert len(parent_reads(fake)) - reads_before == MAX_SPACE_DEPTH  # bounded: never more reads than the bound
    assert "past-bound" not in str(refused.value)
    assert SPACE_NAME not in str(refused.value)
    assert [c for c in fake.provider_calls if c[1] == "DELETE"] == []
    with pytest.raises(OutOfScope):  # nothing of the refused walk was kept: it is walked, and refused, again
        client.mutate("DELETE", f"/data/{too_deep}", provider="alpha")
    assert len(parent_reads(fake)) - reads_before == 2 * MAX_SPACE_DEPTH


def test_o09_a_file_outside_every_known_space_is_refused_and_nothing_is_sent(config, fake, client):
    """O09: a file whose parent links end at a root that is no configured space's (another space's root, whose parent
    is null) resolves to no space, whatever name Onezone would give that space."""
    stranger = fake.add_tree("root-of-another-space", "shared/dir")
    with pytest.raises(OutOfScope, match="no space this configuration knows"):
        client.mutate("DELETE", f"/data/{stranger}", provider="alpha")
    with pytest.raises(OutOfScope, match="no space this configuration knows"):
        client.read_range(f"/data/{stranger}/content", provider="alpha", offset=0, length=1)
    assert name_reads(fake) == []  # no name is asked for: there is no space to name
    assert [c for c in fake.provider_calls if c[1] == "DELETE" or "/content" in c[2]] == []


@pytest.mark.parametrize("fault", ["status", "reset"])
def test_o09_a_failed_lookup_fails_closed_and_nothing_derived_from_it_is_kept(config, fake, client, fault):
    """O09: when a parent read fails (a 500, or no answer at all), the mutation is refused as not sent
    (`ScopeUnread`, a `NotSent`), the failure's status and transport flag are kept for a caller that retries, and no
    link the failed walk read is kept: once the provider answers again, the walk reads every link again."""
    file_id = fake.add_file("a/b/data.bin", DATA)
    middle = fake.id_of("a/b")
    if fault == "status":
        fake.statuses = [[f"/data/{middle}", 1, 500]]
    else:
        fake.resets = [[f"/data/{middle}", 1]]
    with pytest.raises(ScopeUnread) as unread:
        client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 0}, content=b"x")
    assert isinstance(unread.value, NotSent)
    assert (unread.value.status, unread.value.transport) == ((500, False) if fault == "status" else (None, True))
    assert "nothing was sent" in str(unread.value)
    assert [c for c in fake.provider_calls if c[1] == "PUT"] == []
    assert parent_reads(fake) == [file_id, middle]
    client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 0}, content=b"x")
    assert parent_reads(fake) == [file_id, middle, file_id, middle, fake.id_of("a")]


def test_o09_a_root_that_cannot_be_read_fails_closed_and_is_read_again_next_time(config, fake, client):
    """O09: the space's root is kept only once a provider has answered it; a failed root read refuses the mutation
    as not sent, and the next one reads the root again."""
    file_id = fake.add_file("data.bin", DATA)
    fake.statuses = [[f"/spaces/{fake.SPACE_ID}", 1, 503]]
    with pytest.raises(ScopeUnread):
        client.mutate("DELETE", f"/data/{file_id}", provider="alpha")
    assert fake.exists("data.bin")
    client.mutate("DELETE", f"/data/{file_id}", provider="alpha")
    assert len(root_reads(fake)) == 2
    assert not fake.exists("data.bin")


def test_o09_a_failed_name_read_is_not_sent_and_says_so(config, fake, client):
    """O09: a failure of Onezone's name read, after a walk that worked, is `ScopeUnread` too, and nothing is sent."""
    file_id = fake.add_file("data.bin", DATA)
    fake.status_override = 503  # every Onezone call fails from here on
    with pytest.raises(ScopeUnread) as unread:
        client.mutate("DELETE", f"/data/{file_id}", provider="alpha")
    assert unread.value.status == 503
    assert fake.exists("data.bin")


@pytest.mark.parametrize(
    "answer",
    [
        {},  # no parent link at all
        {"parent_id": ""},
        {"parent_id": 5},
        {"parent_id": "../root-s"},  # not a plain id, so it cannot be put in a path
        {"parent_id": "a/b"},
        {"parent_id": ["root-s"]},
        {"parentId": "root-s"},  # another key than the one 25.0 answers
    ],
)
def test_o09_a_parent_link_that_is_not_a_plain_id_or_null_fails_closed(config, answer):
    """O09: a parent link is a plain file id, or null at the top of a tree; anything else is unreadable, and the
    mutation is refused as not sent."""
    client, seen = scripted(config, lambda request: httpx.Response(204), links={"abc": answer})
    with pytest.raises(ScopeUnread):
        client.mutate("DELETE", "/data/abc", provider="alpha")
    assert "DELETE" not in [r.method for r in seen]


def by_host(config, links):
    """A client whose providers answer parent links from `links`, host to file id to parent (None at the top); both
    give the space's root as `root-s`, and Onezone the space's configured name."""
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path == f"{ZONE}/spaces/{config.space.id}":
            return httpx.Response(200, json={"name": config.space.name})
        if path == f"{PROVIDER}/spaces/{config.space.id}":
            return httpx.Response(200, json={"fileId": SCRIPTED_ROOT})
        if request.url.params.get("attribute") == "parent_id":
            return httpx.Response(200, json={"parent_id": links[request.url.host][path.rsplit("/", 1)[-1]]})
        return httpx.Response(204)

    return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_o09_a_link_one_provider_gave_never_resolves_a_file_through_another(config):
    """O09: the links a walk keeps are the provider's that gave them. A file whose chain reaches the configured root
    through one provider, and leaves the space through another, is refused through the second, though the first's
    chain is kept: the second provider is asked for its own links, and nothing is sent to it."""
    links = {
        "alpha.test": {"fid": SCRIPTED_ROOT},
        "beta.test": {"fid": "elsewhere-dir", "elsewhere-dir": None},  # beta's chain ends above every known root
    }
    client, seen = by_host(config, links)
    client.mutate("DELETE", "/data/fid", provider="alpha")
    with pytest.raises(OutOfScope, match="no space this configuration knows"):
        client.mutate("DELETE", "/data/fid", provider="beta")
    assert [(r.method, r.url.host) for r in seen if r.method == "DELETE"] == [("DELETE", "alpha.test")]
    assert [r.url.host for r in seen if r.url.params.get("attribute") == "parent_id"] == [
        "alpha.test",
        "beta.test",
        "beta.test",
    ]


def test_o09_a_directory_moved_up_after_a_deep_walk_is_walked_fresh_before_a_child_is_refused(config, fake, client):
    """O09: a directory walked when it lay deep keeps its old ancestry in the client. Moved near the root, it gets a
    child whose walk over the kept links passes the bound; before refusing, the walk drops the kept links on its chain
    and reads every link fresh, and the child resolves. The fresh links are kept in place of the stale ones."""
    deep = "/".join(f"d{i}" for i in range(1, MAX_SPACE_DEPTH - 1))  # 62 directories below the root
    first = fake.add_file(f"{deep}/moved/old.bin", DATA)  # 64 links up to the root
    assert client.file_space_name(first, "alpha") == SPACE_NAME
    moved = fake.id_of(f"{deep}/moved")
    fake.nodes[moved].parent = ROOT_ID  # the directory now lies right under the root
    child = fake.add_file("moved/new/child.bin", DATA)  # 3 links up to the root, 65 over the kept ones
    reads_before = len(parent_reads(fake))
    assert client.file_space_name(child, "alpha") == SPACE_NAME
    fresh = parent_reads(fake)[reads_before:]
    assert fresh[-3:] == [child, fake.id_of("moved/new"), moved]  # the fresh walk, every link read
    later = fake.add_file("moved/later.bin", DATA)
    reads_before = len(parent_reads(fake))
    assert client.file_space_name(later, "alpha") == SPACE_NAME
    assert parent_reads(fake)[reads_before:] == [later]  # the moved directory's fresh link was kept


def test_o09_a_walk_still_too_deep_when_walked_fresh_is_refused(config, fake, client):
    """O09: the fresh walk is a second chance for kept links only; a file that is deeper than the bound when every
    link is read again is refused, with nothing sent."""
    too_deep = fake.add_file("/".join(["e"] * MAX_SPACE_DEPTH + ["past.bin"]), DATA)
    with pytest.raises(OutOfScope, match=f"more than {MAX_SPACE_DEPTH} directories"):
        client.mutate("DELETE", f"/data/{too_deep}", provider="alpha")
    assert [c for c in fake.provider_calls if c[1] == "DELETE"] == []


def test_o09_the_kept_links_are_bounded_least_recently_used_first_and_cleared_on_close(config, fake):
    """O09: the client keeps at most `kept_links` parent links (`MAX_KEPT_LINKS` by default), dropping the least
    recently used first, so a long campaign does not grow it without bound; a dropped link is read again when next
    needed, and close() drops them all."""
    client = OnezoneClient(config, http=fake.client_http(), kept_links=3)
    files = [fake.add_file(f"dir{i}/f.bin", DATA) for i in range(3)]  # each file costs two links
    for file_id in files:
        client.file_space_id(file_id, "alpha")
    assert client.kept_links() <= 3
    reads = len(parent_reads(fake))
    client.file_space_id(files[-1], "alpha")  # the most recent file's links are still kept
    assert len(parent_reads(fake)) == reads
    client.file_space_id(files[0], "alpha")  # the oldest were dropped, so they are read again
    assert len(parent_reads(fake)) == reads + 2
    assert MAX_KEPT_LINKS >= 10_000
    client.close()
    assert client.kept_links() == 0


def test_o09_a_cycle_of_parent_links_is_refused_at_the_bound(config):
    """O09: links that loop never reach a root; the walk stops at the bound and refuses, sending nothing."""
    links = {"abc": {"parent_id": "abd"}, "abd": {"parent_id": "abc"}}
    client, seen = scripted(config, lambda request: httpx.Response(204), links=links)
    with pytest.raises(OutOfScope, match=f"more than {MAX_SPACE_DEPTH} directories"):
        client.mutate("DELETE", "/data/abc", provider="alpha")
    assert "DELETE" not in [r.method for r in seen]
    assert len([r for r in seen if r.url.params.get("attribute") == "parent_id"]) <= MAX_SPACE_DEPTH


def test_o09_the_walk_is_the_gates_own_with_the_admin_credential_even_for_a_write_sent_as_a_token(config, fake, client):
    """O09, O05: day-0 sends a write as the data-path token to see it refused. Every read the gate makes to resolve
    the target (the root, the parent links, the name) is the client's own, with the admin credential, through the
    client's own transport and so its [tls]; only the write carries the token. None of them passes the data gate, so
    none is in the gate's log or in what the agent was served."""
    fake.add_file("a/b/placeholder.bin", b"")
    with pytest.raises(PlatformError):  # the fake does not know the token, so the write is refused; it was sent
        client.mutate(
            "POST",
            f"/data/{fake.id_of('a/b')}/children",
            provider="alpha",
            params={"name": "n", "type": "DIR"},
            token="a-data-path-token",
        )
    sent = list(zip(gate_reads(fake), (r.token for r in fake.requests), strict=True))
    assert [kind for kind, _ in sent] == ["root", "parent", "parent", "name", "POST"]
    assert {token for kind, token in sent if kind != "POST"} == {fake.ADMIN}
    assert [token for kind, token in sent if kind == "POST"] == ["a-data-path-token"]


def test_o09_every_mutation_by_file_id_reads_its_spaces_name_again_right_before_it_is_sent(config, seeded, client):
    """O09: the walk is kept, but the space's name is read for every mutation of a file, just before it."""
    file_id = seeded.id_of(PATH)
    for offset in (0, 10, 20):
        client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": offset}, content=b"x")
    assert gate_reads(seeded) == ["root", "parent", "parent", "name", "PUT", "name", "PUT", "name", "PUT"]


def test_o09_a_space_renamed_out_of_scope_between_two_mutations_by_file_id_refuses_the_second(config, seeded, client):
    """O09: the name that authorised one write authorises nothing later. Onezone names the file's space otherwise
    for the second write, which is refused and not sent."""
    file_id = seeded.id_of(PATH)
    client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 0}, content=b"x")
    writes_before = [c for c in seeded.provider_calls if c[1] == "PUT"]
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 1}, content=b"y")
    assert [c for c in seeded.provider_calls if c[1] == "PUT"] == writes_before


def test_o09_a_space_renamed_out_of_scope_between_two_range_reads_refuses_the_second(config, seeded, client):
    """O09: a range read reads its file's space's name again as well, so a rename between two reads stops the second."""
    file_id = seeded.id_of(PATH)
    client.read_range(f"/data/{file_id}/content", provider="beta", offset=0, length=5)
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.read_range(f"/data/{file_id}/content", provider="beta", offset=5, length=5)
    assert len(name_reads(seeded)) == 2
    assert len([r for r in seeded.requests if r.range]) == 1


def test_o09_a_space_renamed_out_of_scope_between_two_transfers_refuses_the_second(config, seeded, client):
    """O09: a transfer job names its file in the body, and that file's space's name is read again for each job."""
    file_id = seeded.id_of(PATH)
    body = {"type": "replication", "replicatingProviderId": "x", "dataSourceType": "file", "fileId": file_id}
    client.mutate("POST", "/transfers", provider="beta", json=body)
    seeded.space_name = "elsewhere"
    with pytest.raises(OutOfScope, match="configured prefixes"):
        client.mutate("POST", "/transfers", provider="beta", json=body)
    assert len([c for c in seeded.provider_calls if c[2] == "/transfers" and c[1] == "POST"]) == 1


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


def test_o35_a_range_read_reports_the_status_and_the_range_it_was_answered_with(config):
    """O37: the bytes are returned as before, and `answers` is told, once, what the platform answered: its status, its
    Content-Range as sent, and the bytes of the body."""
    client, _ = scripted(config, ranged(206, b"abcde", "bytes 10-14/100"))
    got = []
    assert read(client, answers=got.append) == b"abcde"
    assert got == [ReadOutcome(206, "bytes 10-14/100", 5)]


def test_o35_a_range_clipped_at_the_end_of_the_file_reports_the_range_it_got_and_its_shorter_body(config):
    """O37: a range that runs past the end is answered with its clipped range and fewer bytes than asked for; the
    outcome says so, so a reader tells a clipped answer from a whole one without the request."""
    client, _ = scripted(config, ranged(206, b"abc", "bytes 10-12/13"))
    got = []
    assert read(client, answers=got.append) == b"abc"
    assert got == [ReadOutcome(206, "bytes 10-12/13", 3)]
    assert got[0].to_json() == {"status": 206, "content_range": "bytes 10-12/13", "body_bytes": 3}


@pytest.mark.parametrize(
    ("status", "body", "content_range"),
    [
        (200, b"abcde", None),  # the whole file, not a range
        (200, b"abcde", "bytes 10-14/100"),  # a 200 is never a range read, whatever it says
        (206, b"abcde", None),  # no Content-Range
        (206, b"abcde", "bytes 10-14/12"),  # a range that ends past the end of the file it names
        (206, b"", "bytes 10-9/10"),  # an empty body at the end of the file: no byte of the range asked for
        (206, b"abcde", "bytes 10-14/*"),  # a file of no known size
    ],
)
def test_o35_an_answer_that_is_not_the_range_asked_for_is_refused_and_never_reported_as_it(
    config, status, body, content_range
):
    """O37: a refused answer reports nothing to `answers`, so no consumer takes a whole file, a range that does not
    fit its file or an empty answer for the bytes asked for."""
    client, _ = scripted(config, ranged(status, body, content_range))
    got = []
    with pytest.raises(PlatformError):
        read(client, answers=got.append)
    assert got == []


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


@pytest.mark.parametrize(
    ("params", "path"),
    [
        (None, "/data/{id}/content"),
        ({}, "/data/{id}/content"),
        ({"limit": 1}, "/data/{id}/content"),
        ({"offset": None}, "/data/{id}/content"),
        ({"offset": -1}, "/data/{id}/content"),
        ({"offset": True}, "/data/{id}/content"),
        ({"offset": 1.0}, "/data/{id}/content"),
        ({"offset": "0"}, "/data/{id}/content"),
        (None, "/data/{id}/content/"),
    ],
)
def test_o44_a_content_write_without_an_offset_is_refused_before_anything_is_read_or_sent(
    config, seeded, client, params, path
):
    """O44: a write of content without the `offset` query truncates the file to 0 first (op-worker f118ed9,
    file_content_rest_handler.erl:224-228), so the client refuses it, a whole offset of 0 or more being the only kind
    it sends: nothing is read for its gate and nothing is sent, and the file keeps its bytes."""
    file_id = seeded.id_of(PATH)
    with pytest.raises(TruncatingWrite, match="offset"):
        client.mutate("PUT", path.format(id=file_id), provider="alpha", params=params, content=b"x")
    assert seeded.provider_calls == []
    assert bytes(seeded.nodes[file_id].content) == DATA


def test_o44_a_content_write_at_an_offset_and_an_empty_one_are_sent(config, seeded, client):
    """O44: an offset of 0 or more passes, an empty body too (the write that extends a file's size, #107)."""
    file_id = seeded.id_of(PATH)
    client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 0}, content=b"y")
    client.mutate("PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 8192}, content=b"")
    assert [r.query for r in seeded.requests if r.method == "PUT"] == [{"offset": "0"}, {"offset": "8192"}]
    assert len(seeded.nodes[file_id].content) == 8192


def test_o09_a_raw_body_is_sent_as_an_octet_stream_and_nothing_else_is_marked_so(config):
    """O09: every mutation with a raw body says it is application/octet-stream, as Onedata 25.0 requires (415
    without), whether sent as the admin or as a token; a mutation without one, or with a JSON body, is not marked so."""
    client, seen = scripted(config, lambda request: httpx.Response(204))
    client.mutate("PUT", "/data/fid/content", provider="alpha", params={"offset": 0}, content=b"\x00\x01")
    client.mutate("PUT", "/data/fid/content", provider="alpha", params={"offset": 2}, content=b"", token="t-1")
    client.mutate("DELETE", "/data/fid", provider="alpha")
    client.mutate("POST", "/transfers", provider="alpha", json={"fileId": "fid", "type": "eviction"})
    sent = [r for r in seen if r.method in ("PUT", "DELETE", "POST")]
    assert [r.headers.get("Content-Type") for r in sent] == [
        "application/octet-stream",
        "application/octet-stream",
        None,
        "application/json",
    ]


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


def test_o32_an_anonymous_dated_read_sends_no_credential_and_only_to_a_public_path_of_onezone(config):
    """O32: Onezone's current-time endpoint needs no privilege, so the dated read sends it with no credential header
    when asked (`anonymous`), where a token's caveats could refuse a header; the answer comes back with its Date
    header. No other path, and no provider's, is ever sent so: the call is refused, unsent, and the default read still
    carries the admin credential."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"timeMillis": 5}, headers={"Date": "Thu, 01 Jan 1970 00:16:44 GMT"})

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get_dated(ZONE_TIME_PATH, anonymous=True) == ({"timeMillis": 5}, "Thu, 01 Jan 1970 00:16:44 GMT")
    assert seen[0].url.path == f"{ZONE}{ZONE_TIME_PATH}"
    assert "x-auth-token" not in seen[0].headers
    for path, provider in (("/spaces", None), (ZONE_TIME_PATH, "alpha")):
        with pytest.raises(OutOfScope, match="only a public read"):
            client.get_dated(path, provider=provider, anonymous=True)
    assert len(seen) == 1  # nothing was sent for either refusal
    client.get_dated(ZONE_TIME_PATH)
    assert "x-auth-token" in seen[1].headers

"""The baseline writer's listing of a directory (deploy/compose-federation/fedcontent.py) ends as Onedata 25.0's does:
on `isLast` true alone (`day0/2026-11-12-federation-facts-listing-end.md`), and a page without that marker is refused.
The provider is an in-memory stand-in, so nothing starts a container."""

import pytest


@pytest.fixture
def content(fed):
    import fedcontent

    return fedcontent


class PagedProvider:
    """The children call of one directory of `count` entries, paged as 25.0 pages it: a token on every page, `isLast`
    true only on a page shorter than its limit, and 400 posix eagain (here an error) for the page after a last one."""

    def __init__(self, count: int, limit: int, edit=None) -> None:
        self.names = [f"entry{number}" for number in range(count)]
        self.limit, self.edit, self.asked = limit, edit or (lambda page: page), []

    def provider(self, service, method: str, path: str, label: str, *, params):
        token = params.get("token")
        self.asked.append(token)
        if token is not None and token.startswith("end-"):
            raise AssertionError("asked for the page after a last page, which 25.0 answers 400 posix eagain")
        start = int(token or "0")
        page = self.names[start : start + self.limit]
        last = len(page) < self.limit
        entries = [{"file_id": f"id-{name}", "name": name, "type": "REG", "size": 1} for name in page]
        answer = {"nextPageToken": f"{'end-' if last else ''}{start + len(page)}", "isLast": last, "children": entries}
        return self.edit(answer)


def lister(content, provider):
    """A Content whose provider calls go to `provider`; only `children` is used."""
    reader = content.Content.__new__(content.Content)
    reader.boot = provider
    return reader


@pytest.mark.parametrize(("count", "pages"), [(4, 3), (5, 3), (1, 1), (0, 1)])
def test_o26_the_baseline_writer_lists_a_directory_to_its_last_page_and_never_past_it(content, count, pages):
    """O26: pages of two; four entries come as two full pages and an empty last one, which carries a token that is not
    followed; five as two full pages and a short one. Every entry is listed once."""
    provider = PagedProvider(count, limit=2)
    found = lister(content, provider).children(None, "dir-id")
    assert [entry["name"] for entry in found] == provider.names
    assert len(provider.asked) == pages


@pytest.mark.parametrize(
    "edit",
    [
        lambda page: {k: v for k, v in page.items() if k != "isLast"},  # read as the last page before
        lambda page: {**page, "isLast": "false"},
        lambda page: {**page, "isLast": None},
        lambda page: {k: v for k, v in page.items() if k != "children"},
        lambda page: {**page, "nextPageToken": None} if not page["isLast"] else page,  # not the last, and no token
        lambda page: {**page, "nextPageToken": ""} if not page["isLast"] else page,
    ],
)
def test_o26_the_baseline_writer_refuses_a_page_without_its_last_page_marker(content, edit):
    """O26: a page without a boolean `isLast`, or not the last and without a token, may be cut; the writer refuses it
    rather than taking the entries so far for the whole directory."""
    with pytest.raises(RuntimeError, match="listing page"):
        lister(content, PagedProvider(5, limit=2, edit=edit)).children(None, "dir-id")


def test_o26_a_last_page_ends_the_listing_whatever_its_token(content):
    """O26: `isLast` true is the end: the last page's token, a non-empty string on 25.0, is not followed."""
    assert content.next_page_token({"nextPageToken": "t" * 71, "isLast": True, "children": []}) is None
    assert content.next_page_token({"nextPageToken": "t" * 158, "isLast": False, "children": []}) == "t" * 158

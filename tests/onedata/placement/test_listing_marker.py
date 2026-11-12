"""Every listing of the platform layer ends only on the API's last-page marker, never on its absence (O26).

The shapes the fakes answer, and the readers must take, are those measured on Onedata 25.0 (`LISTING_END_FACTS`,
`deploy/compose-federation/day0/2026-11-12-federation-facts-listing-end.md`): a children page ends a listing on
`isLast` true alone, its token present on the last page too, and a transfer page on `nextPageToken` null, with no
`isLast`; an exactly full last page of either is followed by an empty one."""

import pytest
from sandbox_support import add_sandbox

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.day0.runner import build_context, run_checks
from expcore_onedata.precampaign import content_type_check
from expcore_onedata.probes import PlatformProbes
from expcore_onedata.provision import next_page_token
from expcore_onedata.testing import ALPHA_ID, DATA, with_placement, write_platform
from expcore_onedata.testing.fake_data import LISTING_END_FACTS
from expcore_onedata.transferrecord import Unreadable, page_of

FOLDER = "shelf"
MARKERS = {"isLast", "nextPageToken"}


def stocked(fake, count=3):
    """A directory of `count` files, listed one entry per page: a listing must follow two pages to see them all."""
    for number in range(count):
        fake.add_file(f"{FOLDER}/volume{number}.bin", b"x")
    fake.page_size = 1


@pytest.mark.parametrize(
    ("omitted", "fields"),
    [
        (MARKERS, {}),  # a page cut with neither marker, which read as the last page
        ({"isLast"}, {}),  # a token for the next page, but no word on whether this one is the last
        (set(), {"isLast": "false"}),  # a marker that is not a boolean: a non-empty string read as true
        (set(), {"isLast": None}),
    ],
)
def test_o26_a_children_page_without_a_boolean_last_page_marker_is_refused_not_taken_as_the_end(
    provisioner, fake, omitted, fields
):
    """O26: Onedata says `isLast` is the only valid sign that a listing has every entry, and a page may be short and
    still not be the last. A page without it, or with one that is not a boolean, is refused; before, the provisioner
    took such a page for the last one and returned the directory's first entry as all of it."""
    stocked(fake)
    folder = fake.id_of(FOLDER)
    fake.children_omit, fake.children_fields = omitted, fields
    with pytest.raises(PlatformError, match="last-page marker"):
        provisioner.children(folder)


def test_o26_a_path_beyond_a_cut_page_is_not_reported_missing(provisioner, fake):
    """O26: resolving a path whose entry lies on a later page refuses the cut listing; it does not say the entry is
    missing, which an upload would act on by creating the file a second time."""
    stocked(fake)
    fake.children_omit = MARKERS
    with pytest.raises(PlatformError, match="last-page marker"):
        provisioner.resolve(f"{FOLDER}/volume2.bin")


def test_o26_a_page_that_is_not_the_last_and_names_no_next_page_is_refused(provisioner, fake):
    """O26: `isLast` false with no token cannot be followed, so the listing is refused rather than ended."""
    stocked(fake)
    fake.children_omit = {"nextPageToken"}
    with pytest.raises(PlatformError, match="names no next page"):
        provisioner.children(fake.id_of(FOLDER))


def test_o26_a_listing_with_its_markers_is_followed_to_the_end(provisioner, fake):
    """O26: the rule refuses only a page without its marker; a listing of many short pages is read whole."""
    stocked(fake, count=4)
    names = sorted(provisioner.children(fake.id_of(FOLDER)))
    assert names == [f"volume{number}.bin" for number in range(4)]


def test_o26_the_pre_campaign_check_does_not_call_a_file_on_a_cut_page_absent(tmp_path, fake):
    """O26: the content-type check looks for its file page by page. A first page without its marker was read as the
    end, and the check reported the file "not in the space"; it now says the listing could not be read."""

    def edit(data):
        with_placement(data)
        add_sandbox(data)
        data["sandbox"]["content_check_file"] = f"{FOLDER}/volume2.bin"

    config = load_platform(write_platform(tmp_path, edit))
    stocked(fake)
    fake.children_omit = MARKERS
    verdict = content_type_check(config, OnezoneClient(config, http=fake.client_http()))
    assert verdict.ok is False
    assert "last-page marker" in verdict.detail
    assert "not in the space" not in verdict.detail


def test_o26_the_day0_listing_check_fails_on_cut_pages_instead_of_agreeing_with_itself(tmp_path, fake, ticker):
    """O26: the day-0 check reads the root by the server's pages and again two at a time. With every page cut, both
    reads stopped at their first page and agreed, so the check passed on a listing it never finished."""
    for number in range(5):
        fake.add_file(f"volume{number}.bin", DATA[:8])
    fake.page_size = 2
    fake.children_omit = MARKERS
    platform = write_platform(tmp_path, with_placement)
    client = OnezoneClient(load_platform(platform), http=fake.client_http())
    options = {"client": client, "tcp": lambda host, port, timeout: 1.5, "clock": ticker.clock, "sleep": ticker.sleep}
    context = build_context(platform, tmp_path / "day0", **options)
    entry = {e.name: e for e in run_checks(context, ["listing"])}["listing"]
    assert entry.ok is False
    assert entry.facts["providers"]["alpha"]["error"] == "PlatformError"


def test_o26_a_transfer_page_without_its_next_page_token_is_unreadable_not_the_last(
    client, provisioner, seeded, ticker
):
    """O26: the transfer listing's page ends the listing with `nextPageToken` null; a page without the key is cut, and
    the probes record the listing as unknown, not as one without transfers."""
    with pytest.raises(Unreadable, match="nextPageToken"):
        page_of({"transfers": []})
    with pytest.raises(Unreadable, match="nextPageToken"):
        page_of({"transfers": [], "nextPageToken": ""})
    assert page_of({"transfers": ["tid-0001"], "nextPageToken": None}) == (["tid-0001"], None)
    seeded.clock = ticker.clock
    probes = PlatformProbes(
        client, provisioner, {"data": "study/data.bin"}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock
    )
    probes.start({})
    seeded.transfer_list_body = {"transfers": []}
    section = probes.finish()["space_transfers"]
    assert section["probe_ok"] is False
    assert "nextPageToken" in section["error"]


# -- the shapes measured on Onedata 25.0 -------------------------------------------------------------------------------


def children_pages(client, folder, limit):
    """Every page of a directory's children, asked for as the provisioner asks, to the page that says it is the last."""
    pages, params = [], {"limit": limit}
    while True:
        page = client.get(f"/data/{folder}/children", provider="alpha", params=params)
        pages.append(page)
        if page["isLast"]:
            return pages
        params = {**params, "token": page["nextPageToken"]}


def test_o26_the_facts_the_fakes_follow_are_the_measured_ones():
    """O26: the fakes cite the dated facts of the federation the listing ends were measured on."""
    assert LISTING_END_FACTS == "deploy/compose-federation/day0/2026-11-12-federation-facts-listing-end.md"


@pytest.mark.parametrize(("limit", "sizes"), [(2, [2, 2, 0]), (3, [3, 1]), (4, [4, 0]), (7, [4])])
def test_o26_the_fake_answers_children_pages_as_onedata_25_does(client, fake, limit, sizes):
    """O26: every page carries `nextPageToken`, `isLast` and `children`, in that order; the token is a non-empty string
    on every page, the last included; `isLast` is false on each page before the last and true on it; and a page that
    the directory's last entries fill is followed by an empty last page, as on 25.0."""
    stocked(fake, count=4)
    fake.page_size = 1000
    pages = children_pages(client, fake.id_of(FOLDER), limit)
    assert [len(page["children"]) for page in pages] == sizes
    assert all(list(page) == ["nextPageToken", "isLast", "children"] for page in pages)
    assert all(isinstance(page["nextPageToken"], str) and page["nextPageToken"] for page in pages)
    assert [page["isLast"] for page in pages] == [False] * (len(sizes) - 1) + [True]


def test_o26_the_page_after_a_last_page_answers_400_posix_eagain(client, fake):
    """O26: on 25.0 the token of a last page, the empty one after a full page included, leads to no page: asking for
    it answers 400 `posix` `eagain`, so a reader that went past the end would fail, not loop."""
    stocked(fake, count=4)
    fake.page_size = 1000
    folder = fake.id_of(FOLDER)
    for limit in (2, 7):
        last = children_pages(client, folder, limit)[-1]
        with pytest.raises(PlatformError) as refused:
            client.get(
                f"/data/{folder}/children", provider="alpha", params={"limit": limit, "token": last["nextPageToken"]}
            )
        assert (refused.value.status, refused.value.code, refused.value.errno) == (400, "posix", "eagain")


@pytest.mark.parametrize(
    "page",
    [
        {"nextPageToken": "t" * 158, "isLast": True, "children": [{"name": "a"}]},  # a last page with entries
        {"nextPageToken": "t" * 71, "isLast": True, "children": []},  # the empty page after a full one
    ],
)
def test_o26_a_children_page_with_is_last_true_ends_the_listing_whatever_its_token(page):
    """O26: `isLast` true is the end, and only that: a last page carries a token on 25.0, which is not followed, and
    an empty last page is no error."""
    assert next_page_token(page) is None


def test_o26_a_directory_that_fills_its_pages_exactly_is_read_whole_and_never_past_its_end(provisioner, fake):
    """O26: four entries in pages of two come as two full pages and an empty last one. The provisioner reads all
    four and stops on the empty page's `isLast`, never asking for the page after it (which would answer 400)."""
    stocked(fake, count=4)
    fake.page_size = 2
    folder = fake.id_of(FOLDER)
    assert sorted(provisioner.children(folder)) == [f"volume{number}.bin" for number in range(4)]
    asked = [r for r in fake.requests if r.method == "GET" and r.path.endswith(f"/data/{folder}/children")]
    assert len(asked) == 3


def test_o26_the_pre_campaign_check_finds_its_file_on_the_last_full_page(tmp_path, fake):
    """O26: the content-type check looks for its file page by page; in a directory whose entries fill its pages
    exactly it finds the file on the last full page, and stops there."""

    def edit(data):
        with_placement(data)
        add_sandbox(data)
        data["sandbox"]["content_check_file"] = f"{FOLDER}/volume3.bin"

    stocked(fake, count=4)
    fake.page_size = 2
    config = load_platform(write_platform(tmp_path, edit))
    assert content_type_check(config, OnezoneClient(config, http=fake.client_http())).ok is True


def test_o26_the_day0_listing_check_passes_on_a_root_that_fills_its_pages_exactly(tmp_path, fake, ticker):
    """O26: the day-0 check reads the root by the server's pages and again two at a time. Four entries fill both
    readings' pages exactly, so each ends on an empty last page; both read all four, agree, and the check passes."""
    for number in range(4):
        fake.add_file(f"volume{number}.bin", DATA[:8])
    fake.page_size = 2
    platform = write_platform(tmp_path, with_placement)
    client = OnezoneClient(load_platform(platform), http=fake.client_http())
    options = {"client": client, "tcp": lambda host, port, timeout: 1.5, "clock": ticker.clock, "sleep": ticker.sleep}
    context = build_context(platform, tmp_path / "day0", **options)
    entry = {e.name: e for e in run_checks(context, ["listing"])}["listing"]
    assert entry.ok is True, entry.facts


def test_o26_a_transfer_page_ends_on_a_null_token_and_needs_no_is_last(client, seeded):
    """O26: a transfer page has `transfers` and `nextPageToken`, no `isLast`: a string token is followed and a null one
    is the end. The fake gives a full page a token, so two ended jobs in pages of two come as a full page and an empty
    one with a null token, as on 25.0, and a reader following the tokens lists both."""
    assert page_of({"transfers": ["tid-0001"], "nextPageToken": "p" * 16}) == (["tid-0001"], "p" * 16)
    assert page_of({"transfers": [], "nextPageToken": None}) == ([], None)
    for _ in range(2):
        seeded.add_transfer("replication", "study/data.bin", ALPHA_ID, scheduled=0)
    path, params, pages = f"/spaces/{seeded.SPACE_ID}/transfers", {"state": "ended", "limit": 2}, []
    while True:
        page = client.get(path, provider="alpha", params=params)
        pages.append(page)
        _, token = page_of(page)
        if token is None:
            break
        params = {**params, "page_token": token}
    assert [list(page) for page in pages] == [["transfers", "nextPageToken"]] * 2
    assert [len(page["transfers"]) for page in pages] == [2, 0]
    assert pages[0]["nextPageToken"]
    assert pages[1]["nextPageToken"] is None

"""Every listing of the platform layer ends only on the API's last-page marker, never on its absence (O22)."""

import pytest
from sandbox_support import add_sandbox

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.day0.runner import build_context, run_checks
from expcore_onedata.precampaign import content_type_check
from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import DATA, with_placement, write_platform
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
def test_o22_a_children_page_without_a_boolean_last_page_marker_is_refused_not_taken_as_the_end(
    provisioner, fake, omitted, fields
):
    """O22: Onedata says `isLast` is the only valid sign that a listing has every entry, and a page may be short and
    still not be the last. A page without it, or with one that is not a boolean, is refused; before, the provisioner
    took such a page for the last one and returned the directory's first entry as all of it."""
    stocked(fake)
    folder = fake.id_of(FOLDER)
    fake.children_omit, fake.children_fields = omitted, fields
    with pytest.raises(PlatformError, match="last-page marker"):
        provisioner.children(folder)


def test_o22_a_path_beyond_a_cut_page_is_not_reported_missing(provisioner, fake):
    """O22: resolving a path whose entry lies on a later page refuses the cut listing; it does not say the entry is
    missing, which an upload would act on by creating the file a second time."""
    stocked(fake)
    fake.children_omit = MARKERS
    with pytest.raises(PlatformError, match="last-page marker"):
        provisioner.resolve(f"{FOLDER}/volume2.bin")


def test_o22_a_page_that_is_not_the_last_and_names_no_next_page_is_refused(provisioner, fake):
    """O22: `isLast` false with no token cannot be followed, so the listing is refused rather than ended."""
    stocked(fake)
    fake.children_omit = {"nextPageToken"}
    with pytest.raises(PlatformError, match="names no next page"):
        provisioner.children(fake.id_of(FOLDER))


def test_o22_a_listing_with_its_markers_is_followed_to_the_end(provisioner, fake):
    """O22: the rule refuses only a page without its marker; a listing of many short pages is read whole."""
    stocked(fake, count=4)
    names = sorted(provisioner.children(fake.id_of(FOLDER)))
    assert names == [f"volume{number}.bin" for number in range(4)]


def test_o22_the_pre_campaign_check_does_not_call_a_file_on_a_cut_page_absent(tmp_path, fake):
    """O22: the content-type check looks for its file page by page. A first page without its marker was read as the
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


def test_o22_the_day0_listing_check_fails_on_cut_pages_instead_of_agreeing_with_itself(tmp_path, fake, ticker):
    """O22: the day-0 check reads the root by the server's pages and again two at a time. With every page cut, both
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


def test_o22_a_transfer_page_without_its_next_page_token_is_unreadable_not_the_last(
    client, provisioner, seeded, ticker
):
    """O22: the transfer listing's page ends the listing with `nextPageToken` null; a page without the key is cut, and
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

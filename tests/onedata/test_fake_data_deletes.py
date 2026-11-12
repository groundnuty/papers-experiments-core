"""The public `FakeData`'s pending delete: a delete that was answered and is not done yet (`delete_delay_s`).

These are tests of the fake, not of the platform layer. The delay is counted on the fake's own `clock`, which every test
binds to a `Ticker`, so none of them waits. The answers a test pins as recorded are those the fake's docstring and
`docs/interfaces.md` mark so (`DELETE_FACTS`); the ones they mark as assumptions are pinned here too, so a live check
that disproves one has a test to change.
"""

from pathlib import Path

import httpx
import pytest

from expcore_onedata.testing import (
    ALPHA_ID,
    BETA_ID,
    DATA,
    ERRORS_LEGACY,
    PROVIDER,
    ROOT_ID,
    FakeData,
    Ticker,
    make_federation,
)
from expcore_onedata.testing.fake_data import DELETE_FACTS

HOSTS = {ALPHA_ID: "https://alpha.test", BETA_ID: "https://beta.test"}
TYPED = {"Content-Type": "application/octet-stream"}
GONE_25_0 = {"error": {"id": "posix", "details": {"errno": "enoent"}}}
DELAY_S = 5.0
REPO = Path(__file__).resolve().parents[2]


def call(fake: FakeData, provider: str, method: str, route: str, **options) -> httpx.Response:
    """Send one request to a provider's data API and return the fake's answer."""
    headers = {"X-Auth-Token": fake.admin_token, **options.pop("headers", {})}
    return fake.respond(httpx.Request(method, f"{HOSTS[provider]}{PROVIDER}{route}", headers=headers, **options))


def names(fake: FakeData, provider: str, directory: str = ROOT_ID) -> list[str]:
    return [c["name"] for c in call(fake, provider, "GET", f"/data/{directory}/children").json()["children"]]


def make(fake: FakeData, provider: str, parent: str, name: str, kind: str) -> str:
    made = call(fake, provider, "POST", f"/data/{parent}/children", params={"name": name, "type": kind})
    assert made.status_code == 201
    return made.json()["fileId"]


def write(fake: FakeData, provider: str, file_id: str, body: bytes) -> None:
    done = call(fake, provider, "PUT", f"/data/{file_id}/content", params={"offset": 0}, headers=TYPED, content=body)
    assert done.status_code == 204


def delete(fake: FakeData, provider: str, file_id: str) -> None:
    assert call(fake, provider, "DELETE", f"/data/{file_id}").status_code == 204


def delayed(**options) -> tuple[FakeData, Ticker]:
    """A federation whose deletes land `DELAY_S` seconds of its clock after they are answered."""
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock, **options)
    fake.delete_delay_s = DELAY_S
    return fake, ticker


def absent(fake: FakeData, provider: str, file_id: str) -> bool:
    """Whether the provider answers an entry as 25.0 answers a deleted one, on every read of it."""
    reads = (f"/data/{file_id}", f"/data/{file_id}/content", f"/data/{file_id}/distribution")
    return all(call(fake, provider, "GET", read).json() == GONE_25_0 for read in reads)


def test_by_default_a_delete_is_done_at_once_everywhere_and_a_reused_name_keeps_its_files():
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock)
    assert fake.delete_delay_s == 0.0
    fake.add_file("study/old.bin", DATA)
    study = fake.id_of("study")
    delete(fake, ALPHA_ID, study)
    assert absent(fake, ALPHA_ID, study)
    assert absent(fake, BETA_ID, study)
    assert names(fake, ALPHA_ID) == names(fake, BETA_ID) == []
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")
    kept = make(fake, ALPHA_ID, fresh, "new.bin", "REG")
    write(fake, ALPHA_ID, kept, b"fresh")
    ticker.sleep(1000)
    assert call(fake, ALPHA_ID, "GET", f"/data/{kept}/content").content == b"fresh"  # nothing took it


def test_a_deleted_file_is_gone_where_the_delete_was_sent_and_shown_elsewhere_until_it_lands():
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert absent(fake, ALPHA_ID, file_id)  # recorded: the provider the delete went to answers as for a deleted entry
    parent = call(fake, ALPHA_ID, "GET", f"/data/{file_id}", params={"attribute": "parent_id"})
    assert parent.json() == {"parent_id": ROOT_ID}  # recorded: the deleted entry's parent link still reads
    assert names(fake, ALPHA_ID) == []
    assert not fake.exists("a.bin")
    # an assumption, from the other providers' directory figures following a delete late: they still show the entry
    assert names(fake, BETA_ID) == ["a.bin"]
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}/content").content == DATA
    ticker.sleep(4)
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200  # a second short of the delay
    ticker.sleep(1)
    assert absent(fake, BETA_ID, file_id)  # landed: every provider answers it as a deleted entry
    assert names(fake, BETA_ID) == []


def test_a_deleted_directory_pends_with_what_is_under_it():
    fake, ticker = delayed()
    inner = fake.add_file("study/deep/b.bin", DATA)
    study, deep = fake.id_of("study"), fake.id_of("study/deep")
    delete(fake, ALPHA_ID, study)
    assert all(absent(fake, ALPHA_ID, gone) for gone in (study, deep, inner))
    assert names(fake, ALPHA_ID) == []
    assert (names(fake, BETA_ID), names(fake, BETA_ID, study), names(fake, BETA_ID, deep)) == (
        ["study"],
        ["deep"],
        ["b.bin"],
    )
    ticker.sleep(5)
    assert all(absent(fake, BETA_ID, gone) for gone in (study, deep, inner))
    assert names(fake, BETA_ID) == []
    assert fake.deleted_parents[inner] == deep  # and each keeps the parent it had


def test_a_directory_name_reused_during_its_delete_loses_the_files_made_in_it_when_the_delete_lands():
    fake, ticker = delayed()
    fake.add_file("study/old.bin", DATA)
    old = fake.id_of("study")
    delete(fake, ALPHA_ID, old)
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")  # recorded: the create is accepted, not refused
    assert fresh != old
    kept = make(fake, ALPHA_ID, fresh, "new.bin", "REG")
    write(fake, ALPHA_ID, kept, b"fresh")
    assert call(fake, ALPHA_ID, "GET", f"/data/{kept}/content").content == b"fresh"
    assert names(fake, ALPHA_ID, fresh) == ["new.bin"]
    ticker.sleep(5)
    assert absent(fake, ALPHA_ID, kept)  # recorded: the old delete's late removal takes the new directory's files
    assert absent(fake, BETA_ID, old)
    assert call(fake, ALPHA_ID, "GET", f"/data/{fresh}").status_code == 200  # assumed: the directory itself stays
    assert names(fake, ALPHA_ID, fresh) == []
    assert names(fake, ALPHA_ID) == ["study"]
    assert fake.exists("study")
    assert not fake.exists("study/new.bin")


def test_a_directory_that_loses_files_loses_the_files_at_any_depth_and_keeps_the_directories():
    fake, ticker = delayed()
    fake.add_file("study/x.bin", DATA)
    delete(fake, ALPHA_ID, fake.id_of("study"))
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")
    sub = make(fake, ALPHA_ID, fresh, "sub", "DIR")
    make(fake, ALPHA_ID, sub, "deep.bin", "REG")
    ticker.sleep(5)
    assert names(fake, ALPHA_ID, fresh) == ["sub"]  # assumed: only files go
    assert names(fake, ALPHA_ID, sub) == []


def test_code_that_waits_until_every_provider_answers_absent_keeps_what_it_writes_next():
    fake, ticker = delayed()
    fake.add_file("study/old.bin", DATA)
    old = fake.id_of("study")
    delete(fake, ALPHA_ID, old)
    started = ticker.now
    for _ in range(60):  # a bounded wait: a fake that never lands the delete fails here, it does not spin
        if all(absent(fake, provider, old) for provider in (ALPHA_ID, BETA_ID)):
            break
        ticker.sleep(1)
    assert ticker.now - started == DELAY_S
    fresh = make(fake, ALPHA_ID, ROOT_ID, "study", "DIR")
    kept = make(fake, ALPHA_ID, fresh, "new.bin", "REG")
    write(fake, ALPHA_ID, kept, b"fresh")
    ticker.sleep(1000)
    assert call(fake, ALPHA_ID, "GET", f"/data/{kept}/content").content == b"fresh"


def test_a_file_name_reused_during_its_delete_keeps_the_new_file():
    """The facts record the loss for a directory's name only, so the fake loses nothing of a file's (an assumption)."""
    fake, ticker = delayed()
    old = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, old)
    fresh = make(fake, ALPHA_ID, ROOT_ID, "a.bin", "REG")
    write(fake, ALPHA_ID, fresh, b"fresh")
    ticker.sleep(10)
    assert absent(fake, ALPHA_ID, old)
    assert absent(fake, BETA_ID, old)
    assert call(fake, BETA_ID, "GET", f"/data/{fresh}/content").content == b"fresh"
    assert names(fake, ALPHA_ID) == names(fake, BETA_ID) == ["a.bin"]


def test_a_provider_that_still_shows_the_entry_refuses_its_name_as_it_refuses_any_taken_one():
    """An assumption: the provider still shows the entry, so a create of its name collides there (409), as the fake
    answers for any taken name; the provider the delete went to takes the name (the test above)."""
    fake, ticker = delayed()
    old = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, old)
    refused = call(fake, BETA_ID, "POST", f"/data/{ROOT_ID}/children", params={"name": "a.bin", "type": "REG"})
    assert refused.status_code == 409
    ticker.sleep(5)
    assert make(fake, BETA_ID, ROOT_ID, "a.bin", "REG")  # landed: the name is free


def test_a_second_delete_of_an_entry_still_shown_does_not_move_when_it_lands():
    """An assumption: the second delete, through the provider that still shows the entry, is answered 204 and changes
    nothing; the entry lands when the first said."""
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    ticker.sleep(3)
    delete(fake, BETA_ID, file_id)
    ticker.sleep(2)
    assert absent(fake, BETA_ID, file_id)


def test_the_delay_is_set_by_the_constructor_and_by_make_federation():
    assert FakeData(delete_delay_s=2.5).delete_delay_s == 2.5
    assert make_federation(delete_delay_s=2.5).delete_delay_s == 2.5


def test_a_pending_delete_lands_in_the_older_shape_as_that_shape_answers_a_missing_entry():
    fake, ticker = delayed(error_shape=ERRORS_LEGACY)
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 404
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 200
    ticker.sleep(5)
    assert call(fake, BETA_ID, "GET", f"/data/{file_id}").status_code == 404


def test_the_facts_the_fake_names_are_files_of_the_repository():
    assert DELETE_FACTS
    assert all((REPO / path).is_file() for path in DELETE_FACTS)


def test_a_delete_that_fails_or_is_ignored_pends_nothing():
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    fake.ignore_deletes = True
    delete(fake, ALPHA_ID, file_id)
    ticker.sleep(10)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 200
    fake.ignore_deletes, fake.fail_deletes = False, True
    assert call(fake, ALPHA_ID, "DELETE", f"/data/{file_id}").status_code == 500
    ticker.sleep(10)
    assert call(fake, ALPHA_ID, "GET", f"/data/{file_id}").status_code == 200


@pytest.mark.parametrize("delay", [0.0, -1.0])
def test_a_delay_of_nothing_or_less_is_no_delay(delay):
    fake = make_federation(clock=Ticker().clock, delete_delay_s=delay)
    file_id = fake.add_file("a.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    assert absent(fake, ALPHA_ID, file_id)
    assert absent(fake, BETA_ID, file_id)


def test_a_test_can_land_every_delete_under_way_without_moving_the_clock():
    fake, ticker = delayed()
    file_id = fake.add_file("a.bin", DATA)
    inner = fake.add_file("study/b.bin", DATA)
    delete(fake, ALPHA_ID, file_id)
    delete(fake, ALPHA_ID, fake.id_of("study"))
    assert call(fake, BETA_ID, "GET", f"/data/{inner}").status_code == 200
    fake.land_deletes()
    assert ticker.now == Ticker().now  # the clock did not move
    for gone in (file_id, inner):
        assert absent(fake, ALPHA_ID, gone)
        assert absent(fake, BETA_ID, gone)
    assert names(fake, BETA_ID) == []

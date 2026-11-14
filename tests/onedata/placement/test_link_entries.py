"""O48: a study that checks the probe space (Day-0, a reset) tells the link check's own files from any other, and a
pair's current probe from its leftovers, by the two name shapes and the retirement markers alone, with no provider id
(`linkentries.link_check_entries`)."""

import dataclasses

import pytest

from expcore_onedata.linkentries import (
    CurrentProbe,
    LinkCheckEntries,
    ListedFile,
    is_link_check_entry,
    link_check_entries,
)
from expcore_onedata.linkprobe import PROBE_PREFIX, RETIRED_PREFIX, generation_of, probe_name, retired_name
from expcore_onedata.provision import Entry

KEY = "invented-key-one"
OTHER_KEY = "invented-key-two"
STEM = probe_name(KEY).removeprefix(PROBE_PREFIX)
OTHER_STEM = probe_name(OTHER_KEY).removeprefix(PROBE_PREFIX)


def probe(generation, file_id, key=KEY):
    return (probe_name(key, generation), file_id)


def marker(file_id, key=KEY):
    return (retired_name(key, file_id), f"marker-of-{file_id}")


def test_o48_generation_zero_alone_is_the_current_probe():
    """O48: a pair's only probe, generation 0, is its current one, and everything else is empty."""
    found = link_check_entries([probe(0, "file-a")])
    assert found.current == (CurrentProbe(STEM, probe_name(KEY), "file-a", 0),)
    assert found.own == (ListedFile(probe_name(KEY), "file-a"),)
    assert found.superseded == found.markers == found.other == ()


def test_o48_generation_three_supersedes_generation_zero():
    """O48: a higher generation is the current probe and the generation below it is superseded."""
    found = link_check_entries([probe(0, "file-a"), probe(3, "file-d")])
    assert [(c.name, c.file_id, c.generation) for c in found.current] == [(probe_name(KEY, 3), "file-d", 3)]
    assert found.superseded == (ListedFile(probe_name(KEY), "file-a"),)


def test_o48_the_listing_order_does_not_decide_the_current_probe():
    """O48: the current probe is the highest generation wherever it stands in the listing."""
    found = link_check_entries([probe(3, "file-d"), probe(0, "file-a"), probe(1, "file-b")])
    assert [c.generation for c in found.current] == [3]
    assert [f.file_id for f in found.superseded] == ["file-a", "file-b"]


def test_o48_a_retired_file_with_its_marker_leaves_the_next_generation_current():
    """O48: a probe whose file id a marker retires is superseded, and the next generation is current."""
    listing = [probe(0, "file-a"), marker("file-a"), probe(1, "file-b")]
    found = link_check_entries(listing)
    assert [(c.name, c.file_id) for c in found.current] == [(probe_name(KEY, 1), "file-b")]
    assert found.superseded == (ListedFile(probe_name(KEY), "file-a"),)
    assert found.markers == (ListedFile(*marker("file-a")),)


def test_o48_a_retired_highest_generation_leaves_the_highest_one_not_retired_current():
    """O48: the current probe is the highest generation among the files not retired, not the highest generation."""
    listing = [probe(0, "file-a"), probe(1, "file-b"), probe(2, "file-c"), marker("file-c")]
    found = link_check_entries(listing)
    assert [(c.generation, c.file_id) for c in found.current] == [(1, "file-b")]
    assert {f.file_id for f in found.superseded} == {"file-a", "file-c"}


def test_o48_a_pair_whose_every_probe_is_retired_has_no_current_probe():
    """O48: with every probe retired a pair has no current one, and each of them is superseded."""
    listing = [probe(0, "file-a"), probe(1, "file-b"), marker("file-a"), marker("file-b")]
    found = link_check_entries(listing)
    assert found.current == ()
    assert {f.file_id for f in found.superseded} == {"file-a", "file-b"}
    assert len(found.markers) == 2


def test_o48_a_marker_whose_file_is_gone_is_a_marker_and_retires_nothing_else():
    """O48: a marker whose file is no longer listed is a marker of the check's own and retires no other probe."""
    found = link_check_entries([probe(1, "file-b"), marker("file-gone")])
    assert [c.file_id for c in found.current] == ["file-b"]
    assert found.markers == (ListedFile(*marker("file-gone")),)
    assert found.superseded == found.other == ()


def test_o48_a_marker_of_another_pair_never_retires_this_pairs_probe():
    """O48: a marker is read by its own stem, so another pair's marker for the same file id retires nothing."""
    other_pairs_marker = (retired_name(OTHER_KEY, "file-a"), "marker-id")
    found = link_check_entries([probe(0, "file-a"), other_pairs_marker])
    assert [c.file_id for c in found.current] == ["file-a"]
    assert found.markers == (ListedFile(*other_pairs_marker),)


def test_o48_a_name_written_again_under_a_new_file_id_is_a_new_file_the_marker_does_not_retire():
    """O48: a file is retired by its id, so the same name under a new id is not retired by the old id's marker."""
    found = link_check_entries([probe(0, "file-new"), marker("file-old")])
    assert [(c.generation, c.file_id) for c in found.current] == [(0, "file-new")]


def test_o48_each_pair_has_its_own_current_probe():
    """O48: the stem groups a pair's probes, so each pair has its own current probe, listed in stem order."""
    listing = [probe(0, "file-a"), probe(2, "file-c", OTHER_KEY), probe(1, "file-b"), probe(0, "file-z", OTHER_KEY)]
    found = link_check_entries(listing)
    assert {c.stem: (c.generation, c.file_id) for c in found.current} == {
        STEM: (1, "file-b"),
        OTHER_STEM: (2, "file-c"),
    }
    assert [c.stem for c in found.current] == sorted([STEM, OTHER_STEM])
    assert {f.file_id for f in found.superseded} == {"file-a", "file-z"}


@pytest.mark.parametrize(
    "name",
    [
        PROBE_PREFIX + STEM[:15],  # 15 hex digits
        PROBE_PREFIX + STEM + "0",  # 17 hex digits
        PROBE_PREFIX + STEM.upper(),
        f"{probe_name(KEY)}-g0",  # generation 0 has no suffix
        f"{probe_name(KEY)}-g01",
        f"{probe_name(KEY)}-g{chr(0x661)}",  # an Arabic-Indic digit one
        f"{probe_name(KEY)}-g",
        f"{probe_name(KEY)}-G1",
        f"{probe_name(KEY)}-g1-g2",
        f"{probe_name(KEY)}\n",
        f"{probe_name(KEY)} ",
        f"{probe_name(KEY)}@suffix",
        f"x{probe_name(KEY)}",
        f"{probe_name(KEY)}.tmp",
        RETIRED_PREFIX + STEM,  # no file digest
        f"{RETIRED_PREFIX}{STEM}-{STEM[:15]}",
        f"{RETIRED_PREFIX}{STEM}-{STEM.upper()}",
        f"{RETIRED_PREFIX}{STEM}-{STEM}0",
        f"{retired_name(KEY, 'file-a')}-g1",
        f"{retired_name(KEY, 'file-a')}\n",
        "expcore-link-probe-",
        "expcore-link-probe",
        "expcore-link-retired-",
        "",
        "readme.txt",
    ],
)
def test_o48_a_near_miss_name_is_not_the_link_checks_own(name):
    """O48: a name that only resembles a probe or a marker is `other`, never `own`."""
    assert not is_link_check_entry(name)
    found = link_check_entries([(name, "file-x")])
    assert found.other == (ListedFile(name, "file-x"),)
    assert found.own == found.current == found.superseded == found.markers == ()


def test_o48_the_near_misses_are_not_all_the_same_digit_case():
    """O48: the upper-case near-misses differ from the real names, so they test the case and not an equal string."""
    assert STEM.upper() != STEM


@pytest.mark.parametrize(
    "name",
    [
        probe_name(KEY),
        probe_name(KEY, 1),
        probe_name(KEY, 7),
        probe_name(KEY, 123456789),
        probe_name(KEY, 10**9),
        probe_name(KEY, 10**40 + 7),
        retired_name(KEY, "file-a"),
    ],
)
def test_o48_both_name_shapes_the_link_check_writes_are_its_own(name):
    """O48: every name `probe_name` and `retired_name` make is the check's own."""
    assert is_link_check_entry(name)


@pytest.mark.parametrize("generation", [0, 1, 9, 10, 99, 123456789])
def test_o48_a_probe_name_is_read_with_the_generation_linkprobe_reads(generation):
    """O48: the generation read from a name is the one `linkprobe.generation_of` reads, so the two agree."""
    name = probe_name(KEY, generation)
    assert generation_of(name, KEY) == generation
    found = link_check_entries([(name, "file-a")])
    assert [c.generation for c in found.current] == [generation]


def test_o48_a_generation_past_the_nine_digits_the_check_reads_is_its_own_and_never_current():
    """O48: `probe_name` has no upper bound, so the name `retire` writes after generation 999999999 is the check's own,
    but `generation_of` reads no more than nine digits and `provision` never selects it, so it is not current."""
    last = probe_name(KEY, 999_999_999)
    successor = probe_name(KEY, 1_000_000_000)
    assert generation_of(successor, KEY) is None
    assert is_link_check_entry(successor)
    found = link_check_entries([(last, "file-a"), (successor, "file-b")])
    assert [(c.generation, c.file_id) for c in found.current] == [(999_999_999, "file-a")]
    assert found.superseded == (ListedFile(successor, "file-b"),)
    assert found.other == ()


def test_o48_a_generation_of_thousands_of_digits_is_classified_without_making_an_integer_of_it():
    """O48: a name with a generation past Python's integer conversion limit has the check's shape and raises nothing."""
    name = f"{probe_name(KEY)}-g{'1' * 5000}"
    assert is_link_check_entry(name)
    found = link_check_entries([(name, "file-a"), probe(2, "file-b")])
    assert [c.file_id for c in found.current] == ["file-b"]
    assert found.superseded == (ListedFile(name, "file-a"),)


def test_o48_an_empty_listing_has_nothing():
    """O48: an empty listing has no entry of any kind."""
    found = link_check_entries([])
    assert found == LinkCheckEntries(own=(), current=(), superseded=(), markers=(), other=())


def test_o48_own_and_other_partition_the_listing_in_its_order():
    """O48: `own` and `other` split the listing in its order, and `own` is the current probes, the superseded and the
    markers together."""
    listing = [
        ("notes.txt", "file-n"),
        probe(1, "file-b"),
        marker("file-a"),
        probe(0, "file-a"),
        (probe_name(KEY) + "@suffix", "file-s"),
        probe(0, "file-o", OTHER_KEY),
    ]
    found = link_check_entries(listing)
    assert found.own == tuple(ListedFile(*item) for item in (listing[1], listing[2], listing[3], listing[5]))
    assert found.other == tuple(ListedFile(*item) for item in (listing[0], listing[4]))
    current = {(c.name, c.file_id) for c in found.current}
    superseded = {tuple(f) for f in found.superseded}
    assert {tuple(f) for f in found.own} == current | superseded | {tuple(f) for f in found.markers}
    assert not current & superseded


def test_o48_the_entries_of_a_provisioners_listing_are_read_as_they_come():
    """O48: a list of `Entry`, the dict `Provisioner.children` returns, and `(name, file_id)` pairs give one result."""
    listing = {
        probe_name(KEY): Entry("file-a", probe_name(KEY), 65536, "REG"),
        probe_name(KEY, 1): Entry("file-b", probe_name(KEY, 1), 65536, "REG"),
        "notes.txt": Entry("file-n", "notes.txt", 3, "REG"),
    }
    from_pairs = link_check_entries([probe(0, "file-a"), probe(1, "file-b"), ("notes.txt", "file-n")])
    assert link_check_entries(listing) == from_pairs
    assert link_check_entries(list(listing.values())) == from_pairs
    assert link_check_entries(iter(listing.values())) == from_pairs


def test_o48_an_item_that_is_not_a_name_and_a_file_id_is_refused_naming_it():
    """O48: an item that is neither an `Entry` nor a pair of strings is a TypeError that names it."""
    with pytest.raises(TypeError, match="not-a-pair"):
        link_check_entries([probe(0, "file-a"), "not-a-pair"])
    with pytest.raises(TypeError, match="three"):
        link_check_entries([("a", "b", "three")])
    with pytest.raises(TypeError):
        link_check_entries([(probe_name(KEY), 7)])


def test_o48_an_entry_with_no_file_id_is_refused_naming_it():
    """O48: an entry with an empty file id is a ValueError that names it, never an entry of the check's own."""
    with pytest.raises(ValueError, match=probe_name(KEY)):
        link_check_entries([(probe_name(KEY), "")])


def test_o48_a_name_listed_with_two_file_ids_is_refused():
    """O48: a directory lists a name once, so a name given two file ids is a ValueError that names it."""
    with pytest.raises(ValueError, match=probe_name(KEY)):
        link_check_entries([probe(0, "file-a"), probe(0, "file-b")])


def test_o48_a_pair_listed_twice_counts_once():
    """O48: the same name and file id given twice is one entry."""
    assert link_check_entries([probe(0, "file-a"), probe(0, "file-a")]) == link_check_entries([probe(0, "file-a")])


def test_o48_the_result_cannot_be_reassigned():
    """O48: the result is a frozen dataclass."""
    found = link_check_entries([probe(0, "file-a")])
    with pytest.raises(dataclasses.FrozenInstanceError):
        found.own = ()  # type: ignore[misc]


def test_o48_the_result_is_hashable_and_compares_by_value():
    """O48: the result holds only tuples, so it can be hashed and compared, and a study can keep it in a record."""
    assert hash(link_check_entries([probe(0, "file-a")])) == hash(link_check_entries([probe(0, "file-a")]))

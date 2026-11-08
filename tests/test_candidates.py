"""Normalising, hashing and candidate extraction. Tokens here are all invented."""

import hashlib
import re

from expcore.provenance import candidates as cd


def test_normalise_applies_nfkc_casefold_and_strip():
    assert cd.normalise("  \uff33ecret-SPACE\n") == "secret-space"


def test_token_hash_is_sha256_of_the_versioned_prefix_and_the_normalised_token():
    expected = hashlib.sha256(b"expcore-study-token-v1:secret-space").hexdigest()
    assert cd.token_hash("secret-space") == expected
    assert cd.token_hash("  Secret-SPACE ") == expected


# --- candidates -----------------------------------------------------------------------------------------------------


def test_candidates_of_a_url_with_host_port_and_path():
    found = set(cd.candidates("https://a.b.example.org:8443/x/study-space/in"))
    assert {"a.b.example.org", "b.example.org", "example.org", "a.b", "example"} <= found
    assert {"study-space", "study", "space"} <= found
    assert {"https", "8443", "x", "in"} <= found
    assert "example.org:8443" not in found
    assert "study-space/in" not in found


def test_candidates_of_a_composed_identifier():
    found = set(cd.candidates("api_host_name"))
    assert {"api_host_name", "api_host", "host_name", "api", "host", "name"} <= found
    assert {"host_", "_host", "_host_"} <= found  # an edge separator kept where the run had one
    assert "api_name" not in found


def test_candidates_keep_the_separators_they_had():
    found = set(cd.candidates("a-b_c.d"))
    assert {"a-b_c.d", "a-b_c", "b_c.d", "a-b", "b_c", "c.d"} <= found
    assert "a_b" not in found


def test_candidates_of_a_path_assignment_and_query():
    found = set(cd.candidates("/data/secret-space/in?token=fake-zone.example.net&x=1"))
    assert {"secret-space", "fake-zone.example.net", "token", "data"} <= found


def test_candidates_include_the_raw_run_when_it_starts_or_ends_with_a_separator():
    found = set(cd.candidates("__init__ and _private_ ."))
    assert {"init", "__init__", "private", "_private_"} <= found


def test_candidates_use_nfkc_so_fullwidth_letters_do_not_hide_a_token():
    assert "secret-space" in set(cd.candidates("\uff53ecret\uff0dspace"))


def test_candidates_keep_an_edge_separator_the_run_had_at_that_position():
    found = set(cd.candidates("prefix._private_.suffix"))
    assert {"_private_", "_private", "private_", "private", "._private_.", "._private", "private_."} <= found
    assert "-private-" not in found
    assert "__private__" not in found  # only the separator characters actually present


def test_edge_separators_are_limited_to_two_characters():
    found = set(cd.candidates("a-----b"))
    assert "-b" in found
    assert "--b" in found
    assert "---b" not in found


def test_candidate_windows_hold_up_to_twenty_four_components():
    assert cd.MAX_COMPONENTS == 24
    run = ".".join(["w"] * 40)
    widths = {len(re.findall("w", c)) for c in cd.candidates(run)}
    assert max(widths) == 24
    assert 24 in widths
    assert not any(len(c) > cd.MAX_CANDIDATE_CHARS for c in cd.candidates("a." * 400))


def test_a_token_after_a_very_long_run_is_still_found():
    assert "secret-space" in set(cd.candidates("x." * 300 + "secret-space"))


def test_is_matchable_is_false_for_tokens_the_candidate_alphabet_cannot_express():
    assert cd.is_matchable("secret-space")
    assert cd.is_matchable("_private_")
    assert not cd.is_matchable("10.99.0.0/16")
    assert not cd.is_matchable("two words")


def test_unicode_windows_do_not_cut_a_word_at_an_accented_letter():
    run = "\u00e9secret_space"
    ascii_windows = {run[a:b] for a, b in cd.iter_windows(run)}
    unicode_windows = {run[a:b] for a, b in cd.iter_windows(run, unicode_words=True)}
    assert "secret_space" in ascii_windows
    assert "secret_space" not in unicode_windows
    assert "\u00e9secret_space" in unicode_windows

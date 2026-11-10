"""The one redactor every package and provenance message goes through: nothing that holds a private name or a listed
term is shown, a hidden label keeps one name everywhere, and the mapping is only what the author asks to see."""

import hashlib

import pytest

from expcore import cli
from expcore.provenance import history, sanitise, study_tokens
from expcore.provenance.hashfile import hashes_from_tokens, render_hash_file
from expcore.provenance.redact import WITHHELD, Redactor, hidden_name

PRIVATE = "zarnothwick"  # invented; a private name in these tests
TERM = "Quillon"  # invented; a term of an anonymisation list


def redactor() -> Redactor:
    return Redactor(hashes_from_tokens([PRIVATE]), lambda text: TERM.casefold() in text.casefold())


def test_k05_a_label_that_holds_a_private_name_or_a_term_is_named_by_its_hash_and_remembered():
    """K05: a label holding a private name or a term is shown as the start of its SHA-256, and the redactor remembers
    which label each name stands for; a clean label is shown as it is."""
    seen = redactor()
    path, other = f"notes/{PRIVATE}/a.txt", f"notes/{TERM.lower()}/b.txt"
    assert seen.name(path) == f"<hidden {hashlib.sha256(path.encode()).hexdigest()[:12]}>" == hidden_name(path)
    assert seen.name(other) == hidden_name(other)
    assert seen.name("notes/clean.txt") == "notes/clean.txt"
    assert seen.hidden == {hidden_name(path): path, hidden_name(other): other}


def test_k05_scrub_names_every_word_that_must_not_be_shown_and_withholds_a_line_it_cannot_mend():
    """K05: the last guard on a message: a label already named, or any word holding a private name or a term, is
    replaced by its name, the rest of the message kept; a line that would still show one is withheld whole."""
    seen = redactor()
    path = f"data/{PRIVATE}.csv"
    message = f"refusing:\n  {path}:3: something\n  see {TERM}'s copy\n  plain line"
    scrubbed = seen.scrub(message)
    assert scrubbed.splitlines() == [
        "refusing:",
        f"  {hidden_name(path)}:3: something",
        f"  see {hidden_name(TERM)}'s copy",
        "  plain line",
    ]
    two_words = Redactor(also=lambda text: "quillon varek" in text.casefold())  # a term no single word holds
    assert two_words.scrub("written by Quillon Varek\nplain") == f"{WITHHELD}\nplain"


def test_k05_a_listed_name_of_several_words_is_hidden_as_a_phrase_among_other_words():
    """K05: the scanner reads one run of letters, digits and `_ . -` at a time, so a listed name with a space in it
    matched only an entire line; the redactor finds it among other words, whatever white space it is written with,
    in a message and in a label."""
    phrase = "Ada Example"  # invented; a listed name of two words
    seen = Redactor(hashes_from_tokens([phrase, PRIVATE]))
    scrubbed = seen.scrub(f"written by {phrase}, and by ada  example too\nplain line")
    assert "example" not in scrubbed.casefold()
    assert scrubbed.splitlines()[1] == "plain line"
    assert f"written by {hidden_name(phrase)}, and by" in scrubbed
    label = f"notes/by {phrase}.txt"
    assert seen.name(label) == hidden_name(label)


def test_k05_a_short_name_matched_only_whole_is_never_replaced_inside_a_longer_word():
    """K05: a name shorter than four characters is matched only as an entire value; once it has been named, a later
    message replaces it where it stands on its own, never inside a longer word, exactly as the scanner matches it."""
    short = "vyk"  # invented; a listed name too short to be matched inside other names
    seen = Redactor(hashes_from_tokens([short]))
    assert seen.name(short) == hidden_name(short)
    scrubbed = seen.scrub(f"the canvyk file, {short}-extra and {short} itself, then data/{short}:3 and {short}.")
    shown = hidden_name(short)
    assert scrubbed == f"the canvyk file, {short}-extra and {shown} itself, then data/{shown}:3 and {shown}."


def test_k05_a_name_already_handed_out_is_replaced_wherever_a_message_repeats_its_label():
    """K05: once a label is named, every later message that spells it, whatever surrounds it, shows the name."""
    seen = redactor()
    path = f"{PRIVATE}-home/study/x.txt"
    shown = seen.name(path)
    assert seen.scrub(f"the file '{path}' (untracked)") == f"the file '{shown}' (untracked)"


@pytest.mark.parametrize(
    ("main", "argv"),
    [
        (history.main, ["--map", "map.toml", f"--{PRIVATE}-home"]),
        (history.main, ["--map", "map.toml", f"/data/{PRIVATE}/extra"]),
        (sanitise.main, [PRIVATE]),
        (sanitise.main, ["verify", "--map", "m", "--source", "s", "--module", "d", f"--{PRIVATE}=1"]),
        (study_tokens.main, [f"{PRIVATE}-check"]),
        (study_tokens.main, ["check-commits", "HEAD", f"--{PRIVATE}"]),
    ],
)
def test_p07_a_command_line_a_provenance_command_does_not_take_is_refused_without_repeating_it(main, argv, capsys):
    """P07: argparse parses before any list is loaded and names an unrecognised argument or a refused value in its
    error; the provenance commands print their usage and a fixed sentence instead, and exit with 2."""
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert "what was typed is not repeated" in err
    assert PRIVATE not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["package", "study", "--out", "dist", f"--{PRIVATE}"],
        ["package-check", "package", f"{PRIVATE}/notes.txt"],
        [PRIVATE],
    ],
)
def test_k05_a_command_line_the_package_commands_do_not_take_is_refused_without_repeating_it(argv, capsys):
    """K05: the package commands' parser never repeats an argument it refuses, which could be a private name typed in
    the wrong place; it prints the usage and a fixed sentence, and the command exits with the usage status."""
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert PRIVATE not in err


def raising(stop: BaseException):
    def raise_it(*_args, **_kwargs):
        raise stop

    return raise_it


def stop_history(monkeypatch, stop, where, hashes, mapped) -> int:
    monkeypatch.setattr(history, "_modules", raising(stop))
    return history.main(["--map", str(mapped), "--source-repos", "origin=/nowhere", "--repo", str(where)])


def stop_sanitise(monkeypatch, stop, where, hashes, mapped) -> int:
    monkeypatch.setattr(sanitise, "import_module", raising(stop))
    blob = where / "blob.py"
    blob.write_text("x = 1\n", encoding="utf-8")
    argv = ["import", "--map", str(mapped), "--source-repo", "origin", "--commit", "0123abcd", "--path", "p.py"]
    return sanitise.main([*argv, str(blob), str(where / "out.py")])


def stop_package_check(monkeypatch, stop, where, hashes, mapped) -> int:
    monkeypatch.setattr("expcore.cli.default_hashes", lambda: hashes)  # stands in for the core's public list
    monkeypatch.setattr("expcore.cli.verify_package", raising(stop))
    return cli.main(["package-check", str(where)])


def stop_package(monkeypatch, stop, where, hashes, mapped) -> int:
    monkeypatch.setattr("expcore.package.build.load_study", raising(stop))  # as a study's plugin would, loading
    return cli.main(["package", str(where), "--out", str(where / "dist"), "--hashes", str(hashes)])


@pytest.mark.parametrize("command", [stop_history, stop_sanitise, stop_package_check, stop_package])
def test_k05_every_guarded_command_ends_an_interrupt_or_an_exit_with_a_message_the_same_way(
    command, tmp_path, monkeypatch, capsys
):
    """K05: the P07 command, the import and both package commands share the study-token command's handling of
    Ctrl-C and of a SystemExit that carries a message (`provenance.redact.stopped`): `interrupted` and 130, or the
    message through the redactor and 1; never a traceback or the raw text."""
    hashes = tmp_path / "list.sha256"
    hashes.write_text(render_hash_file([PRIVATE]), encoding="utf-8")
    mapped = tmp_path / "map.toml"
    entry = f"[[entry]]\ntoken = '{PRIVATE}'\nreplacement = 'place-a'\ncategory = 'hosts'\n"
    mapped.write_text(f"version = 1\n{entry}", encoding="utf-8")
    assert command(monkeypatch, KeyboardInterrupt(), tmp_path, hashes, mapped) == 130
    assert capsys.readouterr().err.endswith("interrupted\n")
    plugin_exit = SystemExit(f"a plugin stopped at /data/{PRIVATE}/run.py")
    assert command(monkeypatch, plugin_exit, tmp_path, hashes, mapped) == 1
    err = capsys.readouterr().err
    assert "a plugin stopped at <hidden " in err
    assert PRIVATE not in err
    assert "Traceback" not in err

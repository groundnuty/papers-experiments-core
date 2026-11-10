# Provenance: new
"""One rule for every message the package's checks and the provenance commands print: nothing that holds a private
name or a listed term is ever shown, whether it is a path, a label, a name or a value.

A `Redactor` holds what must not be shown: study-token hashes, and any other test of a text (the terms of an
anonymisation list, say). `name(label)` gives a label that holds one of them as `<hidden 1a2b3c4d5e6f>`, the start of
the SHA-256 of the label, so that every check names the same label the same way and two labels never fold into one;
`scrub(text)` is the last guard on a whole message: every label already named is replaced by its name where it stands
on its own (never inside a longer word: a short name matched only whole stays whole), then every listed name of
several words, matched as a phrase, then every word that holds a private name or a term, and a line that would still
show one is withheld whole. Each name it hands out is remembered with the label it stands for (`hidden`), for an
author who asks to see them on their own terminal (`python -m expcore package --show-private-paths`); nothing else
ever prints that mapping.
"""

import argparse
import hashlib
import re
import sys
from collections.abc import Callable, Iterable
from typing import NoReturn

from expcore.provenance.candidates import MAX_CANDIDATE_CHARS, token_hash
from expcore.provenance.hashfile import Hashes
from expcore.provenance.scanner import scan_source

HIDDEN_PREFIX_CHARS = 12
NO_HASHES = Hashes({}, {})
WITHHELD = "(a line withheld: it would show a private name or a listed term)"
MAX_PHRASE_WORDS = 12  # the most words (letters and digits, between any separators) a listed phrase is looked for in
_WORD = re.compile(r"[^\s\"'`,;()\[\]{}<>=]+")  # a path, a name or a value as a message writes it
_TAIL = re.compile(r"(?::\d+)*[:.]?$")  # a line number and the punctuation a message puts after a path
_UNIT = re.compile(r"[^\W_]+")  # one word of a phrase: a run of letters and digits
_RUN = re.compile(r"[\w.\-]")  # a character of the runs the scanner reads a value from


def _standing_alone(label: str) -> re.Pattern[str]:
    """A label wherever it is not glued to a longer value: not after a letter, digit, `_`, `-` or `.`, and not before
    a letter, digit, `_` or `-`, nor a `.` that goes on (`a.txt`). A sentence's full stop or a `:12` may follow."""
    before = r"(?<![\w.\-])" if _RUN.fullmatch(label[0]) else ""
    after = r"(?![\w\-])(?!\.\w)" if _RUN.fullmatch(label[-1]) else ""
    return re.compile(before + re.escape(label) + after)


class QuietParser(argparse.ArgumentParser):
    """A command-line parser whose errors never repeat what was typed.

    argparse names an unrecognised argument, or a value it refuses, in its error, and it parses before any list is
    loaded that could hide it: a private name typed in the wrong place, a path or a map entry pasted as an argument,
    would be printed as it is. This parser prints its usage line and one fixed sentence instead, and exits with 2 as
    argparse does. Its subcommands are parsers of the same kind.
    """

    def error(self, message: str) -> NoReturn:
        """Print the usage and a fixed sentence; `message`, which may quote the command line, is never shown."""
        del message
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: the command line is not one this command takes (an unknown argument, a missing or "
            "an extra one, or a value it does not accept); what was typed is not repeated, see --help\n",
        )


NOT_SHOWN = "its message is not shown: no list is loaded yet to check it against"


def failure(
    error: BaseException,
    redactor: "Redactor | None",
    *,
    expected: tuple[type[BaseException], ...] = (),
    fixed: tuple[type[BaseException], ...] = (),
) -> str:
    """How a command shows the exception that ended it: the last guard at its entry point, which catches every one.

    The text of one of the command's own errors (`expected`) is shown as it is, and any other's after its kind; either
    way through `redactor`, which knows the lists the command loaded. Before they are loaded there is nothing to check
    a message against, so only an error the command builds from fixed words (`fixed`: no path, no value) is shown, and
    any other by its kind alone.
    """
    text = str(error) if isinstance(error, expected) else f"{type(error).__name__}: {error}"
    if redactor is not None:
        return redactor.scrub(text)
    if isinstance(error, fixed):
        return text
    return f"{type(error).__name__} ({NOT_SHOWN})"


def hidden_name(label: str) -> str:
    """How a message names a label it must not show: the start of the label's SHA-256."""
    return f"<hidden {hashlib.sha256(label.encode('utf-8')).hexdigest()[:HIDDEN_PREFIX_CHARS]}>"


class Redactor:
    """Decides what a message may show, and remembers each hidden label by its name.

    `hidden` grows as names are handed out, on purpose: it is the mapping an author may ask to see. `also` may be set
    once more is known (the terms of an anonymisation list load after the hashes).
    """

    def __init__(self, hashes: Hashes = NO_HASHES, also: Callable[[str], bool] | None = None) -> None:
        self.hashes = hashes
        self.also = also
        self.hidden: dict[str, str] = {}

    def hides(self, text: str) -> bool:
        """Whether a text holds a private name, as a path, as words or as a phrase, or anything `also` refuses to
        show."""
        if text and (scan_source(text, text, self.hashes, exempt_header=False, python=False) or self._phrases(text)):
            return True
        return bool(self.also is not None and self.also(text))

    def _phrases(self, text: str) -> list[tuple[int, int]]:
        """Where a text holds a listed name of several words, as (start, end), longest first and never overlapping.

        The scanner reads a value from one run of letters, digits and `_ . -`, so a name with a space in it matches
        only an entire line or literal; inside a message it stands among other words. Every span of words with white
        space in it is compared, as it is and with each white-space run read as one space, with the listed hashes.
        """
        words = [match.span() for match in _UNIT.finditer(text)]
        found = []
        for index, (start, _) in enumerate(words):
            for _, end in words[index + 1 : index + MAX_PHRASE_WORDS]:
                if end - start > MAX_CANDIDATE_CHARS:
                    break
                value = text[start:end]
                if any(char.isspace() for char in value) and self._listed(value):
                    found.append((start, end))
        chosen: list[tuple[int, int]] = []
        for start, end in sorted(found, key=lambda span: (span[0] - span[1], span[0])):
            if all(end <= taken_start or start >= taken_end for taken_start, taken_end in chosen):
                chosen.append((start, end))
        return sorted(chosen)

    def _listed(self, value: str) -> bool:
        for form in {value, " ".join(value.split())}:
            digest = token_hash(form)
            if digest in self.hashes.anywhere or digest in self.hashes.whole:
                return True
        return False

    def name(self, label: str, *, hide: bool = False) -> str:
        """The label itself, or its hidden name when it holds what must not be shown (or `hide` says so)."""
        if not (hide or self.hides(label)):
            return label
        shown = hidden_name(label)
        self.hidden[shown] = label
        return shown

    def scrub(self, text: str, labels: Iterable[str] = ()) -> str:
        """A message with every hidden label where it stands on its own, every listed phrase, and every word that must
        not be shown, replaced by its name; a line that would still show something is withheld whole."""
        known = {label for label in (*self.hidden.values(), *labels) if label}
        ordered = sorted(known, key=len, reverse=True)  # a longer label first, so a shorter one inside it stays whole
        return "\n".join(self._line(line, ordered) for line in text.split("\n"))

    def _line(self, line: str, labels: list[str]) -> str:
        hidden = set(self.hidden.values())
        for label in labels:
            if label in line and (label in hidden or self.hides(label)):
                line = _standing_alone(label).sub(lambda _, label=label: self.name(label, hide=True), line)
        for start, end in reversed(self._phrases(line)):
            line = line[:start] + self.name(line[start:end], hide=True) + line[end:]
        line = _WORD.sub(self._word, line)
        return WITHHELD if self.hides(line) else line

    def _word(self, match: re.Match[str]) -> str:
        word = match.group(0)
        if word.startswith("hidden"):
            return word
        core = _TAIL.sub("", word)  # a path as a message writes it, `a/b.txt:12:` or at the end of a sentence
        if core and core != word and self.hides(core):
            return self.name(core, hide=True) + word[len(core) :]
        return self.name(word, hide=True) if self.hides(word) else word

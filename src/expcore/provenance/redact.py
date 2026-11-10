# Provenance: new
"""One rule for every message the package's checks and the provenance commands print: nothing that holds a private
name or a listed term is ever shown, whether it is a path, a label, a name or a value.

A `Redactor` holds what must not be shown: study-token hashes, and any other test of a text (the terms of an
anonymisation list, say). `name(label)` gives a label that holds one of them as `<hidden 1a2b3c4d5e6f>`, the start of
the SHA-256 of the label, so that every check names the same label the same way and two labels never fold into one;
`scrub(text)` is the last guard on a whole message: every label already named is replaced by its name, then every
word that holds a private name or a term, and a line that would still show one is withheld whole. Each name it hands
out is remembered with the label it stands for (`hidden`), for an author who asks to see them on their own terminal
(`python -m expcore package --show-private-paths`); nothing else ever prints that mapping.
"""

import hashlib
import re
from collections.abc import Callable, Iterable

from expcore.provenance.hashfile import Hashes
from expcore.provenance.scanner import scan_source

HIDDEN_PREFIX_CHARS = 12
NO_HASHES = Hashes({}, {})
WITHHELD = "(a line withheld: it would show a private name or a listed term)"
_WORD = re.compile(r"[^\s\"'`,;()\[\]{}<>=]+")  # a path, a name or a value as a message writes it
_TAIL = re.compile(r"(?::\d+)*[:.]?$")  # a line number and the punctuation a message puts after a path


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
        """Whether a text holds a private name, as a path or as words, or anything `also` refuses to show."""
        if text and scan_source(text, text, self.hashes, exempt_header=False, python=False):
            return True
        return bool(self.also is not None and self.also(text))

    def name(self, label: str, *, hide: bool = False) -> str:
        """The label itself, or its hidden name when it holds what must not be shown (or `hide` says so)."""
        if not (hide or self.hides(label)):
            return label
        shown = hidden_name(label)
        self.hidden[shown] = label
        return shown

    def scrub(self, text: str, labels: Iterable[str] = ()) -> str:
        """A message with every hidden label, and every word that must not be shown, replaced by its name; a line that
        would still show something is withheld whole."""
        known = {label for label in (*self.hidden.values(), *labels) if label}
        ordered = sorted(known, key=len, reverse=True)  # a longer label first, so a shorter one inside it stays whole
        return "\n".join(self._line(line, ordered) for line in text.split("\n"))

    def _line(self, line: str, labels: list[str]) -> str:
        for label in labels:
            if label in line and (label in self.hidden.values() or self.hides(label)):
                line = line.replace(label, self.name(label, hide=True))
        line = _WORD.sub(self._word, line)
        return WITHHELD if self.hides(line) else line

    def _word(self, match: re.Match[str]) -> str:
        word = match.group(0)
        if word.startswith("hidden") or not self.hides(word):
            return word
        core = _TAIL.sub("", word)
        if core and core != word and self.hides(core):
            return self.name(core, hide=True) + word[len(core) :]
        return self.name(word, hide=True)

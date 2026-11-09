# Provenance: new
"""Replication packages: what a study ships, how the build checks it, and how a reader verifies it.

- `spec`: the inclusion list in the study's `package.toml`; anything it does not list is left out.
- `anonymise`: anonymisation lists for double-blind submissions, applied only to the prose and metadata files named.
- `archive`: the SHA-256 checksum manifest and the deterministic archive.
- `build`: `python -m expcore package` builds a package, and `python -m expcore package-check`, which the package's own
  `make check` runs, verifies one.
- `views`: what the package's checks read in a file, and which files they refuse.

Threat model. The study-token and anonymisation checks catch accidental leaks: a private name or a listed term left in
text, in a common encoding of text (UTF-16, escapes, entities, percent-escapes, base64 and hex runs, `data:` URIs),
in compressed or archived content, or in an SQLite database. They do not defend against deliberate concealment by
someone who means to hide a name from them, and they never read text inside images. A file they cannot read in full is
refused unless the author attests it by hash, and then it rests on the author's word.
"""

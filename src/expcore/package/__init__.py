# Provenance: new
"""Replication packages: what a study ships, how the build checks it, and how a reader verifies it.

- `spec`: the inclusion list in the study's `package.toml`; anything it does not list is left out.
- `anonymise`: anonymisation lists for double-blind submissions, applied only to the prose and metadata files named.
- `archive`: the SHA-256 checksum manifest and the deterministic archive.
- `build`: `python -m expcore package` builds a package, and `python -m expcore package-check`, which the package's own
  `make check` runs, verifies one.
"""

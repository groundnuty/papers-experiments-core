"""The in-process RDF store: a graph loaded once from a file, read through SPARQL queries only.

The store is pyoxigraph's in-memory `Store`. Nothing in this platform calls its update API, and `Store.query` parses
queries only (an update is a syntax error there), so the agent's tools cannot change the graph; the instrument checks
that at every run's end.
"""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyoxigraph

FORMATS = {".ttl": pyoxigraph.RdfFormat.TURTLE, ".nt": pyoxigraph.RdfFormat.N_TRIPLES}


class GraphStore:
    """One graph in an in-memory store. Build it with `load`."""

    def __init__(self, store: pyoxigraph.Store) -> None:
        self._store = store

    @classmethod
    def load(cls, path: Path) -> "GraphStore":
        """Load a Turtle (.ttl) or N-Triples (.nt) file into a new store.

        Blank nodes are refused: the store renames them on every load, so the snapshot of the same file would differ
        from one process to the next. Name every node with an IRI.
        """
        path = Path(path)
        rdf_format = FORMATS.get(path.suffix)
        if rdf_format is None:
            raise ValueError(f"{path.name}: not a graph file this platform reads (one of {sorted(FORMATS)})")
        store = pyoxigraph.Store()
        try:
            store.load(path.read_bytes(), format=rdf_format)
        except SyntaxError as error:
            raise ValueError(f"{path.name}: not valid {path.suffix} ({error})") from None
        blank = sum(1 for quad in store if _has_blank_node(quad))
        if blank:
            raise ValueError(f"{path.name}: {blank} triples hold a blank node; name every node with an IRI")
        if not len(store):
            raise ValueError(f"{path.name}: the graph is empty")
        return cls(store)

    def query(self, text: str) -> Any:
        """Run one SPARQL query. Raises SyntaxError for text that is not a query, OSError if the store fails."""
        return self._store.query(text)

    def triples(self) -> int:
        """How many triples the store holds."""
        return len(self._store)

    def snapshot(self) -> Mapping[str, Any]:
        """The store's state: its triple count and the SHA-256 of its N-Quads dump with the lines sorted."""
        dump = self._store.dump(format=pyoxigraph.RdfFormat.N_QUADS)
        lines = sorted(dump.splitlines(keepends=True))
        return {"triples": len(lines), "sha256": hashlib.sha256(b"".join(lines)).hexdigest()}


def _has_blank_node(quad: pyoxigraph.Quad) -> bool:
    return any(isinstance(term, pyoxigraph.BlankNode) for term in (quad.subject, quad.object))

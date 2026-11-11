"""The platform a study builds once: the store, the per-run session (tools and instrument), the health check.

    platform = SparqlPlatform(Path("graph.ttl"), max_rows=8)
    session = platform.session()                 # one per run
    providers = [session.provider(schema=True)]  # the run's ToolProvider
    instruments = [session.instrument()]         # the run's Instrument

The store is loaded once and shared by every run, read-only; each run gets its own log of what the platform served,
which the run's instrument reads.
"""

from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from expcore.campaign.healthgate import Health
from expcore.errors import InfraError
from expcore.plugin import ToolSpec
from expcore.tools.function_provider import FunctionProvider
from sparql_platform.kinds import STORE_KIND
from sparql_platform.store import GraphStore
from sparql_platform.tools import QueryLog, SparqlTools, tool_specs

DISTRIBUTION = "sparql-platform"
NAMESPACE = "rdf_store"


def platform_info() -> dict[str, str]:
    """The platform's version and its store's, for the instrument's snapshot: an editable checkout's version says
    little, so a record names both."""
    return {"distribution": DISTRIBUTION, "version": metadata.version(DISTRIBUTION), "store": _store_version()}


def _store_version() -> str:
    return f"pyoxigraph {metadata.version('pyoxigraph')}"


class StoreSnapshot:
    """The instrument: the store's state at the run's start and end, and what the platform served in between.

    `finish` returns `{"ok", "platform", "start", "end", "unchanged", "served"}`. A reading that failed is None with
    its error class beside it, and `unchanged` is then None too, never a guess.
    """

    namespace = NAMESPACE

    def __init__(self, store: GraphStore, log: QueryLog) -> None:
        self.store, self.log = store, log
        self.before: Mapping[str, Any] | None = None

    def start(self, identity: Mapping[str, Any]) -> None:
        try:
            self.before = self.store.snapshot()
        except OSError as error:  # raised from start, so the attempt is infrastructure of this kind, with no record
            raise InfraError(STORE_KIND, f"the store could not be read ({type(error).__name__})") from error

    def finish(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"platform": platform_info(), "start": self.before, "served": self.log.as_dict()}
        try:
            after = self.store.snapshot()
        except OSError as error:
            return {**snapshot, "ok": False, "end": None, "error": type(error).__name__, "unchanged": None}
        return {**snapshot, "ok": True, "end": after, "unchanged": after == self.before}


class RunSession:
    """One run's view of the platform: its own log, the tools that count into it and the instrument that reads it."""

    def __init__(self, store: GraphStore, max_rows: int) -> None:
        self.log = QueryLog()
        self.tools = SparqlTools(store, self.log, max_rows=max_rows)
        self.store = store

    def provider(self, *, schema: bool) -> FunctionProvider:
        """The run's ToolProvider: the query tool, and the schema tool when `schema` is true."""
        return FunctionProvider(self.tools.functions(schema=schema))

    def instrument(self) -> StoreSnapshot:
        return StoreSnapshot(self.store, self.log)


class StoreHealth:
    """The health check: the store answers a query and holds as many triples as when the platform was built."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self.expected = store.triples()

    def __call__(self) -> Health:
        try:
            answered = bool(self.store.query("ASK { ?s ?p ?o }"))
            triples = self.store.triples()
        except OSError as error:
            return Health(False, f"{STORE_KIND}: the store could not be read ({type(error).__name__})")
        if not answered or triples != self.expected:
            return Health(False, f"{STORE_KIND}: the store holds {triples} triples, not {self.expected}")
        return Health(True, f"the store answers and holds its {triples} triples")


class SparqlPlatform:
    """The platform: a graph file loaded into an in-process store, with tools capped at `max_rows` rows a result."""

    def __init__(self, graph: Path, *, max_rows: int = 8) -> None:
        self.store = GraphStore.load(graph)
        self.max_rows = max_rows
        self.loaded = self.store.snapshot()

    @staticmethod
    def tool_specs(*, schema: bool) -> list[ToolSpec]:
        """What a run's provider shows the model, for the condition's tool-manifest hash."""
        return tool_specs(schema=schema)

    def session(self) -> RunSession:
        return RunSession(self.store, self.max_rows)

    def health(self) -> StoreHealth:
        return StoreHealth(self.store)

    def fingerprint(self) -> dict[str, Any]:
        """The settings that make one experiment, for `StudyPlugin.platform`: a resume under another cap or another
        graph is refused."""
        return {"platform": DISTRIBUTION, "max_rows": self.max_rows, "graph": dict(self.loaded)}

"""The read-only query tools and the per-run log the platform keeps of what they served.

Two tools: `sparql_query` runs one SPARQL query (SELECT, ASK, CONSTRUCT or DESCRIBE) and `list_predicates` lists the
predicates the graph uses with their counts. Both return at most `max_rows` rows and say when they cut a result
(`source_truncated`), so the cut is the platform's, recorded as such, before the core's own cap on what the model sees.

What the model should read, a refused or malformed query included, is a `ToolOutput`. Only a failure of the store
itself raises, as `InfraError` of the platform's kind.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyoxigraph

from expcore.errors import InfraError
from expcore.plugin import ToolOutput, ToolSpec
from sparql_platform.kinds import STORE_KIND
from sparql_platform.store import GraphStore

QUERY_TOOL = "sparql_query"
SCHEMA_TOOL = "list_predicates"
PREDICATES = "SELECT ?predicate (COUNT(*) AS ?uses) WHERE { ?s ?predicate ?o } GROUP BY ?predicate ORDER BY ?predicate"

# Text the update hint does not look into: strings, IRIs and comments. It only words a syntax error, never a refusal.
_OPAQUE = re.compile(
    r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|<[^<>\s]*>|#[^\r\n]*'
)
# SERVICE sends part of a query to another endpoint over HTTP, which pyoxigraph would do: never allowed here. The word
# is refused anywhere in the query, a string or a comment included, once SPARQL's codepoint escapes are decoded:
# telling a keyword from a string or a comment needs the parser's own tokenizer, and a guard that gets that wrong lets
# a query reach the network. A query that only mentions the word in a literal is refused too, by design.
_SERVICE = re.compile(r"service", re.IGNORECASE)
_CODEPOINT = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")
_UPDATE = re.compile(r"\b(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD|WITH)\b", re.IGNORECASE)


@dataclass
class QueryLog:
    """What the platform served in one run, by its own count. One log per run; the instrument reads it."""

    queries: int = 0
    rows_served: int = 0
    truncated: int = 0
    refused: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "queries": self.queries,
            "rows_served": self.rows_served,
            "truncated": self.truncated,
            "refused": self.refused,
        }


def tool_specs(*, schema: bool) -> list[ToolSpec]:
    """The platform's tools: the query tool, and the schema tool when `schema` is true. None is terminal."""
    specs = [
        ToolSpec(
            QUERY_TOOL,
            "Run one read-only SPARQL query (SELECT, ASK, CONSTRUCT or DESCRIBE) over the graph. Results are capped.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        )
    ]
    if schema:
        specs.append(ToolSpec(SCHEMA_TOOL, "List the predicates the graph uses, with how often.", {"type": "object"}))
    return specs


class SparqlTools:
    """The tools of one run, over a shared store, counting what they serve in the run's log.

    The functions take the `FunctionProvider` calling convention, `(arguments, *, timeout_s)`, and run in its worker
    thread. A query's evaluation cannot be interrupted, so only the reading of its results is bounded by `timeout_s`:
    keep the graph small enough that one query is quick.
    """

    def __init__(self, store: GraphStore, log: QueryLog, *, max_rows: int) -> None:
        if max_rows < 1:
            raise ValueError(f"max_rows must be at least 1, not {max_rows}")
        self.store, self.log, self.max_rows = store, log, max_rows

    def functions(self, *, schema: bool) -> list[tuple[ToolSpec, Callable[..., ToolOutput]]]:
        """Each tool's spec with the function that serves it, in the order of `tool_specs`."""
        serve = {QUERY_TOOL: self.query, SCHEMA_TOOL: self.predicates}
        return [(spec, serve[spec.name]) for spec in tool_specs(schema=schema)]

    def query(self, arguments: dict[str, Any], *, timeout_s: float) -> ToolOutput:
        text = arguments.get("query")
        if not isinstance(text, str) or not text.strip():
            return self._refuse("the query argument must be the text of a SPARQL query", "MissingQuery")
        if _SERVICE.search(_decoded(text)):
            return self._refuse(
                "SERVICE is refused, and so is the word anywhere in a query: this tool queries only the local graph",
                "ServiceRefused",
            )
        return self._run(text, time.monotonic() + timeout_s)

    def predicates(self, arguments: dict[str, Any], *, timeout_s: float) -> ToolOutput:
        return self._run(PREDICATES, time.monotonic() + timeout_s)

    def _run(self, text: str, deadline: float) -> ToolOutput:
        try:
            result = self.store.query(text)
            if isinstance(result, pyoxigraph.QueryBoolean):
                self.log.queries += 1
                return ToolOutput("true" if bool(result) else "false", source_truncated=False)
            header, rows, cut = self._read(result, deadline)
        except SyntaxError as error:
            if _UPDATE.search(_OPAQUE.sub(" ", text)):
                return self._refuse("updates are refused: this tool runs read-only queries", "ReadOnly")
            return self._refuse(f"not a valid SPARQL query: {error}", "QuerySyntax")
        except TimeoutError:  # before OSError, of which it is a subclass: a slow query is the model's, not the store's
            return self._refuse("the query's results took longer than the time left", "QueryTimeout")
        except _BlankResult:
            return self._refuse(
                "results with blank nodes are refused: their labels differ on every run; select IRIs or literals",
                "BlankNodeResult",
            )
        except OSError as error:
            raise InfraError(STORE_KIND, f"the store could not be read ({type(error).__name__})") from error
        self.log.queries += 1
        self.log.rows_served += len(rows)
        self.log.truncated += cut
        lines = ["\t".join(header), *("\t".join(row) for row in rows)]
        if cut:
            lines.append(f"(the first {self.max_rows} rows; more exist: narrow the query)")
        elif not rows:
            lines.append("(no rows)")
        return ToolOutput("\n".join(lines), source_truncated=cut)

    def _read(self, result: Any, deadline: float) -> tuple[list[str], list[list[str]], bool]:
        """Read at most max_rows rows, and tell whether more exist."""
        if isinstance(result, pyoxigraph.QuerySolutions):
            header, render = [f"?{variable.value}" for variable in result.variables], _solution_row
        else:  # CONSTRUCT or DESCRIBE: triples
            header, render = ["subject", "predicate", "object"], _triple_row
        rows: list[list[str]] = []
        for item in result:
            if time.monotonic() > deadline:
                raise TimeoutError
            if len(rows) == self.max_rows:
                return header, rows, True
            rows.append(render(item))
        return header, rows, False

    def _refuse(self, message: str, error_class: str) -> ToolOutput:
        self.log.refused += 1
        return ToolOutput(message, is_error=True, error_class=error_class)


class _BlankResult(Exception):
    """A result holds a blank node, whose label a query mints afresh on every run."""


def _decoded(text: str) -> str:
    """The query with SPARQL's codepoint escapes decoded, as the parser reads it."""
    return _CODEPOINT.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), text)


def _term(term: Any) -> str:
    """A term in N-Triples syntax; a blank node, or a quoted triple holding one, is refused (its label is random)."""
    if isinstance(term, pyoxigraph.BlankNode):
        raise _BlankResult
    if isinstance(term, pyoxigraph.Triple):
        for part in (term.subject, term.predicate, term.object):
            _term(part)
    return str(term)


def _solution_row(solution: pyoxigraph.QuerySolution) -> list[str]:
    """A SELECT row in N-Triples syntax; an unbound variable is an empty cell."""
    return ["" if term is None else _term(term) for term in solution]


def _triple_row(triple: pyoxigraph.Triple) -> list[str]:
    return [_term(triple.subject), _term(triple.predicate), _term(triple.object)]

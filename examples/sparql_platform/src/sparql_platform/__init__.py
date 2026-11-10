"""An example platform for the core: an in-process RDF store queried read-only over SPARQL.

It uses only the core's documented interfaces: `ToolProvider` (through `FunctionProvider`), `Instrument`,
`HealthCheck`, `ToolOutput` and `InfraError`, and an infrastructure kind registered in `sparql_platform.kinds`.
"""

from sparql_platform.kinds import STORE_KIND
from sparql_platform.platform import NAMESPACE, RunSession, SparqlPlatform, StoreHealth, StoreSnapshot
from sparql_platform.store import GraphStore
from sparql_platform.tools import QUERY_TOOL, SCHEMA_TOOL, QueryLog, SparqlTools, tool_specs

__all__ = [
    "NAMESPACE",
    "QUERY_TOOL",
    "SCHEMA_TOOL",
    "STORE_KIND",
    "GraphStore",
    "QueryLog",
    "RunSession",
    "SparqlPlatform",
    "SparqlTools",
    "StoreHealth",
    "StoreSnapshot",
    "tool_specs",
]

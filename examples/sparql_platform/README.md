# SPARQL platform

A second platform for the core, beside the Onedata layer: an in-process RDF store ([pyoxigraph](https://pypi.org/project/pyoxigraph/)) that an agent queries read-only over SPARQL. It needs no Docker, no network and no key. It is built only on the core's documented interfaces, and it is its own distribution, `sparql-platform`, which a study names in `[study] requires` and whose wheel a replication package vendors.

| Part | What it is |
|---|---|
| `sparql_platform.kinds` | registers the infrastructure kind `rdf_store` (the store could not be read); a study lists the module in `[study] platforms` |
| `GraphStore` | a Turtle or N-Triples file loaded into an in-memory store; its snapshot is the triple count and the SHA-256 of the sorted N-Quads dump |
| `SparqlPlatform.session()` | one run's view: a `FunctionProvider` with `sparql_query` (and `list_predicates` when `schema=True`), each capped at `max_rows` rows and marking a cut result `source_truncated`, and the run's instrument |
| `StoreSnapshot` | the instrument, namespace `rdf_store`: the store's state at the run's start and end, whether it changed, and what the platform served (queries, rows, cut results, refusals), with the platform's and the store's versions |
| `StoreHealth` | the health check: the store answers and holds as many triples as when it was loaded |
| `SparqlPlatform.fingerprint()` | the row cap and the graph's snapshot, for `StudyPlugin.platform`, so a resume under another is refused |

Read-only: nothing here calls the store's update API, and `Store.query` parses queries only, so an update is refused as a syntax error; the tool says it was refused for being an update. `SERVICE`, which would send part of a query to another endpoint over HTTP, is refused before the query runs, wherever the keyword appears outside a string, an IRI or a comment.

## The example study

`study/` is a small study on the platform: two conditions that differ in tool exposure (`bare` has the query tool, `guided` also the schema tool), four questions about an invented orchard (`study/orchard.ttl`), two stub models with scripted transcripts. From the core's checkout, after `uv sync` (which installs this platform editable, as a development dependency of the core):

```
uv run python -m expcore run examples/sparql_platform/study --allow-dirty
```

`tests/e2e/test_sparql_platform.py` copies the study, runs it end to end, builds its replication package and runs the package's check.

In a study repository of your own, add the core and this platform, copy `study/`, commit, and run the commands of the core's study guide:

```
uv add <the core>
uv add <this directory>
uv run python -m expcore run study
uv run python -m expcore check study/out/runs
uv run python -m expcore analyze study
uv run python -m expcore report study
uv run python -m expcore package study --out dist
```

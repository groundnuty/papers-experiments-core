# Orchard study

A small study on the SPARQL platform (`sparql-platform`): does showing the agent a schema tool change how it answers questions about a graph? It runs in seconds with no network and no key: both models are stubs that play a script, and the platform is an in-process RDF store.

| File | Holds |
|---|---|
| `study.toml` | one factor, `exposure`, and two conditions: `bare` (the query tool) and `guided` (also `list_predicates`); four questions with a `shape` attribute; the platform in `[study] requires` and its kinds module in `[study] platforms` |
| `orchard.ttl` | the invented graph the platform loads: three groves, two crews, twenty-five trees |
| `plugin.py` | the questions and the answer key, the terminal `submit` tool, the stub's scripts, the scorer and the executor |
| `models.toml`, `campaign.toml`, `preregistration.toml` | two stub models; the plan and the caps; two contrasts in the Holm family and one exploratory |
| `prompt.txt` | the prompt template |
| `package.toml` | the inclusion list of the replication package |

## What the stub model does

| Run | What happens |
|---|---|
| `guided` | lists the predicates, runs the right query, submits |
| `bare` | guesses a predicate the graph does not use (no rows), lists the predicates with a query, runs the right query, submits |
| `bare` on `thistledown-fruits` | lists every tree of the grove first, which the platform cuts at eight rows (`source_truncated`), then counts |
| `bare` on `marrowby-crew` with `beta-stub` | tries an update first: refused, the store unchanged |
| `guided` on `quillfen-count` with `beta-stub` | tries a `SERVICE` query first: refused before it runs |
| `guided` on `oldest-fruit` with `alpha-stub` | sends a query with a syntax error first |
| `beta-stub` on two runs | a wrong answer |

Every record holds the instrument's snapshot under `instruments.rdf_store`: the store's triple count and digest at the run's start and end, whether it changed, and what the platform served. With four tasks in two shapes, a contrast has two clusters, so no difference can reach significance: the study shows the shape of the output, not a finding.

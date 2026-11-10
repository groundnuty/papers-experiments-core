# Tiny study

A complete minimal study for the core. Copy this directory, change the names and the behaviour, keep the shape. It runs in seconds with no network and no key: both models are stubs that play a script, and the platform is a stub whose health check, resource probe and leases always succeed.

| File | Holds |
|---|---|
| `study.toml` | two factors (`access`, `tools`), three named conditions, four tasks with a `region` attribute. `extended` runs on three of the tasks only. `relay-down` declares the tool server's failure (`knowledge`) as its treatment |
| `models.toml` | the model table: two priced stub models |
| `campaign.toml` | the plan (models, seeds, run order seed), the loop's caps, the scheduler's caps and a budget |
| `preregistration.toml` | two contrasts in the Holm family (one with an equivalence margin, one declaring that its treatment attempts are scored), one exploratory contrast outside it, the estimator, power settings |
| `plugin.py` | the tools (two functions, one waits on tool-server start-up), the stub platform's shelf, which serves the lookups and counts the facts it handed out, an instrument (that count, as a snapshot at the run's end), the scorer (which reads the count and returns values for a treatment attempt), the executor, the health check and the resource probe |
| `prompt.txt` | the prompt template with its named slots |
| `package.toml` | the inclusion list of the study's replication package |

The interfaces the plugin implements are described in [`docs/interfaces.md`](../../docs/interfaces.md), and how to build a study on them in [`docs/study-guide.md`](../../docs/study-guide.md). In a copy that `python -m expcore example` writes, they point at the documentation of the core that wrote it: links on GitHub at the commit the core was installed from, when it came from a GitHub repository, and otherwise each page's path in the core's repository with the tag or commit to read it at. A link to the core's repository can lead to its authors, so drop or rewrite such links before a double-blind submission; an anonymised build refuses them when the core was installed from that repository.

## Run it

A study written by `uv run python -m expcore example my-study` and committed, as [the study guide's section 1](../../docs/study-guide.md#1-start-a-study) sets it up, runs from the directory that holds `my-study`:

```
uv run python -m expcore run my-study
uv run python -m expcore check my-study/out/runs
uv run python -m expcore analyze my-study
uv run python -m expcore report my-study
```

Everything is written under `my-study/out/`, which the study's `.gitignore` keeps out of git.

- `run` opens the campaign or resumes it, so killing the command and running it again finishes the plan. It exits 3 if runs are left pending (held by the budget or a stop).
- `check` replays every record offline, copies the campaign store beside them, and stores the result with a hash of each. It refuses a campaign with attempts still running unless `--partial`.
- `analyze` refuses unless the check is current, then uses only what the check verified (the copy of the store and the replayed records, never the live store) and writes `export.json`, `scores.json` and `analysis.json` under `out/`. It scores every record anew from the record alone, with no model and no platform, and reads no earlier `scores.json`; to apply a changed scorer, stamp the copy again if it has no git, run `check` (the scorer is code, so the identity changed), then `analyze` and `report`. With `--partial` it tolerates attempts that were still running and leaves out a final attempt no replayed record covers. `export.json`, `analysis.json` and the report all say whether the analysis is partial and list what was left out.
- `report` writes `out/report.md`: every contrast with its estimate, interval, equivalence margin and interval at 1 - 2 alpha, raw and Holm-adjusted p-value and its conclusion, its pairs used and missing by cause, the exploratory contrast apart, each condition's planned runs and how they ended, the infrastructure failures by condition, and the preregistration hash, the code stamp, the core version and the version of each scorer.

The replication package: once `report` has run on committed code, `uv run python -m expcore package my-study --out dist` writes `dist/tiny-study-package.tar.gz` with what `package.toml` lists and the wheels of the environment, and the package's own `make -f Makefile check` installs those wheels offline into a fresh environment, then derives the tables and the report anew from the records and compares them byte for byte. See [the study guide's section 8](../../docs/study-guide.md#8-ship-a-replication-package).

### Uncommitted changes

The records are stamped with the study's code identity: the commit plus a hash of the files named in `[study] code`. `run`, `check`, `analyze` and `report` refuse a study with uncommitted changes in that code unless you pass `--allow-dirty`. That is for a pilot, or for trying a change before you commit it; leave it out for a result you publish, and a replication package is built only from a replay check made under a clean commit (or a clean stamp), so it cannot carry a dirty one. A copy made without git needs a stamp instead: `expcore.provenance.codestamp.write_stamp(directory, <commit>, scope)` writes `.code-stamp.json`.

In the core's own checkout, the example is `examples/tiny_study`: after `uv sync`, the commands above take that path from the repository root and write under `examples/tiny_study/out/`, which git ignores. A checkout you are changing has uncommitted code, so there they need `--allow-dirty` until you commit. `scripts/fresh_repo.py` runs the example the way a study does, from the installed core in a new repository.

## What the stub model does

The script of each run is a pure function of its condition, task and model (`script_for` in `plugin.py`), so a rerun plays the same turns:

| Run | What happens | Outcome |
|---|---|---|
| most runs | look the fact up, give the answer with the terminal `submit` tool | `finished` |
| `baseline` on `river` | one call with malformed arguments, then the model recovers | `finished`, one malformed call counted; the platform served one fact, since the malformed call never ran |
| `baseline` on `hill` | looks things up for ever | `turn_cap` |
| `relay-down` on `lake` | the tool server fails after the first lookup: `knowledge` infrastructure | scored as treatment (no answer: accuracy 0, one round, one lookup, one fact served) and kept in `relay-vs-baseline`, never run again (a failure before the agent's first turn would be set aside and run again) |
| `beta-stub` on a few runs | a wrong answer | `finished`, accuracy 0 |

With four tasks in two regions, a contrast has two clusters, so no difference can reach significance here. The example shows the shape of the output, not a finding.

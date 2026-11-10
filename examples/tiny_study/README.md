# Tiny study

A complete minimal study for the core. Copy this directory, change the names and the behaviour, keep the shape. It runs in seconds with no network and no key: both models are stubs that play a script, and the platform is a stub whose health check, resource probe and leases always succeed.

| File | Holds |
|---|---|
| `study.toml` | two factors (`access`, `tools`), three named conditions, four tasks with a `region` attribute. `extended` runs on three of the tasks only. `relay-down` declares the tool server's failure (`knowledge`) as its treatment |
| `models.toml` | the model table: two priced stub models |
| `campaign.toml` | the plan (models, seeds, run order seed), the loop's caps, the scheduler's caps and a budget |
| `preregistration.toml` | two contrasts in the Holm family (one with an equivalence margin, one declaring that its treatment attempts are scored), one exploratory contrast outside it, the estimator, power settings |
| `plugin.py` | the tools (two functions, one waits on tool-server start-up), the scorer (which returns values for a treatment attempt), the executor, the health check and the resource probe |
| `prompt.txt` | the prompt template with its named slots |
| `package.toml` | the inclusion list of the study's replication package |

The interfaces the plugin implements are described in [`docs/interfaces.md`](../../docs/interfaces.md).

## Run it

From the repository root, with the core installed (`uv sync`). The study is written under `examples/tiny_study/out/`, which git ignores.

```
uv run python -m expcore run examples/tiny_study --allow-dirty
uv run python -m expcore check examples/tiny_study/out/runs --allow-dirty
uv run python -m expcore analyze examples/tiny_study --allow-dirty
uv run python -m expcore report examples/tiny_study --allow-dirty
```

- `run` opens the campaign or resumes it, so killing the command and running it again finishes the plan. It exits 3 if runs are left pending (held by the budget or a stop).
- `check` replays every record offline, copies the campaign store beside them, and stores the result with a hash of each. It refuses a campaign with attempts still running unless `--partial`.
- `analyze` refuses unless the check is current, then uses only what the check verified (the copy of the store and the replayed records, never the live store) and writes `export.json`, `scores.json` and `analysis.json` under `out/`. It scores every record anew from the record alone, with no model and no platform, and reads no earlier `scores.json`; to apply a changed scorer, stamp the copy again if it has no git, run `check` (the scorer is code, so the identity changed), then `analyze` and `report`. With `--partial` it tolerates attempts that were still running and leaves out a final attempt no replayed record covers. `export.json`, `analysis.json` and the report all say whether the analysis is partial and list what was left out.
- `report` writes `out/report.md`: every contrast with its estimate, interval, raw and Holm-adjusted p-value and its conclusion, its pairs used and missing by cause, the exploratory contrast apart, each condition's planned runs and how they ended, the infrastructure failures by condition, and the preregistration hash, the code stamp, the core version and the version of each scorer.

A study that installs the core by tag or commit (see `docs/study-guide.md`, section 1) gets the example from the installed package instead: `uv run python -m expcore example my-study` writes it into a new directory, and the commands above then take `my-study`. `scripts/fresh_repo.py` does exactly that in a new repository.

The replication package: once `report` has run on committed code, `python -m expcore package examples/tiny_study --out dist` writes `dist/tiny-study-package.tar.gz` with what `package.toml` lists and the wheels of the environment, and the package's own `make check` installs those wheels offline into a fresh environment, then derives the tables and the report anew from the records and compares them byte for byte. A package is built only from a replay check made under a clean commit (or a clean stamp), so the commands above without `--allow-dirty`. See [`docs/study-guide.md`](../../docs/study-guide.md).

`--allow-dirty` accepts uncommitted changes in the code the records are stamped with. Leave it out when the study is committed: the code identity is the commit plus a hash of the files named in `[study] code`, and a clean one is what a published result should carry. A copy made without git needs a stamp instead: `expcore.provenance.codestamp.write_stamp(directory, <commit>, scope)` writes `.code-stamp.json`.

## What the stub model does

The script of each run is a pure function of its condition, task and model (`script_for` in `plugin.py`), so a rerun plays the same turns:

| Run | What happens | Outcome |
|---|---|---|
| most runs | look the fact up, give the answer with the terminal `submit` tool | `finished` |
| `baseline` on `river` | one call with malformed arguments, then the model recovers | `finished`, one malformed call counted |
| `baseline` on `hill` | looks things up for ever | `turn_cap` |
| `relay-down` on `lake` | the tool server fails after the first lookup: `knowledge` infrastructure | scored as treatment (no answer: accuracy 0, one round, one lookup) and kept in `relay-vs-baseline`, never run again (a failure before the agent's first turn would be set aside and run again) |
| `beta-stub` on a few runs | a wrong answer | `finished`, accuracy 0 |

With four tasks in two regions, a contrast has two clusters, so no difference can reach significance here. The example shows the shape of the output, not a finding.

# Study guide

How to build a study on the core: start it, configure it, declare its design, run and record it, check, score and analyse it, freeze its protocol and ship its replication package. The exact interfaces are in [`interfaces.md`](interfaces.md), and the reasons behind them in [`architecture.md`](architecture.md); this guide is the order to use them in. It follows the example study (`examples/tiny_study/`), which the core carries, so every step below runs as written. `tests/test_study_guide.py` runs every `sh` and `python` block of this file in order, in a new directory, and checks every `toml` block against the example's files. The two lines that make the project and add the core are the exception: the test runs with the core already installed, and `scripts/fresh_repo.py`, which the fresh-repo workflow runs after every push to `main`, runs those two from a pushed commit.

## 1. Start a study

A study is its own git repository that depends on the core by tag and copies nothing from it. Start from the example: write it out of the installed core, commit it, and change the names and the behaviour while keeping the shape.

```sh
git init --quiet
uv init --bare
uv add git+https://github.com/groundnuty/papers-experiments-core@v0.1.0
uv run python -m expcore example my-study
git add .
git commit --quiet -m "the study, from the example"
```

The commit matters. A run, a replay check and a replication package are stamped with the study's code identity: the commit plus a hash of every file `[study] code` names. Uncommitted changes make the identity dirty, and a dirty identity refuses to start a study run unless you pass `--allow-dirty`, which a pilot may do and a published result should not. A copy without git needs `.code-stamp.json` instead (`expcore.provenance.codestamp.write_stamp`).

### What you implement

The study directory holds four configuration files and one Python file, the plugin, named by `[study] plugin`. The core imports the plugin and calls `build(context)`, which returns a `StudyPlugin`:

| Part | What it is | In the example |
|---|---|---|
| `executor` | a `RunExecutor`: given a `RunRequest`, script or configure the backend the request carries, run the loop with `record_run`, write the record to `request.run_dir`, return it | `TinyExecutor` |
| `scorers` | `Scorer`s: a pure function of a record, returning named numeric outcomes | `TinyScorer` |
| `prompts`, `tools` | per condition, the SHA-256 of its prompt template and of its tool manifest; both go into the campaign fingerprint | from `PromptTemplate` and `tools_sha256` |
| tools | `ToolProvider`s the executor passes to `record_run`: plain functions through `FunctionProvider`, or an MCP server through `McpToolProvider` | `lookup` and `submit` |
| `health`, `probe`, `lease_group`, `lease_keys` | the platform's health check, resource probe and leases; the Onedata layer supplies real ones | stubs that always succeed |

The plugin names everything; the core names nothing. What each interface guarantees, and what you must guarantee in return (an executor never builds a backend, a scorer never reads the platform), is in [`interfaces.md`](interfaces.md).

## 2. Where configuration lives

All configuration is TOML in the study directory, validated when the study is loaded, with every error naming the file and the key; nothing is read lazily.

| File | Holds |
|---|---|
| `study.toml` | the study's name, its plugin, the code identity's scope, the factors, the conditions and the tasks |
| `models.toml` | the single model table: each model once, with backend, API id, sampling, limits, price and status |
| `campaign.toml` | the plan (models, seeds, order seed), the loop's caps, the scheduler's caps and the budget |
| `preregistration.toml` | the named contrasts, the Holm family, alpha, the estimators, the bootstrap and the power settings |
| `package.toml` | the inclusion list of the replication package ([section 7](#7-ship-a-replication-package)) |
| `platform.toml` | a platform's own settings, read by the platform layer (`expcore_onedata`), not by the core |

Secrets are never written in configuration: a model names the environment variable or file that holds its key. The hash of every configuration file goes into the campaign's fingerprint, so resuming a campaign after any of them changed is refused.

```python
from expcore.study.config import load_study

study = load_study("my-study")
assert study.name == "tiny-study"
assert sorted(study.config_sha256) == ["campaign.toml", "models.toml", "preregistration.toml", "study.toml"]
statuses = {key: study.models.resolve(key).status for key in study.models}
assert statuses == {"alpha-stub": "frozen", "beta-stub": "frozen"}  # only frozen settings may start a study run
```

## 3. Declare the design

### Factors and conditions

A study declares its factors; `task`, `model` and `seed` are built in. The identity order says how a run key is composed, and a condition assigns every declared factor.

```toml
# my-study/study.toml
[factors.access]
levels = ["direct", "relay"]

[factors.tools]
levels = ["basic", "extended"]

[identity]
order = ["task", "access", "tools", "model", "seed"]

[conditions.baseline]
access = "direct"
tools = "basic"
```

Conditions need not form a full cross. Contrasts are declared in `preregistration.toml`, either between two named conditions, paired on task, model and seed, or as a factor contrast that varies one factor and pairs on every other. Both are clustered by a task attribute.

```toml
# my-study/preregistration.toml
[contrasts.extended-vs-baseline]
conditions = ["extended", "baseline"]
outcome = "accuracy"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "region"
equivalence_margin = 0.2

[contrasts.tools-effect-on-rounds]
factor = "tools"
levels = ["extended", "basic"]
outcome = "rounds"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "region"
```

### Chain designs

In a chain design an arm runs on a subset of the tasks: the condition lists them with `tasks = [...]`. The plan expands each condition over its own tasks, and a contrast with that arm drops, for that contrast only, the tasks the arm did not run, and names them in the report.

```toml
# my-study/study.toml
[conditions.extended]
access = "direct"
tools = "extended"
tasks = ["lake", "river", "hill"]
```

### Treatment kinds

An infrastructure failure is normally set aside and the run is tried again. A condition whose manipulation is the failure itself, a tool server that goes down for example, declares that kind as its treatment, and a run that ends in it is then scored as that condition's outcome and never run again.

```toml
# my-study/study.toml
[conditions.relay-down]
access = "relay"
tools = "basic"
infrastructure_is_treatment = ["knowledge"]
```

Two rules bound this. A failure before the model's first turn is never treatment, whatever the condition declares: nothing the agent did can have met the manipulation, so the attempt is set aside and run again. And a kind may be non-treatable: `tool_plane_integrity` (the tool server's rights or token could not be verified) can never be a treatment, nor can a kind a platform registers with `treatable=False`, and loading a study whose condition declares one is refused, naming the file and the key. The scheduler also refuses a campaign in which a condition declares the kind its health check or its endpoint-identity check reports.

```python
import shutil
from pathlib import Path

from expcore.config.loading import ConfigError
from expcore.errors import untreatable_kinds

assert study.schema.treatment_kinds["relay-down"] == ("knowledge",)
assert study.schema.condition_tasks["extended"] == ("lake", "river", "hill")
assert "tool_plane_integrity" in untreatable_kinds()

tampered = Path("tampered-study")
shutil.copytree("my-study", tampered)
toml = tampered / "study.toml"
toml.write_text(toml.read_text().replace('["knowledge"]', '["tool_plane_integrity"]'))
try:
    load_study(tampered)
    raise AssertionError("a non-treatable kind was accepted")
except ConfigError as error:
    assert "infrastructure_is_treatment" in str(error) and "can never be a treatment" in str(error)
shutil.rmtree(tampered)
```

## 4. Run, record and check

```sh
uv run python -m expcore run my-study
uv run python -m expcore check my-study/out/runs
```

`run` opens the campaign or resumes it: a killed run is settled and the plan finishes, and it exits 3 if runs are left pending, held by the budget or a stop. Every attempt writes one record under `my-study/out/runs/<digest>/try<N>/`: the route and the settings actually sent, the prompt and the tool manifest, the agent configuration as resolved, every model turn and tool call with what the model saw and what was raw, every clock reading, the outcome from a closed vocabulary, and the code identity. Secrets are redacted before writing.

`check` is the replay check. It loads every record, writes it back, and replays it: the real loop runs again with every model turn, tool result and clock reading served from the record, and must re-derive the transcript, the tool-call log and the outcome byte for byte, with no model and no platform. It copies the campaign store beside the records and hashes the copy with them. Analysis refuses a batch without a current passing check, so a record edited, added or removed after the check, or code changed since, makes `analyze` and `report` refuse until the check is run again. A campaign with attempts still running is refused unless `--partial`, and every output then says that it is partial and lists what was left out.

The check is stored beside the records, with the code identity it was made under and the hash of every input:

```python
import json

check = json.loads(Path("my-study/out/runs/replay-check.json").read_text())
assert check["n_ok"] == check["n_records"] == len(check["replayed"]) and not check["failures"]
assert check["code"]["dirty"] is False and "campaign-snapshot.sqlite" in check["inputs"]
```

## 5. Score and analyse

```sh
uv run python -m expcore analyze my-study
uv run python -m expcore report my-study
```

`analyze` scores every record anew, from the record alone, with the scorers the plugin returns, and writes `export.json`, `scores.json` and `analysis.json` under `my-study/out/`. Nothing reads an earlier score back, so `analyze` is also how a study is re-scored after a scorer changes: the scorer is code, so run `check` again first. `report` writes `report.md`: every contrast with its estimate, interval, raw and Holm-adjusted p-value and conclusion, the infrastructure failures by condition, the preregistration hash, the code identity, the core version and each scorer's version.

The infrastructure failure the example declares as treatment comes after the model's first turn, so it is an analysed attempt, marked as treatment with its kind, and it is scored. A scorer is a pure function of the record: no model, no platform, no clock. It returns named numeric outcomes, and an outcome it could not compute is `None`, never 0; a `not_determined` verdict leaves every outcome that depends on the unobserved state `None`. Here is one applied to the treatment attempt:

```python
from expcore.records.record import load_record, record_filename
from expcore.records.rescore import rescore

export = json.loads(Path("my-study/out/export.json").read_text())
treated = [e for e in export["analysed_attempts"] if e["treatment"]]
assert {(e["condition"], e["infra_kind"]) for e in treated} == {("relay-down", "knowledge")}
entry = treated[0]
record = load_record(Path("my-study/out/runs") / entry["run_dir"] / record_filename(entry["run_key"], entry["attempt"]))


class LookupCount:
    """How many lookups the agent made; not computable when the platform failed under the run."""

    name = "lookups"
    version = "1"

    def score(self, record):
        if record.outcome["name"] == "infrastructure":
            return {"verdict": "not_determined", "outcomes": {"lookups": None}}
        looked = [call for call in record.tool_calls if call["name"] == "lookup"]
        return {"verdict": "fulfilled", "outcomes": {"lookups": float(len(looked))}}


scored = rescore(record, [LookupCount()])
assert scored.scores["lookups"] == {
    "version": "1",
    "result": {"verdict": "not_determined", "outcomes": {"lookups": None}},
}
```

## 6. What a conclusion may say

Every contrast ends in one of four words, `difference`, `trivial_difference`, `equivalent` or `not_detected`, and never in "no difference". Only `equivalent` may be read as no difference, and only a contrast with an `equivalence_margin` in the preregistration can reach it: the interval at 1 - 2 alpha lies inside plus or minus the margin and the test is not significant. `not_detected` is the absence of evidence, never evidence of absence; without a margin, it is the most a non-significant result can say.

A contrast outside the preregistered Holm family is exploratory. It is judged by its raw p-value, nothing controls its multiplicity, and every table and report shows it apart, labelled exploratory, never as confirmatory. Only the Holm family carries the study's confirmatory claims.

```python
from expcore.stats.analysis import CONCLUSIONS, conclude

assert CONCLUSIONS == ("difference", "trivial_difference", "equivalent", "not_detected")
assert conclude(significant=False, equivalent=True) == "equivalent"  # needs a margin: without one, equivalent is False
assert conclude(significant=False, equivalent=False) == "not_detected"
assert conclude(significant=True, equivalent=True) == "trivial_difference"

analysis = json.loads(Path("my-study/out/analysis.json").read_text())
labels = {c["name"]: (c["exploratory"], c["equivalence_margin"]) for c in analysis["contrasts"]}
assert labels == {
    "extended-vs-baseline": (False, 0.2),  # in the family, and the only one that could ever read "equivalent"
    "relay-vs-baseline": (False, None),
    "tools-effect-on-rounds": (True, None),  # outside the family: exploratory wherever it is shown
}
report = Path("my-study/out/report.md").read_text()
assert "tools-effect-on-rounds" in report.split("## Exploratory contrasts")[1]
```

## 7. Freeze the protocol

A protocol is frozen when nothing that decides a result can change unnoticed. Before the study's runs:

- Freeze every model: `status = "frozen"` in `models.toml`, with its settings collected. A study run refuses a model that is not frozen.
- Write `preregistration.toml`: the contrasts, the Holm family, the margins, alpha, the estimators and the bootstrap and power settings. Its hash goes into every analysis and the report.
- Commit the study, and pin the core by tag in its dependencies (`uv add ...@v0.1.0`); every record carries the core version.
- Then run. The campaign fingerprint holds the hash of every configuration file, the prompts, the tool manifests and the scorers' versions, and a resume under any change is refused.

```python
from expcore.provenance.codestamp import version

assert version("my-study", study.scope)["dirty"] is False  # the committed study: a clean identity
assert analysis["preregistration"]["preregistration_sha256"] == study.prereg.sha256
assert analysis["code"]["dirty"] is False
```

## 8. Ship a replication package

`package.toml` lists what the package holds; anything it does not list is left out. The example lists its configuration and code, the records with the replay check (`out/runs`), and the four outputs.

```toml
# my-study/package.toml
[package]
name = "tiny-study-package"
```

```sh
uv run python -m expcore package my-study --out dist
tar -xzf dist/tiny-study-package.tar.gz -C dist
make -C dist/tiny-study-package -f Makefile check PYTHON="uv run python"
```

The build refuses a check made under code that is not a clean commit, outputs made by another core version, a file the package's check needs and the list leaves out, and any path or content that matches a name private everywhere in the core's study-token hashes (`--hashes` takes another hash file). Before it writes the archive it runs the package's own check on the staged files: every file against `SHA256SUMS`, then `export.json`, `scores.json`, `analysis.json` and `report.md` derived anew from the records and compared byte for byte. That check is what `make -f Makefile check` runs for a reader; naming the Makefile matters, because the package may hold no other file make would read first, and the build refuses one, but a reader's copy could. The archive is deterministic: two builds of one tree are the same bytes.

For a double-blind submission, `--anonymise LIST` takes an anonymisation list, kept outside the study's repository and the output. It names the prose and metadata files to rewrite (`files = [...]`) and each term with its label (`[terms]`). A term is replaced only as a whole word, only in those files: JSON in its parsed keys and strings, then written out again; TOML, YAML and text as text. A term found anywhere else, code and records included, fails the build naming the file and the line, never the term, and so does one that an HTML entity, a percent-escape or a backslash escape hid from the rewrite. The token and term checks also read inside gzip, bzip2, xz, zip and tar content and every UTF-16 encoding, and the build refuses another compressed or archive format as unscannable. Code is never rewritten.

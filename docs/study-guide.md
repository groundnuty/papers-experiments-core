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

The identity covers the study's directory and nothing else. Platform and backend code outside it is part of the campaign's *environment*: the core and every distribution named in `[study] requires` (for example `requires = ["toy-platform"]`, names only, no versions), with everything they require, each with its version and, for an install from a URL, where it came from. A package the plugin imports and `requires` does not name is not recorded, and a reader's check fails on it. The campaign fingerprint holds the environment, so a resume under another one is refused; the replay check refuses to run under anything but the campaign's environment and records the one it ran under, and the analysis refuses a check made under another. A version says nothing about an editable checkout, whose code can change under the same version: bump the version when the code changes, and have a platform report its version in its instrument snapshot or `info()`, as a backend can in its route.

### What you implement

The study directory holds four configuration files and one Python file, the plugin, named by `[study] plugin`. The core imports the plugin and calls `build(context)`, which returns a `StudyPlugin`:

| Part | What it is | In the example |
|---|---|---|
| `executor` | a `RunExecutor`: given a `RunRequest`, script or configure the backend the request carries, run the loop with `record_run`, write the record to `request.run_dir`, return it | `TinyExecutor` |
| `scorers` | `Scorer`s: a pure function of a record, returning named numeric outcomes | `TinyScorer` |
| `prompts`, `tools` | per condition, the SHA-256 of its prompt template and of its tool manifest; both go into the campaign fingerprint | from `PromptTemplate` and `tools_sha256` |
| tools | `ToolProvider`s the executor passes to `record_run`: plain functions through `FunctionProvider`, or an MCP server through `McpToolProvider` | `lookup` and `submit` |
| `health`, `probe`, `lease_group`, `lease_keys` | the platform's health check, resource probe and leases; the Onedata layer supplies real ones | stubs that always succeed |
| `backend_factory` | optional: a `BackendFactory` that builds the backend of every model, for a study with a model backend of its own. Its model entries name that backend in `models.toml` with a plain lower-case `backend` value; the factory serves those and hands every other model to the core's `default_backend_factory`. It carries a `version` attribute that goes into the campaign fingerprint with its name; a factory imported from an installed package must declare the installed version of the distribution that owns its module's file, one defined in a file `[study] code` lists, which the code identity covers, may declare any, and one from anywhere else is refused. Without a factory, such a model is refused when the plugin is loaded | none: the core's factory builds the stub |

The plugin names everything; the core names nothing. What each interface guarantees, and what you must guarantee in return (an executor never builds a backend, a scorer never reads the platform), is in [`interfaces.md`](interfaces.md).

## 2. Where configuration lives

All configuration is TOML in the study directory, validated when the study is loaded, with every error naming the file and the key; nothing is read lazily.

| File | Holds |
|---|---|
| `study.toml` | the study's name, its plugin, the code identity's scope, the distributions it requires, the platforms it imports and its own infrastructure kinds ([section 3](#treatment-kinds)), the factors, the conditions and the tasks |
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

### The federation's certificate

On the Onedata platform four clients talk to the federation: the platform layer's REST client, the data gate that carries the agent's REST data path, oneclient in the mount sidecar, and the MCP tool server. All four take their certificate check from one `[tls]` table of `platform.toml`, so no two can check differently. Without the table, every client verifies the federation's certificate against its own default CAs, which is right for a federation with a publicly trusted certificate. A federation whose certificate no default trusts, such as a self-signed one, takes one of two modes:

- **`ca_file`**, preferred: `ca_file = "federation-ca.pem"` names a CA bundle the federation's certificate chains to, relative to `platform.toml`. Verification stays on, against that bundle. The bundle must hold certificates and no private key. No client reads the file you named: its bytes are hashed when `platform.toml` is loaded, read once more for each run and refused unless they have that hash, and every client gets those bytes, the REST client in memory and the gate, the sidecar and the tool server as the run's own read-only copy. A bundle replaced after the load is never trusted; a run started after it is refused until `platform.toml` is loaded again.
- **`insecure_tls`**: `insecure_tls = true` turns verification off for all four clients. It is for disposable test federations only: anyone on the path to the federation can then read and alter the data and the tokens. While it is on, every health reading ends with a warning.

The two keys never go together, and the per-client keys of earlier versions (`insecure_tls` under `[toolserver]`, `tls` under `[sandbox.mount]`) are refused at load. Each run's sandbox metadata records the mode, `verify`, `ca_file` with the bundle's SHA-256, or `insecure`, and never the bundle's path or contents. To make the mode part of the campaign's fingerprint, the plugin passes the platform's half of it, `StudyPlugin(..., platform=config.fingerprint())` with `config` the loaded `platform.toml`; a resume under another mode or another bundle is then refused.

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

A condition may name only the kinds known when `study.toml` is loaded, and it is loaded before the plugin is imported. Two keys of `[study]` add kinds first. `platforms = ["module", ...]` names modules imported before anything is validated, so the kinds a platform registers in them with `register_infra_kind` are known; for the Onedata layer the module is `expcore_onedata.sandbox.kinds`. A module that cannot be imported is refused, naming the file and the key. `[study.kinds]` declares the study's own kinds, one line each: `name = { description = "...", treatable = true }`, where `treatable` is true unless it says false. A study's kind that is a core kind or a platform's is refused, and it lasts only while the study is loaded or run. A kind named nowhere is refused as unknown, and the error lists the treatable kinds.

```python
from expcore.errors import infra_kinds

own = Path("own-kind-study")
shutil.copytree("my-study", own)
toml = own / "study.toml"
declared = '"prompt.txt"]\n\n[study.kinds]\nrelay_restart = { description = "the relay restarted", treatable = true }\n'
text = toml.read_text().replace('"prompt.txt"]\n', declared, 1)
toml.write_text(text.replace('["knowledge"]', '["relay_restart"]'))
assert load_study(own).schema.treatment_kinds["relay-down"] == ("relay_restart",)
assert "relay_restart" not in infra_kinds()  # the kind lasts only while its study is loaded or run
shutil.rmtree(own)
```

## 4. Run, record and check

```sh
uv run python -m expcore run my-study
uv run python -m expcore check my-study/out/runs
```

`run` opens the campaign or resumes it: a killed run is settled and the plan finishes, and it exits 3 if runs are left pending, held by the budget or a stop. Every attempt writes one record under `my-study/out/runs/<digest>/try<N>/`: the route and the settings actually sent, the prompt and the tool manifest, the agent configuration as resolved, every model turn and tool call with what the model saw and what was raw, every clock reading, the outcome from a closed vocabulary, and the code identity. Secrets are redacted before writing.

`check` is the replay check. It loads every record, writes it back, and replays it: the real loop runs again with every model turn, tool result and clock reading served from the record, and must re-derive the transcript, the tool-call log and the outcome byte for byte, with no model and no platform. It copies the campaign store beside the records and hashes the copy with them. Analysis refuses a batch without a current passing check, so a record edited, added or removed after the check, or code changed since, makes `analyze` and `report` refuse until the check is run again. A campaign with attempts still running is refused unless `--partial`, and every output then says that it is partial and lists what was left out.

`check` reads only the `[study]` table of `study.toml`. A batch recorded entirely with the core's backends, whose infrastructure kinds are the core's or declared in `[study.kinds]`, imports nothing from the study, so checking it runs none of the study's code. Checking a batch can import the platforms it names: a record that names a kind still unknown makes `check` print one line and import the modules in `[study] platforms`, which are installed code the study declares, and not the plugin; a kind still unknown after that fails its record. A record of a backend the core has no parser for needs the parser the study registers, so `check` prints one line saying so and also imports the plugin module: checking such a batch runs the plugin's code, so check it only if you trust the study. It does not call `build`, so it needs no model and no key. A module that cannot be imported is refused, naming the module and the exception's type.

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

`analyze` scores every record anew, from the record alone, with the scorers the plugin returns, and writes `export.json`, `scores.json` and `analysis.json` under `my-study/out/`. Nothing reads an earlier score back, so `analyze` is also how a study is re-scored after a scorer changes: the scorer is code, so run `check` again first. `report` writes `report.md`: every contrast with its estimate, interval, raw and Holm-adjusted p-value and conclusion, the pairs it used and the pairs it is missing by cause, each condition's planned runs and how they ended, the infrastructure failures by condition (every condition, zeros included), the preregistration hash, the code identity, the core version and each scorer's version. `analysis.json` carries the same counts.

The infrastructure failure the example declares as treatment comes after the model's first turn, so it is an analysed attempt, marked as treatment with its kind, and it is scored as its condition's outcome. A scorer is a pure function of the record: no model, no platform, no clock. It returns named numeric outcomes, and an outcome it could not compute is `None`, never 0; a `not_determined` verdict leaves every outcome that depends on the unobserved state `None`.

A treatment outcome is what the agent achieved under the manipulation, so a scorer returns values for a treatment attempt: the failure itself (the example's agent gave no answer, so its accuracy is 0) and what the agent did before it (its rounds and lookups). `None` stays reserved for what could not be measured, and it drops the run's pair from every contrast on that outcome: the attempt is scored as treatment and then leaves the comparison it was kept for. The analysis counts these drops per contrast. A confirmatory contrast (one in the Holm family) with any drop is refused, naming the contrast, the count and the fix, unless its preregistration says how treatment attempts enter it:

- `treatment_outcomes = "scored"`: the scorer returns values for them. This is a claim the analysis checks: a confirmatory contrast that still loses a pair to a treatment attempt's `None` is refused. For a rare attempt that genuinely could not be measured, the contrast may preregister `max_unscored_treatment = N` (0 unless set; only on a contrast in the Holm family that declares `"scored"`; it cannot excuse every treatment attempt of the contrast), and the report shows how many pairs that cost;
- `treatment_outcomes = "excluded"`: every pair with a treatment attempt is left out of that contrast, whatever the scorer returned, and the report names the contrast and how many pairs it left out beside its result. A confirmatory contrast this leaves with no pair is refused, since its conclusion would read as no effect detected.

An exploratory contrast reports its drops and is not refused. The example's confirmatory contrast on the treatment condition declares its choice:

```toml
# my-study/preregistration.toml
[contrasts.relay-vs-baseline]
treatment_outcomes = "scored"
```

Here is a scorer applied to the treatment attempt:

```python
from expcore.records.record import load_record, record_filename
from expcore.records.rescore import rescore

export = json.loads(Path("my-study/out/export.json").read_text())
treated = [e for e in export["analysed_attempts"] if e["treatment"]]
assert {(e["condition"], e["infra_kind"]) for e in treated} == {("relay-down", "knowledge")}
entry = treated[0]
record = load_record(Path("my-study/out/runs") / entry["run_dir"] / record_filename(entry["run_key"], entry["attempt"]))


class LookupCount:
    """How many lookups the agent made, under the declared treatment as well: the lookups before the failure count."""

    name = "lookups"
    version = "1"

    def score(self, record):
        if record.outcome["name"] == "infrastructure" and not record.outcome["treatment"]:
            # set aside and run again: the core never scores it, and nothing the agent did can be judged
            return {"verdict": "not_determined", "outcomes": {"lookups": None}}
        looked = [call for call in record.tool_calls if call["name"] == "lookup"]
        return {"verdict": "fulfilled", "outcomes": {"lookups": float(len(looked))}}


scored = rescore(record, [LookupCount()])
assert scored.scores["lookups"] == {
    "version": "1",
    "result": {"verdict": "fulfilled", "outcomes": {"lookups": 1.0}},  # one lookup, then the tool server went down
}

analysis = json.loads(Path("my-study/out/analysis.json").read_text())
relay = next(c for c in analysis["contrasts"] if c["name"] == "relay-vs-baseline")
assert relay["treatment_outcomes"] == "scored" and relay["pairs_missing"]["treatment_outcome_none"] == 0
assert relay["n_pairs"] == 8  # both treatment attempts stay in their own contrast
```

### Unknown platform estimates are data

A study on the Onedata platform reads the run-end platform probes from each record's instruments (`platform_probes`). Per tracked provider they give `gained_bytes` (placement gained: the bytes of the blocks the provider's block map shows at run end that it did not show at run start; what it showed at the start and no longer does is `lost_bytes`, kept apart, so it is not a net change, and it is not bytes on the wire), `transfer_bytes_in` (what the transfers in the run window moved into it), and `on_demand_lower_bound`, which is `None` whenever one of its inputs is not established, with each reason in `unknown` (`transfer_not_ended`, `transfer_carried_over`, `listing_not_settled`, ...).

That missingness is informative. An estimate is unknown exactly when, for example, the agent starts a transfer and its run ends first, which is likelier under conditions that encourage placement, so dropping the unknowns would bias a comparison between conditions. Count them per condition and reason over every attempt, set-aside and excluded ones as well as those analysed, and report the counts beside any analysis of the bound; never drop them silently. `gained_bytes` and `transfer_bytes_in` are missing far less often, when a block map or the transfer listing could not be read or a tracked path named another file at the run's end than at its start, but they too can be missing (the split is then `None`), so count those as well, under `no_split`, whichever measure you analyse; or analyse the bound and state its missingness as a limitation. A study without the platform has no probes, and the count is empty:

```python
from collections import Counter


def unknown_on_demand(export, runs):
    """Count the on-demand estimates that are unknown, by condition, provider and reason, over every attempt's record
    under `runs`, analysed, set aside or excluded. An attempt whose probes give no split at all counts once under the
    reason "no_split"."""
    condition_of = {
        entry["run_key"]: entry["condition"]
        for part in ("analysed_attempts", "exhausted_runs", "unfinished_runs", "excluded_attempts")
        for entry in export.get(part, [])
    }
    counts = Counter()
    for path in sorted(runs.glob("*/try*/*.json")):
        if not path.name.split(".")[0].isalnum() or path.name.count(".") != 2:
            continue  # only record files, named <sha256 of the run key>.<attempt>.json
        record = load_record(path)
        entry = {"condition": condition_of.get(record.run_key, "unknown condition")}
        probes = record.instruments.get("platform_probes")
        if probes is None:  # no platform: nothing to count
            continue
        if probes.get("on_demand") is None:
            counts[entry["condition"], None, "no_split"] += 1
            continue
        for provider, split in probes["on_demand"].items():
            for reason in split["unknown"] or ():
                counts[entry["condition"], provider, reason] += 1
    return counts


counts = unknown_on_demand(export, Path("my-study/out/runs"))
for (condition, provider, reason), n in sorted(counts.items(), key=str):
    print(condition, provider, reason, n)
records = [p for p in Path("my-study/out/runs").glob("*/try*/*.json") if p.name.count(".") == 2]
assert records and all(load_record(p).run_key for p in records)  # every attempt's record is read
assert counts == Counter()  # the example study runs without the platform
```

## 6. What a conclusion may say

Every contrast ends in one of five words, `difference`, `trivial_difference`, `equivalent`, `not_detected` or `untestable`, and never in "no difference". Only `equivalent` may be read as no difference, and only a contrast with an `equivalence_margin` in the preregistration can reach it: the interval at 1 - 2 alpha lies inside plus or minus the margin and the test is not significant. `not_detected` is the absence of evidence, never evidence of absence; without a margin, it is the most a non-significant result can say.

`untestable` means the contrast used no pair, whatever the cause: every partner run exhausted or unfinished, or scored `None` on an ordinary run. Nothing was tested, so it is not evidence of anything, least of all of no effect, and it has no estimate, p-value or minimum detectable effect. Its row in the report gives the causes of its missing pairs with their counts. In the Holm family it stays a member as p = 1, so the family keeps its size and the other members are adjusted as if it were there; it never rejects. An exploratory contrast can be untestable too.

A contrast outside the preregistered Holm family is exploratory. It is judged by its raw p-value, nothing controls its multiplicity, and every table and report shows it apart, labelled exploratory, never as confirmatory. Only the Holm family carries the study's confirmatory claims.

A contrast that declares `treatment_outcomes = "excluded"` compares its arms without the runs the outage ended, so its conclusion says nothing about the outage itself, and `not_detected` there is not the absence of an outage effect. The report puts `excluded` and the pairs it left out in the contrast's own row, beside its conclusion.

```python
from expcore.stats.analysis import CONCLUSIONS, conclude

assert CONCLUSIONS == ("difference", "trivial_difference", "equivalent", "not_detected", "untestable")
assert conclude(significant=False, equivalent=True) == "equivalent"  # needs a margin: without one, equivalent is False
assert conclude(significant=False, equivalent=False) == "not_detected"
assert conclude(significant=True, equivalent=True) == "trivial_difference"
assert conclude(significant=False, equivalent=False, untestable=True) == "untestable"  # no pair: nothing was tested

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

The build refuses a check made under code that is not a clean commit, outputs made by another core version, a check made under another environment than the build's, a file the package's check needs and the list leaves out, and any path or content that matches a name private everywhere in the core's study-token hashes (`--hashes` takes another hash file). Before it writes the archive it runs the package's own check on the staged files: every file against `SHA256SUMS`, the environment against the check's, then `export.json`, `scores.json`, `analysis.json` and `report.md` derived anew from the records and compared byte for byte. That check is what `make -f Makefile check` runs for a reader; naming the Makefile matters, because the package may hold no other file make would read first, and the build refuses one, but a reader's copy could. The archive is deterministic: two builds of one tree are the same bytes.

### What a reviewer runs

A reviewer needs only the package, `make` and Python. `wheels/` holds one wheel per distribution of the environment the records were checked under: the core, what `[study] requires` names, and everything they require. `make -f Makefile check` makes a fresh environment beside the package (`<package>.venv`, or `VENV=...`), installs those wheels from `wheels/` alone, with no index and no network, each pinned by its SHA-256 in `wheels/requirements.txt`, then verifies every file, the environment and the outputs. pip runs isolated from the reader's environment variables and configuration files, so neither can add a requirement or a URL. The wheels are the ones of the build's platform and Python, so the reviewer needs the same; `make check` says so if the Python differs. `make -f Makefile check-installed PYTHON=...` runs the same check with an interpreter that already holds the environment. The example's package grows by about 65 MB (52 MB of it NumPy and SciPy), and a study on a heavier stack by what that stack weighs.

`PACKAGE.json` records what the package was built from: the study's commit and tree hash, the core version, `environment` (Python, every distribution's version and, for an install from a URL, its source, which is how a reader learns where the core came from), `wheels` (each with its SHA-256, how deep its text was read, the members that are not text with their hashes, and, for a wheel you supplied, the members there was nothing installed to compare with), `study_tokens_exempt` (the wheels the study-token list was not applied to, each with the hash your `uv.lock` records for it) and `uncommitted`.

The wheels are made at package time. A distribution is packed again from its installed files, each checked against its `RECORD`. An editable install cannot be, since its installed files only point at a checkout: build its wheel (`uv build --wheel --out-dir DIR`) and pass `--wheels DIR`. A wheel given there goes in byte for byte once its code is shown to be what is installed. For an installed distribution, each member must equal the installed file its `RECORD` names, byte for byte (the hash `RECORD` records is not trusted, since an installed file can be edited after the install), and every installed file must be in the wheel. For an editable install, each member must equal the checkout's file it was built from: below a directory its `.pth` puts on the path, where its top-level package imports from (which finds a src layout behind an import hook), or through the wheel mapping your `pyproject.toml` declares (hatch's `sources` and `force-include`; with none declared, the member's path below the project with its top-level package stripped). Every Python file the install imports must be in the wheel, a Python member found nowhere is refused, and a data member found nowhere goes in, listed in `PACKAGE.json`. A wheel of older code under the same version is refused, naming the member. An editable checkout is also part of the environment by a digest of its Python source, so editing it changes the environment like a new version would.

Every wheel is opened, whatever it was installed from. Each text member (Python source, `METADATA`, `RECORD`, entry points, licences) is read with the study-token list, and in an anonymised build with the terms of the anonymisation list and the identity it must not hold; a member that is not text (a compiled extension, a scientific library's test data) is attested by its hash and named in `PACKAGE.json`. Your own code (the core, a wheel from a URL or a directory, a wheel you supplied) is read with every view the checks use for any file; a release from an index is read as it is, line by line, since its text runs to tens of megabytes.

A public library's code can hold a word that the study-token list also holds, so that list applies by provenance. A wheel is a public release, read without the study-token list, only when its SHA-256 equals a hash your `uv.lock` records for a wheel of that name and version from the pypi.org registry; a matching name and version are not enough. The lock is the nearest `uv.lock` from the study directory up to the top of its repository (only the study's own directory for a study outside a repository); with none, one that cannot be read (the build warns, naming it, and refuses nothing for it), or a package it does not hold, every wheel gets the list. A wheel the build packs again from installed files never has the published artifact's bytes, so it is never exempt: to exempt a release, put its published wheel (the file whose URL and hash the lock records) in the `--wheels` directory, where it goes in byte for byte once its code is shown to be what is installed. The core's own wheel, and the wheel of anything installed from a URL, a repository or a directory, editable or not, always get the list. The anonymisation list and the identity apply to every wheel, always. Any other member that holds a listed word makes the build name it and print the `[[opaque]]` entry (`path = "wheels/<wheel file>/<member>"` and its SHA-256) that attests it once you have read it.

### A double-blind submission

`--anonymise LIST` takes an anonymisation list, kept outside the study's repository and the output. It names the prose and metadata files to rewrite (`files = [...]`) and each term with its label (`[terms]`). A term is replaced only as a whole word, only in those files: JSON lexically, one string at a time, so every other byte, numbers included, stays as it was; TOML and text as text. YAML cannot be named, because without a YAML parser its rewrite cannot be checked: leave it out, or include an anonymised copy from another path. A term found anywhere else, code and records included, fails the build naming the file and the line, never the term, and so does one that an HTML entity, a percent-escape, a backslash escape or a line continuation hid from the rewrite. Code is never rewritten.

The same option gives the package a code identity by tree hash alone, because a commit id against a public repository is a lookup key to its authors, and so is a source URL. Every commit of the study (in the records, the replay check, the code stamp, `analysis.json`, the report and `PACKAGE.json`) becomes the first 40 hexadecimal digits of the tree hash of the same code, which a reader can recompute; `code_identity` in `PACKAGE.json` says so. The environment keeps names and versions and loses every source, so the core's commit and URL go too, and so does an editable checkout's digest; a distribution with a local version label (`1.2+g1a2b3c`) is refused, since a label can spell a commit, and so is a wheel that holds a `direct_url.json`. The provenance header at the top of each vendored module, which names the earlier repository the code was extracted from with its commit, is replaced by one neutral comment line, and the build checks that each rewritten module has the same syntax tree as before. No seal covers a record's code block, so the seals stay valid; the build makes the replay check again over the rewritten records, and the tables must be exactly yours with the commit replaced, so `make check` regenerates them byte for byte. The build holds every record to the bytes the original check hashed before it rewrites anything. The real commits stay in your local campaign. Then every file of the assembled package, the wheels included, is searched for each commit of the study's repository, the records, the environment and the withheld headers, for each source URL and repository remote, and for each repository name the headers held, and a hit fails the build naming the file and the line, never the value.

An anonymised package still shows the distribution name `papers-experiments-core` and the module name `expcore`, in its wheel, its environment and the command its Makefile runs, and the core's code, which the reviewer has to run. If the core is public under your name, those are lookup keys no build removes: check what your venue allows.


### What the checks read

What these checks are for: they catch accidental leaks of private names and listed terms, in text and in its common encodings (UTF-16, escapes, entities, base64 and hex runs, compressed and archived content, SQLite databases). They do not defend against someone who sets out to hide a name from them, and they never read text inside images, so a figure is your responsibility. The study-token exemption trusts your own `uv.lock`, so a hash forged there to carry a private name past the check is deliberate concealment, which is outside this threat model. A wheel exempt as a published release is read against the anonymisation list as literal text, without decoding escapes or encodings, so a dependency you published yourselves should not rely on that: supply a wheel you build from its checkout, which the lock does not record and the checks therefore read with every view as your own code, or check its text yourself.

The token and term checks fail closed: a file goes in only if they can read all of it. That is text (UTF-8 without NUL bytes, or UTF-16 with a byte-order mark), an SQLite database, or a gzip, bzip2, xz, zip or tar archive of such files, opened down to its last member. In text, every base64 or hex run of 16 characters or more, a `data:` URI's included, is decoded and read the same way (a run that only looks encoded, a digest for instance, refuses nothing), and in a database every value is read, each BLOB as a file of its own. Anything else is refused at the first such stream, naming the file and what was found in it: a PDF or PostScript file (even one in plain ASCII), a PNG, a raw zlib stream, unknown binary, an archive member that fails to decompress. A path that must not be shown is named by its place in the package's sorted file list and the start of its SHA-256. A file you have inspected by hand, a figure for instance, can go in unscanned if `package.toml` attests that exact file by its SHA-256 (`sha256sum figures/flow.pdf`):

```toml
# an [[opaque]] entry of package.toml
[[opaque]]
path = "figures/flow.pdf"
sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
reason = "a figure drawn by hand; read page by page, no name in it"
```

The build checks the hash, refuses the file if its bytes changed, and lists every attestation in `PACKAGE.json`, so a reader sees which files rest on the author's word rather than on the checks.

### Warnings

The code identity vouches for the code, and the replay check and the regeneration for what is under `out/`. Any other file the package includes (a README, `package.toml`, a figure) has only the commit it came from behind it, so the build warns about each included file no commit holds and lists it in `PACKAGE.json` under `uncommitted`: `untracked` if git does not track it, ignored or not, and `modified` if it differs from HEAD. An anonymised package names no commit, so the build refuses such a file unless you pass `--allow-uncommitted`, and then it is listed like any other. A study outside any repository (a stamped snapshot) has no commit to compare with: the build says so, `vcs` in `PACKAGE.json` is `none`, and nothing is refused.

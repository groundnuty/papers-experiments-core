"""Rehearse a sharded campaign on the live federation: step 3 of the rehearsal of #154.

    uv run python deploy/compose-federation/rehearse_shards.py --out DIR [--layout PATH]

The federation must be up (`federation.py up`, with the same --layout) and is taken down by you afterwards: this script
never runs `up` or `down`. Run it outside any sandbox, as `federation.py` is run. It prints no secret. It writes only
below DIR and, through `federation.py reset --clean` and the platform layer's provisioner, into the federation's own
study space; DIR must be empty or new, and may not lie inside the federation's root, which `down` empties. A
`reset --clean` brings every space of the federation back to its baseline, so run it on a federation whose ingested data
you do not need.

The campaign is the example study (stub models), run as 2 shards, one after the other, each in a directory of its own
as on two hosts. The federation is the environment the shards record the identity of: before each shard starts, the
script ingests a small dataset into the study space through the platform layer, reads every file back, and takes the
identity from what was read back: the platform's configuration (`PlatformConfig.fingerprint()`), the layout file, and
the ingest manifest of the study space (each file's name, size and SHA-256). The study's plugin hands that identity to
the core when the shard starts (`StudyPlugin.environment_identity`); here it reads the file the script wrote a moment
before, since the verified read-back is the script's.

The steps, each printed as it begins:

  1. Check that the federation is up, and ingest the dataset; read it back.
  2. Run shard 1/2 (`run --shard 1/2 --shard-label shard-1`), then `check` it.
  3. `federation.py reset --clean`: the ingested files are gone. Ingest the same data again, and read it back: its
     identity must be the first one.
  4. Run shard 2/2, then `check` it.
  5. Merge the two shards. The merge must accept, and `check` on the merged batch must pass.
  6. `reset --clean`, then ingest the dataset with one file changed by one byte: its identity must differ from the
     first, in the study space's ingest manifest only. Run shard 2/2 again, in a directory of its own, and `check` it.
  7. Merge shard 1 with that shard 2. The merge must refuse, naming the shard and the environment identity.
  8. `reset --clean` once more, so the study space ends at its baseline.

Exit status: 0 the merge accepted the first pair and refused the second; 1 a step did not end as it must, or the
federation or the platform answered with a failure (the message names the step); 2 a usage or configuration error.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import federation
from fedlayout import Layout
from fedspec import LayoutError, Spec, load_spec

from expcore.campaign.shard import check_identity
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.example import write_example
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement

SHARDS = 2
COMMIT = "1" * 40  # the stamped commit of every study copy: one value, so every copy has one code identity
UNIT_CAP = 2  # the example's unit caps are 1, which two shards cannot share
IDENTITY_FILE_VARIABLE = "REHEARSAL_IDENTITY_FILE"
PLUGIN = "rehearsal_plugin.py"
CLEAN_WAIT_S = 90.0
CLEAN_POLL_S = 3.0
DATASET_NAMES = tuple(f"rehearsal-data-{n}.txt" for n in range(1, 5))
PLANTED = DATASET_NAMES[2]  # the file whose first byte a re-ingest changes
DATASET_BYTES = 4096

WRAPPER = '''"""The example's plugin, with the environment identity the rehearsal verified just before the shard."""

import dataclasses
import json
import os
from pathlib import Path

from .plugin import build as example_build


def read_identity():
    return json.loads(Path(os.environ["REHEARSAL_IDENTITY_FILE"]).read_text(encoding="utf-8"))


def build(context):
    return dataclasses.replace(example_build(context), environment_identity=read_identity)
'''


class Failed(RuntimeError):
    """A step of the rehearsal did not end as it must; the message says which and why, and holds no secret."""


# What a step may end with: a refusal of ours, the federation's or the platform layer's (RuntimeError, which
# PlatformError and ProvisionError are), a file or a timeout (OSError), a configuration, or an identity of a wrong form.
EXPECTED = (RuntimeError, OSError, ConfigError, ValueError)


# -- what the rehearsal ingests and what identifies it ---------------------------------------------------------------


def dataset(planted: str | None = None) -> dict[str, bytes]:
    """The files the rehearsal ingests, by name: deterministic bytes, so a re-ingest writes the same files. `planted`
    names one file whose first byte is changed, and nothing else about it."""
    files = {}
    for name in DATASET_NAMES:
        data = bytearray()
        counter = 0
        while len(data) < DATASET_BYTES:
            data += hashlib.sha256(f"{name}:{counter}".encode()).digest()
            counter += 1
        files[name] = bytes(data[:DATASET_BYTES])
    if planted is not None:
        if planted not in files:
            raise ValueError(f"{planted!r} is not a file of the dataset")
        files[planted] = bytes([files[planted][0] ^ 0xFF]) + files[planted][1:]
    return files


def digest_of(value: Any) -> str:
    """The SHA-256 of a JSON value written canonically."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def manifest_of(read_back: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    """The ingest manifest of the files as they were read back: each file's size and SHA-256."""
    return {
        name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(read_back.items())
    }


def ingest_name(spec: Spec) -> str:
    """The name of the study space's ingest manifest in an environment identity."""
    return f"ingest-{spec.study_space}"


def environment_identity(fingerprint: Mapping[str, Any], spec: Spec, manifest: Mapping[str, Any]) -> dict[str, str]:
    """Names mapped to SHA-256 digests only: the platform's configuration, the layout file and the study space's ingest
    manifest. Nothing in it names a host, a space or a path."""
    return check_identity(
        {
            "platform": digest_of(fingerprint),
            "layout": spec.digest,
            ingest_name(spec): digest_of(manifest),
        }
    )


# -- the study copies ------------------------------------------------------------------------------------------------


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise Failed(f"{path.name}: expected {old!r} once, found it {text.count(old)} times")
    path.write_text(text.replace(old, new), encoding="utf-8")


def make_study(destination: Path) -> Path:
    """The example, written into a new directory, stamped, with caps two shards can share and a plugin that hands the
    core the environment identity the rehearsal wrote. Every copy is made alike, so every shard has one fingerprint."""
    write_example(destination)
    for model in ("alpha-stub", "beta-stub"):
        replace_once(destination / "campaign.toml", f"{model} = 1\n", f"{model} = {UNIT_CAP}\n")
    (destination / PLUGIN).write_text(WRAPPER, encoding="utf-8")
    replace_once(destination / "study.toml", 'plugin = "plugin.py"', f'plugin = "{PLUGIN}"')
    replace_once(destination / "study.toml", '"plugin.py", "prompt.txt"]', f'"plugin.py", "{PLUGIN}", "prompt.txt"]')
    write_stamp(destination, COMMIT, load_study(destination).scope)
    return destination


# -- the rehearsal ---------------------------------------------------------------------------------------------------


class Rehearsal:
    """The federation, a directory of our own, and the steps."""

    def __init__(self, layout: Layout, layout_args: Sequence[str], out: Path) -> None:
        self.layout, self.layout_args, self.out = layout, list(layout_args), out
        self.spec = layout.spec
        self.dirty = False  # whether a step may have left the dataset in the study space

    # the federation and the platform layer

    def connect(self) -> tuple[PlatformConfig, OnezoneClient, Provisioner]:
        config = load_platform(self.layout.platform)
        placement = load_placement(self.layout.platform, config)
        client = OnezoneClient(config)
        provisioner = Provisioner(
            client,
            config,
            placement.settings,
            provider_ids=resolve_provider_ids(config),
            log=self.out / "ingest-changes.jsonl",
        )
        return config, client, provisioner

    def require_absent(self, provisioner: Provisioner) -> None:
        """Wait, bounded, until the study space's root holds none of the dataset's files."""
        began = time.monotonic()
        while True:
            held = sorted(set(DATASET_NAMES) & set(provisioner.children(provisioner.root())))
            if not held:
                return
            if time.monotonic() - began > CLEAN_WAIT_S:
                raise Failed(f"the study space still holds {len(held)} file(s) of the dataset: {', '.join(held)}")
            time.sleep(CLEAN_POLL_S)

    def ingest(self, planted: str | None = None) -> dict[str, str]:
        """Write the dataset through the data provider, read every file back whole, and return the identity of the
        environment as the read-back shows it."""
        config, client, provisioner = self.connect()
        try:
            self.require_absent(provisioner)
            self.dirty = True
            holder, read_back = provisioner.settings.holder, {}
            for name, data in dataset(planted).items():
                file_id = provisioner.put_file(name, data)
                read = provisioner.fetch(holder, file_id, 0, len(data))
                if read != data:
                    raise Failed(f"{name}: the bytes read back are not the bytes written")
                read_back[name] = read
            return environment_identity(config.fingerprint(), self.spec, manifest_of(read_back))
        finally:
            client.close()

    def reset(self) -> None:
        """`federation.py reset --clean`, then wait until the study space holds none of the dataset."""
        code = federation.main([*self.layout_args, "reset", "--clean"])
        if code != 0:
            raise Failed(f"federation.py reset --clean ended with status {code}")
        _, client, provisioner = self.connect()
        try:
            self.require_absent(provisioner)
        finally:
            client.close()
        self.dirty = False

    # the shards and the merge

    def expcore(self, *argv: str, identity_file: Path | None = None) -> tuple[int, str, str]:
        """Run `python -m expcore` with these arguments; return its status, standard output and standard error."""
        env = dict(os.environ)
        if identity_file is not None:
            env[IDENTITY_FILE_VARIABLE] = str(identity_file)
        done = subprocess.run([sys.executable, "-m", "expcore", *argv], env=env, capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr

    def require_ok(self, step: str, result: tuple[int, str, str]) -> None:
        code, _, errors = result
        if code != 0:
            raise Failed(f"{step} ended with status {code}: {errors.strip()}")
        print(f"    {step}: ok", flush=True)

    def shard(self, index: int, directory: str, identity: Mapping[str, str]) -> Path:
        """Run shard `index` of 2 in `directory` below the out directory, then check it; return its `out/`."""
        where = self.out / directory
        study = make_study(where / "study")
        identity_file = where / "environment-identity.json"
        identity_file.write_text(json.dumps(dict(identity), sort_keys=True), encoding="utf-8")
        shard = ["--shard", f"{index}/{SHARDS}", "--shard-label", f"shard-{index}"]
        self.require_ok(f"run {' '.join(shard)}", self.expcore("run", str(study), *shard, identity_file=identity_file))
        self.require_ok("check", self.expcore("check", str(study / "out" / "runs")))
        return study / "out"

    def merge(self, directory: str, shards: Sequence[Path]) -> tuple[int, str, Path]:
        """Merge the shards' `out/` directories into the batch of a study copy of its own; return the merge's status,
        its standard error and the copy."""
        study = make_study(self.out / directory / "study")
        code, _, errors = self.expcore("merge", str(study / "out"), *map(str, shards))
        return code, errors, study

    # the rehearsal

    def run(self) -> None:
        federation.require_up(self.layout)
        name = ingest_name(self.spec)
        print("1. ingest the dataset and read it back", flush=True)
        first = self.ingest()
        print("2. shard 1/2", flush=True)
        shard_1 = self.shard(1, "shard-1", first)
        print("3. reset --clean, then ingest the same data again", flush=True)
        self.reset()
        again = self.ingest()
        if again != first:
            raise Failed("the same data, ingested again after reset --clean, has another environment identity")
        print("    the identity is the first one", flush=True)
        print("4. shard 2/2", flush=True)
        shard_2 = self.shard(2, "shard-2", again)
        print("5. merge the two shards: it must accept", flush=True)
        code, errors, merged = self.merge("merged", [shard_1, shard_2])
        if code != 0:
            raise Failed(f"the merge of two shards of one environment was refused ({code}): {errors.strip()}")
        self.require_ok("check on the merged batch", self.expcore("check", str(merged / "out" / "runs")))
        print("6. reset --clean, ingest with one changed file, shard 2/2 again", flush=True)
        self.reset()
        planted = self.ingest(planted=PLANTED)
        differing = sorted(key for key in first if first[key] != planted.get(key))
        if differing != [name]:
            raise Failed(f"the changed file must change the ingest manifest's digest alone, not {differing}")
        print(f"    the identity differs in {name} alone", flush=True)
        other = self.shard(2, "shard-2-changed", planted)
        print("7. merge shard 1 with that shard 2: it must refuse", flush=True)
        code, errors, _ = self.merge("refused", [shard_1, other])
        message = errors.strip()
        # The merge's words (campaign.merge): "shard 2 (shard-2): ran against another environment than shard 1
        # (shard-1); its environment identity differs in <name>", and the name is the study space's ingest manifest.
        if code != 1 or "ran against another environment" not in message or f"differs in {name}" not in message:
            raise Failed(f"the merge had to refuse for the environment identity; it ended {code}: {message}")
        if not any(f"shard-{i}" in message for i in range(1, SHARDS + 1)):
            raise Failed(f"the merge's refusal names no shard: {message}")
        print(f"    refused: {message}", flush=True)

    def finish(self) -> None:
        """Step 8: leave the study space at its baseline, when any step may have left the dataset in it."""
        if self.dirty:
            print("8. reset --clean, so the study space ends at its baseline", flush=True)
            self.reset()


def check_out(out: Path, layout: Layout) -> Path:
    """The out directory, resolved; it must be new or empty and lie outside the federation's root."""
    resolved = out.resolve()
    if layout.root in resolved.parents or resolved == layout.root:
        raise ValueError("--out lies inside the federation's root, which down empties; give a directory outside it")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise ValueError("--out must be a new directory or an empty one")
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rehearse_shards.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="a new or empty directory outside the federation's root"
    )
    parser.add_argument(
        "--layout", type=Path, help="the layout of the federation that is up (default: federation.toml)"
    )
    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.layout)
        layout = Layout.from_environment(spec=spec)  # before anything is touched: a root elsewhere is refused
        out = check_out(args.out, layout)
    except (LayoutError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    layout_args = ["--layout", str(args.layout)] if args.layout is not None else []
    rehearsal = Rehearsal(layout, layout_args, out)
    status = 0
    try:
        rehearsal.run()
    except EXPECTED as error:
        print(f"error: the rehearsal failed: {type(error).__name__}: {error}", file=sys.stderr)
        status = 1
    try:
        rehearsal.finish()
    except EXPECTED as error:
        print(
            f"error: the study space was not brought back to its baseline: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        status = 1
    if status == 0:
        print("rehearsal passed: the merge accepted the shards of one environment and refused the changed one")
    return status


if __name__ == "__main__":
    sys.exit(main())

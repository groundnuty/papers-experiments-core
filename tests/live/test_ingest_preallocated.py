"""The preallocated progressive ingest (O44, #107) against a real deployment, for the next live slot.

    EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml \
        uv run pytest tests/live/test_ingest_preallocated.py

It works under a fresh scratch directory of the first study space (`expcore-live-<hex>`), which it removes at the end;
nothing else of the space is read or changed. `EXPCORE_LIVE_FACTS_OUT` names a file the instrument's snapshot is
written to, and `<it>.extension.json` the first test's readings, as JSON: providers by position, files by label, never
a name or a path. Without the variables nothing is built or contacted.

What the fake can only assume, and the source and the 10 Nov measurement say: that a file made and extended by an empty
write at its final size, before any byte of it is written, shows that size with no block anywhere; that its prefix
written at offset 0 is all the block map holds; that a read of the gap through the data provider and through every
other provider answers zeros and places no block; and that the instrument, driven as a study drives it, sets that
start state, writes its parts from the first turn with each confirmed on the block maps, and completes the file in
place at the reset, its id kept.
"""

import json
import os
import secrets
import time
from pathlib import Path

import pytest

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
if not PLATFORM or os.environ.get("EXPCORE_LIVE_MUTATE") != "1":
    pytest.skip(
        "EXPCORE_LIVE_PLATFORM and EXPCORE_LIVE_MUTATE=1 are not both set; this test writes into a live study space",
        allow_module_level=True,
    )

from expcore.config.loading import ConfigError  # noqa: E402 -- only once the module is known to run
from expcore_onedata import consistency  # noqa: E402
from expcore_onedata.client import OnezoneClient, PlatformError, answered_absent  # noqa: E402
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.ingest import ProgressiveIngest  # noqa: E402
from expcore_onedata.ingestplan import load_ingest_plan  # noqa: E402
from expcore_onedata.provision import Provisioner, resolve_provider_ids  # noqa: E402
from expcore_onedata.rangemap import load_placement  # noqa: E402
from expcore_onedata.streamed import digest_of  # noqa: E402

FACTS_OUT = os.environ.get("EXPCORE_LIVE_FACTS_OUT")
FINAL = (2 << 20) + 4321  # ends inside a chunk
PREFIX = (512 << 10) + 7
CONTENT = bytes((i * 7 + 3) % 251 + 1 for i in range(FINAL))  # no zero byte, so a zero read back is not data
GAP = (FINAL - 8192, 4096)  # a range of the gap: offset, length


@pytest.fixture(scope="module")
def platform():
    config = load_platform(PLATFORM)
    try:
        settings = load_placement(PLATFORM, config).settings
    except ConfigError:
        pytest.skip("the preallocated ingest needs a [placement] table, which platform.toml does not have")
    client = OnezoneClient(config)
    yield config, client, settings
    client.close()


def provisioner(platform, log: Path) -> Provisioner:
    config, client, settings = platform
    return Provisioner(client, config, settings, provider_ids=resolve_provider_ids(config), log=log)


def blocks_by_position(p: Provisioner, file_id: str) -> dict[str, list[list[int]]]:
    """Each provider's blocks of the file, as the data provider's block map shows them, by position."""
    return {
        p.client.provider_label(name): [list(b) for b in p.blocks(file_id, name, reader=p.settings.holder)]
        for name in p.space.support.providers
    }


def remove_scratch(seed: Provisioner, directory: str | None) -> list[str]:
    """Delete the scratch directory with everything under it (an ingest's own files among them, which its claimed log
    keeps from any teardown), then tear the seeding provisioner down; return what was left."""
    if directory is not None:
        try:
            found = seed.resolve(directory)
            seed.mutate("DELETE", f"/data/{found.file_id}", provider=seed.settings.holder)
        except KeyError:
            pass  # never made
        except PlatformError as error:
            if not answered_absent(error):
                raise
    return [entry["reason"] for entry in seed.teardown().left]


def test_o44_an_empty_write_extends_a_new_file_and_the_gap_reads_as_zeros_placing_nothing(platform, tmp_path):
    """O44: create, extend by an empty write at the final size (the size reads back, no block anywhere), write the
    prefix at 0 (the data provider holds exactly it), then read the gap through every provider: zeros at its full
    length, and no provider's block map gains a byte."""
    p = provisioner(platform, tmp_path / "changes.jsonl")
    local, readings = tmp_path / "day.bin", {}
    local.write_bytes(CONTENT)
    directory = f"expcore-live-{secrets.token_hex(6)}"
    try:
        p.make_dir(directory)
        file_id = p.preallocate(f"{directory}/day.bin", FINAL)
        assert all(not held for held in blocks_by_position(p, file_id).values())
        assert p.write_range(file_id, local, 0, PREFIX) == digest_of(CONTENT[:PREFIX])
        assert p.read_file(file_id, size=FINAL, stop=PREFIX) == digest_of(CONTENT[:PREFIX])
        before = blocks_by_position(p, file_id)
        readings["prefix_blocks"] = before
        assert before[p.client.provider_label(p.settings.holder)] == [[0, PREFIX]]
        assert all(not held for name, held in before.items() if name != p.client.provider_label(p.settings.holder))
        for reader in p.space.support.providers:
            began = time.monotonic()
            assert p.fetch(reader, file_id, *GAP) == bytes(GAP[1])
            readings[f"gap_read_s {p.client.provider_label(reader)}"] = round(time.monotonic() - began, 3)
        time.sleep(p.settings.poll_s * p.settings.consecutive_polls)
        assert blocks_by_position(p, file_id) == before
    finally:
        left = remove_scratch(p, None)
        if FACTS_OUT:
            Path(FACTS_OUT).with_suffix(".extension.json").write_text(json.dumps(readings, indent=2) + "\n")
    assert not left, left


def test_o44_the_instrument_sets_the_start_state_writes_its_parts_from_the_first_turn_and_completes_in_place(
    platform, tmp_path
):
    """O44: a seeded baseline file is taken to its prefix at its final size (confirmed by the instrument on every
    provider's block map), its two parts arrive after the first turn, and the reset completes it in place: the staged
    bytes, the data provider's map covering it, the id the start made kept, and the post-run check passing."""
    seed = provisioner(platform, tmp_path / "seed.jsonl")
    own = provisioner(platform, tmp_path / "ingest-changes.jsonl")
    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / "day.bin").write_bytes(CONTENT)
    directory, snapshot = f"expcore-live-{secrets.token_hex(6)}", None
    try:
        seed.upload(f"{directory}/day.bin", staging / "day.bin")  # the dataset's own file, not the ingest's
        plan_file = tmp_path / "ingest.toml"
        plan_file.write_text(
            f'[ingest]\nstaging = "staged"\nmode = "preallocated"\n\n[[ingest.files]]\nlabel = "day"\n'
            f'path = "{directory}/day.bin"\nsource = "day.bin"\nat_s = 2\nbaseline = "complete"\nprefix = {PREFIX}\n'
            "parts = 2\nover_s = 4\n",
            encoding="utf-8",
        )
        ingest = ProgressiveIngest(
            own, load_ingest_plan(plan_file, platform[0]), journal=tmp_path / "ingest-journal.jsonl"
        )
        ingest.start({"run_key": "live"})
        arrived = own.resolve(f"{directory}/day.bin")
        assert arrived.size == FINAL
        for provider in own.space.support.providers:
            consistency.wait_visible(own.client, provider, arrived.file_id, size=FINAL)
        ingest.at_first_turn({"clock_s": time.monotonic()})
        deadline = time.monotonic() + 300
        while ingest.progress < 2 and ingest.failure is None and time.monotonic() < deadline:
            time.sleep(0.5)
        snapshot = ingest.finish()
        ingest.check()
        assert snapshot["files"]["day"]["state"] == "arrived"
        assert snapshot["restore"]["files"]["day"]["file_id"] == arrived.file_id
        assert own.read_file(arrived.file_id, size=FINAL) == digest_of(CONTENT)
        assert own.blocks(arrived.file_id, own.settings.holder) == [(0, FINAL)]
    finally:
        left = remove_scratch(seed, directory)
        if FACTS_OUT and snapshot is not None:
            Path(FACTS_OUT).write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    assert not left, left


def test_o44_the_reset_restores_a_mirrors_replica_of_the_file_the_start_made_again(platform, tmp_path):
    """O44: a seeded baseline file replicated to a mirror (a provider of the space other than the data provider),
    declared in the plan's `replicas`: the start removes it with the mirror's replica and makes it again with nothing
    on the mirror, and the reset completes it in place and replicates it to the mirror, whose own block map then covers
    the file the start made, as an outage run reading through the mirror next needs it."""
    seed = provisioner(platform, tmp_path / "seed.jsonl")
    own = provisioner(platform, tmp_path / "ingest-changes.jsonl")
    holder = own.settings.holder
    mirror = next((name for name in own.space.support.providers if name != holder), None)
    if mirror is None:
        pytest.skip("the first study space has no provider but its data provider to hold a replica")
    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / "day.bin").write_bytes(CONTENT)
    directory, snapshot = f"expcore-live-{secrets.token_hex(6)}", None
    try:
        seed.upload(f"{directory}/day.bin", staging / "day.bin")
        seed.replicate(f"{directory}/day.bin", mirror)
        plan_file = tmp_path / "ingest.toml"
        plan_file.write_text(
            f'[ingest]\nstaging = "staged"\nmode = "preallocated"\n\n[[ingest.files]]\nlabel = "day"\n'
            f'path = "{directory}/day.bin"\nsource = "day.bin"\nat_s = 2\nbaseline = "complete"\nprefix = {PREFIX}\n'
            f"replicas = {json.dumps([mirror])}\n",
            encoding="utf-8",
        )
        ingest = ProgressiveIngest(
            own, load_ingest_plan(plan_file, platform[0]), journal=tmp_path / "ingest-journal.jsonl"
        )
        ingest.start({"run_key": "live-mirror"})
        made = own.resolve(f"{directory}/day.bin").file_id
        assert own.blocks(made, mirror) == []
        ingest.at_first_turn({"clock_s": time.monotonic()})
        deadline = time.monotonic() + 300
        while ingest.progress < 1 and ingest.failure is None and time.monotonic() < deadline:
            time.sleep(0.5)
        snapshot = ingest.finish()
        ingest.check()
        assert snapshot["restore"]["files"]["day"]["replicas"] == {own.client.provider_label(mirror): "replicated"}
        assert own.blocks(made, mirror) == [(0, FINAL)]
        assert own.read_file(made, size=FINAL) == digest_of(CONTENT)
    finally:
        left = remove_scratch(seed, directory)
        if FACTS_OUT and snapshot is not None:
            Path(FACTS_OUT).with_suffix(".mirror.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    assert not left, left

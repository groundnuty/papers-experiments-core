"""The federation's two resets: `reset --clean` back to the baseline, `reset --replicas` between runs, and a bare
`reset` refused. The per-run reset runs here against fake provider answers: it keeps every owner's data, evicts only
what mirrors hold, never schedules an eviction on an owner, and believes only the block maps."""

from pathlib import Path

import pytest
from federation_support import EXAMPLE


def file_id(space: str, path: str) -> str:
    return f"{space}:{path}".replace("/", "|")


class FakeProviders:
    """The provider answers the per-run reset reads, for one layout: each space's tree, every file's block map per
    provider, and the transfers it schedules. An eviction of a space's root empties that provider's blocks of every
    file in the space, as a recursive eviction would, unless `evicts` is off."""

    def __init__(self, fed, spec, files: dict[str, dict[str, int]], root: Path) -> None:
        self.layout = fed.layout.Layout(root, spec=spec)
        self.ids = fed.boot.Ids(
            providers={p.name: f"pid-{p.name}" for p in spec.providers},
            spaces={s.name: f"sid-{s.name}" for s in spec.spaces},
        )
        self.children: dict[str, list[dict]] = {}
        self.space_of: dict[str, str] = {}
        self.sizes: dict[str, int] = {}
        self.maps: dict[str, dict[str, list[list[int]]]] = {}
        self.transfers: list[tuple[str, dict]] = []
        self.evicts = True
        self.unlisted: set[str] = set()  # provider ids a distribution answer leaves out
        for space in spec.spaces:
            self.children[f"root-{space.name}"] = []
            for path, size in files.get(space.name, {}).items():
                self.add(space, path, size)

    def add(self, space, path: str, size: int) -> None:
        parent, parts = f"root-{space.name}", path.split("/")
        for depth, name in enumerate(parts[:-1]):
            directory = file_id(space.name, "/".join(parts[: depth + 1]))
            if directory not in self.children:
                self.children[directory] = []
                self.children[parent].append({"file_id": directory, "name": name, "type": "DIR"})
            parent = directory
        fid = file_id(space.name, path)
        self.children[parent].append({"file_id": fid, "name": parts[-1], "type": "REG", "size": size})
        self.space_of[fid], self.sizes[fid] = space.name, size
        self.maps[fid] = {self.ids.providers[name]: [] for name in space.supporters}
        self.maps[fid][self.ids.providers[space.owner]] = [[0, size]] if size else []

    def hold(self, space: str, path: str, provider: str, blocks: list[list[int]]) -> None:
        self.maps[file_id(space, path)][self.ids.providers[provider]] = blocks

    def held(self, space: str, path: str, provider: str) -> list[list[int]]:
        return self.maps[file_id(space, path)][self.ids.providers[provider]]

    def provider(self, service, method: str, path: str, label: str, **options):
        parts = path.split("/")
        if method == "GET" and parts[1] == "spaces":
            return {"fileId": "root-" + parts[2].removeprefix("sid-")}
        if method == "GET" and path.endswith("/children"):
            return {"children": self.children[parts[2]], "isLast": True}
        if method == "GET" and path.endswith("/distribution"):
            listed = {pid: blocks for pid, blocks in self.maps[parts[2]].items() if pid not in self.unlisted}
            return {
                "distributionPerProvider": {
                    pid: {"distributionPerStorageBackend": {"storage": {"blocks": blocks}}}
                    for pid, blocks in listed.items()
                }
            }
        if method == "POST" and path == "/transfers":
            body = options["json"]
            self.transfers.append((service.name, body))
            if self.evicts:
                space = body["fileId"].removeprefix("root-")
                for fid, owner in self.space_of.items():
                    if owner == space:
                        self.maps[fid][body["evictingProviderId"]] = []
            return {"transferId": f"t{len(self.transfers)}"}
        if method == "GET" and parts[1] == "transfers":
            return {"transferStatus": "completed"}
        raise AssertionError(f"unexpected call: {method} {label}")


@pytest.fixture
def quick(fed, monkeypatch):
    import fedreplicas

    monkeypatch.setattr(fedreplicas, "POLL_S", 0.0)
    return fedreplicas


DEFAULT_FILES = {
    "alpha-space": {"hello.txt": 64, "samples/sample-1.bin": 100, "listing/entry-0.txt": 16, "empty.txt": 0},
    "beta-space": {"beta.txt": 64},
}


def test_the_per_run_reset_evicts_every_mirrors_replicas_and_keeps_every_owners_data(fed, quick, tmp_path):
    fake = FakeProviders(fed, fed.spec.load_spec(), DEFAULT_FILES, tmp_path)
    fake.hold("alpha-space", "hello.txt", "birch", [[0, 64]])
    fake.hold("alpha-space", "samples/sample-1.bin", "cedar", [[0, 50]])
    done = quick.evict_replicas(fake, timeout_s=5)
    alpha, beta = done["alpha-space"], done["beta-space"]
    assert (alpha.files, alpha.evicted_from, alpha.held_before) == (4, ("birch", "cedar"), {"birch": 64, "cedar": 50})
    assert alpha.sweeps == quick.CONSECUTIVE
    assert (beta.evicted_from, beta.held_before) == ((), {"cedar": 0})  # nothing held: nothing scheduled
    assert fake.held("alpha-space", "hello.txt", "pine") == [[0, 64]]  # the owner keeps its data
    assert fake.held("alpha-space", "samples/sample-1.bin", "pine") == [[0, 100]]
    assert fake.held("beta-space", "beta.txt", "birch") == [[0, 64]]
    assert fake.held("alpha-space", "hello.txt", "birch") == []
    assert fake.held("alpha-space", "samples/sample-1.bin", "cedar") == []
    for posted_on, body in fake.transfers:
        assert body["type"] == "eviction"
        assert body["evictingProviderId"] == fake.ids.providers[posted_on]  # scheduled on the mirror it empties
        assert posted_on in ("birch", "cedar")
        assert body["fileId"] == "root-alpha-space"
    assert "evicted from birch, cedar" in str(alpha)


def test_a_provider_that_owns_one_space_keeps_it_while_its_mirror_of_another_owners_space_is_emptied(
    fed, quick, tmp_path
):
    """In the example, alder owns gamma-space and mirrors hazel's delta-space: its replica of delta goes, its own data
    of gamma stays, and nothing is ever evicted from an owner."""
    spec = fed.spec.load_spec(EXAMPLE)
    files = {"alpha-space": {"hello.txt": 64}, "gamma-space": {"gamma.txt": 64}, "delta-space": {"delta.txt": 64}}
    fake = FakeProviders(fed, spec, files, tmp_path)
    fake.hold("delta-space", "delta.txt", "alder", [[0, 64]])
    done = quick.evict_replicas(fake, timeout_s=5)
    assert done["delta-space"].evicted_from == ("alder",)
    assert fake.held("delta-space", "delta.txt", "alder") == []
    assert fake.held("delta-space", "delta.txt", "hazel") == [[0, 64]]
    assert fake.held("gamma-space", "gamma.txt", "alder") == [[0, 64]]
    owners = {fake.ids.providers[s.owner] for s in spec.spaces}
    evicted = {body["evictingProviderId"] for _, body in fake.transfers}
    assert evicted == {fake.ids.providers["alder"]}
    assert all(body["fileId"] != "root-gamma-space" for _, body in fake.transfers)
    assert owners >= evicted  # alder is an owner, yet only its mirror's directory was evicted


def test_a_file_not_whole_at_its_owner_refuses_the_reset_before_any_eviction_in_any_space(fed, quick, tmp_path):
    """A file only a mirror holds whole is an original, not a replica: the reset evicts nothing anywhere."""
    fake = FakeProviders(fed, fed.spec.load_spec(), DEFAULT_FILES, tmp_path)
    fake.hold("alpha-space", "hello.txt", "birch", [[0, 64]])
    fake.hold("beta-space", "beta.txt", "birch", [[0, 10]])
    fake.hold("beta-space", "beta.txt", "cedar", [[0, 64]])
    with pytest.raises(RuntimeError, match=r"1 file\(s\) of beta-space are not held whole by its owner birch"):
        quick.evict_replicas(fake, timeout_s=5)
    assert fake.transfers == []
    assert fake.held("alpha-space", "hello.txt", "birch") == [[0, 64]]


def test_an_eviction_reported_ended_that_removed_nothing_fails_on_the_block_maps(fed, quick, tmp_path):
    """Onedata 25.0 reports `completed` for an eviction that removed nothing; only the block map decides."""
    fake = FakeProviders(fed, fed.spec.load_spec(), DEFAULT_FILES, tmp_path)
    fake.hold("alpha-space", "hello.txt", "birch", [[0, 64]])
    fake.evicts = False
    with pytest.raises(RuntimeError, match=r"alpha-space was not verified empty .*birch 64 B"):
        quick.evict_replicas(fake, timeout_s=0.05)


def test_a_block_map_that_leaves_a_mirror_out_is_unknown_never_empty(fed, quick, tmp_path):
    fake = FakeProviders(fed, fed.spec.load_spec(), DEFAULT_FILES, tmp_path)
    fake.unlisted = {fake.ids.providers["cedar"]}
    with pytest.raises(RuntimeError, match=r"[1-9]\d* reading\(s\) unknown"):
        quick.evict_replicas(fake, timeout_s=0.05)
    assert {body["evictingProviderId"] for _, body in fake.transfers} == {fake.ids.providers["cedar"]}


# -- the command line ---------------------------------------------------------------------------------------------


@pytest.fixture
def dispatched(fed, monkeypatch):
    monkeypatch.delenv("EXPCORE_FED_ROOT", raising=False)
    monkeypatch.delenv("EXPCORE_FED_ROOT_PARENT", raising=False)
    called = []
    for name in ("up", "reset_clean", "reset_replicas", "down", "status", "memory"):
        monkeypatch.setattr(fed.cli, name, lambda layout, name=name: called.append(name) or name)
    return called


def test_a_bare_reset_is_refused_naming_both_resets_and_touches_nothing(fed, dispatched, capsys):
    """Emptying the spaces would wipe ingested datasets, so reset never guesses which reset was meant."""
    assert fed.cli.main(["reset"]) == 2
    assert dispatched == []
    err = capsys.readouterr().err
    assert "reset --clean" in err
    assert "reset --replicas" in err
    assert "keeps every owner's data" in err


@pytest.mark.parametrize(
    ("argv", "called"),
    [
        (["reset", "--clean"], "reset_clean"),
        (["reset", "--replicas"], "reset_replicas"),
        (["--clean", "reset"], "reset_clean"),
    ],
)
def test_each_reset_flag_runs_its_own_reset(fed, dispatched, argv, called):
    assert fed.cli.main(argv) == 0
    assert dispatched == [called]


def test_a_reset_flag_with_another_command_or_both_flags_together_is_refused(fed, dispatched, capsys):
    assert fed.cli.main(["down", "--clean"]) == 2
    assert "belong to reset" in capsys.readouterr().err
    with pytest.raises(SystemExit) as refused:
        fed.cli.main(["reset", "--clean", "--replicas"])
    assert refused.value.code == 2
    assert dispatched == []

"""A space declared `baseline = false` (deploy/compose-federation): up writes no baseline file into it, `reset --clean`
leaves it empty, and the facts written after up say so. The writer runs here against an in-memory tree standing in
for the providers, so nothing starts a container."""

import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from federation_support import MINIMAL, STUDY_BASELINE_OFF, WITH_BASELINE_OFF


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "federation.toml"
    path.write_text(text, encoding="utf-8")
    return path


class FakeWriter:
    """The provider calls the baseline writer makes, over an in-memory tree: each space has a root directory, every
    call is recorded, and a write is recorded with the space it went into and the provider it went through."""

    def __init__(self, fed, spec, root: Path) -> None:
        self.layout = fed.layout.Layout(root, spec=spec)
        self.ids = fed.boot.Ids(
            providers={p.name: f"pid-{p.name}" for p in spec.providers},
            spaces={s.name: f"sid-{s.name}" for s in spec.spaces},
        )
        self.rest = self  # the writer reads files back through the Rest it is given
        self.nodes: dict[str, dict] = {}
        self.counter = itertools.count(1)
        self.writes: list[tuple[str, str, str]] = []  # (space, provider, path of what was created)
        self.transfers: list[dict] = []
        for space in spec.spaces:
            self.nodes[f"root-{space.name}"] = {"name": "", "type": "DIR", "parent": None, "space": space.name}

    def admin_token(self) -> str:
        return "token"

    def call(self, method: str, url: str, *, label: str, headers: dict) -> SimpleNamespace:
        assert method == "GET"
        return SimpleNamespace(content=bytes(self.nodes[url.split("/")[-2]]["content"]))

    def ingest(self, space: str, path: str, content: bytes = b"ingested") -> None:
        """Put a file in a space the way a study would, directories on the way as needed."""
        parent = f"root-{space}"
        *directories, name = path.split("/")
        for directory in directories:
            parent = self.child(parent, directory) or self.create(space, parent, directory, "DIR")
        self.nodes[self.create(space, parent, name, "REG")]["content"] = bytearray(content)

    def child(self, parent: str, name: str) -> str | None:
        return next((i for i, n in self.nodes.items() if n["parent"] == parent and n["name"] == name), None)

    def create(self, space: str, parent: str, name: str, kind: str) -> str:
        file_id = f"f{next(self.counter)}"
        self.nodes[file_id] = {"name": name, "type": kind, "parent": parent, "space": space, "content": bytearray()}
        return file_id

    def inside(self, space: str) -> list[str]:
        """Every path in a space, directories included."""
        found = []
        for node in self.nodes.values():
            parts, current = [], node
            while current["parent"] is not None:
                parts.append(current["name"])
                current = self.nodes[current["parent"]]
            if parts and node["space"] == space:
                found.append("/".join(reversed(parts)))
        return sorted(found)

    def remove(self, file_id: str) -> None:
        for child in [i for i, n in self.nodes.items() if n["parent"] == file_id]:
            self.remove(child)
        self.nodes.pop(file_id, None)

    def provider(self, service, method: str, path: str, label: str, **options):
        parts = path.split("/")
        if method == "GET" and parts[1] == "spaces":
            return {"fileId": "root-" + parts[2].removeprefix("sid-")}
        if method == "GET" and path.endswith("/children"):
            children = [
                {"file_id": i, "name": n["name"], "type": n["type"], "size": len(n.get("content", b""))}
                for i, n in list(self.nodes.items())
                if n["parent"] == parts[2]
            ]
            return {"children": children, "isLast": True}
        if method == "POST" and path.endswith("/children"):
            parent = self.nodes[parts[2]]
            name, kind = options["params"]["name"], options["params"]["type"]
            file_id = self.create(parent["space"], parts[2], name, kind)
            self.writes.append((parent["space"], service.name, name))
            return {"fileId": file_id}
        if method == "PUT" and path.endswith("/content"):
            self.nodes[parts[2]]["content"] = bytearray(options["content"])
            return {}
        if method == "DELETE":
            self.remove(parts[2])
            return {}
        if method == "POST" and path == "/transfers":
            self.transfers.append(options["json"])
            return {"transferId": f"t{len(self.transfers)}"}
        if method == "GET" and parts[1] == "transfers":
            return {"transferStatus": "completed"}
        raise AssertionError(f"unexpected call: {method} {label}")


@pytest.fixture
def content(fed):
    import fedcontent

    return fedcontent


def settled(fed, content, tmp_path, text: str):
    """A FakeWriter over the layout, and the writer that works through it."""
    fake = FakeWriter(fed, fed.spec.load_spec(write(tmp_path, text)), tmp_path / "fed")
    return fake, content.Content(fake)


def test_a_space_declared_baseline_false_loads_and_renders_the_same_compose_file_as_one_that_is_not(fed, tmp_path):
    """The key changes what up writes, never what runs: the generated Compose file is the same."""
    off = fed.spec.load_spec(write(tmp_path, WITH_BASELINE_OFF))
    on = fed.spec.load_spec(write(tmp_path, WITH_BASELINE_OFF.replace("baseline = false\n", "")))
    assert [(s.name, s.baseline) for s in off.spaces] == [("alpha-space", True), ("scratch-space", False)]
    assert [s.baseline for s in on.spaces] == [True, True]
    assert fed.compose.render(off) == fed.compose.render(on)


def test_a_space_without_the_key_keeps_its_baseline_as_it_always_did(fed):
    """The default layout and the example declare no key, so every space of both keeps its baseline files."""
    from federation_support import EXAMPLE

    for path in (None, EXAMPLE):
        spec = fed.spec.load_spec(path)
        assert all(space.baseline for space in spec.spaces)
        plan = fed.layout.baseline_plan(spec)
        assert set(plan) == {space.name for space in spec.spaces}
        assert all(plan.values())
        for name, files in plan.items():
            assert files == fed.layout.baseline(name, spec.study_space)


def test_the_plan_gives_a_baseline_off_space_no_file_and_every_other_space_its_files(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, WITH_BASELINE_OFF))
    plan = fed.layout.baseline_plan(spec)
    assert plan["scratch-space"] == ()
    assert plan["alpha-space"] == fed.layout.baseline("alpha-space", "alpha-space")
    study_off = fed.spec.load_spec(write(tmp_path, STUDY_BASELINE_OFF))
    assert study_off.study_space == "alpha-space"
    assert fed.layout.baseline_plan(study_off) == {"alpha-space": ()}


def test_up_writes_nothing_into_a_baseline_off_space_and_reset_clean_leaves_it_empty(fed, content, tmp_path):
    """Up and reset --clean run the same writer. Fresh, it writes the baseline into the space that keeps one and nothing
    into the other; after a study ingested into both, it removes everything from both and writes the baseline into the
    one that keeps it again."""
    fake, writer = settled(fed, content, tmp_path, WITH_BASELINE_OFF)
    plan = fed.layout.baseline_plan(fake.layout.spec)
    wanted = sorted({p for f in plan["alpha-space"] for p in (f.path, *(str(Path(f.path).parent),)) if p != "."})

    writer.reset(timeout_s=5)  # up
    assert fake.inside("scratch-space") == []
    assert {space for space, _, _ in fake.writes} == {"alpha-space"}
    assert fake.inside("alpha-space") == wanted
    wrote = len(fake.writes)

    fake.ingest("scratch-space", "ingested/part-1.txt")
    fake.ingest("scratch-space", "loose.txt")
    fake.ingest("alpha-space", "ingested/part-1.txt")
    removed = writer.reset(timeout_s=5)  # reset --clean
    assert removed["scratch-space"] == 2
    assert removed["alpha-space"] >= 1  # the ingested directory, and the baseline files its writer replaces
    assert fake.inside("scratch-space") == []
    assert fake.inside("alpha-space") == wanted
    assert {space for space, _, _ in fake.writes[wrote:]} <= {"alpha-space"}


def test_a_baseline_off_space_is_written_through_no_provider_at_all(fed, content, tmp_path):
    """Nothing is created, so no provider is asked to: the only calls on the space are its root and its listing."""
    fake, writer = settled(fed, content, tmp_path, WITH_BASELINE_OFF)
    writer.reset(timeout_s=5)
    assert all(space != "scratch-space" for space, _, _ in fake.writes)
    assert {provider for _, provider, _ in fake.writes} == {"oak"}  # alpha-space's owner, never scratch-space's elm


def test_a_baseline_off_study_space_is_emptied_and_gets_no_replica_and_no_transfer(fed, content, tmp_path):
    """The study space has a mirror, but with no baseline it has no file to replicate: the writer neither fails on the
    missing replicated file nor schedules a transfer, and it still removes whatever a study ingested."""
    fake, writer = settled(fed, content, tmp_path, STUDY_BASELINE_OFF)
    writer.reset(timeout_s=5)
    assert fake.inside("alpha-space") == []
    assert fake.writes == []
    assert fake.transfers == []
    fake.ingest("alpha-space", "dataset/a.csv")
    assert writer.reset(timeout_s=5) == {"alpha-space": 1}
    assert fake.inside("alpha-space") == []
    assert fake.writes == fake.transfers == []


def test_a_study_space_that_keeps_its_baseline_still_replicates_its_file_to_the_first_mirror(fed, content, tmp_path):
    fake, writer = settled(fed, content, tmp_path, MINIMAL)
    writer.reset(timeout_s=5)
    assert [t["replicatingProviderId"] for t in fake.transfers] == ["pid-elm"]
    assert [t["type"] for t in fake.transfers] == ["replication"]


def facts_for(fed, spec, tmp_path):
    from fedboot import Ids

    layout = fed.layout.Layout(tmp_path / "fed", spec=spec)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout = layout
    boot.ids = Ids(
        users={p.name: f"uid-{p.name}" for p in spec.principals},
        providers={p.name: f"pid-{p.name}" for p in spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in spec.spaces},
    )
    return fed.facts.facts_of(boot), fed.facts.describe(layout).splitlines()


def test_the_facts_written_after_up_say_which_spaces_hold_no_baseline_and_name_no_file_that_is_not_there(fed, tmp_path):
    facts, rows = facts_for(fed, fed.spec.load_spec(write(tmp_path, WITH_BASELINE_OFF)), tmp_path)
    assert {name: space["baseline"] for name, space in facts["spaces"].items()} == {
        "alpha-space": True,
        "scratch-space": False,
    }
    assert facts["files"] == {"content": "hello.txt", "listing_directory": "listing-1200", "listing_entries": 1200}
    assert "| Space `scratch-space` | owner `elm`, mirrors `oak`, support 10.00 GiB each; no baseline files |" in rows
    assert not any("alpha-space" in row and "no baseline" in row for row in rows)

    study_off, rows = facts_for(fed, fed.spec.load_spec(write(tmp_path, STUDY_BASELINE_OFF)), tmp_path)
    assert study_off["spaces"]["alpha-space"]["baseline"] is False
    assert study_off["files"] is None  # the study space holds neither the content file nor the listing directory
    assert (
        "| Space `alpha-space` (study space) | owner `oak`, mirrors `elm`, support 10.00 GiB each; no baseline files |"
        in (rows)
    )


def test_a_baseline_off_study_space_still_writes_facts_and_the_platform_file_without_a_baseline_file(fed, tmp_path):
    """Up ends by writing facts.json from the ids it holds: it names no file the space does not have."""
    spec = fed.spec.load_spec(write(tmp_path, STUDY_BASELINE_OFF))
    facts, _ = facts_for(fed, spec, tmp_path)
    assert facts["study_space"] == "alpha-space"
    assert facts["data_provider"] == "oak"
    assert facts["files"] is None
    assert fed.facts.placeholders(facts)["space_name"] == "alpha-space"


def facts_file(layout, spaces: dict | str) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    text = spaces if isinstance(spaces, str) else json.dumps({"spaces": spaces})
    layout.facts.write_text(text, encoding="utf-8")


def stale_layout(fed, tmp_path):
    return fed.layout.Layout(tmp_path / "fed", spec=fed.spec.load_spec(write(tmp_path, WITH_BASELINE_OFF)))


@pytest.mark.parametrize(
    ("recorded", "left"),
    [
        ({"alpha-space": {"baseline": True}, "scratch-space": {"baseline": True}}, ["scratch-space"]),
        ({"alpha-space": {"baseline": True}, "scratch-space": {}}, ["scratch-space"]),  # facts from before the key
        ({"alpha-space": {"baseline": True}, "scratch-space": {"baseline": False}}, []),
        ({"alpha-space": {"baseline": True}}, []),  # a space the federation was not brought up with
    ],
    ids=["had files", "recorded no flag", "had none", "not there before"],
)
def test_a_space_brought_up_with_baseline_files_and_now_declared_off_is_found_from_the_facts(
    fed, tmp_path, recorded, left
):
    layout = stale_layout(fed, tmp_path)
    facts_file(layout, recorded)
    assert fed.facts.baseline_left_behind(layout) == left


def test_no_facts_means_no_baseline_files_left_behind_and_unreadable_facts_are_an_error(fed, tmp_path):
    layout = stale_layout(fed, tmp_path)
    assert fed.facts.baseline_left_behind(layout) == []  # a first up: nothing was written yet
    for text in ("not json", "[]", '{"spaces": 3}'):
        facts_file(layout, text)
        with pytest.raises(RuntimeError, match=r"facts\.json cannot be read"):
            fed.facts.baseline_left_behind(layout)


def test_up_on_a_federation_whose_layout_now_declares_a_space_baseline_off_refuses_before_touching_anything(
    fed, tmp_path, monkeypatch
):
    """Up on a federation that is up writes and removes no baseline file, so the files of a space now declared off would
    stay where an agent can read them: it refuses, says reset --clean, and starts nothing."""
    layout = stale_layout(fed, tmp_path)
    facts_file(layout, {"alpha-space": {"baseline": True}, "scratch-space": {"baseline": True}})
    touched = []
    monkeypatch.setattr(fed.cli, "require_images", lambda: None)
    monkeypatch.setattr(fed.cli, "project_containers", lambda project: [])
    monkeypatch.setattr(fed.cli, "project_networks", lambda project: [])
    monkeypatch.setattr(fed.cli, "require_own", lambda *arguments: None)
    for name in ("require_ports_free", "require_memory_fits", "prepare", "bring_up"):
        monkeypatch.setattr(fed.cli, name, lambda *arguments, name=name: touched.append(name))
    with pytest.raises(RuntimeError, match=r"scratch-space.*reset --clean"):
        fed.cli.up(layout)
    assert touched == []

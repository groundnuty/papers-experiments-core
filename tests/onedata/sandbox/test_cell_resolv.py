"""O56: every agent cell reads the same neutral /etc/resolv.conf on both backends, and a run records its hash.

The Docker agent's own `/etc/resolv.conf` is the engine's: it names the engine's embedded resolver and copies the host's
`search` line, so a host's search domain could reach the agent. The sandbox binds the Apptainer backend's file over it,
read-only, the one `cellresolv.RESOLV_TEXT`; the start has a cell read it back and refuses a run whose cell reads
anything else; the run records the hash the cell read, `infrastructure_facts.resolv_conf_sha256`, the same key and
value on both backends. The Apptainer side is in `apptainer/test_cell_resolv_apptainer.py`; the fakes (`fake_host`,
`fake_node`) model what a cell reads: a bound file's text, else the engine's or the node's own.
"""

import hashlib
import itertools
import stat
from pathlib import Path

import pytest
from apptainer_support import make_settings as apptainer_settings
from apptainer_support import make_spec as apptainer_spec
from apptainer_support import start as apptainer_start
from fake_host import ENGINE_RESOLV
from fake_node import FakeNode
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import cellresolv, inputs
from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.api import DataPathMode, SandboxError
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

READ = "print(open('/etc/resolv.conf').read(), end='')"  # what a study's cell would run to read the file
LEVELS = ["via-gate", "mounted", "isolated"]
SHA = hashlib.sha256(cellresolv.RESOLV_TEXT.encode()).hexdigest()


def keywords(text: str) -> list[str]:
    """The first word of each line of a resolv.conf that is not a comment or blank."""
    return [line.split()[0] for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", ";"))]


def start(world, level):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def reading_cells(world) -> None:
    """The fake's cells read what the container holds at the path: the bound file, else the engine's own."""

    def handler(container, argv, stdin):
        if stdin == READ:
            return CmdResult(0, container.file("/etc/resolv.conf") or "")
        return world.host._default_exec(container, argv, stdin)

    world.host.exec_handler = handler


def resolv_mounts(container) -> list[str]:
    return [spec for spec in container.options.get("--mount", []) if "dst=/etc/resolv.conf" in spec.split(",")]


def test_o56_the_neutral_file_names_no_name_server_and_no_search_or_domain_line():
    """O56: nothing in it is a `nameserver`, `search`, `domain` or `sortlist` line: only options, so the file carries no
    detail of a host or a site."""
    assert set(keywords(cellresolv.RESOLV_TEXT)) == {"options"}
    assert cellresolv.RESOLV_SHA256 == SHA


def test_o56_both_backends_take_the_path_and_the_text_from_one_constant():
    """O56: the Apptainer backend's `RESOLV_TEXT` and `RESOLV_CONF` are the shared module's objects, and a study cannot
    bind another file onto the path."""
    assert AV.RESOLV_TEXT is cellresolv.RESOLV_TEXT
    assert AV.RESOLV_CONF is cellresolv.RESOLV_CONF
    assert cellresolv.RESOLV_CONF == "/etc/resolv.conf"
    assert cellresolv.RESOLV_CONF in inputs.TAKEN


@pytest.mark.parametrize(
    ("exit_code", "out", "digest", "refused"),
    [
        (0, f"resolv {SHA}\n", SHA, False),
        (0, f"resolv {'0' * 64}\n", "0" * 64, True),  # a file that is another: its hash is kept, the start is refused
        (1, f"resolv {SHA}\n", None, True),  # a probe that failed reads nothing, whatever it printed
        (0, "", None, True),
        (0, f"resolv {SHA}\nmore\n", None, True),
        (0, "blocked 30\n", None, True),
    ],
)
def test_o56_the_probe_s_answer_is_the_neutral_file_s_hash_or_a_refusal(exit_code, out, digest, refused):
    """O56: only an exit 0 and one line naming the neutral file's hash pass; the problem never holds a file's text."""
    got, problem = cellresolv.judge(exit_code, out)
    assert got == digest
    assert (problem is not None) is refused


def agent_args(world, mode, resolv=Path("/w/resolv.conf")) -> list[str]:
    """`docker run` of the agent of a data path, with the run's copy of the neutral file at `resolv`."""
    s = world.s
    names = C.Names.make(s.name_prefix, "k1-0123abcd")
    extra = {
        DataPathMode.REST: {"gate_ip": "10.1.0.2", "gate_hosts": ["h.test"], "ca": Path("/w/ca.pem"), "zone_host": "z"},
        DataPathMode.MOUNT: {"share": Path("/w/mount")},
        DataPathMode.NONE: {},
    }[mode]
    given = {"share": None, "gate_ip": None, "gate_hosts": [], "ca": None, "trust_bundles": {}, "zone_host": None}
    args, _ = C.agent_run_args(
        s, names, ["--label", "org.test.sandbox=1"], image_id=s.agent.id, mode=mode, work=Path("/w/work"),
        resolv=resolv, **(given | extra),
    )  # fmt: skip
    return args


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_o56_the_agent_binds_the_neutral_resolv_conf_read_only_in_every_data_path(world, mode):
    """O56: `docker run` of the agent has one `--mount type=bind,...,readonly` onto /etc/resolv.conf, never a `-v`, and
    no `--dns` option: the same file in every condition."""
    args = agent_args(world, mode)
    mounted = [args[i + 1] for i, word in enumerate(args) if word == "--mount" and "/etc/resolv.conf" in args[i + 1]]
    assert mounted == ["type=bind,src=/w/resolv.conf,dst=/etc/resolv.conf,readonly"]
    volumes = [value for word, value in itertools.pairwise(args) if word in ("-v", "--volume")]
    assert not [value for value in volumes if "resolv" in value]
    assert not [word for word in args if word.startswith("--dns")]


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_o56_the_resolv_path_changes_one_argument_of_the_agent_s_command_and_no_other(world, mode):
    """O56: nothing else of the container depends on the file: two commands that differ in where the run keeps its copy
    differ in that one bind's source, and the bind adds no other word (the image and the command stay last)."""
    first, second = agent_args(world, mode), agent_args(world, mode, Path("/elsewhere/resolv.conf"))
    assert len(first) == len(second)
    changed = [n for n, (a, b) in enumerate(zip(first, second, strict=True)) if a != b]
    assert len(changed) == 1
    assert first[changed[0]].startswith("type=bind,src=")
    assert first[changed[0] - 1] == "--mount"
    assert first[-3:] == [world.s.agent.id, "sleep", "infinity"]


def test_o56_the_start_binds_the_run_s_own_copy_of_the_neutral_file_into_the_agent(world):
    """O56: the file bound is the run's own copy, 0444 so the agent's user reads it whatever the driver's umask, in the
    run's directory, with exactly the neutral text; the container started holding it."""
    sbx = start(world, "via-gate")
    try:
        agent = world.host.containers[sbx.names.agent]
        (spec,) = resolv_mounts(agent)
        fields = dict(part.split("=", 1) if "=" in part else (part, "") for part in spec.split(","))
        source = Path(fields["src"])
        assert "readonly" in fields
        assert source.parent == sbx.dir
        assert source.read_text(encoding="utf-8") == cellresolv.RESOLV_TEXT
        assert stat.S_IMODE(source.stat().st_mode) == 0o444
        assert agent.options["_files"]["/etc/resolv.conf"] == cellresolv.RESOLV_TEXT
    finally:
        sbx.stop()


@pytest.mark.parametrize("level", LEVELS)
def test_o56_a_cell_of_the_docker_agent_reads_exactly_the_shared_text(world, level):
    """O56: in every data path a cell that reads /etc/resolv.conf gets the shared text: no `nameserver`, no `search` or
    `domain` line, not the engine's own file with the host's `search` line."""
    reading_cells(world)
    sbx = start(world, level)
    try:
        read = sbx.exec(READ, 30).stdout
        assert read == cellresolv.RESOLV_TEXT
        assert read != ENGINE_RESOLV
        assert not {"nameserver", "search", "domain"} & set(keywords(read))
    finally:
        sbx.stop()


@pytest.mark.parametrize("level", LEVELS)
def test_o56_a_docker_run_records_the_hash_a_cell_read(world, level):
    """O56: `infrastructure_facts.resolv_conf_sha256` is the hash of the bytes a cell read at the start, beside the
    node's readings; no text of the file is in the record."""
    sbx = start(world, level)
    try:
        facts = sbx.metadata()["infrastructure_facts"]
        assert facts[cellresolv.FACT] == SHA
        assert "node" in facts
        assert cellresolv.RESOLV_TEXT not in str(sbx.metadata())
    finally:
        sbx.stop()


def test_o56_an_engine_that_shows_its_own_file_refuses_the_start_and_leaves_nothing_behind(world):
    """O56: fail closed. A cell that reads a file other than the neutral one (the engine's, with the host's search
    domain) ends the start and the teardown runs; the message names the problem and holds none of the file."""
    world.host.engine_resolv_wins = True
    with pytest.raises(SandboxError) as raised:
        start(world, "via-gate")
    assert str(raised.value) == cellresolv.REFUSAL
    assert "host-search" not in str(raised.value)
    assert not world.host.containers
    assert not world.host.networks
    assert not world.host.mounts


def test_o56_a_probe_that_cannot_run_also_refuses_the_start(world):
    """O56: no answer is taken for the file: a read that fails, with every other probe answered, refuses the start."""
    host = world.host

    def handler(container, argv, stdin):
        if stdin == cellresolv.PROBE:
            return CmdResult(1, "", "no python here")
        return host._default_exec(container, argv, stdin)

    host.exec_handler = handler
    with pytest.raises(SandboxError, match="could not read"):
        start(world, "via-gate")
    assert not host.containers


def test_o56_both_backends_record_the_same_hash_and_their_cells_read_the_same_text(world, tmp_path):
    """O56: a Docker run and an Apptainer run (the fake node) have a cell read the same bytes and record the same key
    with the same value."""
    reading_cells(world)
    docker = start(world, "via-gate")
    node, there = FakeNode(), tmp_path / "apptainer"
    there.mkdir()
    apptainer = apptainer_start(node, apptainer_settings(there), apptainer_spec("via-gate"))
    try:

        def apptainer_cell(cell):
            return CmdResult(0, node.read_in(cell, "/etc/resolv.conf") or "")

        default = node.cell_handler
        node.cell_handler = lambda cell: apptainer_cell(cell) if cell.program == READ else default(cell)
        read_here, read_there = docker.exec(READ, 30).stdout, apptainer.exec(READ, 30).stdout
        assert read_here == read_there == cellresolv.RESOLV_TEXT
        keys = [box.metadata()["infrastructure_facts"][cellresolv.FACT] for box in (docker, apptainer)]
        assert keys == [SHA, SHA]
        assert hashlib.sha256(read_here.encode()).hexdigest() == keys[0]
    finally:
        docker.stop()
        apptainer.stop()

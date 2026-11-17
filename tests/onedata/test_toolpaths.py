"""O84 (#382): which `[toolserver]` command and arguments name a place relative to the working directory, which the
tool server no longer has (it starts in an empty directory of its own). The rule is lexical: nothing is looked up.
"""

import pytest

from expcore_onedata.toolpaths import relative_argument, relative_command

PYTHON = "/venv/bin/python"


@pytest.mark.parametrize("command", ["./bin/server", "bin/server", "../venv/bin/python", "venv/bin/python", "~/bin/x"])
def test_o84_a_command_with_a_directory_that_is_not_absolute_is_relative(command):
    """O84: a command with a path separator that does not begin at the root resolves against the working directory."""
    assert relative_command(command)


@pytest.mark.parametrize("command", ["/usr/bin/python3", "/opt/venv/bin/onedata-mcp", "python3", "uv", "onedata-mcp"])
def test_o84_an_absolute_command_and_a_bare_name_are_not_relative(command):
    """O84: an absolute path is one place whatever the directory, and a bare name is found on `PATH`
    (docs/interfaces.md, O84: "or find the command on `PATH`"), never in the working directory."""
    assert not relative_command(command)


@pytest.mark.parametrize(
    ("args", "index"),
    [
        (["./server.py"], 0),
        (["../server.py", "one"], 0),
        (["-m", "pkg.mod", "--config", "./x.toml"], 3),
        (["-m", "pkg.mod", "--config=./x.toml"], 2),
        (["-m", "pkg.mod", "--config=../conf/x.toml"], 2),
        (["run", "--project", ".", "onedata-mcp"], 2),
        (["run", "--project", "..", "onedata-mcp"], 2),
        (["--root", "~/data"], 1),
        (["-o=./x"], 0),
        (["--quiet", "./a", "./b"], 1),
    ],
)
def test_o84_an_argument_that_names_a_place_beside_the_working_directory_is_found_by_position(args, index):
    """O84: an argument that is `.`, `..` or begins `./`, `../` or `~/`, whole or as the value of `--option=value`, is a
    path relative to the working directory; the first one is reported by its position, never by its value."""
    assert relative_argument("uv", args) == index


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["-m", "expcore_onedata.testing.fake_onedata_mcp"],
        ["-m", "onedata_mcp.main", "--quiet"],
        ["/srv/study/server.py", "one", "two"],
        ["--relay", "http://127.0.0.1:9"],
        ["--url=https://zone.test/x/../y"],
        ["--config=/etc/server.toml", "--name=a/b"],
        ["--filter", "application/json"],
        ["-c", "import os; os.chdir('./x')"],
        ["-", "--"],
    ],
)
def test_o84_modules_options_urls_and_absolute_paths_are_not_relative_paths(args):
    """O84: nothing else is read as a path: a module, a flag, a URL, an absolute path, a value with a slash that does
    not begin at the working directory (`application/json`), and code given with `-c`."""
    assert relative_argument("uv", args) is None
    assert relative_argument(PYTHON, args) is None


def test_o84_the_words_of_another_program_are_not_paths():
    """O84: `uv run onedata-mcp` names no place: only a Python interpreter's first word is a script."""
    assert relative_argument("uv", ["run", "onedata-mcp"]) is None
    assert relative_argument("/usr/bin/uv", ["run", "--project", "/srv/study", "onedata-mcp"]) is None


@pytest.mark.parametrize(
    ("args", "index"),
    [(["server.py"], 0), (["server.py", "one"], 0), (["tools/server.py"], 0), (["--relay", "x"], None)],
)
def test_o84_the_script_of_a_python_server_is_a_path_by_its_position(args, index):
    """O84: for a Python interpreter, the first argument that is not an option is the script, so `server.py` names a
    place beside the working directory though it has no directory part; any other command's first word is not."""
    assert relative_argument(PYTHON, args) == index
    assert relative_argument("python3.12", args) == index
    assert relative_argument("uv", ["server.py"]) is None


def test_o84_a_python_server_run_by_module_or_with_an_option_first_has_no_script_to_find():
    """O84: `-m <module>` names no script, and an interpreter option first leaves the script's position unknown: only
    the shapes above apply then (the launcher refuses such a command line itself where it is used)."""
    assert relative_argument(PYTHON, ["-m", "server"]) is None
    assert relative_argument(PYTHON, ["-X", "dev", "server.py"]) is None
    assert relative_argument(PYTHON, ["-X", "dev", "-m", "m", "./y"]) == 4

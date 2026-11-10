"""The study guide's code runs as written.

Every fenced block of docs/study-guide.md is taken in order, in one new directory: an `sh` line runs (`uv run python`
becomes this interpreter, `python -m expcore` runs in this process, and `cd` moves every later block into the
directory it names), a `python` block is executed in one namespace
shared by all of them, and a `toml` block must agree with the file of the example it names on its first line, or, if
it shows an `[[opaque]]` entry, load as one through the package's own inclusion-list loader. The two
lines that make the project and add the core cannot run here, where the core is already installed: the `uv add` line
must be exactly what depends on this version's tag, and scripts/fresh_repo.py runs both (tests/test_fresh_repo.py).
The core is installed editable here, so the package vendors stand-in wheels and the package's `make check` runs with
this interpreter (`check-installed`); scripts/fresh_repo.py runs the offline install for real.
"""

import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from study_support import clean_environment

from expcore.cli import main
from expcore.package import build as package_build
from expcore.package.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "study-guide.md"
BLOCK = re.compile(r"^```(\w*)\n(.*?)^```$", re.M | re.S)
NAMED_FILE = re.compile(r"# (\S+\.toml)\n")
OPAQUE_ENTRY = "# an [[opaque]] entry of package.toml\n"
RUNNABLE = ("git", "tar", "make")

__all__ = ["clean_environment"]  # a fixture the test uses, imported from the support module


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def contained(part: object, whole: object) -> bool:
    """Whether every key and value of `part` is in `whole`, recursively."""
    if isinstance(part, dict):
        return isinstance(whole, dict) and all(k in whole and contained(v, whole[k]) for k, v in part.items())
    return part == whole


def run_line(line: str) -> None:
    if line == "uv init --bare":
        return  # makes the project; here the core is installed already
    if line.startswith("uv add "):
        assert line == f"uv add git+https://github.com/groundnuty/papers-experiments-core@v{version()}", line
        return
    argv = shlex.split(line.replace("uv run python", shlex.quote(sys.executable)))
    if argv[0] == "make":
        argv = [*["check-installed" if a == "check" else a for a in argv], f"PYTHON={sys.executable}"]
    if argv[:3] == [sys.executable, "-m", "expcore"]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(argv[3:])
        assert status == 0, f"{line}: exit {status}\n{out.getvalue()}\n{err.getvalue()}"
        return
    assert argv[0] in RUNNABLE, f"{line}: the guide may only run {', '.join(RUNNABLE)} and `uv run python -m expcore`"
    done = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ))
    assert done.returncode == 0, f"{line}: exit {done.returncode}\n{done.stdout}\n{done.stderr}"


def test_every_block_of_the_study_guide_runs_in_order_in_a_new_directory(tmp_path, monkeypatch, clean_environment):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(package_build, "vendor_wheels", stand_in_vendor)
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Guide Reader",
        "GIT_AUTHOR_EMAIL": "reader@example.invalid",
        "GIT_COMMITTER_NAME": "Guide Reader",
        "GIT_COMMITTER_EMAIL": "reader@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    namespace: dict[str, object] = {"__name__": "study_guide"}
    found = [(m.group(1), m.group(2)) for m in BLOCK.finditer(GUIDE.read_text(encoding="utf-8"))]
    assert {language for language, _ in found} == {"sh", "python", "toml"}
    for number, (language, body) in enumerate(found, start=1):
        if language == "sh":
            for line in body.splitlines():
                if line.startswith("cd "):
                    [directory] = shlex.split(line)[1:]
                    target = Path(directory).resolve()  # relative to where the guide's reader stands now
                    assert target.is_relative_to(tmp_path.resolve()), f"block {number}: cd leaves the scratch dir"
                    monkeypatch.chdir(target)
                elif line.strip() and not line.startswith("#"):
                    run_line(line)
        elif language == "python":
            exec(compile(body, f"{GUIDE.name}, block {number}", "exec"), namespace)
        elif language == "toml" and body.startswith(OPAQUE_ENTRY):
            attest = tmp_path / "attest"
            attest.mkdir()
            (attest / "package.toml").write_text(f'[package]\nname = "p"\ninclude = ["x"]\n\n{body}', "utf-8")
            assert load_spec(attest).opaque, number
        elif language == "toml":
            named = NAMED_FILE.match(body)
            assert named, f"block {number}: a toml block names the file of the example it shows on its first line"
            shown = tomllib.loads(body)
            assert contained(shown, tomllib.loads((Path.cwd() / named.group(1)).read_text(encoding="utf-8"))), number
        else:
            pytest.fail(f"block {number}: a block is sh, python or toml, so the test can run or check it")
    assert Path.cwd() != tmp_path  # the guide made its project in a directory of its own, and worked in it
    assert (Path.cwd() / "dist" / "tiny-study-package" / "SHA256SUMS").is_file()  # the last step ran

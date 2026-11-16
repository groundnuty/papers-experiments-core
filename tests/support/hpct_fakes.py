"""Fakes for the HPC test runner's tests (deploy/hpc-tests): a Slurm (`squeue`, `sbatch`, `scontrol`, `scancel`) that
reads its answers from files and logs every call, a clock (`date +%s`) read from a file, an `ssh` that runs the
remote command in a local shell, an `srun` that drops its options and runs the step's command, and a `uv` whose sync
builds a small venv with a stand-in pytest. Every fake writes under the directory it is given, nothing else."""

import json
import os
import stat
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "hpc-tests"
RECORD = (
    "JobId={id} JobName=expcore-tests UserId=someone(1000) Account=acct-x QOS=normal JobState=PENDING Requeue=0 "
    "TimeLimit=12:00:00 Partition=std-part NumNodes=1 NumCPUs=16 MinMemoryNode=30G"
)
LOGIN_ENV = (
    "HPCT_ACCOUNT=acct-x\nHPCT_PARTITION=std-part\nHPCT_JOB_NAME=expcore-tests\nHPCT_CPUS=16\nHPCT_MEM=30G\n"
    "HPCT_HOURS=12\nHPCT_MAX_ALIVE=2\n"
)


def script(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def slurm(bin_dir: Path, fake: Path) -> None:
    """squeue prints fake/squeue (exit 1 when fake/squeue-fail exists); sbatch logs its argv and the two input
    variables to fake/sbatch.jsonl and prints fake/next-id (700 at first, then one more each time); scontrol prints
    fake/record (RECORD by default) for the id asked; scancel logs its argv to fake/scancel.jsonl; date +%s prints
    fake/now."""
    python = sys.executable
    script(
        bin_dir / "squeue",
        f'#!/bin/bash\n[ ! -e "{fake}/squeue-fail" ] || exit 1\n[ ! -e "{fake}/squeue" ] || cat "{fake}/squeue"\n',
    )
    script(
        bin_dir / "sbatch",
        f"#!{python}\nimport json, os, sys\n"
        f"open('{fake}/sbatch.jsonl', 'a').write(json.dumps({{'argv': sys.argv[1:], "
        f"'account': os.environ.get('SBATCH_ACCOUNT'), 'partition': os.environ.get('SBATCH_PARTITION')}}) + '\\n')\n"
        f"rc = int(open('{fake}/sbatch-rc').read()) if os.path.exists('{fake}/sbatch-rc') else 0\n"
        f"n = int(open('{fake}/next-id').read()) if os.path.exists('{fake}/next-id') else 700\n"
        f"open('{fake}/next-id', 'w').write(str(n + 1))\n"
        f"print(n) if rc == 0 else print('sbatch: error: refused', file=sys.stderr)\nsys.exit(rc)\n",
    )
    script(
        bin_dir / "scontrol",
        f"#!{python}\nimport os, sys\njob = sys.argv[-1]\n"
        f"text = open('{fake}/record').read() if os.path.exists('{fake}/record') else {RECORD!r}\n"
        f"print(text.replace('{{id}}', job))\n",
    )
    script(
        bin_dir / "scancel",
        f"#!{python}\nimport json, sys\nopen('{fake}/scancel.jsonl', 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n",
    )
    script(
        bin_dir / "date",
        f'#!/bin/bash\nif [ "$#" = 1 ] && [ "$1" = "+%s" ] && [ -e "{fake}/now" ]; then cat "{fake}/now"; exit 0; fi\n'
        'exec /bin/date "$@"\n',
    )


def remote(bin_dir: Path, fake: Path) -> None:
    """ssh runs the remote command with bash in $HOME; srun logs its options (up to /usr/bin/env) to fake/srun.argv and
    runs the step's command, then sleeps on (an srun client that does not return) when fake/hang exists."""
    script(
        bin_dir / "ssh",
        '#!/bin/bash\nwhile [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) break;; esac; done\n'
        f'echo "$1" >> "{fake}/ssh.hosts"\nshift\ncd "$HOME" || exit 255\nexec bash -c "$1"\n',
    )
    script(
        bin_dir / "srun",
        '#!/bin/bash\nargs=("$@"); i=0\n'
        'while [ "$i" -lt "${#args[@]}" ] && [ "${args[$i]}" != /usr/bin/env ]; do i=$((i + 1)); done\n'
        f'printf \'%s\\n\' "${{args[@]:0:$i}}" -- >> "{fake}/srun.argv"\n'
        f'if [ -e "{fake}/hang" ]; then "${{args[@]:$i}}"; exec sleep 600; fi\n'
        'exec "${args[@]:$i}"\n',
    )


FAKE_PYTEST = r"""
import json, os, sys
args = sys.argv[1:]
junit = next(a.split("=", 1)[1] for a in args if a.startswith("--junitxml="))
fail = os.path.exists("fake-outcome-fail")
print("env-keys: " + ",".join(sorted(os.environ)))
print("args: " + json.dumps(args))
with open(junit, "w") as out:
    out.write('<testsuites><testsuite name="pytest" errors="0" failures="%d" skipped="1" tests="3" time="0.5"/>'
              '</testsuites>' % (1 if fail else 0))
print("=========================== short test summary info ============================")
print("SKIPPED [1] tests/test_x.py:3: a stand-in skip")
if fail:
    print("FAILED tests/test_x.py::test_b - assert 0")
    print("================ 1 failed, 1 passed, 1 skipped in 0.50s ================")
    sys.exit(1)
print("================ 2 passed, 1 skipped in 0.50s ================")
"""

FAKE_UV = r"""
import glob, os, subprocess, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("uv 0.11.19 (stand-in)")
    sys.exit(0)
if args[:2] == ["python", "install"]:
    sys.exit(0)
if args[:1] == ["sync"]:
    env = os.environ["UV_PROJECT_ENVIRONMENT"]
    subprocess.run([PYTHON, "-m", "venv", "--without-pip", env], check=True)
    site = glob.glob(os.path.join(env, "lib", "python3*", "site-packages"))[0]
    if not os.path.exists("fake-wrong-core"):  # a venv whose core is not the clone's: no .pth to its src
        with open(os.path.join(site, "hpct_fake.pth"), "w") as pth:
            pth.write(os.path.join(os.getcwd(), "src") + "\n")
    os.mkdir(os.path.join(site, "pytest"))
    open(os.path.join(site, "pytest", "__init__.py"), "w").close()
    with open(os.path.join(site, "pytest", "__main__.py"), "w") as main:
        main.write(FAKE_PYTEST)
    sys.exit(0)
sys.exit(2)
"""


def uv(path: Path) -> Path:
    body = f"PYTHON = {sys.executable!r}\nFAKE_PYTEST = {FAKE_PYTEST!r}\n" + FAKE_UV
    return script(path, f"#!{sys.executable}\n{body}")


def calls(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def login_dir(home: Path) -> Path:
    """A login directory as `install` leaves it: alloc.sh and login.env, 0700."""
    directory = home / ".expcore-hpct"
    directory.mkdir(mode=0o700, parents=True)
    (directory / "alloc.sh").write_bytes((DEPLOY / "alloc.sh").read_bytes())
    (directory / "login.env").write_text(LOGIN_ENV, encoding="utf-8")
    return directory


def environment(bin_dir: Path, fake: Path, home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HPCT_", "SBATCH_", "SLURM_"))}
    env.update(PATH=f"{bin_dir}:/usr/bin:/bin", HOME=str(home), TZ="UTC", FAKE=str(fake))
    return env

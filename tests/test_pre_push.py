"""The pre-push hook checks every commit being pushed, driven here with git's stdin protocol and a stub uv."""

import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "pre-push"
ZERO = "0" * 40
TOKEN = "secret-space"


def run_hook(repo, bin_dir: Path, lines: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [str(HOOK), "origin"], cwd=repo.path, input=lines, capture_output=True, text=True, env=env, check=False
    )


def test_hook_is_executable():
    assert os.access(HOOK, os.X_OK)


def test_an_update_whose_range_contains_a_leak_is_refused_even_if_the_tip_is_clean(pushed, bin_dir):
    repo, first, _, third = pushed
    done = run_hook(repo, bin_dir, f"refs/heads/main {third} refs/heads/main {first}\n")
    assert done.returncode == 1
    assert "src/leak.py:1 string" in done.stdout
    assert TOKEN not in done.stdout + done.stderr


def test_an_update_of_clean_commits_is_allowed(pushed, bin_dir):
    repo, _, second, third = pushed
    done = run_hook(repo, bin_dir, f"refs/heads/main {third} refs/heads/main {second}\n")
    assert done.returncode == 0, done.stderr


def test_a_new_ref_checks_its_whole_history(pushed, bin_dir):
    repo, _, _, third = pushed
    done = run_hook(repo, bin_dir, f"refs/heads/topic {third} refs/heads/topic {ZERO}\n")
    assert done.returncode == 1
    assert "src/leak.py:1 string" in done.stdout


def test_a_stale_tracking_ref_is_never_trusted(pushed, bin_dir):
    repo, _, second, third = pushed
    repo.git("update-ref", "refs/remotes/origin/main", second)  # claims the leaking commit is already published
    done = run_hook(repo, bin_dir, f"refs/heads/topic {third} refs/heads/topic {ZERO}\n")
    assert done.returncode == 1


def test_a_new_ref_with_a_clean_history_is_allowed(pushed, bin_dir):
    repo, first, _, _ = pushed
    done = run_hook(repo, bin_dir, f"refs/heads/topic {first} refs/heads/topic {ZERO}\n")
    assert done.returncode == 0, done.stderr


def test_deleting_a_remote_ref_pushes_no_commits(pushed, bin_dir):
    repo, _, second, _ = pushed
    assert run_hook(repo, bin_dir, f"(delete) {ZERO} refs/heads/gone {second}\n").returncode == 0


def test_one_bad_ref_among_several_fails_the_push(pushed, bin_dir):
    repo, first, second, third = pushed
    lines = f"refs/heads/a {third} refs/heads/a {second}\nrefs/heads/b {third} refs/heads/b {first}\n"
    assert run_hook(repo, bin_dir, lines).returncode == 1

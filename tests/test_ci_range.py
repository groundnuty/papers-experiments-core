"""scripts/ci-check-commits picks the commits an event adds, driven here with a stub uv and a temporary repository."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci-check-commits"
ZERO = "0" * 40


def run_script(repo, bin_dir: Path, **event: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in {"EVENT", "BEFORE", "SHA", "BASE_SHA", "HEAD_SHA"}}
    env.update({"PATH": f"{bin_dir}{os.pathsep}{env['PATH']}", **event})
    return subprocess.run([str(SCRIPT)], cwd=repo.path, capture_output=True, text=True, env=env, check=False)


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_a_push_checks_the_whole_range_not_only_the_tip(pushed, bin_dir):
    repo, first, _, third = pushed
    done = run_script(repo, bin_dir, EVENT="push", BEFORE=first, SHA=third)
    assert done.returncode == 1
    assert "src/leak.py:1 string" in done.stdout


def test_a_push_of_clean_commits_passes(pushed, bin_dir):
    repo, _, second, third = pushed
    assert run_script(repo, bin_dir, EVENT="push", BEFORE=second, SHA=third).returncode == 0


def test_a_new_branch_without_a_default_branch_checks_its_whole_history(pushed, bin_dir):
    repo, _, _, third = pushed
    done = run_script(repo, bin_dir, EVENT="push", BEFORE=ZERO, SHA=third)
    assert done.returncode == 1
    assert "src/leak.py:1 string" in done.stdout


def test_a_new_branch_is_compared_with_the_default_branch(pushed, bin_dir):
    repo, first, second, third = pushed
    repo.git("update-ref", "refs/remotes/origin/main", second)
    assert run_script(repo, bin_dir, EVENT="push", BEFORE=ZERO, SHA=third).returncode == 0
    repo.git("update-ref", "refs/remotes/origin/main", first)
    assert run_script(repo, bin_dir, EVENT="push", BEFORE=ZERO, SHA=third).returncode == 1


def test_the_first_push_of_the_default_branch_checks_its_whole_history(pushed, bin_dir):
    repo, _, _, third = pushed
    repo.git("update-ref", "refs/remotes/origin/main", third)  # checkout has already fetched the pushed branch
    done = run_script(repo, bin_dir, EVENT="push", BEFORE=ZERO, SHA=third)
    assert done.returncode == 1


def test_a_pull_request_checks_base_to_head(pushed, bin_dir):
    repo, first, second, third = pushed
    assert run_script(repo, bin_dir, EVENT="pull_request", BASE_SHA=first, HEAD_SHA=third).returncode == 1
    assert run_script(repo, bin_dir, EVENT="pull_request", BASE_SHA=second, HEAD_SHA=third).returncode == 0


def test_missing_or_unsupported_event_data_is_an_error(pushed, bin_dir):
    repo, _, _, third = pushed
    assert run_script(repo, bin_dir, EVENT="schedule").returncode == 2
    assert run_script(repo, bin_dir, EVENT="pull_request", HEAD_SHA=third).returncode != 0
    assert run_script(repo, bin_dir, EVENT="push", SHA=third).returncode != 0
    assert run_script(repo, bin_dir).returncode != 0

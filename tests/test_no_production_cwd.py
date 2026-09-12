"""A test may not start a child process in the repo. Asserted, not assumed.

Fleet board #368 Phase 2. `scripts/pytest_fleet_guard.py` measured this suite on
2026-09-11 and caught `test_crash_breadcrumb_wiring.py` writing AND deleting the
repo's real `.health/crash.txt` -- from a child process launched with `cwd=REPO`,
where `cli.CRASH_PATH` ("`.health/crash.txt`", relative by design so a CI
checkout gets it right) resolves onto production.

`tests/conftest.py::_no_child_process_in_the_repo` refuses that before the launch.
These tests are what stops the fixture from being removed, or from drifting into
something that refuses everything (which would break the legitimate `tmp_path`
subprocess in `test_conferences.py`) -- both sides of the classifier, not just the
one that failed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_a_child_with_the_repo_as_cwd_is_refused():
    with pytest.raises(RuntimeError, match="repo as its CWD"):
        subprocess.run([sys.executable, "-c", "pass"], cwd=str(REPO))


def test_a_child_that_inherits_pytests_cwd_is_refused():
    """`cwd=None` is the dangerous default, not a safe one: pytest runs in the
    repo, so an unspecified cwd is the repo. A guard that only checked an
    explicit `cwd=` would have missed every plain `subprocess.run([...])`."""
    with pytest.raises(RuntimeError, match="repo as its CWD"):
        subprocess.run([sys.executable, "-c", "pass"])


def test_a_child_in_tmp_path_still_runs(tmp_path):
    r = subprocess.run([sys.executable, "-c", "print('ok')"], cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ok" in r.stdout


def test_a_subdirectory_of_the_repo_is_also_refused(tmp_path):
    """`cwd=REPO/"scripts"` would resolve `.health/` to `scripts/.health/` -- a
    different production path, not a safe one."""
    with pytest.raises(RuntimeError, match="repo as its CWD"):
        subprocess.run([sys.executable, "-c", "pass"], cwd=str(REPO / "scripts"))


@pytest.mark.repo_cwd
def test_the_marker_opts_a_test_out():
    """The escape hatch has to work, or the next author deletes the fixture
    instead of marking their test."""
    r = subprocess.run([sys.executable, "-c", "print('ok')"], cwd=str(REPO),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ok" in r.stdout

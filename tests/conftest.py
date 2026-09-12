"""⛑ REFUSE a child process whose CWD is the repo, BEFORE it is launched.

**What was measured.** 2026-09-11, `scripts/pytest_fleet_guard.py` (fleet board
#368 Phase 2) ran this suite with every write/delete outside the system temp dir
refused, and caught
`tests/test_crash_breadcrumb_wiring.py::test_a_crash_under_dunder_main_writes_the_breadcrumb`
both WRITING and DELETING `.health/crash.txt` -- the repo's real health
breadcrumb. The subprocess itself was fine; its `cwd=REPO` was not. Every health
path in this project is relative (`cli.CRASH_PATH = Path(".health")/"crash.txt"`,
`health.HEALTH_DIR = Path(".health")`, `DEFAULT_DB = "data/events.db"`), which is
correct for a GitHub Actions checkout and means a child started in the repo
writes production by default.

**Why the CWD and not the paths.** The in-process tests already solve this with
`monkeypatch.chdir(tmp_path)` (`tests/test_cli_weekly_isolation.py:229`, `:244`,
`:257`, `tests/test_health.py:94`), so the convention exists -- it just cannot
reach into a child process, where `monkeypatch` does not apply and this repo's
own guard cannot see the write. The CWD is the one lever the parent still holds,
and it is derived from `REPO` rather than from a list of paths, so it cannot go
stale the way an enumeration of `.health/`, `data/`, `exports/` would.

**Not blanket-hostile to subprocesses.** `test_conferences.py` shells out to
`scripts/build_conference_page.py` with `--db`/`--out` under `tmp_path` and needs
`cwd=REPO` for nothing in particular; it is marked `repo_cwd` so the choice is a
reviewed exception instead of the default. That marker is the whole mechanism: a
NEW test that shells out lands in `tmp_path` unless its author says otherwise out
loud.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _inside_repo(cwd) -> bool:
    if cwd is None:          # inherits pytest's cwd, which IS the repo
        return True
    try:
        resolved = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    return resolved == REPO or REPO in resolved.parents


@pytest.fixture(autouse=True)
def _no_child_process_in_the_repo(request, monkeypatch):
    """A test that shells out must give the child a `tmp_path` working directory.

    Opt out with `@pytest.mark.repo_cwd` when the child genuinely needs the repo
    as its CWD *and* every path it touches is absolute and disposable.
    """
    if request.node.get_closest_marker("repo_cwd"):
        return

    def _refuse(cwd):
        raise RuntimeError(
            f"child process with the repo as its CWD refused in tests "
            f"(cwd={cwd!r}). Every health/data path in this project is relative, "
            f"so the child would write production `.health/` and `data/`. Pass "
            f"cwd=tmp_path (add the repo to PYTHONPATH so `-m src.cli` still "
            f"imports), or mark the test `@pytest.mark.repo_cwd`.")

    for name in ("run", "call", "check_call", "check_output", "Popen"):
        real = getattr(subprocess, name)

        def guarded(*args, _real=real, **kwargs):
            if _inside_repo(kwargs.get("cwd")):
                _refuse(kwargs.get("cwd"))
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guarded)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "repo_cwd: this test may launch a child process with the repo as its "
        "CWD (opt out of conftest._no_child_process_in_the_repo)")

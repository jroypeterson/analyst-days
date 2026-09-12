"""The __main__ crash-breadcrumb handler must be exercised AS __main__.

Adversarial review finding H1 (2026-07-29): every other breadcrumb test calls
`_write_crash_breadcrumb()` or `main()` directly. pytest *imports* src.cli, so
the `if __name__ == "__main__"` guard never runs -- meaning you could delete the
six-line `except BaseException` handler, or refactor the entry point to a
console_scripts hook that calls main() directly (bypassing the guard entirely),
and the whole suite would stay green while CI silently regressed to the constant
generic fallback message. That is the exact defect class this commit exists to
fix: by its own standard, a docstring is not a mechanism -- and neither is an
untested one.

These run the module the way the workflows do: `python -m src.cli`.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run(args, env_extra, cwd):
    env = {**dict(__import__("os").environ), **env_extra}
    env.pop("SLACK_WEBHOOK_STATUS_REPORTS", None)   # never post from a test
    # `src.cli` is importable because the repo is on PYTHONPATH, NOT because the
    # child's cwd is the repo -- see the comment on the test below. Prepending
    # keeps a stray installed `src` package from shadowing this one.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return subprocess.run(
        [sys.executable, "-m", "src.cli", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)


def _repo_crumb_state() -> bytes | None:
    """The repo's real breadcrumb, byte-for-byte, or None if absent.

    Compared before/after so the check reports "this test touched production"
    without assuming production was clean to begin with -- a genuine local crash
    may have left a real traceback there, and that file is evidence.
    """
    crumb = REPO / ".health" / "crash.txt"
    return crumb.read_bytes() if crumb.exists() else None


def test_a_crash_under_dunder_main_writes_the_breadcrumb(tmp_path):
    """Run as __main__, crash OUTSIDE a phase, assert the traceback lands.

    Note the first draft of this test used `--weekly`, and it failed -- for the
    right reason. A phase exception is caught by `_run_phase`, which posts a
    real `error` heartbeat and exits non-zero WITHOUT reaching __main__. That is
    the phase isolation working. So the breadcrumb path needs a crash that is
    genuinely outside a phase: `--discover` is dispatched directly by `main()`.
    No test-only hook is used -- a hook would itself be the thing that rots.

    ⛑ **The child runs in `tmp_path`, never in the repo.** Until 2026-09-11 it
    ran with `cwd=REPO`, and `cli.CRASH_PATH` is the RELATIVE `.health/crash.txt`
    -- so this test wrote the repo's real health breadcrumb and then `unlink()`ed
    it in a `finally`. Two harms, and the second is the worse one: `.health/` is
    where a genuine local crash leaves its traceback for
    `_clear_crash_breadcrumb`/the workflow fallback to report, so a `pytest` run
    DESTROYED that diagnostic, and the deletion is indistinguishable from "no
    crash happened". Found by `scripts/pytest_fleet_guard.py` (fleet board #368
    Phase 2) refusing the write and the delete; no assertion here could have,
    because the test asserted on exactly the file it was clobbering.

    Running in `tmp_path` is the same isolation every in-process breadcrumb test
    in `tests/test_cli_weekly_isolation.py` already uses (`monkeypatch.chdir`) --
    the relative path is the mechanism under test, so pointing the CWD somewhere
    disposable exercises it honestly rather than patching it away.
    """
    before = _repo_crumb_state()
    r = _run(["--discover"],
             {"COVERAGE_MANAGER_PATH": str(tmp_path / "does-not-exist"),
              "CI": "true"},
             cwd=tmp_path)

    crumb = tmp_path / ".health" / "crash.txt"
    assert r.returncode != 0, f"expected a non-zero exit, got {r.returncode}"
    assert crumb.exists(), (
        "no .health/crash.txt -- the __main__ handler did not run. "
        f"stdout={r.stdout[-400:]!r} stderr={r.stderr[-400:]!r}")
    body = crumb.read_text(encoding="utf-8")
    assert "Traceback" in body, body[:400]
    assert "FileNotFoundError" in body, body[:400]
    body.encode("ascii")            # must be cp1252-safe
    assert _repo_crumb_state() == before, (
        "the child touched the REPO's own .health/crash.txt -- it is not running "
        "in tmp_path")


def test_the_entry_point_still_routes_through_the_handler():
    """Guard the refactor the review named: if the module ever grows a
    console_scripts entry point that calls main() directly, the __main__ guard
    stops being the only path in and this protection silently lapses."""
    src = (REPO / "src" / "cli.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    guard = src.split('if __name__ == "__main__":', 1)[1]
    assert "except BaseException" in guard, (
        "the __main__ guard no longer installs the crash-breadcrumb handler")
    assert "_write_crash_breadcrumb()" in guard
    for path in (REPO / "pyproject.toml", REPO / "setup.py", REPO / "setup.cfg"):
        if path.exists():
            assert "console_scripts" not in path.read_text(encoding="utf-8"), (
                f"{path.name} declares a console_scripts entry point, which "
                "bypasses the __main__ guard -- route it through a wrapper "
                "that installs the same handler, or this breadcrumb is dead.")

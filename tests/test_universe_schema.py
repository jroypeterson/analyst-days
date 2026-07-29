"""Guard tests for the Coverage Manager exports schema gate in src/universe.py.

Phase 1 of the CM v4 (dual-ISIN) release widened the gate from `== 3` to a
frozenset `{3, 4}` so this repo stays green both before and after CM flips.
These tests pin BOTH halves of that window plus the loud failure outside it —
the guard is the feature, so "accepts 4" without "still rejects 5" would be a
silent downgrade.

NARROW TO {4} in phase 4: `test_accepts_v3` becomes a rejection case then.
"""
import csv
import json

import pytest

from src.universe import _ACCEPTED_CM_SCHEMA, _assert_schema, load_core_watchlist


def _make_cm_root(tmp_path, schema_version, rows=None):
    exports = tmp_path / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "watchlist_status.json").write_text(
        json.dumps({"schema_version": schema_version, "validation_passed": True}),
        encoding="utf-8",
    )
    rows = rows if rows is not None else [
        {"Ticker": "IDXX", "Company Name": "IDEXX Laboratories",
         "Sector (JP)": "MedTech", "ISIN": "US45168D1046", "Core": "Y"},
    ]
    fields = ["Ticker", "Company Name", "Sector (JP)", "ISIN", "Core"]
    with (exports / "watchlist.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return tmp_path


def test_accepts_v3(tmp_path):
    """CM still publishes 3 during phases 1-3; it must keep working."""
    _assert_schema(_make_cm_root(tmp_path, 3))


def test_accepts_v4(tmp_path):
    """The point of phase 1: v4 must not raise."""
    _assert_schema(_make_cm_root(tmp_path, 4))


@pytest.mark.parametrize("bad", [2, 5, 99])
def test_rejects_versions_outside_the_window(tmp_path, bad):
    """Loud failure outside the accepted set is the whole point of the pin."""
    with pytest.raises(RuntimeError, match="Coverage Manager exports schema"):
        _assert_schema(_make_cm_root(tmp_path, bad))


def test_error_message_names_the_accepted_set(tmp_path):
    """A reader of the traceback must see the window, not a single number."""
    with pytest.raises(RuntimeError) as exc:
        _assert_schema(_make_cm_root(tmp_path, 5))
    msg = str(exc.value)
    for v in sorted(_ACCEPTED_CM_SCHEMA):
        assert f"v{v}" in msg


def test_v4_load_returns_rows_not_just_no_crash(tmp_path):
    """Zero rows under a passing gate is the BOM signature — assert non-empty."""
    root = _make_cm_root(tmp_path, 4)
    out = load_core_watchlist(root)
    assert len(out) == 1
    assert out[0].ticker == "IDXX"


def test_v4_tolerates_the_two_new_columns(tmp_path):
    """v4 adds `ISIN (Primary Listing)` + `Country (Incorporation)`. The loader
    reads by name, so unknown columns must be inert, not fatal."""
    exports = tmp_path / "exports"
    exports.mkdir(parents=True)
    (exports / "watchlist_status.json").write_text(
        json.dumps({"schema_version": 4, "validation_passed": True}), encoding="utf-8"
    )
    (exports / "watchlist.csv").write_text(
        "Ticker,Company Name,ISIN,ISIN (Primary Listing),Country (Incorporation),Core\n"
        "AZN,AstraZeneca PLC,US0463531089,GB0009895292,GB,Y\n",
        encoding="utf-8",
    )
    out = load_core_watchlist(tmp_path)
    assert len(out) == 1
    assert out[0].isin == "US0463531089"

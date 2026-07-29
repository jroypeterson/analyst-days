"""Guard tests for the Coverage Manager exports schema gate in src/universe.py.

The gate accepts exactly `{3}`. It is a frozenset rather than a bare `== 3` so
the error message can name the accepted set and a real future bump is one line.

These tests were added 2026-07-28 during a briefly-widened `{3, 4}` window and
KEPT when it narrowed back (no v4 bump is coming -- adding a CSV column does not
bump CM's EXPORTS_SCHEMA_VERSION; only a `universe_metadata.json` entry-shape
change does). They are the real gain from that exercise: this repo had no test
for its schema gate before.
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
    """v3 is what CM publishes, and there is no bump coming."""
    _assert_schema(_make_cm_root(tmp_path, 3))


@pytest.mark.parametrize("bad", [2, 4, 5, 99])
def test_rejects_versions_outside_the_accepted_set(tmp_path, bad):
    """Loud failure outside the accepted set is the whole point of the pin.

    `4` is in this list deliberately: it was briefly ACCEPTED on 2026-07-28 in
    anticipation of a CM bump that was then disproven. An unannounced v4 must
    stop the run like any other unknown version.
    """
    with pytest.raises(RuntimeError, match="Coverage Manager exports schema"):
        _assert_schema(_make_cm_root(tmp_path, bad))


def test_error_message_names_the_accepted_set(tmp_path):
    """A reader of the traceback must see the accepted set, not just a failure."""
    with pytest.raises(RuntimeError) as exc:
        _assert_schema(_make_cm_root(tmp_path, 5))
    msg = str(exc.value)
    for v in sorted(_ACCEPTED_CM_SCHEMA):
        assert f"v{v}" in msg


def test_load_returns_rows_not_just_no_crash(tmp_path):
    """Zero rows under a passing gate is the BOM signature -- assert non-empty."""
    root = _make_cm_root(tmp_path, 3)
    out = load_core_watchlist(root)
    assert len(out) == 1
    assert out[0].ticker == "IDXX"


def test_tolerates_extra_identity_columns_without_a_bump(tmp_path):
    """New columns (`ISIN (Primary Listing)`, `Country (Incorporation)`) arrive
    WITHOUT a schema bump -- that is CM's documented precedent. The loader reads
    by name, so unknown columns must be inert at v3, not fatal."""
    exports = tmp_path / "exports"
    exports.mkdir(parents=True)
    (exports / "watchlist_status.json").write_text(
        json.dumps({"schema_version": 3, "validation_passed": True}), encoding="utf-8"
    )
    (exports / "watchlist.csv").write_text(
        "Ticker,Company Name,ISIN,ISIN (Primary Listing),Country (Incorporation),Core\n"
        "AZN,AstraZeneca PLC,US0463531089,GB0009895292,GB,Y\n",
        encoding="utf-8",
    )
    out = load_core_watchlist(tmp_path)
    assert len(out) == 1
    assert out[0].isin == "US0463531089"

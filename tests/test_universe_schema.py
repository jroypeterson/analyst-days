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

from src.universe import (
    _ACCEPTED_CM_SCHEMA,
    _assert_schema,
    load_by_sectors,
    load_core_watchlist,
    load_portfolio,
)

UTF8_BOM = b"\xef\xbb\xbf"


def _prefix_bom(path):
    """Re-write an existing export with a UTF-8 BOM, exactly as Coverage
    Manager published `watchlist.csv` on 2026-07-27."""
    path.write_bytes(UTF8_BOM + path.read_bytes())


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


def test_load_survives_a_utf8_bom_on_the_header(tmp_path):
    """The owed 2026-07-27 regression test, in one assertion.

    CM published `exports/watchlist.csv` with a UTF-8 BOM, so DictReader's first
    fieldname became "\\ufeffTicker", `_row_to_ticker`'s `row["Ticker"]` raised
    `KeyError`, and the whole weekly run died.

    `test_load_returns_rows_not_just_no_crash` above was written to catch the
    BOM's zero-row signature, but `_make_cm_root` writes a plain-utf-8 fixture,
    so it never exercised the BOM path. This one does.

    Fails against `git show 77995b7:src/universe.py` (encoding="utf-8");
    passes against HEAD (3df75d0, encoding="utf-8-sig").
    """
    root = _make_cm_root(tmp_path, 3)
    _prefix_bom(root / "exports" / "watchlist.csv")

    out = load_core_watchlist(root)

    assert len(out) == 1, "a BOM must not silently zero the join"
    assert out[0].ticker == "IDXX"


def test_schema_gate_survives_a_bom_on_watchlist_status_json(tmp_path):
    """A BOM here raises `JSONDecodeError: Expecting value: line 1 column 1`
    from inside the SCHEMA GATE, which reads as "CM broke its schema" rather
    than "encoding" -- a misleading diagnosis for the on-call operator.

    Tolerant in, strict out: we still write utf-8 ourselves.
    """
    root = _make_cm_root(tmp_path, 3)
    _prefix_bom(root / "exports" / "watchlist_status.json")

    _assert_schema(root)  # must not raise


def test_schema_gate_still_rejects_a_bad_version_under_a_bom(tmp_path):
    """Tolerating the BOM must not tolerate the wrong schema."""
    root = _make_cm_root(tmp_path, 99)
    _prefix_bom(root / "exports" / "watchlist_status.json")

    with pytest.raises(RuntimeError, match="Coverage Manager exports schema"):
        _assert_schema(root)


def test_position_json_survives_a_utf8_bom(tmp_path):
    root = _make_cm_root(tmp_path, 3)
    (root / "exports" / "portfolio.json").write_text(
        json.dumps({"IDXX": {"Ticker": "IDXX", "name": "IDEXX Laboratories",
                             "sector": "MedTech", "Core": "Y"}}),
        encoding="utf-8",
    )
    _prefix_bom(root / "exports" / "portfolio.json")

    out = load_portfolio(root)

    assert [t.ticker for t in out] == ["IDXX"]


def test_universe_csv_survives_a_utf8_bom(tmp_path):
    """`load_by_sectors` reads a SECOND csv that `3df75d0` did not fix. Its BOM
    signature is the quieter one: the `Sector (JP)` filter still matches, so it
    returns rows -- until the BOM lands on the first column and `row["Ticker"]`
    raises, or the first column IS the filter column and it returns zero."""
    root = _make_cm_root(tmp_path, 3)
    (root / "exports" / "universe.csv").write_text(
        "Ticker,Company Name,Sector (JP),ISIN,Core\n"
        "IDXX,IDEXX Laboratories,MedTech,US45168D1046,Y\n",
        encoding="utf-8",
    )
    _prefix_bom(root / "exports" / "universe.csv")

    out = load_by_sectors(["MedTech"], root)

    assert [t.ticker for t in out] == ["IDXX"]


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

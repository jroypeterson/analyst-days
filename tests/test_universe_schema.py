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
    load_scan_universe,
    ScanUniverseError,
    load_portfolio,
)

UTF8_BOM = b"\xef\xbb\xbf"


def _prefix_bom(path):
    """Re-write an existing export with a UTF-8 BOM, exactly as Coverage
    Manager published `watchlist.csv` on 2026-07-27."""
    path.write_bytes(UTF8_BOM + path.read_bytes())


def _write_lists(exports, members, core=None, schema_version=1):
    """A lists.json shaped like Coverage Manager's, for the `analyst_days` scope."""
    core = members if core is None else core
    (exports / "lists.json").write_text(json.dumps({
        "schema_version": schema_version,
        "lists": {"core": {"count": len(core), "members": list(core)}},
        "scopes": {"analyst_days": {"count": len(members), "members": list(members)}},
    }), encoding="utf-8")


def _make_cm_root(tmp_path, schema_version, rows=None):
    exports = tmp_path / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "universe_status.json").write_text(
        json.dumps({"schema_version": schema_version, "validation_passed": True}),
        encoding="utf-8",
    )
    rows = rows if rows is not None else [
        {"Ticker": "IDXX", "Company Name": "IDEXX Laboratories",
         "Sector (JP)": "MedTech", "ISIN": "US45168D1046", "Core": "Y"},
    ]
    fields = ["Ticker", "Company Name", "Sector (JP)", "ISIN", "Core"]
    with (exports / "universe.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    _write_lists(exports, [r["Ticker"] for r in rows])
    return tmp_path


def test_accepts_v3(tmp_path):
    """v3 is still accepted; CM bumped to v4 on 2026-07-30 and both are in the set."""
    _assert_schema(_make_cm_root(tmp_path, 3))


def test_accepts_v4(tmp_path):
    """CM really did ship v4 on 2026-07-30 (weekly_universe.py EXPORTS_SCHEMA_VERSION = 4)."""
    _assert_schema(_make_cm_root(tmp_path, 4))


def test_accepted_set_covers_what_cm_actually_emits():
    """Pin the set against CM's own constant, not against a note in a plan file.

    THIRD instance of the same stale note in one day (2026-08-05): exec_interviews and
    earnings_kpi carried word-for-word the same "v4 was disproven on 07-28" comment. CM
    shipped v4 on 07-30, two days later, so all three were asserting a pre-bump world.
    earnings_kpi's copy CRASHED its lane; this one only reddened a test because the
    constant here had already been widened -- the CONSTANT was fixed and the TEST was not,
    which is exactly what a fleet sweep over constants alone will miss.
    """
    import re
    from pathlib import Path
    cm = Path(__file__).resolve().parents[2] / "Coverage Manager" / "weekly_universe.py"
    if not cm.exists():
        pytest.skip("Coverage Manager is a Dropbox sibling; absent in this checkout")
    m = re.search(r"^EXPORTS_SCHEMA_VERSION\s*=\s*(\d+)",
                  cm.read_text(encoding="utf-8"), re.M)
    assert m, "could not read EXPORTS_SCHEMA_VERSION from Coverage Manager"
    live = int(m.group(1))
    assert live in _ACCEPTED_CM_SCHEMA, (
        f"Coverage Manager now emits schema_version={live}; _ACCEPTED_CM_SCHEMA is "
        f"{sorted(_ACCEPTED_CM_SCHEMA)}. Widen it after confirming the export "
        f"shape is readable, and remove {live} from the reject list below.")


@pytest.mark.parametrize("bad", [2, 5, 99])
def test_rejects_versions_outside_the_accepted_set(tmp_path, bad):
    """Loud failure outside the accepted set is the whole point of the pin.

    `4` was in this list until 2026-08-05, on a note saying a v4 bump floated on 07-28 had
    been "disproven". CM shipped v4 on 07-30. A version listed here is a claim about a
    SIBLING repo's CURRENT output and expires whenever that repo ships -- check the
    artifact before adding one.
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
    out = load_scan_universe(root)
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
    _prefix_bom(root / "exports" / "universe.csv")

    out = load_scan_universe(root)

    assert len(out) == 1, "a BOM must not silently zero the join"
    assert out[0].ticker == "IDXX"


def test_schema_gate_survives_a_bom_on_the_status_json(tmp_path):
    """A BOM here raises `JSONDecodeError: Expecting value: line 1 column 1`
    from inside the SCHEMA GATE, which reads as "CM broke its schema" rather
    than "encoding" -- a misleading diagnosis for the on-call operator.

    Tolerant in, strict out: we still write utf-8 ourselves.
    """
    root = _make_cm_root(tmp_path, 3)
    _prefix_bom(root / "exports" / "universe_status.json")

    _assert_schema(root)  # must not raise


def test_schema_gate_still_rejects_a_bad_version_under_a_bom(tmp_path):
    """Tolerating the BOM must not tolerate the wrong schema."""
    root = _make_cm_root(tmp_path, 99)
    _prefix_bom(root / "exports" / "universe_status.json")

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
    (exports / "universe_status.json").write_text(
        json.dumps({"schema_version": 3, "validation_passed": True}), encoding="utf-8"
    )
    _write_lists(exports, ["AZN"])
    (exports / "universe.csv").write_text(
        "Ticker,Company Name,ISIN,ISIN (Primary Listing),Country (Incorporation),Core\n"
        "AZN,AstraZeneca PLC,US0463531089,GB0009895292,GB,Y\n",
        encoding="utf-8",
    )
    out = load_scan_universe(tmp_path)
    assert len(out) == 1
    assert out[0].isin == "US0463531089"


# -- the scan set is CM's `analyst_days` scope, read by name (2026-11-13) ------

def _scope_root(tmp_path, members, core, rows=None):
    rows = rows or [{"Ticker": t, "Company Name": f"{t} Inc", "Sector (JP)": "MedTech",
                     "ISIN": "", "Core": "Y" if t in core else ""} for t in members]
    root = _make_cm_root(tmp_path, 4, rows)
    _write_lists(root / "exports", members, core)
    return root


def test_scan_set_is_every_member_of_the_scope_core_or_not(tmp_path):
    """The Revvity case and the 19 dropped holdings in one: a Core name on no position
    list is scanned, and so is a held name that is NOT Core."""
    root = _scope_root(tmp_path, ["RVTY", "UBER", "TSM"], core=["RVTY"])
    assert [t.ticker for t in load_scan_universe(root)] == ["RVTY", "UBER", "TSM"]


def test_scan_set_does_not_read_the_watchlist(tmp_path):
    """A watchlist.csv beside the exports must not add or remove a name."""
    root = _scope_root(tmp_path, ["RVTY"], core=["RVTY"])
    (root / "exports" / "watchlist.csv").write_text(
        "Ticker,Company Name,Core\nGHOST,Ghost Inc,Y\n", encoding="utf-8")
    assert [t.ticker for t in load_scan_universe(root)] == ["RVTY"]


def test_missing_lists_json_raises_instead_of_falling_back(tmp_path):
    root = _scope_root(tmp_path, ["RVTY"], core=["RVTY"])
    (root / "exports" / "lists.json").unlink()
    with pytest.raises(ScanUniverseError, match="lists.json"):
        load_scan_universe(root)


def test_unknown_lists_schema_raises(tmp_path):
    root = _scope_root(tmp_path, ["RVTY"], core=["RVTY"])
    _write_lists(root / "exports", ["RVTY"], schema_version=2)
    with pytest.raises(ScanUniverseError, match="schema"):
        load_scan_universe(root)


@pytest.mark.parametrize("members, core", [
    (["UBER", "TSM"], ["RVTY"]),      # the scope lost its Core names
    (["UBER", "TSM"], []),            # Core itself came back empty
])
def test_a_scope_that_lost_core_raises(tmp_path, members, core):
    """~90 position names with no Core is plausible, non-empty, and exactly the miss."""
    rows = [{"Ticker": t, "Company Name": f"{t} Inc", "Sector (JP)": "Tech",
             "ISIN": "", "Core": ""} for t in ("UBER", "TSM", "RVTY")]
    root = _scope_root(tmp_path, members, core, rows)
    with pytest.raises(ScanUniverseError, match="Core"):
        load_scan_universe(root)


def test_a_scope_member_with_no_universe_row_raises_and_is_named(tmp_path):
    rows = [{"Ticker": "RVTY", "Company Name": "Revvity", "Sector (JP)": "MedTech",
             "ISIN": "", "Core": "Y"}]
    root = _scope_root(tmp_path, ["RVTY", "NOROW"], ["RVTY"], rows)
    with pytest.raises(ScanUniverseError, match="NOROW"):
        load_scan_universe(root)


def test_the_real_exports_load_and_include_every_core_name():
    """Against the live Coverage Manager exports when they are a sibling checkout."""
    from pathlib import Path
    cm = Path(__file__).resolve().parents[2] / "Coverage Manager"
    if not (cm / "exports" / "lists.json").exists():
        pytest.skip("Coverage Manager is a Dropbox sibling; absent in this checkout")
    meta = json.loads((cm / "exports" / "universe_metadata.json").read_text(encoding="utf-8-sig"))
    core = {t for t, v in meta.items() if str(v.get("core") or "").strip().upper() == "Y"}
    scanned = {t.ticker for t in load_scan_universe(cm)}
    assert core and core <= scanned

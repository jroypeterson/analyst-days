"""
Load the universe of tickers to track from Coverage Manager exports.

In dev, set COVERAGE_MANAGER_PATH to your local CM checkout. In CI, the
Coverage Manager exports are sparse-checked-out into a sibling directory.

Phase 1: core watchlist (`watchlist.csv` rows where `Core == 'Y'`).
Phase 2/3 swap the loader for HC Services / MedTech filters or the full
universe — the rest of the discovery pipeline is universe-agnostic.

We assert on schema_version so a Coverage Manager schema bump fails loudly
here instead of silently propagating bad data downstream.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

CM_WATCHLIST_SCHEMA_VERSION = 2  # exports/watchlist_status.json schema_version


@dataclass(frozen=True)
class Ticker:
    ticker: str
    company_name: str
    sector_jp: Optional[str]
    subsector_jp: Optional[str]
    sub_subsector_jp: Optional[str]
    yf_sector: Optional[str]
    yf_industry: Optional[str]
    cik: Optional[str]
    website: Optional[str]
    country_hq: Optional[str]
    isin: Optional[str]


def _resolve_cm_path(cm_path: Optional[str | Path] = None) -> Path:
    cm_path = cm_path or os.environ.get("COVERAGE_MANAGER_PATH")
    if not cm_path:
        raise RuntimeError(
            "COVERAGE_MANAGER_PATH not set. Point it at your Coverage Manager "
            "checkout (the directory containing exports/)."
        )
    p = Path(cm_path)
    if not (p / "exports").is_dir():
        raise FileNotFoundError(
            f"No exports/ directory under {p}. Is COVERAGE_MANAGER_PATH right?"
        )
    return p


def _assert_schema(cm_root: Path) -> None:
    status_path = cm_root / "exports" / "watchlist_status.json"
    if not status_path.exists():
        # Older CM checkouts may not have the watchlist status file yet.
        # Fall back to the universe status file's schema_version, which moves
        # in lockstep with the watchlist's.
        status_path = cm_root / "exports" / "universe_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    sv = int(status.get("schema_version", -1))
    if sv != CM_WATCHLIST_SCHEMA_VERSION:
        raise RuntimeError(
            f"Coverage Manager exports schema is v{sv}, expected "
            f"v{CM_WATCHLIST_SCHEMA_VERSION}. Update analyst-days "
            "to match the new schema before continuing."
        )


def _row_to_ticker(row: dict) -> Ticker:
    def g(*keys: str) -> Optional[str]:
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                return v
        return None

    return Ticker(
        ticker=row["Ticker"].strip(),
        company_name=g("Company Name") or "",
        sector_jp=g("Sector (JP)"),
        subsector_jp=g("Subsector (JP)"),
        sub_subsector_jp=g("Sub-subsector (JP)"),
        yf_sector=g("YF Sector"),
        yf_industry=g("YF Industry"),
        cik=g("CIK"),
        website=g("Website"),
        country_hq=g("Country (HQ)"),
        isin=g("ISIN"),
    )


def load_core_watchlist(cm_path: Optional[str | Path] = None) -> list[Ticker]:
    """Return all watchlist rows where Core == 'Y'.

    Continues to read `exports/watchlist.csv`, which Coverage Manager Phase B
    keeps writing as a derived back-compat view of `positions_and_researching.csv`
    for one cycle. This loader returns the union of Portfolio + Researching ∩ Core.

    For the explicit Portfolio-only or Researching-only split, use
    `load_portfolio()` and `load_researching()`.
    """
    cm_root = _resolve_cm_path(cm_path)
    _assert_schema(cm_root)

    csv_path = cm_root / "exports" / "watchlist.csv"
    out: list[Ticker] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("Core") or "").strip().upper() == "Y":
                out.append(_row_to_ticker(row))
    return out


def _load_position_json(cm_root: Path, filename: str) -> list[Ticker]:
    """Load tickers from exports/portfolio.json or exports/researching.json,
    filtered to Core == 'Y'."""
    path = cm_root / "exports" / filename
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    out: list[Ticker] = []
    for ticker, row in data.items():
        if not isinstance(row, dict):
            continue
        if (row.get("Core") or "").strip().upper() != "Y":
            continue
        # The JSON entries carry both legacy flat keys and raw universe columns.
        # _row_to_ticker reads the raw universe column names (e.g. "Company Name"),
        # so fall back to the legacy key ("name") when the raw column is absent.
        merged = dict(row)
        merged.setdefault("Ticker", ticker)
        merged.setdefault("Company Name", row.get("name", ""))
        merged.setdefault("Sector (JP)", row.get("sector", ""))
        merged.setdefault("Subsector (JP)", row.get("subsector", ""))
        merged.setdefault("Sub-subsector (JP)", row.get("sub_subsector", ""))
        out.append(_row_to_ticker(merged))
    return out


def load_portfolio(cm_path: Optional[str | Path] = None) -> list[Ticker]:
    """Return Position == 'Portfolio' rows where Core == 'Y' — names the user
    actively covers AND owns. Subset of `load_core_watchlist()`."""
    cm_root = _resolve_cm_path(cm_path)
    _assert_schema(cm_root)
    return _load_position_json(cm_root, "portfolio.json")


def load_researching(cm_path: Optional[str | Path] = None) -> list[Ticker]:
    """Return Position == 'Researching' rows where Core == 'Y' — names the user
    actively covers AND is building a thesis to buy. Subset of
    `load_core_watchlist()`."""
    cm_root = _resolve_cm_path(cm_path)
    _assert_schema(cm_root)
    return _load_position_json(cm_root, "researching.json")


def load_by_sectors(
    sectors_jp: Iterable[str],
    cm_path: Optional[str | Path] = None,
) -> list[Ticker]:
    """Phase 2/3: load all universe rows for the given Sector (JP) values."""
    cm_root = _resolve_cm_path(cm_path)
    _assert_schema(cm_root)

    wanted = {s.strip() for s in sectors_jp}
    csv_path = cm_root / "exports" / "universe.csv"
    out: list[Ticker] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("Sector (JP)") or "").strip() in wanted:
                out.append(_row_to_ticker(row))
    return out

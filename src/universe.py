"""
Load the universe of tickers to track from Coverage Manager exports.

In dev, set COVERAGE_MANAGER_PATH to your local CM checkout. In CI, the
Coverage Manager exports are sparse-checked-out into a sibling directory.

The scan set is the `analyst_days` scope of CM's `exports/lists.json`
(Portfolio + Researching + Ready to Buy + every Core=Y name + Following for
Interest), read BY NAME. This repo does not compose the union itself: until
2026-11-13 it did, from the legacy `watchlist.csv`, and scanned 75 names while
earnings_review covered 351 -- which is how Revvity's Investor Day was missed.

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

_ACCEPTED_CM_SCHEMA = frozenset({3, 4})  # v4 (2026-07-30): universe_metadata.json
# is keyed by the RAW ticker instead of a suffix-stripped one. `DIA.MI` was published
# as `DIA`, which broke `metadata[row["Ticker"]]` for 183 of 1,096 rows and silently
# dropped Rogers Corporation, since `ROG` and `ROG.SW` collided. Both versions are
# accepted so this repo works either side of CM republishing.  # exports/watchlist_status.json schema_version
# CM exports schema. Adding a CSV column does NOT bump this (the `LEI` and
# `IPO Date`/`Est Lockup` backfills all landed in the CSVs, and in portfolio.json
# entry keys, with no bump); only a change to `universe_metadata.json`'s entry
# shape does -- that is what the 2026-05-06 v3 bump was for. So new identity
# columns arriving in watchlist.csv are expected to show up under v3, and this
# loader reads by column name via `_row_to_ticker`, so they are inert for it.
#
# Bumped 2->3 on 2026-06-29 to match Coverage Manager exports schema v3
# (sibling sa-monitor took the same bump in 565af1c on 2026-06-14). The v3
# watchlist.csv columns this loader reads (Ticker, Company Name, Sector (JP),
# Subsector (JP), Sub-subsector (JP), YF Sector, YF Industry, CIK, Website,
# Country (HQ), ISIN, Core) are unchanged, so this is a pure gate bump.
#
# Briefly widened to {3, 4} on 2026-07-28 for an anticipated v4 bump that was
# then disproven (see plans/identity_export_v4_release_2026-07-28.md, R1) and
# narrowed back the same night. The frozenset form is kept deliberately: the
# error message names the accepted set, and a real future bump is a one-line
# change here.


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
    # universe_status.json, not watchlist_status.json: the watchlist files are a
    # deprecated back-compat view CM is retiring, and a gate hung on a file that is
    # about to disappear would fail the run for the wrong reason.
    status_path = cm_root / "exports" / "universe_status.json"
    # utf-8-sig: a BOM here raises `JSONDecodeError: Expecting value: line 1
    # column 1` from inside the schema gate, which reads as "CM broke its
    # schema" rather than "encoding" -- a misleading diagnosis. Tolerant in,
    # strict out (we still write utf-8 ourselves).
    status = json.loads(status_path.read_text(encoding="utf-8-sig"))
    sv = int(status.get("schema_version", -1))
    if sv not in _ACCEPTED_CM_SCHEMA:
        accepted = ", ".join(f"v{v}" for v in sorted(_ACCEPTED_CM_SCHEMA))
        raise RuntimeError(
            f"Coverage Manager exports schema is v{sv}, expected one of "
            f"{accepted}. Update analyst-days "
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


SCAN_SCOPE = "analyst_days"
_ACCEPTED_LISTS_SCHEMA = frozenset({1})


class ScanUniverseError(RuntimeError):
    """The scan set could not be established. Raised, never defaulted: falling back
    to a smaller list is exactly the failure this loader replaced."""


def load_scan_universe(cm_path: Optional[str | Path] = None) -> list[Ticker]:
    """Every name discovery scans: the `analyst_days` scope of CM's lists.json.

    JP 2026-11-13: "I want all of my core stocks to be flagged ... the same stocks
    that we flag for earnings review I want to flag here", and on Core: "Core means
    I want to stay up on it and if there is one thing to pay attention to for a
    stock each year it is the analyst day." Following for Interest stays in (his
    2026-08-23 ruling: bellwethers whose analyst days are the point of the list).

    Membership follows the lists -- a name gaining or losing Core moves on the next
    run with no edit here. Identity fields (name, CIK, website) come from
    `universe.csv`, joined on the raw ticker.

    There is NO fallback to `watchlist.csv` or to any smaller set. A missing or
    unreadable lists.json, an unknown schema, or a scope that has lost Core raises.
    """
    cm_root = _resolve_cm_path(cm_path)
    _assert_schema(cm_root)

    lists_path = cm_root / "exports" / "lists.json"
    if not lists_path.exists():
        raise ScanUniverseError(
            f"{lists_path} is missing. Coverage Manager publishes it from the "
            "positions export; refusing to fall back to a smaller scan set.")
    doc = json.loads(lists_path.read_text(encoding="utf-8-sig"))
    sv = doc.get("schema_version") if isinstance(doc, dict) else None
    if sv not in _ACCEPTED_LISTS_SCHEMA:
        raise ScanUniverseError(
            f"lists.json schema is {sv!r}, expected one of "
            f"{sorted(_ACCEPTED_LISTS_SCHEMA)}. Update analyst-days before continuing.")
    try:
        members = list(doc["scopes"][SCAN_SCOPE]["members"])
        core = set(doc["lists"]["core"]["members"])
    except (KeyError, TypeError) as e:
        raise ScanUniverseError(
            f"lists.json has no usable scopes.{SCAN_SCOPE} / lists.core ({e!r})") from e
    # A scope that silently lost Core is ~90 position names: plausible, non-empty,
    # and the exact shape of the miss this loader exists to prevent.
    if not core or not core <= set(members):
        raise ScanUniverseError(
            f"lists.json: scopes.{SCAN_SCOPE} ({len(members)} names) does not contain "
            f"the Core list ({len(core)} names). Refusing to scan a partial universe.")

    rows: dict[str, dict] = {}
    csv_path = cm_root / "exports" / "universe.csv"
    # utf-8-sig: a BOM on CM's export otherwise makes the first field "﻿Ticker"
    # and every row lookup below silently misses.
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = (row.get("Ticker") or "").strip()
            if t:
                rows[t] = row

    missing = [t for t in members if t not in rows]
    if missing:
        # Every scope member is a universe row by CM's own acceptance check, so this
        # is a broken publish, not a detail. Naming them beats scanning a name with
        # no company name (a web search for "" returns confident noise).
        raise ScanUniverseError(
            f"{len(missing)} scan-scope ticker(s) have no universe.csv row: "
            f"{missing[:10]}")
    return [_row_to_ticker(rows[t]) for t in members]


def _load_position_json(cm_root: Path, filename: str) -> list[Ticker]:
    """Load tickers from exports/portfolio.json or exports/researching.json,
    filtered to Core == 'Y'."""
    path = cm_root / "exports" / filename
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))  # tolerate a CM BOM
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
    actively covers AND owns. Subset of `load_scan_universe()`."""
    cm_root = _resolve_cm_path(cm_path)
    _assert_schema(cm_root)
    return _load_position_json(cm_root, "portfolio.json")


def load_researching(cm_path: Optional[str | Path] = None) -> list[Ticker]:
    """Return Position == 'Researching' rows where Core == 'Y' — names the user
    actively covers AND is building a thesis to buy. Subset of
    `load_scan_universe()`."""
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
    # utf-8-sig for the same reason as load_scan_universe above: 3df75d0 fixed
    # only that one call site, and this reader has the identical BOM exposure.
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("Sector (JP)") or "").strip() in wanted:
                out.append(_row_to_ticker(row))
    return out

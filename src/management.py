"""Strata and the three management modes: uniform, by zone, per cell.

Strata
------
A stratum is one AgERA5 weather cell with one water regime. ``strata()``
builds them from the 100 m rasters: rice area per cell (``rasters.aggregate``),
the irrigation class (MoA, gaps filled from the nearest class) mapped to a
regime, and the dominant agro-ecological zone of the cell's rice. Every stratum
carries ``ha``, so results can be area-weighted. One soil profile and one
weather file per cell (``soil_id`` XS<iy><ix>, ``wsta`` W<iy><ix>).

Seasons and years
-----------------
Dates are placed in an agricultural year that starts on ``ANCHOR`` (1 Aug):
``season_year`` Y runs from 1 Aug Y to 31 Jul Y+1, so a wet season planted in
Nov Y and the dry season planted in Apr Y+1 share the label Y.

Modes
-----
All three return the units table that ``runner.run_units`` takes (``cell``,
``wsta``, ``soil_id``, ``pdate``, ``trno_template`` plus stratum columns):

- ``uniform``: the same calendar everywhere (``Season.month_day``).
- ``by_zone``: a table zone -> season -> month-day, looked up by a stratum
  column (default ``aez``).
- ``per_cell``: planting days per cell, e.g. from ``onset`` (rainfall onset
  computed from AgERA5 per cell).

Settings
--------
Calendar, agricultural year start, irrigation classes and the template come
from the experiment file (``config.CURRENT``, see configs/rice-java.yml); the
module attributes below are its values and the functions take them as
arguments where a run may differ. Template treatments are found by their
irrigation (MI) and fertilizer (MF) levels (``template_treatment``); in
``templates/XDRI8601.RIX`` water level 1 is a constant 50 mm flood and 3 is
rainfed, bunded (``scripts/make_templates.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
from filex import FileX

ANCHOR = config.CURRENT.anchor        # agricultural year start (seasons > year_starts), default 1 Aug
TEMPLATE = config.CURRENT.template_file
# MoA irrigation classes (scripts/prepare_rasters.py MOA_CLASSES) -> water regime
# (treatments > water_regimes > irrigation_classes). By default all three
# irrigated classes are treated as fully irrigated.
REGIME = config.CURRENT.regime_of_class()


@dataclass(frozen=True)
class Season:
    name: str
    regimes: tuple = ("irrigated", "rainfed")
    month_day: str | None = None          # "MM-DD" for uniform calendars


def seasons_from(exp: "config.Experiment") -> tuple[Season, ...]:
    """The calendar of an experiment (seasons > calendar)."""
    return tuple(Season(s["name"], tuple(s["regimes"]), s["date"]) for s in exp.data["seasons"]["calendar"])


# Placeholders, not an agronomic calendar: by default wet season (MT-I) on 15 Nov
# for all rice, dry season (MT-II) on 1 Apr for irrigated rice only.
DEFAULT_SEASONS = seasons_from(config.CURRENT)


# ------------------------------------------------------------ template treatments


def template_cultivars(template: FileX) -> dict[str, tuple[int, str]]:
    """Cultivars of the template's *CULTIVARS section: CNAME -> (level, INGENO)."""
    out = {}
    for lv, block in template.blocks("CU").items():
        row = next(ln for ln in block if not ln.startswith("@"))
        out[row[13:].strip()] = (lv, row[6:12].strip())
    return out


def template_treatment(template: FileX, water_level: int, n_level: int, ingeno: str | None = None,
                       name: str | None = None):
    """The template treatment to copy for a water level (MI), N level (MF) and cultivar.

    Returns ``(treatment number, overrides)``. A cultivar that the template
    lists keeps its own cultivar level; any other cultivar takes a treatment
    of the first matching level and the overrides ``CU.INGENO`` (and
    ``CU.CNAME``) put its code in. When several treatments match, the last one
    is used: in ``templates/XDRI8601.RIX`` that is the generated grid (treatments
    12 onward), the treatments xdssat ran before the experiment file existed.
    """
    trt = template.treatments()
    sel = trt[(trt.MI == water_level) & (trt.MF == n_level)]
    if sel.empty:
        raise ValueError(f"template has no treatment with MI {water_level} and MF {n_level}")
    levels = {code: lv for lv, code in template_cultivars(template).values()}
    if ingeno in levels and (sel.CU == levels[ingeno]).any():
        return int(sel.index[sel.CU == levels[ingeno]][-1]), {}
    over = {"CU.INGENO": ingeno} if ingeno else {}
    if ingeno and name:
        over["CU.CNAME"] = name
    return int(sel.index[-1]), over


# ------------------------------------------------------------ strata


def strata(stats: pd.DataFrame, region_codes: dict, region: str | None = None,
           zone_col: str | None = "aez", regime: dict | None = None) -> pd.DataFrame:
    """Strata from a ``rasters.aggregate`` table with ``region`` and ``irr`` columns.

    ``region_codes`` maps region name to its raster code. Rice outside
    ``region`` is dropped; rice with irrigation class 0 (no MoA data) raises,
    because the regime would be unknown. ``region`` defaults to aoi > region and
    ``regime`` (irrigation class -> water regime) to ``REGIME``.
    """
    region = region or config.CURRENT.region
    regime = REGIME if regime is None else regime
    s = stats[stats["region"] == region_codes[region]].copy()
    if (s["irr"] == 0).any():
        raise ValueError(f"{(s['irr'] == 0).sum()} strata have no irrigation class; "
                         "use the filled irrigation raster")
    s["water"] = s["irr"].map(regime)
    if s["water"].isna().any():
        bad = sorted(s.loc[s["water"].isna(), "irr"].unique())
        raise ValueError(f"irrigation classes {bad} have no water regime "
                         "(treatments > water_regimes > irrigation_classes)")
    keys = ["iy", "ix", "lat", "lon"]
    out = s.groupby(keys + ["water"], as_index=False).agg(ha=("ha", "sum"))
    if zone_col and zone_col in s:
        dom = (s.groupby(keys + [zone_col]).ha.sum().reset_index()
                 .sort_values("ha", ascending=False).drop_duplicates(keys))
        out = out.merge(dom[keys + [zone_col]], on=keys, how="left")
    out["cell"] = out.iy.astype(int) * 10000 + out.ix.astype(int)
    out["wsta"] = [f"W{iy:03d}{ix:04d}" for iy, ix in zip(out.iy, out.ix)]
    out["soil_id"] = [f"XS{iy:04d}{ix:04d}" for iy, ix in zip(out.iy, out.ix)]
    out["stratum"] = range(len(out))
    return out.sort_values(["iy", "ix", "water"]).reset_index(drop=True)


# ------------------------------------------------------------ dates


def anchor_date(season_year: int) -> pd.Timestamp:
    return pd.Timestamp(season_year, *ANCHOR)


def date_in_year(season_year: int, month_day: str) -> pd.Timestamp:
    """The date of ``month_day`` inside agricultural year ``season_year``."""
    m, d = (int(v) for v in month_day.split("-"))
    year = season_year if (m, d) >= ANCHOR else season_year + 1
    return pd.Timestamp(year, m, d)


def _units(rows: list[dict], strata_df: pd.DataFrame) -> pd.DataFrame:
    u = pd.DataFrame(rows)
    u = u.merge(strata_df, on="stratum", how="left")
    return u.sort_values(["season_year", "iy", "ix", "pdate"]).reset_index(drop=True)


# ------------------------------------------------------------ modes


def uniform(strata_df: pd.DataFrame, years, seasons=None) -> pd.DataFrame:
    """Same calendar for every stratum."""
    seasons = seasons or DEFAULT_SEASONS
    rows = []
    for st in strata_df.itertuples():
        for y in years:
            for s in seasons:
                if st.water in s.regimes:
                    rows.append(dict(stratum=st.stratum, season=s.name, season_year=y,
                                     pdate=date_in_year(y, s.month_day)))
    return _units(rows, strata_df)


def by_zone(strata_df: pd.DataFrame, years, table: pd.DataFrame, zone_col: str = "aez",
            seasons=None) -> pd.DataFrame:
    """Calendar per zone. ``table`` has columns ``zone``, ``season``, ``month_day``.

    Zones absent from ``table`` use the ``month_day`` of ``seasons``.
    """
    seasons = seasons or DEFAULT_SEASONS
    look = {(z, s): md for z, s, md in table[["zone", "season", "month_day"]].itertuples(index=False)}
    rows = []
    for st in strata_df.itertuples():
        z = getattr(st, zone_col)
        for y in years:
            for s in seasons:
                if st.water in s.regimes:
                    md = look.get((z, s.name), s.month_day)
                    rows.append(dict(stratum=st.stratum, season=s.name, season_year=y,
                                     pdate=date_in_year(y, md), zone_used=(z, s.name) in look))
    return _units(rows, strata_df)


def per_cell(strata_df: pd.DataFrame, years, plant_days: dict[str, pd.Series],
             seasons=None) -> pd.DataFrame:
    """Planting day per cell and season, as days after the 1 Aug anchor.

    ``plant_days[season]`` is a Series indexed by (iy, ix). Cells missing from
    a season's Series fall back to that season's ``month_day``.
    """
    seasons = seasons or DEFAULT_SEASONS
    rows = []
    for st in strata_df.itertuples():
        for y in years:
            a = anchor_date(y)
            for s in seasons:
                if st.water not in s.regimes:
                    continue
                days = plant_days.get(s.name, pd.Series(dtype=float)).get((st.iy, st.ix))
                pdate = (a + pd.Timedelta(days=int(days)) if days is not None and np.isfinite(days)
                         else date_in_year(y, s.month_day))
                rows.append(dict(stratum=st.stratum, season=s.name, season_year=y, pdate=pdate))
    return _units(rows, strata_df)


# ------------------------------------------------------------ rainfall onset


def dasarian_totals(dates: pd.DatetimeIndex, rain: np.ndarray):
    """Sum daily rain (time, cells) into dasarian: days 1-10, 11-20, 21-end of month.

    Returns (start date of each dasarian, totals (n_dasarian, cells)).
    """
    d = pd.DatetimeIndex(dates)
    key = d.year * 100 + d.month
    part = np.minimum((d.day - 1) // 10, 2)
    lab = key.to_numpy() * 10 + part
    starts = np.r_[0, np.nonzero(np.diff(lab))[0] + 1]
    totals = np.add.reduceat(np.nan_to_num(rain, nan=0.0), starts, axis=0)
    return d[starts], totals


def onset(dates, rain, season_year: int, threshold: float = 50.0, n: int = 3) -> np.ndarray:
    """Wet-season onset per cell for one agricultural year, as days after the year start (1 Aug).

    BMKG rule: the first dasarian with at least ``threshold`` mm of rain that is
    followed by ``n - 1`` more dasarian also at least ``threshold`` (BMKG
    Stasiun Klimatologi Sumatera Selatan, Buletin Prakiraan Musim Hujan
    2025/2026; Jurnal Meteorologi dan Geofisika, doi:10.31172/jmg.v13i1.113).
    NaN where no onset is found before 31 Jul.
    """
    a = anchor_date(season_year)
    keep = (dates >= a) & (dates < anchor_date(season_year + 1))
    starts, tot = dasarian_totals(dates[keep], rain[keep])
    wet = tot >= threshold
    run = wet.copy()
    for k in range(1, n):
        run[:-k] &= wet[k:]
        run[-k:] = False
    first = np.where(run.any(axis=0), run.argmax(axis=0), -1)
    days = (starts - a).days.to_numpy()
    return np.where(first >= 0, days[np.clip(first, 0, None)], np.nan).astype(float)


def onset_climatology(dates, rain, years, **kw) -> tuple[np.ndarray, np.ndarray]:
    """Onset (days after 1 Aug) per year and cell, and its median over years."""
    per_year = np.vstack([onset(dates, rain, y, **kw) for y in years])
    with np.errstate(all="ignore"):
        med = np.nanmedian(per_year, axis=0)
    return per_year, med


def _names(seasons) -> list[str]:
    return [s.name for s in (seasons or DEFAULT_SEASONS)]


def planting_from_onset(cells: pd.DataFrame, onset_days: np.ndarray, transplant_after: int = 20,
                        dry_gap: int = 120, seasons=None) -> dict[str, pd.Series]:
    """Planting days per cell: the first season of the calendar (wet) = onset +
    ``transplant_after``; each later season (dry) ``dry_gap`` days after the one before.

    ``transplant_after``: seedbed sown at onset, transplanted about 20 days
    later (the template transplants 23-day-old seedlings). ``dry_gap``: wet
    season field duration plus turnaround. Both are assumptions to tune
    (seasons > onset in the experiment file).
    """
    idx = pd.MultiIndex.from_arrays([cells.iy.to_numpy(), cells.ix.to_numpy()])
    day = pd.Series(np.round(onset_days + transplant_after), index=idx)
    out = {}
    for k, name in enumerate(_names(seasons)):
        out[name] = day + k * dry_gap
    return out


def zone_table_from_onset(cells: pd.DataFrame, zone_col: str = "aez", transplant_after: int = 20,
                          dry_gap: int = 120, ref_year: int = 2001, seasons=None) -> pd.DataFrame:
    """A by-zone calendar from per-cell onset: the median onset of each zone's cells.

    ``cells`` has ``onset_days`` (days after the year start) and ``zone_col``.
    Returns columns zone, season, month_day, onset_days, cells (for ``by_zone``).
    """
    g = cells.dropna(subset=["onset_days"]).groupby(zone_col).onset_days.agg(["median", "size"])
    rows = []
    for z, r in g.iterrows():
        first = anchor_date(ref_year) + pd.Timedelta(days=int(round(r["median"])) + transplant_after)
        for k, name in enumerate(_names(seasons)):
            d = first + pd.Timedelta(days=k * dry_gap)
            rows.append(dict(zone=z, season=name, month_day=d.strftime("%m-%d"),
                             onset_days=r["median"], cells=int(r["size"])))
    return pd.DataFrame(rows)

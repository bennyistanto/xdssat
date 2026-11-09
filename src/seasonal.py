"""Seasonal analysis of run folders: how results vary over the years, per cell and per region.

The DSSAT seasonal analysis tool runs one field over many weather years and shows the spread
of yields (box plots, cumulative distributions, mean-variance plots), optionally with prices and
costs. xdssat runs every cell over many years already; this module summarises a run folder's
``summary.csv`` the same way:

* ``season_table``: one row per simulated season with the variable of interest (default
  ``HWAH``, grain yield, dry matter) and, with ``Economics``, the gross margin per ha.
* ``cell_stats``: per weather cell, water regime and season, statistics over the years.
* ``region_series``: per year, the area-weighted mean over all cells, per (water regime, season)
  and for all of them together (``system`` "all").
* ``distribution``: statistics of a series over the years (mean, sd, cv, quantiles, share of
  years below a threshold).
* ``cdf``: the empirical cumulative distribution of a series (Weibull plotting position
  rank / (n + 1)), for cumulative-probability plots.
* ``mean_variance``: mean and standard deviation over the years for several run folders (e.g.
  one per scenario or N rate), for mean-variance comparisons.

The engine has no economics (no reader of price files, temp/engine-capability-inventory.md);
the gross margin is computed here: price x yield - fixed costs - N price x N applied (NICM) -
irrigation price x irrigation applied (IRCM).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


@dataclass(frozen=True)
class Economics:
    """Prices and costs per ha, in one currency. ``price`` is per kg of the yield variable
    (HWAH is dry matter; convert a market price per kg at its moisture content first)."""
    price: float
    fixed_cost: float = 0.0
    n_cost: float = 0.0                 # per kg N applied (NICM)
    irrigation_cost: float = 0.0        # per mm applied (IRCM)

    def margin(self, s: pd.DataFrame, var: str = "HWAH") -> pd.Series:
        n = _num(s["NICM"]).fillna(0) if "NICM" in s else 0.0
        irr = _num(s["IRCM"]).fillna(0) if "IRCM" in s else 0.0
        return self.price * _num(s[var]) - self.fixed_cost - self.n_cost * n - self.irrigation_cost * irr


def _num(v: pd.Series) -> pd.Series:
    """DSSAT summary values as numbers; -99 and other negatives (missing) to NaN."""
    n = pd.to_numeric(v, errors="coerce")
    return n.where(n >= 0)


def read_run(run_dir: Path) -> pd.DataFrame:
    return pd.read_csv(Path(run_dir) / "summary.csv", low_memory=False)


def season_table(summary: pd.DataFrame, var: str = "HWAH", economics: Economics | None = None) -> pd.DataFrame:
    """One row per season: keys, area (ha), the variable as ``value``, and ``margin``."""
    keys = [c for c in ("iy", "ix", "lat", "lon", "water", "season", "season_year", "ha") if c in summary]
    out = summary[keys].copy()
    out["value"] = _num(summary[var])
    if economics is not None:
        out["margin"] = economics.margin(summary, var)
    return out


def distribution(x, below: float | None = None) -> dict:
    """Statistics of a series over the years (NaN years left out)."""
    v = pd.Series(x, dtype=float).dropna()
    d = {"n": int(v.size), "mean": v.mean(), "sd": v.std(ddof=1) if v.size > 1 else np.nan}
    d["cv"] = d["sd"] / d["mean"] if d["mean"] else np.nan
    d["min"], d["max"] = v.min(), v.max()
    for q in QUANTILES:
        d[f"p{int(round(q * 100)):02d}"] = v.quantile(q)
    if below is not None:
        d["share_below"] = float((v < below).mean()) if v.size else np.nan
    return d


def cell_stats(seasons: pd.DataFrame, column: str = "value", below: float | None = None) -> pd.DataFrame:
    """Per cell, water regime and season: ``distribution`` over the years."""
    keys = [c for c in ("iy", "ix", "lat", "lon", "water", "season") if c in seasons]
    rows = []
    for k, g in seasons.groupby(keys, sort=True):
        rows.append({**dict(zip(keys, k)), "ha": g["ha"].iloc[0] if "ha" in g else np.nan,
                     **distribution(g[column], below)})
    return pd.DataFrame(rows)


def region_series(seasons: pd.DataFrame, column: str = "value") -> pd.DataFrame:
    """Area-weighted mean per year: per (water, season) and over all systems (``all``)."""
    s = seasons.dropna(subset=[column])
    s = s.assign(w=s[column] * s["ha"])
    by = s.groupby(["water", "season", "season_year"]).agg(w=("w", "sum"), ha=("ha", "sum")).reset_index()
    by["system"] = by["season"] + "_" + by["water"]
    tot = s.groupby("season_year").agg(w=("w", "sum"), ha=("ha", "sum")).reset_index()
    tot["system"] = "all"
    out = pd.concat([by[["system", "season_year", "w", "ha"]], tot[["system", "season_year", "w", "ha"]]])
    out[column] = out["w"] / out["ha"]
    return out.drop(columns="w").rename(columns={"ha": "harvested_ha"}).reset_index(drop=True)


def region_stats(series: pd.DataFrame, column: str = "value", below: float | None = None) -> pd.DataFrame:
    return pd.DataFrame([{"system": k, **distribution(g[column], below)} for k, g in series.groupby("system")])


def cdf(series: pd.DataFrame, column: str = "value") -> pd.DataFrame:
    """Empirical CDF per system: sorted values with probability rank / (n + 1)."""
    rows = []
    for k, g in series.groupby("system"):
        v = np.sort(g[column].dropna().to_numpy())
        n = v.size
        rows += [{"system": k, "rank": i + 1, column: x, "probability": (i + 1) / (n + 1)}
                 for i, x in enumerate(v)]
    return pd.DataFrame(rows)


def mean_variance(runs: dict[str, pd.DataFrame], column: str = "value") -> pd.DataFrame:
    """``runs``: label -> region series; mean and sd over the years per label and system."""
    rows = []
    for label, series in runs.items():
        for k, g in series.groupby("system"):
            d = distribution(g[column])
            rows.append({"run": label, "system": k, "mean": d["mean"], "sd": d["sd"], "cv": d["cv"],
                         "p10": d["p10"], "p50": d["p50"], "p90": d["p90"], "n": d["n"]})
    return pd.DataFrame(rows)


def summarise(run_dir: Path, var: str = "HWAH", economics: Economics | None = None,
              below: float | None = None) -> dict[str, pd.DataFrame]:
    """Every table of one run folder, for ``value`` and (with economics) ``margin``."""
    s = season_table(read_run(run_dir), var, economics)
    out = {}
    for column in ("value", "margin") if economics is not None else ("value",):
        series = region_series(s, column)
        tag = "" if column == "value" else "_margin"
        out[f"seasonal_cells{tag}"] = cell_stats(s, column, below if column == "value" else None)
        out[f"seasonal_years{tag}"] = series
        out[f"seasonal_region{tag}"] = region_stats(series, column, below if column == "value" else None)
        out[f"seasonal_cdf{tag}"] = cdf(series, column)
    return out

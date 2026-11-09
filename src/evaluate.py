"""Simulated against observed yields by region and year, for any area, crop and region map.

The Java case study compares its runs with district statistics through scripts written for
its own rasters and the BDSP tables (compare_districts.py, fit_slpf.py). This module does the
same from an experiment's prepared inputs and a plain table of observations, so it works for
any country:

* ``crop_pieces``: crop hectares per (weather cell, water regime, region), from the
  experiment's crop mask and water regime input, exactly as a run splits them into strata
  (``run_aoi.build_strata``), with a region input (a class map, e.g. districts made with
  ``prepare_layers.py``) on top. Without a region input everything is one region, ``all``.
* ``read_observed``: a CSV with columns ``region`` (a class name of the region input, or
  ``all``), ``year`` and ``yield``, optionally ``area`` (harvested ha) and ``group`` (e.g. a
  province, for leave-one-group-out checks). ``units`` says what ``yield`` is in: kg/ha,
  t/ha or ku/ha (100 kg/ha).
* ``simulated``: each season goes to the calendar year of its harvest (HDAT, else MDAT) or to
  its agricultural year (``year_basis="season"``); the region yield of a year is the
  area-weighted mean over the seasons in it. Simulated yield (HWAH) is dry matter; it is
  divided by (1 - moisture) to compare with yields reported at a moisture content (0.14 by
  default for grain, an assumption to check against the statistics' own definition).
* ``compare``: per region the mean over the years both have, the ratio sim / obs, trends and
  the detrended and high-pass correlations of the yearly values; overall the area-weighted
  level, the spatial correlation of region means and the median year-to-year correlation.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

UNITS = {"kg/ha": 1.0, "t/ha": 1000.0, "ku/ha": 100.0}


def crop_pieces(exp, region_input: str | None = None, ins: dict | None = None) -> pd.DataFrame:
    """Crop ha per (iy, ix, water, region) of the experiment's area."""
    import inputs
    import management as mg
    import rasters
    ins = ins or inputs.open_all(exp)
    lat, lon, valid = ins["weather"].grid()
    crop = ins["crop_mask"]
    grid, _ = crop.grid().window(*exp.bbox)
    layers, weights = {}, {}
    reg = inputs.open_dataset("region", region_input, exp.datasets()) if region_input else None
    if reg is not None:
        layers["eval_region"] = reg.file()
    if ins.get("region") is not None:
        layers["region"] = ins["region"].file()
    wr = ins.get("water_regime")
    if wr is not None:
        (weights if wr.is_share else layers)["irr"] = wr.file()
    stats = rasters.aggregate(crop.file(), layers, lat, lon, valid, grid=grid, units_per_ha=crop.units_per_ha,
                              class_band=crop.class_band, weights=weights, sub=rasters.subdivision(grid))
    if ins.get("region") is not None and exp.region is not None:       # the run's own region filter
        stats = stats[stats["region"] == ins["region"].codes()[exp.region]]
    stats = stats.drop(columns=["region"], errors="ignore")
    tr = exp.data["treatments"]
    name_of = {v: k for k, v in reg.codes().items()} if reg is not None else {}
    parts = []
    for code, g in (stats.groupby("eval_region") if reg is not None else [("all", stats)]):
        st = mg.strata(g.drop(columns=["eval_region"], errors="ignore"), None, None, zone_col=None,
                       regime=exp.regime_of_class(), default_regime=tr["default_regime"],
                       share_regimes=tr["share_regimes"])
        parts.append(st[["iy", "ix", "water", "ha"]].assign(region=str(name_of.get(code, code))))
    return pd.concat(parts, ignore_index=True)


def read_observed(path: Path, units: str = "kg/ha") -> pd.DataFrame:
    """Observed yields in kg/ha: columns region, year, obs (+ obs_area, group when given)."""
    if units not in UNITS:
        raise ValueError(f"units {units!r}: use one of {', '.join(UNITS)}")
    t = pd.read_csv(path, dtype={"region": str, "group": str})
    miss = {"region", "year", "yield"} - set(t.columns)
    if miss:
        raise ValueError(f"{path}: needs columns region, year, yield (missing {', '.join(sorted(miss))})")
    out = pd.DataFrame({"region": t["region"].astype(str), "year": t["year"].astype(int),
                        "obs": pd.to_numeric(t["yield"], errors="coerce") * UNITS[units]})
    if "area" in t:
        out["obs_area"] = pd.to_numeric(t["area"], errors="coerce")
    if "group" in t:
        out["group"] = t["group"]
    return out.dropna(subset=["obs"])


def season_years(summary: pd.DataFrame, year_basis: str = "harvest") -> pd.Series:
    if year_basis == "season":
        return summary["season_year"].astype(int)
    hd = pd.to_numeric(summary["HDAT"], errors="coerce")
    hd = hd.where(hd > 1000000, pd.to_numeric(summary["MDAT"], errors="coerce"))
    return (hd // 1000).astype("Int64")


def simulated(summary: pd.DataFrame, pieces: pd.DataFrame, moisture: float = 0.14,
              year_basis: str = "harvest") -> pd.DataFrame:
    """Area-weighted simulated yield (kg/ha at ``moisture``) per region and year."""
    s = summary[["iy", "ix", "water", "season", "season_year", "HWAH", "HDAT", "MDAT"]].copy()
    s["HWAH"] = pd.to_numeric(s.HWAH, errors="coerce").where(lambda v: v >= 0)
    s["year"] = season_years(s, year_basis)
    m = s.merge(pieces, on=["iy", "ix", "water"])
    m = m[m.HWAH.notna()]
    g = m.assign(w=m.ha * m.HWAH).groupby(["region", "year"])
    out = pd.DataFrame({"sim_dm": g.w.sum() / g.ha.sum(), "harvested_ha": g.ha.sum(),
                        "seasons": g.size()}).reset_index()
    out["sim"] = out.sim_dm / (1 - moisture)
    return out


def detrended_r(x, y, t) -> float:
    if len(t) < 8:
        return np.nan
    rx = x - np.polyval(np.polyfit(t, x, 1), t)
    ry = y - np.polyval(np.polyfit(t, y, 1), t)
    return float(np.corrcoef(rx, ry)[0, 1])


def highpass_r(x, y, t, window: int = 5) -> float:
    """Correlation of the deviations from a centred running mean."""
    if len(t) < 8:
        return np.nan
    s = pd.DataFrame({"x": x, "y": y}, index=t).sort_index()
    s = s.reindex(range(int(min(t)), int(max(t)) + 1))
    h = (s - s.rolling(window, center=True, min_periods=3).mean()).dropna()
    return float(h.x.corr(h.y)) if len(h) >= 8 else np.nan


def compare(sim: pd.DataFrame, obs: pd.DataFrame, pieces: pd.DataFrame, min_years: int = 5,
            min_ha: float = 0.0) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(years table, regions table, overall statistics)."""
    ha = pieces.groupby("region").ha.sum().rename("crop_ha")
    j = sim.merge(obs, on=["region", "year"], how="left").join(ha, on="region")
    j = j[j.crop_ha >= min_ha]
    j["ratio"] = j.sim / j.obs
    if "obs_area" in j:
        j["area_ratio"] = j.harvested_ha / j.obs_area
    rows = []
    for region, g in j.dropna(subset=["obs"]).groupby("region"):
        g = g.sort_values("year")
        if len(g) < min_years:
            continue
        t, x, y = g.year.to_numpy(), g.sim.to_numpy(), g.obs.to_numpy()
        rows.append(dict(region=region, group=g["group"].iloc[0] if "group" in g else None,
                         crop_ha=g.crop_ha.iloc[0], years=len(g), obs_mean=y.mean(), sim_mean=x.mean(),
                         ratio=x.mean() / y.mean(), obs_trend_decade=np.polyfit(t, y, 1)[0] * 10,
                         sim_trend_decade=np.polyfit(t, x, 1)[0] * 10, r_detrended=detrended_r(x, y, t),
                         r_highpass=highpass_r(x, y, t),
                         area_ratio=float(np.nanmedian(g.area_ratio)) if "area_ratio" in g else np.nan))
    d = pd.DataFrame(rows)
    overall = {"regions": len(d)}
    if len(d):
        w = d.crop_ha
        overall.update(level=float(np.average(d.ratio, weights=w)),
                       ratio_p10=float(np.percentile(d.ratio, 10)), ratio_p90=float(np.percentile(d.ratio, 90)),
                       spatial_r=float(d[["obs_mean", "sim_mean"]].corr().iloc[0, 1]) if len(d) > 2 else np.nan,
                       r_detrended_median=float(d.r_detrended.median()),
                       r_highpass_median=float(d.r_highpass.median()),
                       share_r_positive=float((d.r_detrended.dropna() > 0).mean()))
    return j, d, overall

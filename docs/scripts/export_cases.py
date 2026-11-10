"""Export the small data files the case-study pages read (docs/cases/data/<case>/).

    python docs/scripts/export_cases.py niger           # one case
    python docs/scripts/export_cases.py all

Each case reads its run folders in <cache>/runs (made with configs/cases/<case>.yml and
scripts/run_aoi.py) and writes CSV/JSON tables only: no engine runs at render time.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import seasonal  # noqa: E402

OUT = ROOT / "docs" / "cases" / "data"


def _num(v):
    n = pd.to_numeric(v, errors="coerce")
    return n.where(n >= 0)


def _weighted(g, col):
    v = _num(g[col])
    ok = v.notna()
    return float((v[ok] * g.ha[ok]).sum() / g.ha[ok].sum()) if ok.any() else np.nan


def niger(runs: Path, out: Path, poor: float = 300.0) -> dict:
    """Niger pearl millet: sowing date x N rate, 1991-2023 (configs/cases/niger-millet.yml)."""
    out.mkdir(parents=True, exist_ok=True)
    rows, cells, info = [], [], {}
    for n in (0, 45):
        for k, off in enumerate(range(0, 80, 10)):
            day = date(2001, 6, 1) + timedelta(days=off)
            name = f"_sow{day:%m%d}"                  # every scenario is named after its date
            folder = runs / f"niger_millet_uniform_1991-2023_civt_n{n}{name}"
            s = pd.read_csv(folder / "summary.csv", low_memory=False)
            info[f"{n}_{day:%m%d}"] = json.loads((folder / "run_info.json").read_text())
            for y, g in s.groupby("season_year"):
                rows.append({"n": n, "sow": f"{day:%m-%d}", "sow_day": off, "year": int(y),
                             "yield": _weighted(g, "HWAH"), "rain": _weighted(g, "PRCM"),
                             "n_uptake": _weighted(g, "NUCM"), "seasons": len(g)})
            per = s.assign(hw=_num(s.HWAH)).groupby(["iy", "ix", "lat", "lon"]).agg(
                mean=("hw", "mean"), ha=("ha", "first")).reset_index()
            cells.append(per.assign(n=n, sow=f"{day:%m-%d}", sow_day=off))
    years = pd.DataFrame(rows)
    years.round(1).to_csv(out / "years.csv", index=False)
    stats = []
    for (n, sow, off), g in years.groupby(["n", "sow", "sow_day"]):
        d = seasonal.distribution(g["yield"], below=poor)
        stats.append({"n": n, "sow": sow, "sow_day": off, "years": d["n"],
                      **{k: d[k] for k in ("mean", "sd", "cv", "p10", "p25", "p50", "p75", "p90", "share_below")},
                      "rain_mean": g.rain.mean()})
    pd.DataFrame(stats).round(3).to_csv(out / "dates.csv", index=False)
    best = years.loc[years.groupby(["n", "year"])["yield"].idxmax(), ["n", "year", "sow", "sow_day", "yield"]]
    best.round(1).to_csv(out / "best_date_per_year.csv", index=False)
    c = pd.concat(cells, ignore_index=True)
    top = c.loc[c.groupby(["n", "iy", "ix"])["mean"].idxmax()]
    first = c[c.sow_day == 0].set_index(["n", "iy", "ix"])["mean"]
    top = top.assign(mean_jun01=first.reindex(pd.MultiIndex.from_frame(top[["n", "iy", "ix"]])).to_numpy())
    top, lg = _with_lgrip(top, (2.0, 13.0, 3.0, 14.0))
    top[["n", "iy", "ix", "lat", "lon", "ha", "sow", "sow_day", "mean", "mean_jun01"]
        + [c for c in ("ha_lgrip_all", "ha_lgrip_irr") if c in top]].round(3) \
        .to_csv(out / "best_date_per_cell.csv", index=False)
    any_run = next(iter(info.values()))
    summary = {"generated": date.today().isoformat(), "cells": any_run["cells"], "ha": any_run["ha"],
               "seasons_per_run": any_run["seasons"], "runs": len(info),
               "seasons_total": int(sum(v["seasons"] for v in info.values())),
               "failed_total": int(sum(v["failed"] for v in info.values())),
               "minutes_total": round(max(v["minutes"] for v in info.values()), 1),   # one job: the last folder
               "years": [int(years.year.min()), int(years.year.max())], "poor_kg_ha": poor, "lgrip30": lg,
               "faostat_niger_millet_t_ha": {"2018": 0.548, "2019": 0.479, "2020": 0.520},
               "inputs": any_run.get("inputs", {}), "template": Path(any_run["template"]).name}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


MOISTURE = 0.14        # market moisture of grain (assumption, stated on the pages)
# N prices, KSh per kg N, from 50 kg bags of urea (46% N, 23 kg N per bag)
N_PRICES = {
    "subsidised 2023": 2500 / 23,    # Capital FM, 2 Aug 2023: subsidised 50 kg bag Sh2,500 (from Sh3,500)
    "subsidised 2022": 3500 / 23,    # Capital FM, 20 Sep 2022: subsidised urea Sh3,500 per 50 kg bag
    "market 2022": 6500 / 23,        # same article: current (market) price Sh6,500 per 50 kg bag
}


def kenya(runs: Path, out: Path, wfp: Path = Path("E:/temp/wfp/wfp_food_prices_ken.csv")) -> dict:
    """Kenya maize: N rates under weather and price risk (configs/cases/kenya-maize.yml)."""
    out.mkdir(parents=True, exist_ok=True)
    # maize price: WFP market monitoring, Eldoret town (Uasin Gishu), white dry maize, wholesale
    w = pd.read_csv(wfp, skiprows=[1], low_memory=False)
    p = w[(w.market == "Eldoret town (Uasin Gishu)") & (w.commodity == "Maize (white, dry)")
          & (w.pricetype == "Wholesale") & (w.unit == "90 KG")].copy()
    p["ksh_kg"] = p.price.astype(float) / 90.0
    p[["date", "ksh_kg"]].round(2).to_csv(out / "maize_price_eldoret.csv", index=False)
    q = p.ksh_kg.quantile([0.1, 0.5, 0.9])
    maize = {"low (p10)": float(q[0.1]), "median": float(q[0.5]), "high (p90)": float(q[0.9])}
    frames, info = [], {}
    for n in (0, 60, 120, 180):
        folder = runs / f"kenya_maize_uniform_1991-2023_h610_n{n}"
        s = pd.read_csv(folder / "summary.csv", low_memory=False)
        info[n] = json.loads((folder / "run_info.json").read_text())
        frames.append(s.assign(n=n, hw=_num(s.HWAH), nl=_num(s.NLCM), nu=_num(s.NUCM)))
    s = pd.concat(frames, ignore_index=True)
    # per year: area-weighted yield (dry matter) and N leached by N rate
    yrs = s.groupby(["n", "season_year"]).apply(lambda g: pd.Series({
        "yield": _weighted(g, "hw"), "n_leached": _weighted(g, "nl"), "n_uptake": _weighted(g, "nu"),
        "rain": _weighted(g, "PRCM")}), include_groups=False).reset_index()
    yrs.round(1).to_csv(out / "years.csv", index=False)
    # economics per year (area-weighted), every maize price x N price
    rows, steps = [], []
    piv = yrs.pivot(index="season_year", columns="n", values="yield")
    for mp_name, mp in maize.items():
        for np_name, npr in N_PRICES.items():
            for n in (0, 60, 120, 180):
                margin = mp * piv[n] / (1 - MOISTURE) - npr * n
                d = seasonal.distribution(margin)
                rows.append({"maize_price": mp_name, "n_price": np_name, "n": n, "mean": d["mean"],
                             "p10": d["p10"], "p50": d["p50"], "p90": d["p90"], "sd": d["sd"]})
            for a, b in ((0, 60), (60, 120), (120, 180)):
                vcr = mp * (piv[b] - piv[a]) / (1 - MOISTURE) / (npr * (b - a))
                steps.append({"maize_price": mp_name, "n_price": np_name, "step": f"{a}-{b}",
                              "vcr_median": float(vcr.median()), "share_pays": float((vcr > 1).mean()),
                              "share_vcr2": float((vcr > 2).mean())})
    pd.DataFrame(rows).round(1).to_csv(out / "margins.csv", index=False)
    pd.DataFrame(steps).round(3).to_csv(out / "steps.csv", index=False)
    # per cell: mean yield by N, and the N rate with the best mean margin (median maize price)
    cell = s.groupby(["n", "iy", "ix", "lat", "lon"]).agg(y=("hw", "mean"), ha=("ha", "first")).reset_index()
    wide = cell.pivot_table(index=["iy", "ix", "lat", "lon", "ha"], columns="n", values="y").reset_index()
    for np_name, npr in N_PRICES.items():
        m = {n: maize["median"] * wide[n] / (1 - MOISTURE) - npr * n for n in (0, 60, 120, 180)}
        wide[f"best_n {np_name}"] = pd.DataFrame(m).idxmax(axis=1)
    wide.columns = [str(c) for c in wide.columns]
    wide, lg = _with_lgrip(wide, (34.6, 0.1, 35.6, 1.1))
    wide.round(2).to_csv(out / "cells.csv", index=False)
    a = info[0]
    summary = {"generated": date.today().isoformat(), "cells": a["cells"], "ha": a["ha"],
               "seasons_per_run": a["seasons"], "runs": len(info),
               "seasons_total": int(sum(v["seasons"] for v in info.values())),
               "failed_total": int(sum(v["failed"] for v in info.values())),
               "minutes_total": round(max(v["minutes"] for v in info.values()), 1),
               "years": [int(yrs.season_year.min()), int(yrs.season_year.max())],
               "lgrip30": lg, "moisture": MOISTURE, "maize_price_ksh_kg": {k: round(v, 2) for k, v in maize.items()},
               "maize_price_months": len(p), "maize_price_period": [str(p.date.min()), str(p.date.max())],
               "n_price_ksh_kg_n": {k: round(v, 1) for k, v in N_PRICES.items()},
               "faostat_kenya_maize_t_ha": {"2018": 1.766, "2019": 1.560, "2020": 1.774}}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


MEKONG_REGIMES = ("irrigated", "irrigated_perc1", "irrigated_perc4", "rainfed")
WATER_TERMS = ["IRCM", "PRCM", "ETCM", "EPCM", "ESCM", "ROCM", "DRCM", "SWXM", "IR#M"]


def mekong(runs: Path, out: Path) -> dict:
    """Mekong rice: water balance of two seasons under four water regimes (configs/cases/mekong-rice.yml).
    Every season: water in (irrigation IRCM, rain PRCM), water out (transpiration EPCM, evaporation from
    soil and flood water ESCM, runoff ROCM, drainage below the profile DRCM) and the rest, left in the
    field at harvest (soil and standing water; DSSAT's ETCM = EPCM + ESCM, SPAM.for:372-373, 462-470)."""
    out.mkdir(parents=True, exist_ok=True)
    frames, info = [], {}
    for r in MEKONG_REGIMES:
        folder = runs / f"mekong_rice_uniform_1991-2023_ir64_n105_{r}"
        s = pd.read_csv(folder / "summary.csv", low_memory=False)
        info[r] = json.loads((folder / "run_info.json").read_text())
        s = s.assign(regime=r, hw=_num(s.HWAH), **{c: _num(s[c]) for c in WATER_TERMS if c in s})
        s["ROCM"] = s["ROCM"].fillna(0.0)
        s["stored"] = s.IRCM + s.PRCM - s.EPCM - s.ESCM - s.ROCM - s.DRCM
        frames.append(s)
    s = pd.concat(frames, ignore_index=True)
    cols = ["hw", "IRCM", "PRCM", "ETCM", "EPCM", "ESCM", "ROCM", "DRCM", "stored", "IR#M"]
    yrs = s.groupby(["regime", "season", "season_year"]).apply(
        lambda g: pd.Series({c: _weighted(g, c) for c in cols}), include_groups=False).reset_index()
    yrs.rename(columns={"hw": "yield"}).round(1).to_csv(out / "seasons.csv", index=False)
    cell = s.groupby(["regime", "season", "iy", "ix", "lat", "lon"]).agg(
        ha=("ha", "first"), **{c: (c, "mean") for c in cols}).reset_index()
    cell, lg = _with_lgrip(cell, (105.2, 9.7, 106.2, 10.7))
    cell.rename(columns={"hw": "yield"}).round(1).to_csv(out / "cells.csv", index=False)
    a = info["irrigated"]
    ha = float(s[s.regime == "irrigated"].drop_duplicates(["iy", "ix"]).ha.sum())
    summary = {"generated": date.today().isoformat(), "cells": a["cells"], "ha": round(ha, 1),
               "seasons_per_run": a["seasons"], "runs": len(info),
               "seasons_total": int(sum(v["seasons"] for v in info.values())),
               "failed_total": int(sum(v["failed"] for v in info.values())),
               "minutes_total": round(sum(v["minutes"] for v in info.values()), 1),
               "years": [int(yrs.season_year.min()), int(yrs.season_year.max())],
               "lgrip30": lg, "percolation_mm_d": {"irrigated": 2, "irrigated_perc1": 1, "irrigated_perc4": 4, "rainfed": 2},
               "faostat_viet_nam_rice_t_ha": {"2018": 5.818, "2019": 5.837, "2020": 5.919},
               "template": Path(a["template"]).name}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


LUZON_RUNS = {"base": "", "no_cold": "_tcldf0_no_cold", "no_heat": "_thot45_no_heat"}
ETOPO = Path("E:/temp/dem/etopo180_4558_b013_12d8.nc")   # ETOPO1, NOAA NGDC (Amante and Eakins 2009)
HEAD_DAYS = 14                                           # window before flowering for the temperatures shown


def _window_mean(weather: Path, var: str, rows: pd.DataFrame, days: int) -> np.ndarray:
    """Mean of a prepared daily weather variable over the `days` days before each row's ADAT, at its cell."""
    import xarray as xr
    files = sorted((weather / var).glob("*.nc"))
    da = xr.open_mfdataset(files, combine="by_coords")[var].load()
    times = pd.DatetimeIndex(da.time.values)
    lat = {round(float(v), 3): i for i, v in enumerate(da.lat.values)}
    lon = {round(float(v), 3): i for i, v in enumerate(da.lon.values)}
    adat = pd.to_datetime(rows.ADAT.astype(int).astype(str), format="%Y%j")
    t1 = times.get_indexer(adat)                         # index of the flowering day
    yi = rows.lat.round(3).map(lat).to_numpy()
    xi = rows.lon.round(3).map(lon).to_numpy()
    idx = t1[:, None] - np.arange(1, days + 1)[None, :]
    vals = da.values[idx, yi[:, None], xi[:, None]]
    return vals.mean(axis=1)


def _cell_elevation(cells: pd.DataFrame, half: float = 0.05) -> np.ndarray:
    """Mean ETOPO1 altitude (m) over each 0.1-degree cell (cells: lat, lon of the centre)."""
    import xarray as xr
    dem = xr.open_dataset(ETOPO).altitude
    out = []
    for la, lo in zip(cells.lat, cells.lon):
        box = dem.sel(latitude=slice(la - half, la + half), longitude=slice(lo - half, lo + half))
        out.append(float(box.where(box > 0).mean()))
    return np.round(out, 0)


def luzon(runs: Path, out: Path) -> dict:
    """Luzon rice: grain lost to cold and to heat sterility along a lowland-to-highland slope
    (configs/cases/luzon-rice.yml). Losses are model experiments: the no_cold run minus the baseline
    (cold), the no_heat run minus the baseline (heat), season by season."""
    out.mkdir(parents=True, exist_ok=True)
    keys = ["season", "season_year", "iy", "ix"]
    frames, info = [], {}
    for cv in ("ir64", "ir58"):
        parts = {}
        for k, suffix in LUZON_RUNS.items():
            folder = runs / f"luzon_rice_uniform_1991-2023_{cv}_n105{suffix}"
            s = pd.read_csv(folder / "summary.csv", low_memory=False)
            info[f"{cv}{suffix}"] = json.loads((folder / "run_info.json").read_text())
            s["hw"] = _num(s.HWAH)
            parts[k] = s
        b = parts["base"][keys + ["lat", "lon", "ha", "ADAT", "MDAT", "PDAT", "hw"]].copy()
        for k in ("no_cold", "no_heat"):
            b = b.merge(parts[k][keys + ["hw"]].rename(columns={"hw": f"hw_{k}"}), on=keys, how="left")
        frames.append(b.assign(cultivar={"ir64": "IR 64", "ir58": "IR 58"}[cv]))
    s = pd.concat(frames, ignore_index=True)
    s["ADAT"] = pd.to_numeric(s.ADAT, errors="coerce")
    s = s[s.ADAT > 0].copy()
    days = lambda c: pd.to_datetime(pd.to_numeric(s[c], errors="coerce").astype("Int64").astype(str), format="%Y%j", errors="coerce")
    s["days"] = (days("MDAT") - days("PDAT")).dt.days                 # transplanting to maturity
    s["cold_loss"] = (s.hw_no_cold - s.hw).clip(lower=0)
    s["heat_loss"] = (s.hw_no_heat - s.hw).clip(lower=0)
    weather = config.CACHE_DIR / "inputs" / "weather" / "agera5_case_luzon"
    s["tmin14"] = _window_mean(weather, "TMIN", s, HEAD_DAYS)
    s["tmax14"] = _window_mean(weather, "TMAX", s, HEAD_DAYS)
    cellxy = s.drop_duplicates(["iy", "ix"])[["iy", "ix", "lat", "lon"]].reset_index(drop=True)
    cellxy["elev"] = _cell_elevation(cellxy)
    s = s.merge(cellxy[["iy", "ix", "elev"]], on=["iy", "ix"])
    s["zone"] = pd.cut(s.elev, [-1, 300, 800, 5000], labels=["lowland (below 300 m)", "middle (300-800 m)",
                                                            "highland (above 800 m)"]).astype(str)
    w = ["hw", "hw_no_cold", "hw_no_heat", "cold_loss", "heat_loss", "tmin14", "tmax14", "days"]

    def wmean(g):
        def one(c):
            m = g[c].notna()
            return float(np.average(g[c][m], weights=g.ha[m])) if m.any() else np.nan
        return pd.Series({c: one(c) for c in w} | {"ha": float(g.drop_duplicates(["iy", "ix"]).ha.sum())})

    cells = s.groupby(["cultivar", "season", "iy", "ix", "lat", "lon", "elev"]).apply(
        lambda g: pd.Series({**{c: g[c].mean() for c in w}, "ha": g.ha.iloc[0],
                             "cold_years": float((g.cold_loss > 0.1 * g.hw_no_cold).mean()),
                             "heat_years": float((g.heat_loss > 0.1 * g.hw_no_heat).mean())}),
        include_groups=False).reset_index()
    cells, lg = _with_lgrip(cells, (120.5, 16.0, 121.5, 17.0))
    cells.round(3).to_csv(out / "cells.csv", index=False)
    s["band"] = (s.elev // 200 * 200).astype(int)
    bands = s.groupby(["cultivar", "season", "band"]).apply(wmean, include_groups=False).reset_index()
    bands.round(2).to_csv(out / "bands.csv", index=False)
    years = s.groupby(["cultivar", "season", "zone", "season_year"]).apply(wmean, include_groups=False).reset_index()
    years.round(2).to_csv(out / "years.csv", index=False)
    a = info["ir64"]
    zone_ha = s.drop_duplicates(["iy", "ix"]).groupby("zone").ha.sum().round(0).to_dict()
    summary = {"generated": date.today().isoformat(), "cells": a["cells"], "ha": round(float(cellxy.merge(
               s.drop_duplicates(["iy", "ix"])[["iy", "ix", "ha"]]).ha.sum()), 1), "zone_ha": zone_ha,
               "elev_range": [float(cellxy.elev.min()), float(cellxy.elev.max())],
               "runs": len(info), "seasons_total": int(sum(v["seasons"] for v in info.values())),
               "failed_total": int(sum(v["failed"] for v in info.values())),
               "minutes_total": round(max(v["minutes"] for v in info.values()), 1),   # one job wrote all six
               "years": [int(s.season_year.min()), int(s.season_year.max())], "head_days": HEAD_DAYS, "lgrip30": lg,
               "coefficients": {"IR 64": {"THOT": 28.0, "TCLDF": 15.0, "TCLDP": 15.0},
                                "IR 58": {"THOT": 33.9, "TCLDF": 15.0, "TCLDP": 15.0}},
               # FAOSTAT QCL via Our World in Data, grapher/rice-yields (read 2026-11-09)
               "faostat_philippines_rice_t_ha": {"2018": 3.972, "2019": 4.045, "2020": 4.089},
               "dem": "ETOPO1 (NOAA NGDC; Amante and Eakins 2009), mean over each 0.1-degree cell"}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def _season_sum(weather: Path, var: str, rows: pd.DataFrame, a: str, b: str) -> np.ndarray:
    """Sum of a prepared daily weather variable from date column a to date column b (YYYYDDD), per row."""
    import xarray as xr
    files = sorted((weather / var).glob("*.nc"))
    da = xr.open_mfdataset(files, combine="by_coords")[var].load()
    cum = np.concatenate([np.zeros((1,) + da.shape[1:]), np.nancumsum(da.values, axis=0)])
    times = pd.DatetimeIndex(da.time.values)
    lat = {round(float(v), 3): i for i, v in enumerate(da.lat.values)}
    lon = {round(float(v), 3): i for i, v in enumerate(da.lon.values)}
    day = lambda c: times.get_indexer(pd.to_datetime(rows[c].astype(int).astype(str), format="%Y%j"))
    i0, i1 = day(a), day(b)
    yi = rows.lat.round(3).map(lat).to_numpy()
    xi = rows.lon.round(3).map(lon).to_numpy()
    return cum[i1 + 1, yi, xi] - cum[i0, yi, xi]


def javamaize(runs: Path, out: Path) -> dict:
    """Java maize: rice then maize as one DSSAT crop sequence, against maize alone on the same cells
    and dates (configs/cases/java-maize.yml, java-maize-alone.yml). Only quantities over the maize
    crop's own dates are compared: a sequence crop's summary counts from the previous harvest."""
    out.mkdir(parents=True, exist_ok=True)
    keys = ["season_year", "iy", "ix"]
    info, parts = {}, []
    num = ["HWAH", "NIAM", "NUCM", "SWXM", "PDAT", "MDAT", "ADAT", "ONAM", "OCAM", "planting_delay_days"]
    todate = lambda v: pd.to_datetime(v.astype("Int64").astype(str), format="%Y%j", errors="coerce")
    for seq, tag in (("flooded", "irrigated"), ("rainfed", "rainfed")):
        folder = runs / f"java_maize_case_uniform_1991-2022_{tag}"
        s = pd.read_csv(folder / "summary.csv", low_memory=False)
        info[tag] = json.loads((folder / "run_info.json").read_text())
        for c in num:
            s[c] = pd.to_numeric(s[c], errors="coerce") if c == "planting_delay_days" else _num(s[c])
        rice = s[s.season == "1_RI"].set_index(keys)
        # the maize of a cycle follows that cycle's rice only when sown on its date; when the rice is
        # still in the field on 15 March DSSAT sows the maize a year later (run_aoi labels it by date)
        mz = s[(s.season == "2_MZ") & (s.planting_delay_days.abs() <= 3)].set_index(keys)
        rice_end = todate(rice.MDAT)
        maize_date = pd.to_datetime(dict(year=rice.index.get_level_values("season_year") + 1, month=3, day=15))
        parts.append(pd.DataFrame({
            f"rice_{seq}": rice.HWAH, f"rice_end_n_{seq}": rice.NIAM, f"rice_end_sw_{seq}": rice.SWXM,
            f"rice_days_{seq}": (rice_end - todate(rice.PDAT)).dt.days,
            f"soil_on_{seq}": rice.ONAM, f"soil_oc_{seq}": rice.OCAM / 1000,   # kg/ha; t/ha at rice harvest
            f"rice_late_{seq}": pd.Series((rice_end.to_numpy() > maize_date.to_numpy()).astype(float), index=rice.index),
            f"maize_{seq}": mz.HWAH, f"maize_nup_{seq}": mz.NUCM}))
    folder = runs / "java_maize_case_uniform_1991-2022_mccurdy84aa_n120"
    a = pd.read_csv(folder / "summary.csv", low_memory=False)
    info["alone"] = json.loads((folder / "run_info.json").read_text())
    for c in [c for c in num if c in a]:
        a[c] = _num(a[c])
    alone = a.set_index(keys)
    weather = config.CACHE_DIR / "inputs" / "weather" / "agera5_case_javamaize"
    rows = a.dropna(subset=["PDAT", "MDAT"]).reset_index(drop=True)
    rows["rain"] = _season_sum(weather, "RAIN", rows, "PDAT", "MDAT")
    base = pd.DataFrame({"maize_alone": alone.HWAH, "maize_nup_alone": alone.NUCM, "lat": alone.lat,
                         "lon": alone.lon, "ha": alone.ha,
                         "rain": rows.set_index(keys).rain.reindex(alone.index)})
    s = pd.concat([base] + parts, axis=1).reset_index()
    s = s.dropna(subset=["maize_alone", "ha"])
    s["gain_flooded"] = s.maize_flooded - s.maize_alone
    s["gain_rainfed"] = s.maize_rainfed - s.maize_alone
    cols = [c for c in s.columns if c not in keys + ["lat", "lon", "ha"]]

    def wmean(g):
        def one(c):
            m = g[c].notna()
            return float(np.average(g[c][m], weights=g.ha[m])) if m.any() else np.nan
        return pd.Series({c: one(c) for c in cols} | {"ha": float(g.ha.sum()), "cells": int(len(g))})

    years = s.groupby("season_year").apply(wmean, include_groups=False).reset_index()
    years.round(1).to_csv(out / "years.csv", index=False)
    cells = s.groupby(["iy", "ix", "lat", "lon"]).agg(ha=("ha", "first"), **{c: (c, "mean") for c in cols},
                                                      share_gain=("gain_flooded", lambda v: float((v.dropna() > 0).mean()) if v.notna().any() else np.nan)
                                                      ).reset_index()
    lg = _lgrip_cells(cells, (111.4, -8.0, 112.4, -7.0))
    if lg is not None:
        cells = cells.merge(lg["cells"], on=["iy", "ix"], how="left").fillna({"ha_lgrip_all": 0, "ha_lgrip_irr": 0})
    cells.round(2).to_csv(out / "cells.csv", index=False)
    # every cell-season, for the rain chart (rounded to keep it small)
    s[["season_year", "iy", "ix", "ha", "rain", "maize_alone", "maize_flooded", "maize_rainfed"]].round(0) \
        .to_csv(out / "seasons.csv", index=False)
    obs = _maize_statistics(s, out)
    summary = {"generated": date.today().isoformat(), "cells": int(s.drop_duplicates(["iy", "ix"]).shape[0]),
               "ha": round(float(s.drop_duplicates(["iy", "ix"]).ha.sum()), 1),
               "cycles": [int(s.season_year.min()), int(s.season_year.max())], "observed": obs,
               "lgrip30": lg["summary"] if lg is not None else None,
               "crop_seasons_total": int(sum(v.get("seasons", 0) for v in info.values())),
               "sequences": {k: {"strata": v.get("strata"), "failed": v.get("failed")} for k, v in info.items()},
               "minutes_total": round(sum(v["minutes"] for v in info.values()), 1),
               # FAOSTAT QCL via Our World in Data, grapher/maize-yields (read 2026-11-10)
               "faostat_indonesia_maize_t_ha": {"2018": 5.307, "2019": 6.144, "2020": 5.530},
               "template": "XDRM8601.SQX (rice then maize); XDMZ8201.MZX (maize alone)"}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


BDSP_MAIZE = "obs/bdsp_jagung/bdsp_jagung_adm2_long.csv"   # scripts/fetch_bdsp.py --komoditas JAGUNG --prov 33 35
LGRIP30 = Path("E:/temp/lgrip30")                          # LGRIP30_2015_*.tif tiles (NASA LP DAAC)


def _with_lgrip(df: pd.DataFrame, box) -> tuple[pd.DataFrame, dict | None]:
    """df (rows with iy, ix, lat, lon) with LGRIP30 cropland per cell added (ha_lgrip_all, ha_lgrip_irr),
    and the summary of the comparison; df unchanged and None when the tiles are missing."""
    lg = _lgrip_cells(df.drop_duplicates(["iy", "ix"])[["iy", "ix", "lat", "lon"]].reset_index(drop=True), box)
    if lg is None:
        return df, None
    out = df.merge(lg["cells"], on=["iy", "ix"], how="left").fillna({"ha_lgrip_all": 0, "ha_lgrip_irr": 0})
    return out, lg["summary"]


def _lgrip_cells(cells: pd.DataFrame, box) -> dict | None:
    """LGRIP30 cropland per 0.1-degree cell (ha): all cropland (classes 2 irrigated + 3 rainfed) and
    irrigated only, from the 30 m tiles covering the box, for comparing the crop mask with MapSPAM.
    Cells are matched on their centres (AgERA5 grid, multiples of 0.1 degree)."""
    import rasterio
    from rasterio.windows import from_bounds
    w, s, e, n = box[0] - 0.05, box[1] - 0.05, box[2] + 0.05, box[3] + 0.05
    # a box only partly covered by tiles would undercount the cropland: refuse, naming the tile
    files = sorted(LGRIP30.glob("LGRIP30_2015_*.tif"))
    bounds = []
    for f in files:
        with rasterio.open(f) as r:
            bounds.append(r.bounds)
    missing = set()
    for la in np.arange(s, n + 1e-9, 0.05):
        for lo_ in np.arange(w, e + 1e-9, 0.05):
            if not any(b.left <= lo_ <= b.right and b.bottom <= la <= b.top for b in bounds):
                y, x = int(np.floor(la / 10) * 10), int(np.floor(lo_ / 10) * 10)
                # LP DAAC names tiles by their south-west corner, two-digit longitudes below 100
                missing.add(f"{'N' if y >= 0 else 'S'}{abs(y):02d}{'E' if x >= 0 else 'W'}{abs(x):02d}")
    if missing:
        print(f"  LGRIP30 tiles missing in {LGRIP30} for this box: {', '.join(sorted(missing))}; "
              "skipped the crop mask comparison")
        return None
    parts = []
    for f in files:
        with rasterio.open(f) as r:
            b = r.bounds
            lo = (max(w, b.left), max(s, b.bottom), min(e, b.right), min(n, b.top))
            if lo[0] >= lo[2] or lo[1] >= lo[3]:
                continue
            win = from_bounds(*lo, r.transform).round_offsets().round_lengths()
            a = r.read(1, window=win)
            t = r.window_transform(win)
        lat = t.f + t.e * (np.arange(a.shape[0]) + 0.5)
        lon = t.c + t.a * (np.arange(a.shape[1]) + 0.5)
        px = (abs(t.e) * 111320.0) * (abs(t.a) * 111320.0 * np.cos(np.radians(lat))) / 1e4   # ha per pixel row
        ci, cj = np.meshgrid(np.round(lat / 0.1).astype(int), np.round(lon / 0.1).astype(int), indexing="ij")
        d = pd.DataFrame({"ci": ci.ravel(), "cj": cj.ravel(), "cls": a.ravel(),
                          "ha": np.repeat(px, a.shape[1])})
        d = d[d.cls.isin([2, 3])]
        parts.append(d.assign(irr=(d.cls == 2) * d.ha).groupby(["ci", "cj"])
                     .agg(ha_lgrip_all=("ha", "sum"), ha_lgrip_irr=("irr", "sum")).reset_index())
    if not parts:
        print(f"  no LGRIP30 tiles over the box in {LGRIP30}: skipped the crop mask comparison")
        return None
    g = pd.concat(parts).groupby(["ci", "cj"]).sum().reset_index()
    key = cells[["iy", "ix", "lat", "lon"]].assign(ci=np.round(cells.lat / 0.1).astype(int),
                                                   cj=np.round(cells.lon / 0.1).astype(int))
    m = key.merge(g, on=["ci", "cj"], how="left")[["iy", "ix", "ha_lgrip_all", "ha_lgrip_irr"]]
    inbox = g[(g.ci >= round(box[1] / 0.1)) & (g.ci <= round(box[3] / 0.1)) &
              (g.cj >= round(box[0] / 0.1)) & (g.cj <= round(box[2] / 0.1))]
    summary = {"source": "GFSAD LGRIP30, nominal 2015, classes 2 (irrigated) and 3 (rainfed) cropland",
               "box_ha_all": round(float(inbox.ha_lgrip_all.sum()), 0),
               "box_ha_irr": round(float(inbox.ha_lgrip_irr.sum()), 0),
               "cells_ha_all": round(float(m.ha_lgrip_all.sum()), 0),
               "cells_ha_irr": round(float(m.ha_lgrip_irr.sum()), 0)}
    return {"cells": m, "summary": summary}


def _maize_statistics(s: pd.DataFrame, out: Path) -> dict:
    """District maize statistics (BDSP, Ministry of Agriculture; BPS as source) against the simulated
    maize of the same districts. Cells go to the district holding their centre (docs/java/data
    districts); a simulated maize season is labelled by its harvest year (cycle + 1). The statistics
    cover all maize of a district, every season and field; the model is dry-season maize on rice fields."""
    import geopandas as gpd
    from evaluate import detrended_r
    f = config.CACHE_DIR / BDSP_MAIZE
    if not f.exists():
        print(f"  no {f}: skipped the comparison with district statistics")
        return {}
    d = pd.read_csv(f, dtype={"code": str})
    wide = d.pivot_table(index=["code", "year"], columns="indicator", values="value").reset_index()
    wide = wide.rename(columns={"LUAS PANEN": "area_ha", "PRODUKSI": "prod_t", "PRODUKTIVITAS": "yield_q"})
    dist = gpd.read_file(ROOT / "docs" / "java" / "data" / "districts.geojson")[["code", "name", "geometry"]]
    cell = s.drop_duplicates(["iy", "ix"])[["iy", "ix", "lat", "lon"]]
    pts = gpd.GeoDataFrame(cell, geometry=gpd.points_from_xy(cell.lon, cell.lat), crs=dist.crs)
    cell = gpd.sjoin(pts, dist, predicate="within", how="left")[["iy", "ix", "code", "name"]]
    sim = s.merge(cell, on=["iy", "ix"]).assign(year=lambda v: v.season_year + 1)
    cols = ["maize_alone", "maize_flooded", "maize_rainfed"]
    wm = lambda g: pd.Series({c: float(np.average(g[c].dropna(), weights=g.ha[g[c].notna()]))
                              if g[c].notna().any() else np.nan for c in cols} | {"rice_ha": float(g.ha.sum())})
    simd = sim.groupby(["code", "name", "year"]).apply(wm, include_groups=False).reset_index()
    both = simd.merge(wide, on=["code", "year"], how="inner").dropna(subset=["area_ha", "prod_t"])
    both["obs"] = both.prod_t / both.area_ha * 1000           # kg/ha, from production and harvested area
    box = both.groupby("year").apply(lambda g: pd.Series({
        "obs": g.prod_t.sum() / g.area_ha.sum() * 1000, "maize_area_ha": g.area_ha.sum(),
        **{c: float(np.average(g[c].dropna(), weights=g.rice_ha[g[c].notna()])) for c in cols}}),
        include_groups=False).reset_index()
    box.round(1).to_csv(out / "observed.csv", index=False)
    per = both.groupby(["code", "name"]).agg(obs=("obs", "mean"), maize_alone=("maize_alone", "mean"),
                                             maize_flooded=("maize_flooded", "mean"), years=("year", "nunique"),
                                             maize_area_ha=("area_ha", "mean"), rice_ha=("rice_ha", "first")).reset_index()
    per.round(1).to_csv(out / "observed_districts.csv", index=False)
    t = box.year.to_numpy()
    return {"source": "BDSP (Ministry of Agriculture), maize (JAGUNG), districts of Central and East Java; BPS as "
                      "source on the portal", "years": [int(t.min()), int(t.max())], "districts": int(per.code.nunique()),
            "obs_mean": round(float(box.obs.mean()), 0),
            "r_detrended": {c: round(detrended_r(box[c].to_numpy(), box.obs.to_numpy(), t), 2) for c in cols},
            "r_raw": {c: round(float(np.corrcoef(box[c], box.obs)[0, 1]), 2) for c in cols}}


CASES = {"niger": niger, "kenya": kenya, "mekong": mekong, "luzon": luzon, "javamaize": javamaize}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case", choices=[*CASES, "all"])
    ap.add_argument("--runs", type=Path, help="run folders (default <cache>/runs)")
    ap.add_argument("--wfp", type=Path, help="kenya: WFP price file wfp_food_prices_ken.csv from HDX")
    a = ap.parse_args(argv)
    runs = a.runs or config.CACHE_DIR / "runs"
    for name in (CASES if a.case == "all" else [a.case]):
        extra = {"wfp": a.wfp} if name == "kenya" and a.wfp else {}
        s = CASES[name](runs, OUT / name, **extra)
        print(f"{name}: {json.dumps({k: v for k, v in s.items() if k != 'inputs'})}")


if __name__ == "__main__":
    main()

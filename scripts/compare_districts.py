"""Compare simulated rice yields with district (admin2) statistics from BDSP.

    python scripts/compare_districts.py                        # per_cell run
    python scripts/compare_districts.py --run E:/temp/xdssat/runs/java_per_cell_1991-2020_ir64_n105

Observed: <XDSSAT_CACHE>/obs/bdsp_rice_{yield,area}_adm2.csv (scripts/clean_bdsp.py),
ku/ha GKG and harvested ha per district and calendar year.

Simulated, made comparable as in scripts/compare_bps.py:
1. each season goes to the calendar year of its harvest (HDAT, else MDAT);
   only years with every season of the run inside (first season year + 1 to
   last season year) are kept;
2. the district yield of a year is the area-weighted mean over all seasons
   harvested in it, with the rice ha of each (weather cell, water regime,
   district) piece from the 100 m rasters (idn_adm2_filled.tif);
3. dry matter to GKG: divided by (1 - moisture), 0.14 by default (assumed).
Simulated harvested area of a district-year is the sum of rice ha over the
seasons harvested in it.

Writes <XDSSAT_CACHE>/checks/districts/<run name>_{years,districts}.csv and prints:
level (sim / obs), spatial agreement of district means, year-to-year agreement
(detrended correlation per district), and harvested area.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agera5  # noqa: E402
import config  # noqa: E402
import rasters  # noqa: E402


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pieces(aoi: str | None = None) -> pd.DataFrame:
    """Rice ha per (weather cell, water regime, district code)."""
    R = config.CACHE_DIR / "rasters"
    lat, lon, valid = agera5.grid()
    grid, _ = rasters.IDN.window(*rasters.AOI[aoi or config.CURRENT.aoi])
    st = rasters.aggregate(R / "idn_rice.tif", {"region": R / "idn_region.tif",
                                                 "irr": R / "idn_irrigation_filled.tif",
                                                 "adm2": R / "idn_adm2_filled.tif"},
                           lat, lon, valid, grid=grid)
    regions = pd.read_csv(R / "idn_region_classes.csv").set_index("name").code
    st = st[st.region == regions["Jawa"]]
    st["water"] = np.where(st.irr == 4, "rainfed", "irrigated")
    st["code"] = st.adm2.astype(int).astype(str)
    return st.groupby(["iy", "ix", "water", "code"], as_index=False).ha.sum()


def obs_long(path: Path, name: str) -> pd.DataFrame:
    t = pd.read_csv(path, dtype={"code": str, "province_code": str})
    yrs = [c for c in t.columns if c.isdigit()]
    return (t.melt(id_vars=["code", "name", "province"], value_vars=yrs, var_name="year", value_name=name)
             .astype({"year": int}).dropna(subset=[name]))


def detrended_r(x, y, t) -> float:
    if len(t) < 8:
        return np.nan
    rx = x - np.polyval(np.polyfit(t, x, 1), t)
    ry = y - np.polyval(np.polyfit(t, y, 1), t)
    return float(np.corrcoef(rx, ry)[0, 1])


def highpass_r(x, y, t, window: int = 5) -> float:
    """Correlation of the deviations from a centred running mean (removes slow,
    non-weather swings such as programmes or statistical method changes)."""
    if len(t) < 8:
        return np.nan
    s = pd.DataFrame({"x": x, "y": y}, index=t).sort_index()
    s = s.reindex(range(int(min(t)), int(max(t)) + 1))
    h = s - s.rolling(window, center=True, min_periods=3).mean()
    h = h.dropna()
    return float(h.x.corr(h.y)) if len(h) >= 8 else np.nan


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, default=config.CACHE_DIR / "runs" / "java_per_cell_1991-2020")
    ap.add_argument("--obs", type=Path, default=config.CACHE_DIR / "obs")
    ap.add_argument("--moisture", type=float, default=0.14)
    ap.add_argument("--min-ha", type=float, default=1000.0, help="districts with less simulated rice are left out")
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "checks" / "districts")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    pc = pieces()
    bps = _script("compare_bps")
    sim = bps.simulated(a.run, pc.rename(columns={"code": "province"}), a.moisture).rename(columns={"province": "code"})
    info = __import__("json").loads((a.run / "run_info.json").read_text())
    y0, y1 = __import__("json").loads(info["args"]["years"])
    sim = sim[sim.year.between(y0 + 1, y1)]
    ha = pc.groupby("code").ha.sum().rename("rice_ha")

    obs = obs_long(a.obs / "bdsp_rice_yield_adm2.csv", "obs_ku_ha").merge(
        obs_long(a.obs / "bdsp_rice_area_adm2.csv", "obs_harvested_ha")[["code", "year", "obs_harvested_ha"]],
        on=["code", "year"], how="outer")
    names = pd.read_csv(a.obs / "bdsp_rice_yield_adm2.csv", dtype={"code": str}).set_index("code")[["name", "province"]]
    j = sim.merge(obs.drop(columns=["name", "province"]), on=["code", "year"], how="left")
    j = j.join(names, on="code").join(ha, on="code")
    j = j[j.rice_ha >= a.min_ha]
    j["ratio"] = j.sim_ku_ha / j.obs_ku_ha
    j["area_ratio"] = j.harvested_ha / j.obs_harvested_ha
    j.to_csv(a.out / f"{a.run.name}_years.csv", index=False)

    rows = []
    for code, g in j.groupby("code"):
        g = g.dropna(subset=["obs_ku_ha"]).sort_values("year")
        if len(g) < 5:
            continue
        rows.append(dict(code=code, name=g.name.iloc[0], province=g.province.iloc[0], rice_ha=g.rice_ha.iloc[0],
                         years=len(g), obs_mean=g.obs_ku_ha.mean(), sim_mean=g.sim_ku_ha.mean(),
                         ratio=g.sim_ku_ha.mean() / g.obs_ku_ha.mean(),
                         obs_trend_decade=np.polyfit(g.year, g.obs_ku_ha, 1)[0] * 10,
                         sim_trend_decade=np.polyfit(g.year, g.sim_ku_ha, 1)[0] * 10,
                         obs_sd_dt=np.std(g.obs_ku_ha - np.polyval(np.polyfit(g.year, g.obs_ku_ha, 1), g.year)),
                         sim_sd_dt=np.std(g.sim_ku_ha - np.polyval(np.polyfit(g.year, g.sim_ku_ha, 1), g.year)),
                         r_detrended=detrended_r(g.sim_ku_ha.to_numpy(), g.obs_ku_ha.to_numpy(), g.year.to_numpy()),
                         r_highpass=highpass_r(g.sim_ku_ha.to_numpy(), g.obs_ku_ha.to_numpy(), g.year.to_numpy()),
                         area_ratio=np.nanmedian(g.area_ratio)))
    d = pd.DataFrame(rows)
    d.to_csv(a.out / f"{a.run.name}_districts.csv", index=False)

    w = d.rice_ha
    pd.set_option("display.width", 200)
    print(f"run {a.run.name}; harvest years {y0 + 1}-{y1}; {len(d)} districts with >= {a.min_ha:.0f} ha rice "
          f"and >= 5 observed years ({d.rice_ha.sum():,.0f} of {ha.sum():,.0f} ha)")
    print(f"level: sim/obs of district means, area-weighted {np.average(d.ratio, weights=w):.2f}; "
          f"quantiles p10/50/90 {np.percentile(d.ratio, [10, 50, 90]).round(2).tolist()}")
    jj = j.dropna(subset=["obs_ku_ha"])
    tot = jj.groupby("year").apply(lambda g: pd.Series({
        "sim": np.average(g.sim_ku_ha, weights=g.rice_ha), "obs": np.average(g.obs_ku_ha, weights=g.rice_ha)}),
        include_groups=False)
    print(f"Java (rice-ha weighted over districts with data): obs {tot.obs.mean():.1f}, sim {tot.sim.mean():.1f} ku/ha; "
          f"obs trend {np.polyfit(tot.index, tot.obs, 1)[0] * 10:+.1f}/decade, sim {np.polyfit(tot.index, tot.sim, 1)[0] * 10:+.1f}; "
          f"detrended r {detrended_r(tot.sim.to_numpy(), tot.obs.to_numpy(), tot.index.to_numpy()):.2f}, "
          f"high-pass r {highpass_r(tot.sim.to_numpy(), tot.obs.to_numpy(), tot.index.to_numpy()):.2f}; "
          f"high-pass sd obs {(tot.obs - tot.obs.rolling(5, center=True, min_periods=3).mean()).std():.2f}, "
          f"sim {(tot.sim - tot.sim.rolling(5, center=True, min_periods=3).mean()).std():.2f} ku/ha")
    sp = d[["obs_mean", "sim_mean"]]
    print(f"spatial (district means): Pearson r {sp.corr().iloc[0, 1]:.2f}, Spearman {sp.rank().corr().iloc[0, 1]:.2f}; "
          f"obs sd across districts {d.obs_mean.std():.1f}, sim {d.sim_mean.std():.1f} ku/ha")
    r = d.r_detrended.dropna()
    print(f"year-to-year (detrended, per district, n={len(r)}): median r {r.median():.2f}, "
          f"p10/p90 {np.percentile(r, [10, 90]).round(2).tolist()}, share r > 0: {(r > 0).mean():.2f}; "
          f"detrended sd median obs {d.obs_sd_dt.median():.1f}, sim {d.sim_sd_dt.median():.1f} ku/ha")
    rh = d.r_highpass.dropna()
    print(f"year-to-year (minus 5-yr running mean, per district, n={len(rh)}): median r {rh.median():.2f}, "
          f"share r > 0: {(rh > 0).mean():.2f}")
    print(f"harvested area sim/obs (median of district medians) {d.area_ratio.median():.2f}, "
          f"p10/p90 {np.nanpercentile(d.area_ratio, [10, 90]).round(2).tolist()}")
    print("\nby province:\n" + d.groupby("province").apply(lambda g: pd.Series({
        "districts": len(g), "ratio": np.average(g.ratio, weights=g.rice_ha), "r_dt_median": g.r_detrended.median(), "r_hp_median": g.r_highpass.median(),
        "area_ratio": g.area_ratio.median()}), include_groups=False).round(2).to_string())
    print(f"\nwrote {a.out / (a.run.name + '_districts.csv')}")


if __name__ == "__main__":
    main()

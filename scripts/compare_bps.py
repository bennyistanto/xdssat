"""Compare simulated rice yields with BPS provincial productivity.

    python scripts/compare_bps.py --bps C:/Users/benny/Downloads/produktivitas_padi_kuintaperha.csv

BPS table (bps.go.id, "Produktivitas Padi Menurut Provinsi", 1993-2015, from
the Statistik Pertanian reports): quintal per ha of gabah kering giling (GKG,
the table's own note), per calendar year, all seasons harvested in that year.
2016-2017 are absent (moratorium, per the table note).

Simulated yields are made comparable in three steps:
1. each season goes to the calendar year of its harvest date (HDAT, else MDAT);
2. the province yield of a year is the rice-area weighted mean over every
   season harvested in it, with the area of each (cell, water regime,
   province) piece from the 100 m rasters, so irrigated rice counts twice a
   year (two seasons) and rainfed once, as in harvested area;
3. dry matter (DSSAT HWAH) to GKG: divided by (1 - moisture), moisture 0.14 by
   default. The GKG moisture is an assumption: 14% is the usual quality limit
   quoted for GKG, but this script does not have a BPS definition to cite.

Writes <XDSSAT_CACHE>/checks/bps/: bps_compare_years.csv (province x year x
mode), bps_compare_summary.csv (province x mode).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agera5  # noqa: E402
import config  # noqa: E402
import rasters  # noqa: E402

BPS_NAMES = {"DAERAH ISTIMEWA YOGYAKARTA": "DI YOGYAKARTA"}
MODES = ["per_cell", "by_zone", "uniform"]


def read_bps(path: Path) -> pd.DataFrame:
    """Long table province, year, bps_ku_ha from the BPS web-table CSV export."""
    raw = pd.read_csv(path, header=None, encoding="utf-8-sig", dtype=str)
    hdr = next(i for i, r in raw.iterrows() if str(r[1]).strip().isdigit())
    years = raw.iloc[hdr, 1:].dropna().astype(int).tolist()
    body = raw.iloc[hdr + 1:, :len(years) + 1]
    body = body[body[0].notna() & body[1].notna() & (body[0] != "Catatan")]
    body.columns = ["province"] + years
    long = body.melt(id_vars="province", var_name="year", value_name="bps_ku_ha")
    long["bps_ku_ha"] = pd.to_numeric(long.bps_ku_ha.replace("-", np.nan), errors="coerce")
    long["province"] = long.province.str.strip()
    return long.dropna().astype({"year": int}).sort_values(["province", "year"]).reset_index(drop=True)


def pieces(aoi: str = "java") -> pd.DataFrame:
    """Rice ha per (cell, water regime, province)."""
    R = config.CACHE_DIR / "rasters"
    lat, lon, valid = agera5.grid()
    grid, _ = rasters.IDN.window(*rasters.AOI[aoi])
    st = rasters.aggregate(R / "idn_rice.tif", {"region": R / "idn_region.tif",
                                                 "irr": R / "idn_irrigation_filled.tif",
                                                 "prov": R / "idn_province_filled.tif"},
                           lat, lon, valid, grid=grid)
    regions = pd.read_csv(R / "idn_region_classes.csv").set_index("name").code
    st = st[st.region == regions["Jawa"]]
    names = pd.read_csv(R / "idn_province_classes.csv").set_index("code").name
    st["province"] = st.prov.map(names).replace(BPS_NAMES)
    st["water"] = np.where(st.irr == 4, "rainfed", "irrigated")
    return st.groupby(["iy", "ix", "water", "province"], as_index=False).ha.sum()


def simulated(run_dir: Path, pc: pd.DataFrame, moisture: float) -> pd.DataFrame:
    s = pd.read_csv(run_dir / "summary.csv", low_memory=False,
                    usecols=["iy", "ix", "water", "season", "season_year", "HWAH", "HDAT", "MDAT"])
    s["HWAH"] = pd.to_numeric(s.HWAH, errors="coerce").where(lambda v: v >= 0)
    hd = pd.to_numeric(s.HDAT, errors="coerce")
    hd = hd.where(hd > 1000000, pd.to_numeric(s.MDAT, errors="coerce"))
    s["year"] = (hd // 1000).astype("Int64")
    m = s.merge(pc, on=["iy", "ix", "water"])
    m = m[m.HWAH.notna()]
    g = m.assign(w=m.ha * m.HWAH).groupby(["province", "year"])
    out = pd.DataFrame({"sim_kg_ha_dm": g.w.sum() / g.ha.sum(), "harvested_ha": g.ha.sum(),
                        "seasons": g.size()}).reset_index()
    out["sim_ku_ha"] = out.sim_kg_ha_dm / (1 - moisture) / 100.0
    return out


def detrend(y: pd.Series, x: pd.Series) -> pd.Series:
    b = np.polyfit(x, y, 1)
    return y - np.polyval(b, x)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bps", type=Path, required=True)
    ap.add_argument("--runs", type=Path, default=config.CACHE_DIR / "runs")
    ap.add_argument("--aoi", default="java")
    ap.add_argument("--years", nargs=2, type=int, default=[1991, 2020])
    ap.add_argument("--moisture", type=float, default=0.14)
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "checks" / "bps")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    bps = read_bps(a.bps)
    pc = pieces(a.aoi)
    print("rice ha per province (100 m rasters):\n" + pc.groupby("province").ha.sum().round(0).to_string())
    frames = []
    for mode in MODES:
        d = a.runs / f"{a.aoi}_{mode}_{a.years[0]}-{a.years[1]}"
        if not (d / "summary.csv").exists():
            print(f"skip {mode}: {d} has no summary.csv")
            continue
        frames.append(simulated(d, pc, a.moisture).assign(mode=mode))
    sim = pd.concat(frames, ignore_index=True)
    # complete years only: both seasons of the year inside the run
    first, last = a.years[0] + 1, a.years[1]
    sim = sim[sim.year.between(first, last)]
    j = sim.merge(bps, on=["province", "year"], how="inner")
    j["ratio"] = j.sim_ku_ha / j.bps_ku_ha
    j.to_csv(a.out / "bps_compare_years.csv", index=False)

    rows = []
    for (prov, mode), g in j.groupby(["province", "mode"]):
        g = g.sort_values("year")
        r_raw = np.corrcoef(g.sim_ku_ha, g.bps_ku_ha)[0, 1]
        r_dt = np.corrcoef(detrend(g.sim_ku_ha, g.year), detrend(g.bps_ku_ha, g.year))[0, 1]
        rows.append(dict(province=prov, mode=mode, years=len(g), ha=pc[pc.province == prov].ha.sum(),
                         bps_mean=g.bps_ku_ha.mean(), sim_mean=g.sim_ku_ha.mean(),
                         ratio_mean=(g.sim_ku_ha.mean() / g.bps_ku_ha.mean()),
                         bps_trend_per_decade=np.polyfit(g.year, g.bps_ku_ha, 1)[0] * 10,
                         sim_trend_per_decade=np.polyfit(g.year, g.sim_ku_ha, 1)[0] * 10,
                         bps_sd_detrended=detrend(g.bps_ku_ha, g.year).std(),
                         sim_sd_detrended=detrend(g.sim_ku_ha, g.year).std(),
                         r=r_raw, r_detrended=r_dt))
    summ = pd.DataFrame(rows)
    summ.to_csv(a.out / "bps_compare_summary.csv", index=False)
    pd.set_option("display.width", 200)
    print(f"\nyears {j.year.min()}-{j.year.max()}, moisture {a.moisture}, ku/ha GKG")
    print(summ.round(2).to_string(index=False))

    # province ranking: mean over years
    for mode, g in summ.groupby("mode"):
        big = g[g.ha > 10000]
        rho = big[["bps_mean", "sim_mean"]].rank().corr().iloc[0, 1]
        print(f"{mode}: rank correlation of province means (provinces > 10,000 ha): {rho:.2f}")
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()

"""How far do nitrogen rate and cultivar move simulated yields towards BPS?

    python spikes/n_cultivar_sensitivity.py --cells 40

A sample of Java cells (weighted by rice area), the per_cell calendar of the
full run (its onset.csv), season years 1992-2014 (harvests 1993-2015, the BPS
years), every grid treatment of the template: IR 58 / IR 64 x 0-140 kg
N, each stratum with its own water regime. Writes
<CACHE>/checks/n_cultivar_sensitivity.csv and prints the area-weighted mean
yield per cultivar and N rate, as kg/ha dry matter and as ku/ha GKG (14%
moisture, assumed), next to the BPS Java mean over the same years.
"""
import argparse
import importlib.util
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agera5  # noqa: E402
import config  # noqa: E402
import management as mg  # noqa: E402
from filex import FileX  # noqa: E402
from genotype import write_cultivar_files  # noqa: E402
from runner import run_units  # noqa: E402
from sol import read_sol, write_sol  # noqa: E402
from wth import tav_amp, write_wth  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TEMPLATE = FileX.read(mg.TEMPLATE)
CULTIVARS = mg.template_cultivars(TEMPLATE)                       # IR 58, IR 64 in XDRI8601.RIX
N_LEVELS = config.CURRENT.data["treatments"]["nitrogen_levels"]  # kg N -> fertilizer level


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cells", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--years", nargs=2, type=int, default=[1992, 2014])
    ap.add_argument("--bps", type=Path, required=True, help="BPS provincial productivity CSV (compare_bps.py)")
    ap.add_argument("--run", type=Path, default=config.CACHE_DIR / "runs" / "java_per_cell_1991-2020")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--soc-factor", type=float, default=1.0,
                    help="multiply SLOC and SLNI of every profile (test of soil N supply)")
    ap.add_argument("--cultivars", nargs="+", default=list(CULTIVARS))
    ap.add_argument("--tcldf", type=float, help="cold sterility threshold (C) for every cultivar used")
    ap.add_argument("--n", nargs="+", type=int, default=list(N_LEVELS))
    ap.add_argument("--slpf", type=float, default=1.0,
                    help="soil fertility factor (0-1], multiplies daily crop carbohydrate in CERES-Rice "
                         "(RI_Grosub.for:648-649); the yield-gap calibration factor")
    a = ap.parse_args()
    t0 = time.time()
    run_aoi, cmp = load_script("run_aoi"), load_script("compare_bps")

    st = run_aoi.build_strata("java", a.cells, a.seed)
    onset = pd.read_csv(a.run / "onset.csv")
    cells = st.drop_duplicates("cell")[["iy", "ix"]].merge(onset, on=["iy", "ix"], how="left")
    years = list(range(a.years[0], a.years[1] + 1))
    base = mg.per_cell(st, years, mg.planting_from_onset(cells, cells.onset_days.to_numpy()))
    parts = []
    for cv, water, n in [(c, w, n) for c in a.cultivars for w in ("irrigated", "rainfed") for n in a.n]:
        k, _ = mg.template_treatment(TEMPLATE, config.CURRENT.water_level(water), N_LEVELS[n], CULTIVARS[cv][1])
        u = base[base.water == water].copy()
        u["trno_template"], u["cultivar"], u["n_kg"] = k, cv, n
        parts.append(u)
    units = pd.concat(parts, ignore_index=True)
    print(f"{st.cell.nunique()} cells, {len(st)} strata, {st.ha.sum():,.0f} ha; {len(units)} seasons")

    cul_over = ({CULTIVARS[c][1]: {"TCLDF": a.tcldf} for c in a.cultivars} if a.tcldf else None)
    profiles = read_sol(config.CACHE_DIR / "soils" / "java.SOL")
    for _p in profiles.values():
        _p.surface["SLPF"] = a.slpf
    if a.soc_factor != 1.0:
        for p in profiles.values():
            for c in ("SLOC", "SLNI"):
                p.layers[c] = p.layers[c].where(p.layers[c] < 0, p.layers[c] * a.soc_factor)
    cl = units.drop_duplicates("cell")[["cell", "iy", "ix"]].reset_index(drop=True)
    dates, w = agera5.series(cl.iy, cl.ix, units.pdate.min() - pd.Timedelta(days=40),
                             units.pdate.max() + pd.Timedelta(days=220))
    col = dict(zip(cl.cell, range(len(cl))))

    def write_inputs(run_dir, u):
        for c in u.drop_duplicates("cell").itertuples():
            df = agera5.cell_frame(dates, w, col[c.cell])
            tav, amp = tav_amp(df.TMAX, df.TMIN, df.index)
            write_wth(run_dir / f"{c.wsta}.WTH", df, "AGE5", c.lat, c.lon, tav=tav, amp=amp,
                      refht=2.0, wndht=agera5.WIND_HEIGHT)
        write_sol(run_dir / "SOIL.SOL", [profiles[s] for s in sorted(set(u.soil_id))])
        write_cultivar_files(run_dir, cul_over)

    res = run_units(units, TEMPLATE, write_inputs, workers=a.workers,
                    soils=profiles, ic_water=0.9)
    s = pd.concat([r.summary for r in res], ignore_index=True)
    failed = sum(len(r.failed) for r in res)
    s["HWAH"] = pd.to_numeric(s.HWAH, errors="coerce")
    pdat = pd.to_datetime(s.PDAT.astype(str), format="%Y%j", errors="coerce")
    mdat = pd.to_datetime(s.MDAT.astype(str), format="%Y%j", errors="coerce")
    s["season_days"] = (mdat - pdat).dt.days
    out = config.CACHE_DIR / "checks" / f"n_cultivar_sensitivity_soc{a.soc_factor:g}_slpf{a.slpf:g}_tcldf{a.tcldf or 0:g}.csv"
    s.to_csv(out, index=False)

    g = s.assign(w=s.ha * s.HWAH).groupby(["cultivar", "n_kg"])
    tab = pd.DataFrame({"kg_ha_dm": g.w.sum() / g.ha.sum(), "season_days": g.season_days.median(),
                        "NUCM": g.apply(lambda d: pd.to_numeric(d.NUCM, errors="coerce").median(),
                                        include_groups=False)})
    tab["ku_ha_gkg"] = tab.kg_ha_dm / 0.86 / 100
    bps = cmp.read_bps(a.bps)
    java = ["BANTEN", "DI YOGYAKARTA", "DKI JAKARTA", "JAWA BARAT", "JAWA TENGAH", "JAWA TIMUR"]
    b = bps[bps.province.isin(java) & bps.year.between(years[0] + 1, years[-1] + 1)]
    pa = cmp.pieces().groupby("province").ha.sum()
    bmean = (b.groupby("province").bps_ku_ha.mean() * pa).sum() / pa[b.province.unique()].sum()
    tab["ratio_to_bps"] = tab.ku_ha_gkg / bmean
    by_wr = s.assign(w=s.ha * s.HWAH).groupby(["cultivar", "n_kg", "water"]).apply(
        lambda d: d.w.sum() / d.ha.sum(), include_groups=False).unstack().round(0)
    pd.set_option("display.width", 200)
    print(f"\n{failed} failed; BPS Java mean {years[0] + 1}-{years[-1] + 1}: {bmean:.1f} ku/ha GKG "
          "(province means weighted by rice area)")
    print(tab.round(2).to_string())
    print("\nkg/ha dm by water regime:\n" + by_wr.to_string())
    print(f"\n{time.time() - t0:.0f} s -> {out}")


if __name__ == "__main__":
    main()

"""Engine check: real AgERA5 weather + SoilGrids profiles for a sample of Java cells.

40 Java rice cells (sampled by rice area), IRMZ8601 treatment 5 as template,
plantings 15 Nov and 1 Apr in 2015-2018. Needs the AgERA5 cache, the rasters
and E:/temp/xdssat/soils/java.SOL. First run 2026-11-06: 320 of 320 seasons
ran in 16 s; planting dates 100% as requested.

    conda run --no-capture-output -n climate python spikes/java_engine_check.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

import agera5
import config
import rasters
from filex import FileX
from runner import run_units
from sol import read_sol, write_sol
from wth import tav_amp, write_wth

R = config.CACHE_DIR / "rasters"
lat, lon, valid = agera5.grid()
java, _ = rasters.IDN.window(*rasters.AOI["java"])
st = rasters.aggregate(R / "idn_rice.tif", {"region": R / "idn_region.tif",
                                            "irr": R / "idn_irrigation_filled.tif"},
                       lat, lon, valid, grid=java)
regions = pd.read_csv(R / "idn_region_classes.csv").set_index("name").code
st = st[st.region == regions["Jawa"]]
cells = st.groupby(["iy", "ix"], as_index=False).ha.sum()
sample = cells.sample(40, random_state=1, weights="ha").reset_index(drop=True)

profs = read_sol(config.CACHE_DIR / "soils" / "java.SOL")
sample["soil_id"] = [f"XS{iy:04d}{ix:04d}" for iy, ix in zip(sample.iy, sample.ix)]
assert sample.soil_id.isin(profs.keys()).all()
sample["cell"] = range(len(sample))
sample["wsta"] = [f"JW{c:06d}" for c in sample.cell]

start, end = "2014-01-01", "2019-12-31"
t0 = time.time()
dates, w = agera5.series(sample.iy, sample.ix, start, end)
print(f"weather for {len(sample)} cells, {len(dates)} days: {time.time() - t0:.1f} s")

rows = []
for c in sample.itertuples():
    for y in (2015, 2016, 2017, 2018):
        for md in ("11-15", "04-01"):           # wet-season and dry-season planting
            rows.append(dict(cell=c.cell, wsta=c.wsta, soil_id=c.soil_id,
                             pdate=pd.Timestamp(f"{y}-{md}"), trno_template=5,
                             lat=lat[c.iy], lon=lon[c.ix]))
units = pd.DataFrame(rows)


def write_inputs(run_dir, u):
    for c in u.drop_duplicates("cell").itertuples():
        k = int(sample.index[sample.cell == c.cell][0])
        df = agera5.cell_frame(dates, w, k)
        tav, amp = tav_amp(df.TMAX, df.TMIN, df.index)
        write_wth(run_dir / f"{c.wsta}.WTH", df, "AGE5", lat[sample.iy[k]], lon[sample.ix[k]],
                  tav=tav, amp=amp, wndht=agera5.WIND_HEIGHT, refht=2.0)
    write_sol(run_dir / "SOIL.SOL", [profs[s] for s in sorted(set(u.soil_id))])


tpl = FileX.read(ROOT.parent / "dssat-csm-data" / "Rice" / "IRMZ8601.RIX")
t0 = time.time()
res = run_units(units, tpl, write_inputs, chunk_size=80, workers=8,
                soils=profs, ic_water=0.9)
summ = pd.concat([r.summary for r in res], ignore_index=True)
fail = pd.concat([r.failed for r in res], ignore_index=True)
print(f"{len(units)} cell-seasons, {len(summ)} done, {len(fail)} failed in {time.time() - t0:.0f} s")
if len(fail):
    print(fail[["cell", "soil_id", "pdate", "error"]].head().to_string())
for c in ["HWAH", "CWAM", "PRCP", "ETCP", "IRCM", "NICM"]:
    summ[c] = pd.to_numeric(summ[c], errors="coerce")
summ["season"] = np.where(summ.pdate.dt.month == 11, "wet (Nov)", "dry (Apr)")
print(summ.groupby("season")[["HWAH", "CWAM", "PRCP", "ETCP", "IRCM"]].describe().T.round(0).to_string())
pdat = pd.to_datetime(summ.PDAT.astype(int).astype(str), format="%Y%j")
print("planting dates as requested:", (pdat.values == summ.pdate.values).mean())
out = config.CACHE_DIR / "checks"
out.mkdir(parents=True, exist_ok=True)
summ.to_csv(out / "java_engine_check.csv", index=False)

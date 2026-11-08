"""Does a soil per soil-map class, instead of one per weather cell, change the yields?

    python scripts/soil_class_sensitivity.py --map rasters/idn_soil.tif --out <folder>

A sensitivity test of the soil per weather cell. On a sample of weather cells it runs
the experiment twice with the unchanged runner:

  cell   one SoilGrids profile per weather cell (as xdssat does), the crop-weighted
         mean under all the cell's crop;
  class  one SoilGrids profile per (cell, soil-map class), the crop-weighted mean
         under that class's crop only; every stratum is split by class, with its area.

and compares the area-weighted yields per water regime and season, and per cell.
--map is a class raster (relative to <cache>, or absolute) sampled at the crop
pixels. Everything else comes from the experiment file (--config); run > sample
cells (default 30) and years as there.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import inputs  # noqa: E402
import management as mg  # noqa: E402
import rasters  # noqa: E402
import soils  # noqa: E402


def class_strata(exp, ins, soil_map: Path, cells):
    """Strata per (cell, water, soil class) for ``cells`` (iy, ix pairs), with a profile ID each."""
    lat, lon, valid = ins["weather"].grid()
    crop = ins["crop_mask"]
    grid, _ = crop.grid().window(*exp.bbox)
    layers = {"region": ins["region"].file(), "irr": ins["water_regime"].file(), "soil": soil_map}
    stats = rasters.aggregate(crop.file(), layers, lat, lon, valid, grid=grid, units_per_ha=crop.units_per_ha,
                              class_band=crop.class_band, sub=rasters.subdivision(grid))
    codes = ins["region"].codes()
    s = stats[(stats.region == codes[exp.region])].copy()
    s = s[[(a, b) in cells for a, b in zip(s.iy, s.ix)]]
    s["water"] = s.irr.map(exp.regime_of_class())
    st = s.groupby(["iy", "ix", "lat", "lon", "water", "soil"], as_index=False).ha.sum()
    st["cell"] = st.iy.astype(int) * 10000 + st.ix.astype(int)
    st["wsta"] = [f"W{a:03d}{b:04d}" for a, b in zip(st.iy, st.ix)]
    st = st.sort_values(["iy", "ix", "water", "soil"]).reset_index(drop=True)
    key = st[["iy", "ix", "soil"]].drop_duplicates().reset_index(drop=True)
    key["soil_id"] = [f"C{i:09d}" for i in range(len(key))]
    st = st.merge(key, on=["iy", "ix", "soil"])
    st["stratum"] = range(len(st))
    return st, key


def class_profiles(exp, ins, soil_map: Path, key, workers):
    lat, lon, _ = ins["weather"].grid()
    crop = ins["crop_mask"]
    grid, _ = crop.grid().window(*exp.bbox)
    props = soils.cell_properties(crop.file(), lat, lon, grid, layers={"soil": soil_map}, keys=("iy", "ix", "soil"),
                                  workers=workers, units_per_ha=crop.units_per_ha, class_band=crop.class_band,
                                  log=lambda m: None)
    props = props.merge(key, on=["iy", "ix", "soil"])
    cols = [f"{p}_{d}" for p in soils.PROPS for d in soils.DEPTHS]
    props = soils.fill_from_nearest(props, cols)
    scale = __import__("soilprep").soc_scale((exp.data["prepare"].get("soil") or {}).get("soc_scale", "none"))
    return {r["soil_id"]: soils.make_profile(r, lat[int(r["iy"])], lon[int(r["ix"])], r["soil_id"], scale)
            for r in props.to_dict("records")}


def weighted(summary: pd.DataFrame, by) -> pd.Series:
    y = pd.to_numeric(summary.HWAH, errors="coerce")
    w = summary.ha.where(y.notna())
    return (y * w).groupby([summary[b] for b in by]).sum() / w.groupby([summary[b] for b in by]).sum()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--map", type=Path, required=True, help="soil class raster")
    ap.add_argument("--out", type=Path, required=True, help="folder for the two runs")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    import run_aoi
    exp = config.use(config.load(a.config))
    soil_map = a.map if a.map.is_absolute() else exp.path("cache") / a.map
    sample = exp.data["run"]["sample"] or 30
    base = ["--config", str(exp.file)] if exp.file else []
    res = {}
    for kind in ("cell", "class"):
        out = a.out / kind
        args = base + ["--sample", str(sample), "--out", str(out)]
        orig_strata, orig_prof, orig_nc = run_aoi.build_strata, inputs.Soil.profiles, __import__("outputs").write_run
        if kind == "class":
            ins = inputs.open_all(exp)
            st0 = orig_strata(exp.aoi, sample, exp.data["run"]["seed"], exp, ins)
            cells = set(zip(st0.iy, st0.ix))
            st, key = class_strata(exp, ins, soil_map, cells)
            if "aez" in st0:
                st = st.merge(st0.drop_duplicates(["iy", "ix"])[["iy", "ix", "aez"]], on=["iy", "ix"], how="left")
            profs = class_profiles(exp, ins, soil_map, key, a.workers)
            run_aoi.build_strata = lambda *x, **k: st.copy()
            inputs.Soil.profiles = lambda self: dict(profs)
            print(f"class: {len(st)} strata ({len(key)} profiles) in {st.cell.nunique()} cells "
                  f"against {len(st0)} strata", flush=True)
        __import__("outputs").write_run = lambda *x, **k: Path("(netCDF skipped)")
        t0 = time.time()
        try:
            run_aoi.main(args)
        finally:
            run_aoi.build_strata, inputs.Soil.profiles = orig_strata, orig_prof
            __import__("outputs").write_run = orig_nc
        run = next(out.glob("summary.csv")) if (out / "summary.csv").exists() else next(out.rglob("summary.csv"))
        res[kind] = (pd.read_csv(run, low_memory=False), time.time() - t0)
    (c, tc), (k, tk) = res["cell"], res["class"]
    print(f"\nseasons: cell {len(c)}, class {len(k)} ({len(k) / len(c):.2f} x); run time {tc:.0f} s vs {tk:.0f} s")
    print(f"area: cell {c.ha.sum():,.0f} ha-seasons, class {k.ha.sum():,.0f}")
    a_ = weighted(c, ["water", "season"]).rename("cell")
    b_ = weighted(k, ["water", "season"]).rename("class")
    t = pd.concat([a_, b_], axis=1)
    t["change_%"] = (t["class"] / t["cell"] - 1) * 100
    print("\narea-weighted yield HWAH (kg/ha) per water regime and season:")
    print(t.round(1).to_string())
    pc = weighted(c, ["lat", "lon", "water", "season"])
    pk = weighted(k, ["lat", "lon", "water", "season"])
    d = ((pk / pc - 1) * 100).dropna()
    print(f"\nper cell, regime and season ({len(d)}): change of the area-weighted yield, %: "
          f"median {d.median():+.1f}, 10th {d.quantile(0.1):+.1f}, 90th {d.quantile(0.9):+.1f}, "
          f"largest {d.abs().max():.1f}")
    pd.DataFrame({"cell": pc, "class": pk, "change_%": (pk / pc - 1) * 100}).to_csv(a.out / "per_cell.csv")
    t.to_csv(a.out / "per_regime.csv")


if __name__ == "__main__":
    main()

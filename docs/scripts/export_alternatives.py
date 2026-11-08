"""Export the results of docs/scripts/run_alternatives.py for the "Alternative input data" page.

    python docs/scripts/export_alternatives.py            # folders from configs/xdssat.yml

Reads <cache>/runs/alternatives/<variant>/<run>/ (strata.csv, summary.csv, run_info.json)
and writes three small files to docs/java/data/alternatives/:

  runs.csv   one row per variant: areas, seasons, failed seasons, mean production per year
             by season, area-weighted yield per system, change from the baseline
  years.csv  per variant, season year and season: production, harvested area, yield
  cells.csv  per variant and weather cell: crop area, irrigated area, mean production per
             year, and the difference from the baseline on the variant's own grid (the
             baseline summed into the variant's cells where the grid differs)

Production is simulated yield (HWAH, kg/ha dry matter) times the stratum's area, so its
level is the uncalibrated model's (SLPF 1); seasons the engine rejected count as missing.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from run_alternatives import VARIANTS  # noqa: E402

OUT = ROOT / "docs" / "java" / "data" / "alternatives"


def load(folder: Path):
    run = next(p.parent for p in folder.glob("*/summary.csv"))
    s = pd.read_csv(run / "summary.csv", low_memory=False)
    st = pd.read_csv(run / "strata.csv")
    info = json.loads((run / "run_info.json").read_text(encoding="utf-8"))
    s["HWAH"] = pd.to_numeric(s["HWAH"], errors="coerce")
    s["t"] = s["HWAH"] * s["ha"] / 1000.0
    s["ha_ok"] = s["ha"].where(s["HWAH"].notna())
    return s, st, info


def step_of(st) -> float:
    lon = np.sort(st.lon.unique())
    return float(round(np.median(np.diff(lon)), 6)) if len(lon) > 1 else 0.1


def water_maps(config_file: Path) -> pd.DataFrame:
    """Rice area of the national map (ATR/BPN) by MoA class and by class of the other water maps, Jawa.

    Rows: map (LGRIP30 or MapSPAM), moa (irrigated or rainfed), cls (the other map's class), ha.
    """
    import inputs
    import rasters
    exp = config.use(config.load(config_file))
    cache = exp.path("cache")
    ins = inputs.open_all(exp)
    lat, lon, valid = ins["weather"].grid()
    crop = ins["crop_mask"]
    grid, _ = crop.grid().window(*exp.bbox)
    lg = inputs.open_dataset("water_regime", VARIANTS["water_lgrip"][3]["inputs"]["water_regime"], cache)
    sp = inputs.open_dataset("water_regime", VARIANTS["water_spam"][3]["inputs"]["water_regime"], cache)
    st = rasters.aggregate(crop.file(), {"region": ins["region"].file(), "irr": ins["water_regime"].file(),
                                         "lg": lg.file()}, lat, lon, valid, grid=grid,
                           units_per_ha=crop.units_per_ha, class_band=crop.class_band, weights={"sp": sp.file()})
    j = st[st.region == ins["region"].codes()[exp.region]].copy()
    j["moa"] = j.irr.map(exp.regime_of_class())
    j["lgc"] = j.lg.map({0: "water", 1: "non-cropland", 2: "irrigated", 3: "rainfed"})
    rows = [{"map": "LGRIP30", "moa": m, "cls": c, "ha": float(h)}
            for (m, c), h in j.groupby(["moa", "lgc"]).ha.sum().items()]
    for m, g in j.groupby("moa"):
        rows += [{"map": "MapSPAM", "moa": m, "cls": "irrigated", "ha": float(g.sp_ha.sum())},
                 {"map": "MapSPAM", "moa": m, "cls": "rainfed", "ha": float(g.sp_known_ha.sum() - g.sp_ha.sum())},
                 {"map": "MapSPAM", "moa": m, "cls": "no MapSPAM rice", "ha": float(g.ha.sum() - g.sp_known_ha.sum())}]
    return pd.DataFrame(rows).round(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=config.DEFAULT_FILE)
    a = ap.parse_args()
    cache = config.load(a.config).path("cache")
    folder = cache / "runs" / "alternatives"
    data = {k: load(folder / k) for k in VARIANTS if list((folder / k).glob("*/summary.csv"))}
    if "baseline" not in data:
        raise SystemExit(f"no baseline results in {folder}; run docs/scripts/run_alternatives.py first")
    n_years = data["baseline"][0].season_year.nunique()
    runs, years, cells = [], [], []
    for k, (s, st, info) in data.items():
        group, label, source, _ = VARIANTS[k]
        y = s.groupby(["season_year", "season"]).agg(production_t=("t", "sum"), harvested_ha=("ha_ok", "sum"))
        y = y.reset_index()
        y["yield_kg_ha"] = y.production_t / y.harvested_ha * 1000.0
        y.insert(0, "variant", k)
        years.append(y)
        sysw = s.groupby(["water", "season"]).apply(lambda g: g.t.sum() / g.ha_ok.sum() * 1000.0,
                                                    include_groups=False)
        by_season = s.groupby("season").t.sum() / n_years
        ha = st.groupby("water").ha.sum()
        runs.append({"variant": k, "group": group, "label": label, "source": source,
                     "cells": int(st.cell.nunique()), "grid_deg": step_of(st),
                     "ha": float(st.ha.sum()), "ha_irrigated": float(ha.get("irrigated", 0.0)),
                     "ha_rainfed": float(ha.get("rainfed", 0.0)),
                     "seasons": int(len(s)), "failed": int(s.HWAH.isna().sum()),
                     "minutes": float(info.get("minutes", np.nan)),
                     "production_t": float(s.t.sum() / n_years),
                     "production_dry_t": float(by_season.get("dry", 0.0)),
                     "production_wet_t": float(by_season.get("wet", 0.0)),
                     "harvested_ha": float(s.ha_ok.sum() / n_years),
                     "yield_wet_irrigated": float(sysw.get(("irrigated", "wet"), np.nan)),
                     "yield_dry_irrigated": float(sysw.get(("irrigated", "dry"), np.nan)),
                     "yield_wet_rainfed": float(sysw.get(("rainfed", "wet"), np.nan))})
        c = st.groupby(["lat", "lon"]).agg(crop_ha=("ha", "sum")).reset_index()
        irr = st[st.water == "irrigated"].groupby(["lat", "lon"]).ha.sum().rename("irrigated_ha")
        prod = (s.groupby(["lat", "lon"]).t.sum() / n_years).rename("production_t")
        c = c.merge(irr, on=["lat", "lon"], how="left").merge(prod, on=["lat", "lon"], how="left")
        c["irrigated_ha"] = c.irrigated_ha.fillna(0.0)
        c.insert(0, "variant", k)
        c["grid_deg"] = step_of(st)
        cells.append(c)
    runs = pd.DataFrame(runs)
    base = runs.set_index("variant").loc["baseline"]
    for col in ("production_t", "production_dry_t", "production_wet_t", "ha", "ha_irrigated"):
        runs[f"{col}_change_pct"] = (runs[col] / base[col] - 1.0) * 100.0
    cells = pd.concat(cells, ignore_index=True)
    b = cells[cells.variant == "baseline"]
    parts = []
    for k, c in cells.groupby("variant", sort=False):
        step = float(c.grid_deg.iloc[0])
        if abs(step - float(b.grid_deg.iloc[0])) < 1e-9:
            ref = b.set_index(["lat", "lon"]).production_t
        else:                                          # baseline summed into this grid's cells
            bb = b.assign(lat=(b.lat / step).round() * step, lon=(b.lon / step).round() * step)
            bb["lat"], bb["lon"] = bb.lat.round(6), bb.lon.round(6)
            ref = bb.groupby(["lat", "lon"]).production_t.sum()
        c = c.copy()
        key = list(zip(c.lat.round(6), c.lon.round(6)))
        ref.index = pd.MultiIndex.from_tuples([(round(a, 6), round(o, 6)) for a, o in ref.index])
        base_t = pd.Series([ref.get(kk, 0.0) for kk in key], index=c.index).fillna(0.0)
        c["baseline_t"] = base_t
        c["diff_t"] = c.production_t.fillna(0.0) - base_t
        # cells the baseline has but this variant lacks: production falls to zero there
        missing = ref[[kk not in set(key) for kk in ref.index]]
        if len(missing):
            extra = pd.DataFrame({"variant": k, "lat": [i[0] for i in missing.index],
                                  "lon": [i[1] for i in missing.index], "crop_ha": 0.0, "irrigated_ha": 0.0,
                                  "production_t": 0.0, "grid_deg": step, "baseline_t": missing.values,
                                  "diff_t": -missing.values})
            c = pd.concat([c, extra], ignore_index=True)
        parts.append(c)
    cells = pd.concat(parts, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    water_maps(a.config).to_csv(OUT / "water_maps.csv", index=False)
    runs.round(4).to_csv(OUT / "runs.csv", index=False)
    pd.concat(years, ignore_index=True).round(3).to_csv(OUT / "years.csv", index=False)
    cells.round({"crop_ha": 1, "irrigated_ha": 1, "production_t": 1, "baseline_t": 1, "diff_t": 1}) \
        .to_csv(OUT / "cells.csv", index=False)
    print(runs[["variant", "cells", "ha", "seasons", "failed", "production_t", "production_t_change_pct"]]
          .round(1).to_string(index=False))
    print(f"wrote {OUT} ({sum(f.stat().st_size for f in OUT.glob('*')) / 1e3:.0f} kB)")


if __name__ == "__main__":
    main()

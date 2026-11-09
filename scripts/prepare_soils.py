"""Make the soil input: one DSSAT soil profile per weather cell with crop.

    python scripts/prepare_soils.py --aoi java
    python scripts/prepare_soils.py --config configs/westjava.yml --aoi westjava
    python scripts/prepare_soils.py --aoi java --weather era5land_java --crop-mask cropland_java
    python scripts/prepare_soils.py                 # SoilGrids over the whole crop mask

How is set in the experiment file, section prepare > soil (configs/rice-java.yml
explains it):

    method: soilgrids   the crop-weighted mean of SoilGrids 2.0 under the crop pixels of
                        each cell, by pedotransfer (src/soils.py); soc_scale scales its
                        organic carbon (wise_indonesia, none, or six factors by depth)
    method: sol         your own profiles (.SOL: a soil survey, WISE, HC27 ...), with one
                        profile for every cell (profile:) or a raster or vector map of
                        profile IDs (map:); each cell takes the profile under most of its crop

The soil belongs to one weather grid and one crop mask (inputs > weather and
inputs > crop_mask, or --weather and --crop-mask): it has a profile only for the
cells with crop. --aoi takes the aoi > name of the experiment file and cuts its
aoi > bbox (method sol always does).

SoilGrids reads the local SoilGrids mirror (paths > soilgrids) anywhere in the world;
scripts/fetch_soilgrids.py --aoi <aoi> fetches the tiles an area lacks, and
scripts/build_soilgrids_cache.py --aoi <aoi> makes preparing it faster (optional).

Outputs: with prepare > soil > name, the soil input <cache>/inputs/soil/<name>/
(soil.SOL, soil_map.csv, soilgrids_cells.csv). Without a name (SoilGrids only),
the original layout in --out (default <cache>/soils): <aoi>[_<weather>][_<crop mask>].SOL,
its _map.csv and _soilgrids_cells.csv, registered as soilgrids_<same>.
"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import inputs  # noqa: E402
import rasters  # noqa: E402
import soilprep  # noqa: E402
import soils  # noqa: E402
from sol import write_sol  # noqa: E402

INDONESIA = (94.9, -11.1, 141.1, 6.2)          # rasters.IDN, the box the WISE factors were fitted in


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml, else built-in defaults)")
    ap.add_argument("--aoi", help="aoi > name of the experiment file")
    ap.add_argument("--weather", help="weather input whose grid the profiles follow, one per cell "
                                      "(default: inputs > weather)")
    ap.add_argument("--crop-mask", help="crop mask input that weights the profiles (default: inputs > crop_mask)")
    ap.add_argument("--rice", type=Path, help=argparse.SUPPRESS)       # old option: a crop raster file
    ap.add_argument("--out", type=Path, help="output folder of the original layout (default <cache>/soils)")
    ap.add_argument("--overwrite", action="store_true", help="make a named soil input again if it exists")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-soc-scale", action="store_true",
                    help="SoilGrids: keep organic carbon as is (same as prepare > soil > soc_scale: none)")
    a = ap.parse_args()
    exp = config.use(config.load(a.config)) if a.config else config.CURRENT
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)  # noqa: E731
    if a.aoi and a.aoi != exp.aoi:
        raise SystemExit(f"--aoi {a.aoi}: the experiment file's aoi is '{exp.aoi}' ({exp.where('aoi.name')})")
    p = soilprep.plan(exp)
    a.weather = a.weather or exp.data["inputs"]["weather"]
    a.crop_mask = a.crop_mask or exp.data["inputs"]["crop_mask"]
    t0 = time.time()
    if p["method"] == "sol":
        log(f"soil '{p['name']}' from {p['file']} for the cells of weather '{a.weather}' with crop in "
            f"'{a.crop_mask}'")
        manifest = soilprep.prepare_sol(exp, p, a.weather, a.crop_mask, overwrite=a.overwrite, log=log)
        name = p["name"]
    else:
        manifest, name = soilgrids(exp, p, a, log)
    s = inputs.open_dataset("soil", name, config.DATASETS_DIR)
    m = s.soil_map()
    log(f"registered as the soil input '{name}' ({manifest.parent}): {s.meta.get('profiles', len(set(m.soil_id)))} "
        f"profile(s) for {len(m):,} weather cells, {(time.time() - t0) / 60:.1f} min")
    used = exp.data["inputs"]["soil"]
    if used != name:
        print(f"\nTo use it, set  inputs > soil: {name}  (now '{used}').")


def soilgrids(exp, p, a, log):
    weather = inputs.open_dataset("weather", a.weather, config.DATASETS_DIR)
    crop = inputs.open_dataset("crop_mask", a.crop_mask, config.DATASETS_DIR)
    a.rice = a.rice or crop.file()
    scale = None if a.no_soc_scale else soilprep.soc_scale(p["soc_scale"])
    if p["soc_scale"] == "wise_indonesia" and not a.no_soc_scale:
        b = exp.bbox
        if b[2] < INDONESIA[0] or b[0] > INDONESIA[2] or b[3] < INDONESIA[1] or b[1] > INDONESIA[3]:
            log("note: soc_scale wise_indonesia was fitted to WISE profiles of Indonesia, and aoi > bbox lies "
                "outside Indonesia; consider soc_scale: none or factors for your area")
    grid, name = crop.grid(), "idn"
    if a.aoi or p["name"]:
        grid, _ = grid.window(*exp.bbox)
        name = exp.aoi
    lat, lon, valid = weather.grid()
    if p["name"]:
        dataset, out_dir = p["name"], inputs.root(config.DATASETS_DIR) / "soil" / p["name"]
        if (out_dir / "soil.SOL").exists() and not a.overwrite:
            raise SystemExit(f"the soil input '{dataset}' exists already ({out_dir}); use --overwrite to make "
                             "it again, or another name.")
        sol, cells_csv, map_csv = out_dir / "soil.SOL", out_dir / "soilgrids_cells.csv", out_dir / "soil_map.csv"
    else:
        # the original layout's soil file keeps its name; a soil for another weather grid or
        # crop mask says which
        stem = name + "".join(f"_{v}" for v, d in ((a.weather, inputs.JAVA["weather"]),
                                                   (a.crop_mask, inputs.JAVA["crop_mask"])) if v != d)
        dataset, out_dir = f"soilgrids_{stem}", a.out or config.CACHE_DIR / "soils"
        sol, cells_csv, map_csv = (out_dir / f"{stem}.SOL", out_dir / f"{stem}_soilgrids_cells.csv",
                                   out_dir / f"{stem}_map.csv")
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"SoilGrids profiles for the {weather.step:g} degree cells of weather input '{a.weather}' with crop in "
        f"'{a.crop_mask}' -> {sol}; organic carbon "
        + ("as SoilGrids" if scale is None else f"scaled ({p['soc_scale'] if isinstance(p['soc_scale'], str) else 'own factors'})"))
    props = soils.cell_properties(a.rice, lat, lon, grid, workers=a.workers, log=log,
                                  units_per_ha=crop.units_per_ha, class_band=crop.class_band)
    cols = [f"{q}_{d}" for q in soils.PROPS for d in soils.DEPTHS]
    n_missing = int(props[cols].isna().any(axis=1).sum())
    if n_missing > 0.2 * len(props):
        raise SystemExit(f"{n_missing} of {len(props)} weather cells with crop have no SoilGrids data: the "
                         f"mirror ({config.SOILGRIDS_DIR}) lacks the tiles of this area. Fetch them (about 9 x 6 "
                         f"layers of 250 m tiles from ISRIC), then run this again:\n"
                         f"  python scripts/fetch_soilgrids.py --aoi {exp.aoi} --dry-run   # how many\n"
                         f"  python scripts/fetch_soilgrids.py --aoi {exp.aoi}")
    if n_missing:
        log(f"note: {n_missing} cells had no SoilGrids data and take the nearest cell's profile; "
            f"python scripts/fetch_soilgrids.py --aoi {exp.aoi} --dry-run says whether tiles are missing")
    props = soils.fill_from_nearest(props, cols)
    # The runner moves crop on weather cells without data (sea) to the nearest cell with data
    # (rasters.nearest_valid). A cell whose only crop is such moved crop has no profile of its
    # own: it gets the profile of the cell its crop came from (the one with most crop).
    iy2, ix2, _ = rasters.nearest_valid(props.iy.to_numpy(), props.ix.to_numpy(), valid)
    have = set(zip(props.iy, props.ix))
    moved = props.assign(iy=iy2, ix=ix2)[(iy2 >= 0) & [(a, b) not in have for a, b in zip(iy2, ix2)]]
    if len(moved):
        moved = moved.sort_values("ha", ascending=False).drop_duplicates(["iy", "ix"])
        props = pd.concat([props, moved], ignore_index=True)
        log(f"{len(moved)} weather cell(s) with data receive crop only from cells without data and take "
            "that crop's profile")
    props["lat"], props["lon"] = lat[props.iy], lon[props.ix]
    props.to_csv(cells_csv, index=False)
    profs = soils.profiles(props, lat, lon, soc_scale=scale, country=p["country"])
    write_sol(sol, list(profs.values()), title=f"xdssat SoilGrids 2.0 profiles, {name}, one per weather cell with crop")
    smap = props[["iy", "ix", "lat", "lon"]].copy()
    smap["soil_id"] = [soils.profile_id(iy, ix) for iy, ix in zip(props.iy, props.ix)]
    smap["ha"], smap["share"] = props["ha"], 1.0
    smap["filled_from"] = props["filled_from"] if "filled_from" in props else ""
    smap.to_csv(map_csv, index=False)
    rel = lambda q: q.relative_to(config.DATASETS_DIR).as_posix() if q.is_relative_to(config.DATASETS_DIR) else str(q)  # noqa: E731
    log(f"{len(profs)} profiles, {n_missing} filled from the nearest cell")
    return inputs.write_manifest("soil", dataset, {
        "description": p["description"] or f"one profile per weather cell with crop, {name}, from SoilGrids 2.0",
        "source": p["source"] or "SoilGrids 2.0 (ISRIC), CC-BY 4.0",
        "made_by": f"scripts/prepare_soils.py --aoi {name} --weather {a.weather} --crop-mask {a.crop_mask}",
        "method": "soilgrids", "soc_scale": None if scale is None else scale,
        "file": rel(sol), "map": rel(map_csv), "weather": a.weather, "crop_mask": a.crop_mask,
        "cell_id": "XS{iy:04d}{ix:04d}", "profiles": len(profs), "cells": len(profs),
        "sub_points": rasters.subdivision(grid)}, config.DATASETS_DIR), dataset


if __name__ == "__main__":
    main()

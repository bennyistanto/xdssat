"""One DSSAT soil profile per AgERA5 cell with rice, from SoilGrids 2.0.

    python scripts/prepare_soils.py --aoi java
    python scripts/prepare_soils.py                 # all of Indonesia

Needs the national rice raster (scripts/prepare_rasters.py), the AgERA5 cache
(scripts/build_agera5_cache.py) and a complete SoilGrids mirror over the area
(scripts/fetch_soilgrids.py). See src/soils.py for sources and equations.

Outputs (in --out, default <XDSSAT_CACHE>/soils):
    <name>_soilgrids_cells.csv   rice-weighted SoilGrids properties per cell (unscaled)
    <name>.SOL                   the DSSAT profiles, IDs XS<iy:04><ix:04>
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agera5  # noqa: E402
import config  # noqa: E402
import rasters  # noqa: E402
import soils  # noqa: E402
from sol import write_sol  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aoi", choices=sorted(rasters.AOI))
    ap.add_argument("--rice", type=Path, default=config.CACHE_DIR / "rasters" / "idn_rice.tif")
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "soils")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-soc-scale", action="store_true",
                    help="keep SoilGrids organic carbon as is (default: scale to WISE)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)  # noqa: E731
    grid, name = rasters.IDN, "idn"
    if a.aoi:
        grid, _ = rasters.IDN.window(*rasters.AOI[a.aoi])
        name = a.aoi
    lat, lon, valid = agera5.grid()
    t0 = time.time()
    props = soils.cell_properties(a.rice, lat, lon, grid, workers=a.workers, log=log)
    cols = [f"{p}_{d}" for p in soils.PROPS for d in soils.DEPTHS]
    n_missing = int(props[cols].isna().any(axis=1).sum())
    props = soils.fill_from_nearest(props, cols)
    props["lat"], props["lon"] = lat[props.iy], lon[props.ix]
    props.to_csv(a.out / f"{name}_soilgrids_cells.csv", index=False)
    profs = soils.profiles(props, lat, lon, soc_scale=None if a.no_soc_scale else soils.SOC_SCALE)
    write_sol(a.out / f"{name}.SOL", list(profs.values()),
              title=f"xdssat SoilGrids 2.0 profiles, {name}, one per AgERA5 cell with rice")
    log(f"{len(profs)} profiles, {n_missing} filled from the nearest cell, "
        f"{(time.time() - t0) / 60:.1f} min -> {a.out / (name + '.SOL')}")


if __name__ == "__main__":
    main()

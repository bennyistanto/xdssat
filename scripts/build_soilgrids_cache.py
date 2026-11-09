"""Copy a region of the local SoilGrids mirror into one GeoTIFF per layer.

    python scripts/build_soilgrids_cache.py --aoi luzon   # the experiment's area
    python scripts/build_soilgrids_cache.py               # Indonesia (AgERA5 region idn)

Writes <cache>/soilgrids/<area>/<prop>_<depth>_mean.tif, 9 properties x 6 depths
(same 250 m Homolosine grid as SoilGrids; pixels copied, not resampled), for the
land cells of the experiment's weather input in aoi > bbox (--aoi), or of the
AgERA5 cache --region. Resumable. src/soils.py samples from these files where
they cover a point and reads the mirror's tiles elsewhere, so a cache only makes
preparing the soil faster; it is optional.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import soils  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--aoi", help="aoi > name of the experiment file: its weather input's land cells in aoi > bbox")
    ap.add_argument("--region", default="idn", help="without --aoi: the AgERA5 cache region (default idn)")
    a = ap.parse_args()
    t0 = time.time()
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)  # noqa: E731
    if a.aoi:
        import config
        import inputs
        exp = config.use(config.load(a.config))
        if a.aoi != exp.aoi:
            raise SystemExit(f"--aoi {a.aoi}: the experiment file's aoi is '{exp.aoi}' ({exp.where('aoi.name')})")
        w = inputs.open_dataset("weather", exp.data["inputs"]["weather"], exp.datasets())
        soils.build_region_cache(a.aoi, points=soils.area_points(*w.grid(), bbox=exp.bbox), log=log)
    else:
        soils.build_region_cache(a.region, log=log)
    print(f"done in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()

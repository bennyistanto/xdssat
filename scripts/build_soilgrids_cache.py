"""Copy a region of the local SoilGrids mirror into one GeoTIFF per layer.

    python scripts/build_soilgrids_cache.py          # Indonesia, 9 properties x 6 depths

Writes <XDSSAT_CACHE>/soilgrids/<region>/<prop>_<depth>_mean.tif (same 250 m
Homolosine grid as SoilGrids; pixels copied, not resampled). Resumable.
src/soils.py samples from these files when they exist.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import soils  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="idn")
    a = ap.parse_args()
    t0 = time.time()
    soils.build_region_cache(a.region, log=lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True))
    print(f"done in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()

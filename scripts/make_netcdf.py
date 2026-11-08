"""Write the gridded CF netCDF of a finished run.

    python scripts/make_netcdf.py <cache>/runs/java_per_cell_1991-2020
    python scripts/make_netcdf.py RUN_DIR --out results.nc

Reads summary.csv, strata.csv and run_info.json from the run folder and writes
<run folder>/<run folder name>.nc (src/outputs.py describes the layout).
run_aoi.py calls this itself at the end of a run.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import outputs  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--region", default="idn")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    t = time.time()
    p = outputs.write_run(a.run_dir, a.region, a.out)
    print(f"wrote {p} ({p.stat().st_size / 1e6:.1f} MB, {time.time() - t:.0f} s)")


if __name__ == "__main__":
    main()

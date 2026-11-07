"""Extract a region of the global AgERA5 archive into the xdssat cache.

    python scripts/build_agera5_cache.py                 # Indonesia, all variables, all years
    python scripts/build_agera5_cache.py --years 2000 2001 --vars TMAX TMIN

Resumable: files already in the cache are skipped. See src/agera5.py for the
layout, the unit conversions and the packing.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agera5  # noqa: E402
import config  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="idn", choices=sorted(agera5.REGIONS))
    ap.add_argument("--vars", nargs="+", default=list(agera5.VARIABLES))
    ap.add_argument("--years", nargs="+", type=int)
    ap.add_argument("--cache-dir", type=Path, default=config.CACHE_DIR)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    t0 = time.time()
    agera5.build_cache(a.region, a.vars, a.years, a.cache_dir, a.workers,
                       log=lambda m: print(m, flush=True))
    print(f"done in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()

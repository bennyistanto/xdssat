"""Fit SoilGrids organic carbon factors to measured WISE profiles of a country.

    python scripts/fit_soc_scale.py --country Philippines
    python scripts/fit_soc_scale.py --country Indonesia --bbox 105 -9 114.7 -5   # Java's sites only

Pairs every layer of the country's WISE 1.1 profiles (DSSAT's WI.SOL, Romero et
al. 2012) with SoilGrids 2.0 organic carbon at the same site and depth, and takes
the median WISE / SoilGrids ratio per depth band (0-30, 30-100, 100-200 cm), as
was done for Indonesia (src/soils.py, SOC_SCALE). It prints the factors with the
number of profiles and layers and their spread, and the setting to paste:

    prepare:
      soil:
        soc_scale: {0-5cm: ..., ...}

SoilGrids is read from the area caches or the mirror (paths > soilgrids); WISE
sites on tiles the mirror lacks are skipped (scripts/fetch_soilgrids.py).
A factor resting on few profiles is uncertain: the output says how many.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import soils  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--country", required=True, help="country as written in WI.SOL profile headers, e.g. Philippines")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="only sites in this box")
    ap.add_argument("--wise", type=Path, help="the WISE .SOL file (default: <paths > engine>/Soil/WI.SOL)")
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--pairs", type=Path, help="also write the site-layer pairs to this CSV")
    a = ap.parse_args()
    exp = config.use(config.load(a.config))
    wise = a.wise or exp.path("engine") / "Soil" / "WI.SOL"
    pairs = soils.wise_soc_pairs(wise, a.country, a.bbox)
    if pairs.empty:
        raise SystemExit(f"no WISE layers of {a.country!r} paired with SoilGrids in {wise} "
                         "(country as in the file's @SITE lines; or the mirror lacks the tiles)")
    if a.pairs:
        pairs.to_csv(a.pairs, index=False)
    f = soils.soc_factors(pairs)
    print(f"WISE {wise.name}, {a.country}{' in ' + str(a.bbox) if a.bbox else ''}: "
          f"{pairs.id.nunique()} profiles, {len(pairs)} layers paired with SoilGrids")
    print(f"  {'band':8s} {'factor':>7s} {'IQR':>12s} {'profiles':>9s} {'layers':>7s}")
    for band, g in pairs.groupby("band", sort=False):
        q1, q3 = g.ratio.quantile([0.25, 0.75])
        print(f"  {band:8s} {g.ratio.median():7.2f} {q1:5.2f}-{q3:<5.2f} {g.id.nunique():9d} {len(g):7d}")
    print("\nprepare:\n  soil:\n    soc_scale: {" + ", ".join(f"{d}: {v:.2f}" for d, v in f.items()) + "}")


if __name__ == "__main__":
    main()

"""Make the optional class inputs from your own maps: water regime, region, zones.

    python scripts/prepare_layers.py --inspect D:/maps/irrigation.tif      # what a file holds
    python scripts/prepare_layers.py                    # every prepare > water_regime / region / zones set
    python scripts/prepare_layers.py --kind water_regime --overwrite

Say in the experiment file (sections prepare > water_regime, prepare > region,
prepare > zones; configs/rice-java.yml explains them) which map to use:

    values: classes            a class map: a raster of codes (classes: CSV code, name) or a
                               vector file (class_column), any grid and CRS
    values: fraction|percent   water regime only: the irrigated share of each pixel
    values: ratio              water regime only: irrigated / total crop area, two rasters
                               on one grid (e.g. MapSPAM physical area, files _I irrigated and _A all)

All three inputs are optional. Without a water regime map (inputs > water_regime: null)
every crop pixel takes treatments > default_regime; without a region map
(inputs > region: null, aoi > region: null) aoi > bbox alone decides which crop counts;
without zones there is no by_zone calendar.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import cropprep  # noqa: E402
import inputs  # noqa: E402
import layerprep  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--kind", choices=layerprep.KINDS, help="only this one (default: every one that is set)")
    ap.add_argument("--inspect", nargs="+", type=Path, metavar="FILE", help="describe these files and stop")
    ap.add_argument("--overwrite", action="store_true", help="make an input again if it exists")
    ap.add_argument("--workers", type=int, default=16, help="processes for burning vector maps")
    a = ap.parse_args()
    if a.inspect:
        for f in a.inspect:
            print(cropprep.inspect(f) + "\n")
        return
    exp = config.use(config.load(a.config))
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)  # noqa: E731
    kinds = [a.kind] if a.kind else [k for k in layerprep.KINDS if exp.data["prepare"].get(k)]
    if not kinds:
        raise SystemExit("none of prepare > water_regime, region, zones is set in the experiment file "
                         f"({exp.file}); configs/rice-java.yml shows how.")
    made = {}
    for kind in kinds:
        t0 = time.time()
        m = layerprep.prepare(exp, kind, overwrite=a.overwrite, workers=a.workers, log=log)
        made[kind] = exp.data["prepare"][kind]["name"]
        log(f"wrote {m.parent} in {time.time() - t0:.0f} s")
    over = dict(exp.data["inputs"], **made)
    probs = [p for p in inputs.check(config.load(a.config, overrides={"inputs": over}))
             if any(f"inputs > {k}" in p.split("\n")[0] for k in made)]
    if probs:
        print("\n" + "\n\n".join(probs))
        sys.exit(1)
    for kind, name in made.items():
        used = exp.data["inputs"][kind]
        print(f"The {kind.replace('_', ' ')} input '{name}' meets the requirements."
              + ("" if used == name else f" To use it, set  inputs > {kind}: {name}  (now '{used}')."))


if __name__ == "__main__":
    main()

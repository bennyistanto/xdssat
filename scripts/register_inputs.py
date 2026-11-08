"""Register prepared inputs made by the original xdssat scripts, and list or check them.

    python scripts/register_inputs.py              # register what exists, then check the experiment
    python scripts/register_inputs.py --list       # list the prepared datasets of every kind
    python scripts/register_inputs.py --config configs/<study>.yml

A run reads its inputs (weather, crop mask, soil, water regime, region, zones)
only as prepared datasets: a manifest ``input.json`` in
``<paths > cache>/inputs/<kind>/<name>/`` that says where the files are and how to
read them (src/inputs.py). This script writes the manifests for files that
build_agera5_cache.py, prepare_rasters.py and prepare_soils.py made, under the
names configs/rice-java.yml uses. Nothing is copied, moved or rebuilt; running it
again only rewrites the manifests.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import inputs  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--list", action="store_true", help="only list the prepared datasets")
    ap.add_argument("--region", default="idn", help="weather and raster region of the original layout")
    ap.add_argument("--aoi", help="area of the soil file soils/<aoi>.SOL (default: aoi > name)")
    a = ap.parse_args()
    exp = config.use(config.load(a.config))
    cache = exp.path("cache")
    if not a.list:
        print(f"Registering the original layout in {cache}:")
        inputs.register_existing(cache, a.region, a.aoi or exp.aoi)
    print(f"\nPrepared datasets in {inputs.root(cache)}:")
    for kind in inputs.KINDS:
        names = inputs.available(kind, cache)
        used = exp.data["inputs"].get(kind)
        print(f"  {kind:13s}" + (", ".join(f"{n}{' (used)' if n == used else ''}" for n in names) or "none"))
    problems = inputs.check(exp)
    print()
    if problems:
        print(f"{len(problems)} problem(s) with the inputs of {exp.file or 'the built-in defaults'}:\n")
        print("\n\n".join(problems))
        sys.exit(1)
    print(f"The inputs of {exp.file or 'the built-in defaults'} meet the requirements.")


if __name__ == "__main__":
    main()

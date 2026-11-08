"""Make a crop mask input (crop ha per pixel) from your own land-cover, crop or field map.

    python scripts/prepare_crop_mask.py --inspect D:/worldcover/ESA_WorldCover_10m_2021_v200_S09E105_Map.tif
    python scripts/prepare_crop_mask.py                      # prepare > crop_mask of configs/xdssat.yml
    python scripts/prepare_crop_mask.py --config configs/<study>.yml --overwrite

Say in the experiment file (section prepare > crop_mask, explained in
configs/rice-java.yml) which file(s) hold the crop and what their values mean:

    values: classes            a land-cover map; classes: the crop class codes
                               (ESA WorldCover: [40] Cropland; MODIS MCD12Q1 IGBP:
                               [12] Croplands, [14] Cropland/Natural Vegetation Mosaics)
    values: fraction|percent   a crop share per pixel (0-1 or 0-100)
    values: ha|m2|km2          a crop area per pixel (e.g. MapSPAM physical area, ha)
    values: polygons           a vector file of crop fields

Use --inspect on a file to see its CRS, pixel size, value type and most common
values. The script cuts the map to aoi > bbox (or box), converts it to crop
hectares per pixel (exact pixel areas, any CRS; aggregate sums source pixels into
larger ones), writes <cache>/inputs/crop_mask/<name>/crop_area.tif with its
manifest, and checks the result. Then set inputs > crop_mask to the name, and
prepare the soil for it (scripts/prepare_soils.py --aoi <aoi>): the soil profiles
are weighted by the crop pixels and exist only for weather cells with crop.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import cropprep  # noqa: E402
import inputs  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--inspect", nargs="+", type=Path, metavar="FILE", help="describe these files and stop")
    ap.add_argument("--overwrite", action="store_true", help="make the crop mask again if it exists")
    ap.add_argument("--workers", type=int, default=16, help="processes for burning polygons")
    a = ap.parse_args()
    if a.inspect:
        for f in a.inspect:
            print(cropprep.inspect(f) + "\n")
        return
    exp = config.use(config.load(a.config))
    t0 = time.time()
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)  # noqa: E731
    manifest = cropprep.prepare(exp, overwrite=a.overwrite, workers=a.workers, log=log)
    name = exp.data["prepare"]["crop_mask"]["name"]
    c = inputs.open_dataset("crop_mask", name, exp.path("cache"))
    m = c.meta
    log(f"wrote {manifest.parent} in {(time.time() - t0) / 60:.1f} min: {m['total_ha']:,.0f} ha of crop in "
        f"{m['pixels_with_crop']:,} pixels of {m['pixel'][0]:.6g} x {m['pixel'][1]:.6g} ({m['crs']})")
    # the runner reads a coarse pixel as at most 10 x 10 sub-points; beyond that they are coarse too
    try:
        w = inputs.open_dataset("weather", exp.data["inputs"]["weather"], exp.path("cache"))
        size = m["pixel"][0] if "degree" in _unit(m["crs"]) else m["pixel"][0] / 111_320
        if size / 10 > w.step / 4:
            log(f"note: pixels of about {size:.3g} degree are read as 10 x 10 sub-points of {size / 10:.3g} "
                f"degree, coarse for the {w.step:g} degree cells of weather input '{w.name}'. Use split: "
                f"{max(2, int(round(size / (w.step / 40))))} for an even spread.")
    except inputs.InputError:
        pass
    over = dict(exp.data["inputs"], crop_mask=name)
    problems = [p for p in inputs.check(config.load(a.config, overrides={"inputs": over}))
                if "inputs > crop_mask" in p.split("\n")[0] or p.startswith("The crop_mask input")]
    if problems:
        print("\n" + "\n\n".join(problems))
        sys.exit(1)
    used = exp.data["inputs"]["crop_mask"]
    print(f"\nThe crop mask '{name}' meets the requirements."
          + ("" if used == name else f" To use it, set  inputs > crop_mask: {name}  (now '{used}'), and "
             f"prepare the soil for it: python scripts/prepare_soils.py --aoi {exp.aoi} --crop-mask {name}"))


def _unit(crs: str) -> str:
    import pyproj
    return "degree" if pyproj.CRS.from_user_input(crs).is_geographic else "m"


if __name__ == "__main__":
    main()

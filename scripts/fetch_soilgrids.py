"""Download SoilGrids 2.0 tiles that a local mirror's .vrt files reference but lack.

    python scripts/fetch_soilgrids.py --aoi luzon --dry-run   # count what the experiment's area lacks
    python scripts/fetch_soilgrids.py --aoi luzon             # fetch it
    python scripts/fetch_soilgrids.py --dry-run               # the original: AgERA5 region idn

The local mirror (paths > soilgrids) has the layout of
https://files.isric.org/soilgrids/latest/data/: ``<prop>/<prop>_<depth>_mean.vrt``
plus a folder of GeoTIFF tiles. Only tiles under land cells are considered (cell
centre and corners), so sea tiles are skipped: with --aoi, the land cells of the
experiment's weather input (inputs > weather) inside aoi > bbox; without, those
of the AgERA5 cache --region.

Each tile is written to ``.part`` and renamed only after it opens with rasterio
and its size and data type match the .vrt entry. Re-running skips tiles that
are already present. SoilGrids is CC-BY 4.0 (ISRIC).
"""
import argparse
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pyproj
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agera5  # noqa: E402
import config  # noqa: E402

BASE = "https://files.isric.org/soilgrids/latest/data"
PROPS = ["clay", "sand", "silt", "soc", "bdod", "cfvo", "nitrogen", "phh2o", "cec"]
DEPTHS = ["0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"]
HOMOLOSINE = ("+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs")


def region_points(region):
    """Centres and corners of the region's land cells, as lon/lat arrays."""
    lat, lon, valid = agera5.grid(region)
    yy, xx = np.nonzero(valid)
    h = agera5.RES / 2
    offs = [(0, 0), (-h, -h), (-h, h), (h, -h), (h, h)]
    plon = np.concatenate([lon[xx] + dx for dx, _ in offs])
    plat = np.concatenate([lat[yy] + dy for _, dy in offs])
    return plon, plat


def missing_tiles(vrt: Path, x: np.ndarray, y: np.ndarray):
    """(relative path, expected width, height, dtype) of absent tiles under the points."""
    root = ET.parse(vrt).getroot()
    gt = [float(v) for v in root.find("GeoTransform").text.split(",")]
    dtype = root.find("VRTRasterBand").get("dataType").lower()
    col = np.floor((x - gt[0]) / gt[1]).astype(np.int64)
    row = np.floor((y - gt[3]) / gt[5]).astype(np.int64)
    out = []
    for src in root.iter("ComplexSource"):
        r = src.find("DstRect").attrib
        x0, y0, w, h = (int(float(r[k])) for k in ("xOff", "yOff", "xSize", "ySize"))
        if not ((col >= x0) & (col < x0 + w) & (row >= y0) & (row < y0 + h)).any():
            continue
        rel = src.find("SourceFilename").text.lstrip("./")
        if not (vrt.parent / rel).exists():
            s = src.find("SrcRect").attrib
            out.append((rel, int(float(s["xSize"])), int(float(s["ySize"])), dtype))
    return out


def fetch(prop, rel, width, height, dtype, mirror: Path, retries=3, timeout=120):
    dest = mirror / prop / rel
    url = f"{BASE}/{prop}/{rel}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tif.part")
    req = urllib.request.Request(url, headers={"User-Agent": "xdssat-soilgrids-fetch"})
    err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
                f.write(r.read())
            with rasterio.open(tmp) as s:
                ok = (s.width, s.height, s.dtypes[0]) == (width, height, dtype)
                s.read(1)
            if not ok:
                raise ValueError(f"unexpected size or type for {url}")
            os.replace(tmp, dest)
            return dest.stat().st_size
        except Exception as e:  # noqa: BLE001 - retry any network or decode failure
            err = e
            time.sleep(2 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"{url}: {err}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mirror", type=Path, help="the SoilGrids mirror (default: paths > soilgrids)")
    ap.add_argument("--config", type=Path, help="experiment file (default: configs/xdssat.yml)")
    ap.add_argument("--aoi", help="aoi > name of the experiment file: its weather input's land cells in aoi > bbox")
    ap.add_argument("--region", default="idn", help="without --aoi: the AgERA5 cache region (default idn)")
    ap.add_argument("--props", nargs="+", default=PROPS)
    ap.add_argument("--depths", nargs="+", default=DEPTHS)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.aoi:
        import inputs
        import soils
        exp = config.use(config.load(a.config))
        if a.aoi != exp.aoi:
            raise SystemExit(f"--aoi {a.aoi}: the experiment file's aoi is '{exp.aoi}' ({exp.where('aoi.name')})")
        w = inputs.open_dataset("weather", exp.data["inputs"]["weather"], exp.path("cache"))
        plon, plat = soils.area_points(*w.grid(), bbox=exp.bbox, centres=True)
        where = f"the land cells of weather '{w.name}' in aoi '{a.aoi}'"
    else:
        plon, plat = region_points(a.region)
        where = f"region {a.region}"
    a.mirror = a.mirror or config.SOILGRIDS_DIR
    x, y = pyproj.Transformer.from_crs("EPSG:4326", HOMOLOSINE, always_xy=True).transform(plon, plat)
    x, y = np.asarray(x), np.asarray(y)
    jobs = []
    for p in a.props:
        for d in a.depths:
            vrt = a.mirror / p / f"{p}_{d}_mean.vrt"
            if not vrt.exists():
                raise FileNotFoundError(vrt)
            jobs += [(p, *m) for m in missing_tiles(vrt, x, y)]
    print(f"{len(jobs)} missing tiles under {where}", flush=True)
    if a.dry_run or not jobs:
        return
    t0, nbytes, failed = time.time(), 0, []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch, *j, a.mirror): j for j in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                nbytes += fut.result()
            except Exception as e:  # noqa: BLE001
                failed.append(str(e))
                print("FAILED", e, flush=True)
            if i % 250 == 0 or i == len(jobs):
                el = time.time() - t0
                print(f"[{i}/{len(jobs)}] {nbytes / 1e9:.2f} GB, {el / 60:.1f} min, "
                      f"eta {el / i * (len(jobs) - i) / 60:.0f} min", flush=True)
    print(f"done: {len(jobs) - len(failed)} fetched, {len(failed)} failed, "
          f"{nbytes / 1e9:.2f} GB in {(time.time() - t0) / 60:.1f} min", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

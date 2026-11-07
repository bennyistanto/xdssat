"""DSSAT soil profiles from SoilGrids 2.0, one per weather cell, weighted by rice area.

Source
------
SoilGrids 2.0 (ISRIC, CC-BY 4.0), 250 m, Interrupted Goode Homolosine, six
depth intervals, ``mean`` layers, read from a local mirror of
https://files.isric.org/soilgrids/latest/data/ (``scripts/fetch_soilgrids.py``
fills gaps). Stored integers are divided by these factors (ISRIC, "SoilGrids
layers", https://docs.isric.org/globaldata/soilgrids/SoilGrids_faqs_01.html):

========  ===============  ======  =================
property  mapped units     factor  conventional
========  ===============  ======  =================
bdod      cg/cm3           100     kg/dm3
cec       mmol(c)/kg       10      cmol(c)/kg
cfvo      cm3/dm3          10      vol %
clay      g/kg             10      %
nitrogen  cg/kg            100     g/kg
phh2o     pH x 10          10      pH
sand      g/kg             10      %
silt      g/kg             10      %
soc       dg/kg            10      g/kg
========  ===============  ======  =================

Each weather cell gets the rice-area-weighted mean of every property at every
depth, over the 100 m rice pixels in that cell (sampled at the pixel centre
from the 250 m grid). Cells whose rice pixels all fall on SoilGrids no-data
take the profile of the nearest cell that has one.

Derived DSSAT layer values
--------------------------
- LL, DUL, SAT (cm3/cm3) and SSKS (cm/h): Saxton, K.E. and Rawls, W.J. (2006),
  Soil water characteristic estimates by texture and organic matter for
  hydrologic solutions, SSSAJ 70:1569-1578, doi:10.2136/sssaj2005.0117.
  Coefficients checked against USDA-ARS WEPP ``saxpar.for``. Sand and clay
  enter as fractions, organic matter as percent; OM = 1.724 x organic carbon
  (the conventional van Bemmelen factor; an assumption, not from the paper).
- SLOC: SoilGrids organic carbon scaled to measured WISE profiles in
  Indonesia (``SOC_SCALE``, factors 0.47-0.70 by depth). The scaled value is
  also what enters the pedotransfer as organic matter.
- SRGF = 1 for layers ending at or above 15 cm, else exp(-0.02 x layer
  mid-depth). This rule reproduces 96.8% of the 16,360 layers in DSSAT's
  WI.SOL (WISE 1.1, Romero et al. 2012), measured in this repo 2026-11-06.
- Surface: SALB 0.13, SLU1 9.3, SLDR 0.60, SLRO 75, SLNF 1, SLPF 1, the medians
  of the 3,404 WI.SOL profiles (measured 2026-11-06). These are placeholders
  for paddy fields, where percolation and bunds are set in the FileX.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pyproj
import rasterio
from rasterio.windows import Window

import config
import rasters
from sol import SoilProfile

HOMOLOSINE = "+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs"
NODATA = -32768

PROPS = {  # property: divisor to conventional units
    "clay": 10.0, "sand": 10.0, "silt": 10.0, "soc": 10.0, "bdod": 100.0,
    "cfvo": 10.0, "nitrogen": 100.0, "phh2o": 10.0, "cec": 10.0,
}
DEPTHS = ["0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"]
LAYER_BOTTOMS = [5, 15, 30, 60, 100, 200]

SURFACE_DEFAULTS = {"SCOM": "-99", "SALB": 0.13, "SLU1": 9.3, "SLDR": 0.60,
                    "SLRO": 75.0, "SLNF": 1.0, "SLPF": 1.0,
                    "SMHB": "IB001", "SMPX": "IB001", "SMKE": "IB001"}
OM_PER_OC = 1.724

# Organic carbon scaling to WISE (Benny's decision, 2026-11-06). SoilGrids SOC
# is higher than the measured WISE 1.1 profiles in Indonesia. Factor = median of
# WISE OC / SoilGrids OC over layers paired at each WISE site, per depth band
# (``wise_soc_pairs`` + ``soc_factors`` on dssat-csm-data/Soil/WI.SOL, regional
# cache, measured 2026-11-06). Indonesia: 78 profiles, 420 layers.
#   band      factor  IQR          (Java only, 18 profiles)
#   0-30      0.70    0.51-0.99    0.66
#   30-100    0.63    0.39-0.89    0.85
#   100-200   0.47    0.28-0.71    0.60
SOC_SCALE = {"0-5cm": 0.70, "5-15cm": 0.70, "15-30cm": 0.70,
             "30-60cm": 0.63, "60-100cm": 0.63, "100-200cm": 0.47}

_to_igh = pyproj.Transformer.from_crs("EPSG:4326", HOMOLOSINE, always_xy=True)


def vrt_path(prop: str, depth: str, root: Path | None = None) -> Path:
    return Path(root or config.SOILGRIDS_DIR) / prop / f"{prop}_{depth}_mean.vrt"


# ------------------------------------------------------------ pedotransfer


def saxton_rawls(sand, clay, om):
    """LL (1500 kPa), DUL (33 kPa), SAT (cm3/cm3) and Ksat (cm/h).

    ``sand`` and ``clay`` are fractions (0-1), ``om`` is organic matter in %.
    Saxton and Rawls (2006), without their density and gravel adjustments.
    """
    S, C, OM = (np.asarray(v, dtype="float64") for v in (sand, clay, om))
    t1500 = (-0.024 * S + 0.487 * C + 0.006 * OM + 0.005 * S * OM
             - 0.013 * C * OM + 0.068 * S * C + 0.031)
    ll = t1500 + (0.14 * t1500 - 0.02)
    t33 = (-0.251 * S + 0.195 * C + 0.011 * OM + 0.006 * S * OM
           - 0.027 * C * OM + 0.452 * S * C + 0.299)
    dul = t33 + (1.283 * t33 ** 2 - 0.374 * t33 - 0.015)
    ts33 = (0.278 * S + 0.034 * C + 0.022 * OM - 0.018 * S * OM
            - 0.027 * C * OM - 0.584 * S * C + 0.078)
    s33 = ts33 + (0.636 * ts33 - 0.107)
    sat = dul + s33 - 0.097 * S + 0.043
    B = (np.log(1500.0) - np.log(33.0)) / (np.log(dul) - np.log(ll))
    ksat_mm_h = 1930.0 * np.clip(sat - dul, 0, None) ** (3.0 - 1.0 / B)
    return ll, dul, sat, ksat_mm_h / 10.0


def root_growth_factor(bottoms) -> np.ndarray:
    """SRGF: 1 down to 15 cm, then exp(-0.02 x layer mid-depth) (see module doc)."""
    b = np.asarray(bottoms, dtype="float64")
    mid = (np.r_[0.0, b[:-1]] + b) / 2
    return np.where(b <= 15, 1.0, np.exp(-0.02 * mid))


def texture_class(sand, silt, clay) -> str:
    """USDA texture class as the DSSAT code (S, LS, SL, L, SIL, SI, SCL, CL, SICL, SC, SIC, C).

    Class limits from the USDA texture triangle (Soil Survey Manual, USDA
    Handbook 18, 2017, ch. 3). Inputs in %.
    """
    if silt + 1.5 * clay < 15:
        return "S"
    if silt + 2 * clay < 30:
        return "LS"
    if clay >= 40 and silt >= 40:
        return "SIC"
    if clay >= 40 and sand <= 45:
        return "C"
    if clay >= 35 and sand > 45:
        return "SC"
    if 27 <= clay < 40 and sand <= 20:
        return "SICL"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "CL"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "SCL"
    if silt >= 80 and clay < 12:
        return "SI"
    if silt >= 50 and (12 <= clay < 27 or clay < 12):
        return "SIL"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "L"
    return "SL"


# ------------------------------------------------------------ sampling


class SoilGridsLayer:
    """Point sampler for one SoilGrids .vrt that reads its GeoTIFF tiles directly.

    Reading through GDAL's VRT driver fails as soon as a window touches a tile
    the local mirror does not have (sea tiles are not downloaded). Here each
    point is looked up in the .vrt's tile rectangles and read from its own
    tile; points on a missing tile, or on no tile, come back as NODATA and are
    counted in ``missing``.
    """

    def __init__(self, vrt: Path):
        import shapely
        root = ET.parse(vrt).getroot()
        self.gt = [float(v) for v in root.find("GeoTransform").text.split(",")]
        recs = []
        for src in root.iter("ComplexSource"):
            d, sr = src.find("DstRect").attrib, src.find("SrcRect").attrib
            recs.append((int(float(d["xOff"])), int(float(d["yOff"])),
                         int(float(d["xSize"])), int(float(d["ySize"])),
                         int(float(sr["xOff"])), int(float(sr["yOff"])),
                         str(vrt.parent / src.find("SourceFilename").text)))
        self.rects = recs
        self.exists = np.array([os.path.exists(r[6]) for r in recs])
        self.tree = shapely.STRtree(shapely.box(*np.array(
            [(r[0], r[1], r[0] + r[2], r[1] + r[3]) for r in recs], dtype="float64").T))
        self.missing = 0

    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        import shapely
        fx = (np.asarray(x) - self.gt[0]) / self.gt[1]
        fy = (np.asarray(y) - self.gt[3]) / self.gt[5]
        out = np.full(fx.shape, NODATA, dtype="int32")
        pt_idx, src_idx = self.tree.query(shapely.points(fx, fy), predicate="intersects")
        pt_idx, first = np.unique(pt_idx, return_index=True)
        src_idx = src_idx[first]
        self.missing += int((~self.exists[src_idx]).sum()) + int(fx.size - pt_idx.size)
        for s in np.unique(src_idx):
            if not self.exists[s]:
                continue
            x0, y0, w, h, sx, sy, fn = self.rects[s]
            sel = pt_idx[src_idx == s]
            cc = np.clip(np.floor(fx[sel]).astype(int) - x0, 0, w - 1) + sx
            rr = np.clip(np.floor(fy[sel]).astype(int) - y0, 0, h - 1) + sy
            r0, c0 = rr.min(), cc.min()
            with rasterio.open(fn) as src:
                a = src.read(1, window=Window(c0, r0, cc.max() - c0 + 1, rr.max() - r0 + 1))
            out[sel] = a[rr - r0, cc - c0]
        return out


def cache_path(prop: str, depth: str, region: str = "idn", cache_dir: Path | None = None) -> Path:
    return Path(cache_dir or config.CACHE_DIR) / "soilgrids" / region / f"{prop}_{depth}_mean.tif"


def build_region_cache(region: str = "idn", props=tuple(PROPS), depths=tuple(DEPTHS),
                       root: Path | None = None, cache_dir: Path | None = None,
                       margin_px: int = 8, log=print) -> list[Path]:
    """Copy the region's part of each SoilGrids layer into one GeoTIFF per layer.

    Same 250 m Homolosine grid as the source (pixels copied, not resampled),
    window = the region's AgERA5 land cells plus a margin. Tiles absent from
    the mirror (sea) stay NODATA. One sequential pass per layer, so later
    sampling reads one file instead of many small tiles on a slow disk.
    """
    import agera5
    from rasterio.transform import Affine
    lat, lon, valid = agera5.grid(region)
    yy, xx = np.nonzero(valid)
    h = agera5.RES / 2
    plon = np.concatenate([lon[xx] - h, lon[xx] + h, lon[xx] - h, lon[xx] + h])
    plat = np.concatenate([lat[yy] - h, lat[yy] - h, lat[yy] + h, lat[yy] + h])
    gx, gy = (np.asarray(v) for v in _to_igh.transform(plon, plat))
    done = []
    for prop in props:
        for depth in depths:
            out = cache_path(prop, depth, region, cache_dir)
            if out.exists():
                done.append(out)
                continue
            vrt = vrt_path(prop, depth, root)
            lay = SoilGridsLayer(vrt)
            gt = lay.gt
            cols = (gx - gt[0]) / gt[1]
            rows = (gy - gt[3]) / gt[5]
            c0 = int(np.floor(cols.min())) - margin_px
            r0 = int(np.floor(rows.min())) - margin_px
            c1 = int(np.ceil(cols.max())) + margin_px
            r1 = int(np.ceil(rows.max())) + margin_px
            arr = np.full((r1 - r0, c1 - c0), NODATA, dtype="int16")
            n_tiles = n_missing = 0
            for (x0, y0, w, hh, sx, sy, fn), ok in zip(lay.rects, lay.exists):
                if x0 >= c1 or x0 + w <= c0 or y0 >= r1 or y0 + hh <= r0:
                    continue
                if not ok:
                    n_missing += 1
                    continue
                n_tiles += 1
                with rasterio.open(fn) as src:
                    a = src.read(1, window=Window(sx, sy, w, hh))
                ax0, ay0 = max(x0, c0), max(y0, r0)
                ax1, ay1 = min(x0 + w, c1), min(y0 + hh, r1)
                arr[ay0 - r0:ay1 - r0, ax0 - c0:ax1 - c0] = a[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0]
            with rasterio.open(vrt) as v:
                crs = v.crs
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tif.part")
            tr = Affine(gt[1], 0, gt[0] + c0 * gt[1], 0, gt[5], gt[3] + r0 * gt[5])
            with rasterio.open(tmp, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0],
                               count=1, dtype="int16", crs=crs, transform=tr, nodata=NODATA,
                               tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                               predictor=2) as dst:
                dst.write(arr, 1)
            os.replace(tmp, out)
            done.append(out)
            log(f"{out.name}: {arr.shape[1]} x {arr.shape[0]} px, {n_tiles} tiles, "
                f"{n_missing} absent (left as no-data)")
    return done


_LAYERS: dict = {}


def _layer(prop, depth, root):
    key = (prop, depth, str(root))
    if key not in _LAYERS:
        cached = cache_path(prop, depth)
        _LAYERS[key] = (CachedLayer(cached) if root is None and cached.exists()
                        else SoilGridsLayer(vrt_path(prop, depth, root)))
    return _LAYERS[key]


class CachedLayer:
    """Point sampler on a regional cache GeoTIFF (``build_region_cache``)."""

    def __init__(self, path: Path):
        self.path = path
        self.missing = 0

    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        with rasterio.open(self.path) as src:
            rr, cc = rasterio.transform.rowcol(src.transform, x, y)
            rr, cc = np.asarray(rr), np.asarray(cc)
            inside = (rr >= 0) & (rr < src.height) & (cc >= 0) & (cc < src.width)
            out = np.full(rr.shape, NODATA, dtype="int32")
            self.missing += int((~inside).sum())
            if inside.any():
                r0, c0 = rr[inside].min(), cc[inside].min()
                win = Window(c0, r0, cc[inside].max() - c0 + 1, rr[inside].max() - r0 + 1)
                a = src.read(1, window=win)
                out[inside] = a[rr[inside] - r0, cc[inside] - c0]
        return out


def _sample_tile(args):
    """Weighted sums of every SoilGrids layer per weather cell, for one tile."""
    rice_path, layers, wlat, wlon, grid, win, root, keys = args
    px = rasters.rice_pixels(rice_path, layers, wlat, wlon, grid, win)
    if px is None:
        return None, 0
    w = px["share"].to_numpy("float64") / 100.0
    x, y = _to_igh.transform(px["lon"].to_numpy(), px["lat"].to_numpy())
    x, y = np.asarray(x), np.asarray(y)
    out = px[keys].copy()
    out["ha"] = w
    cols, missing = {}, 0
    for prop, div in PROPS.items():
        for depth in DEPTHS:
            lay = _layer(prop, depth, root)
            before = lay.missing
            v = lay.sample(x, y).astype("float64")
            missing += lay.missing - before
            ok = v != NODATA
            col = f"{prop}_{depth}"
            cols[col + "_wx"] = np.where(ok, v / div * w, 0.0)
            cols[col + "_w"] = np.where(ok, w, 0.0)
    out = pd.concat([out.reset_index(drop=True), pd.DataFrame(cols)], axis=1)
    return out.groupby(keys, as_index=False).sum(), missing


def cell_properties(rice_path, wlat, wlon, grid=rasters.IDN, layers: dict | None = None,
                    keys=("iy", "ix"), root: Path | None = None, tile: int = 2048,
                    workers: int = 16, log=print) -> pd.DataFrame:
    """Rice-weighted mean SoilGrids properties per weather cell (or per ``keys``).

    Returns one row per key with ``ha`` and ``<prop>_<depth>`` columns in
    conventional units (NaN where no rice pixel had data).
    """
    layers = layers or {}
    keys = list(keys)
    jobs = [(rice_path, layers, wlat, wlon, grid, win, root, keys) for win in grid.tiles(tile)]
    parts, missing = [], 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for part, miss in ex.map(_sample_tile, jobs):
            missing += miss
            if part is not None:
                parts.append(part)
    if missing:
        log(f"warning: {missing} pixel-layer samples fell on SoilGrids tiles missing from "
            f"the mirror (run scripts/fetch_soilgrids.py)")
    tot = pd.concat(parts, ignore_index=True).groupby(keys, as_index=False).sum()
    out = tot[keys + ["ha"]].copy()
    for prop in PROPS:
        for depth in DEPTHS:
            c = f"{prop}_{depth}"
            out[c] = tot[c + "_wx"] / tot[c + "_w"].where(tot[c + "_w"] > 0)
    log(f"SoilGrids sampled for {len(out)} groups from {len(jobs)} tiles")
    return out


def fill_from_nearest(props: pd.DataFrame, cols=None) -> pd.DataFrame:
    """Rows missing any property take all properties of the nearest complete row (iy, ix)."""
    from scipy.spatial import cKDTree
    cols = cols or [f"{p}_{d}" for p in PROPS for d in DEPTHS]
    props = props.copy()
    bad = props[cols].isna().any(axis=1).to_numpy()
    props["filled_from"] = ""
    if bad.any():
        good = props[~bad]
        tree = cKDTree(good[["iy", "ix"]].to_numpy())
        _, j = tree.query(props.loc[bad, ["iy", "ix"]].to_numpy())
        src = good.iloc[j]
        props.loc[bad, cols] = src[cols].to_numpy()
        props.loc[bad, "filled_from"] = [f"{a},{b}" for a, b in zip(src.iy, src.ix)]
    return props


# ------------------------------------------------------------ profiles


def profile_id(iy: int, ix: int, prefix: str = "XS") -> str:
    """10-character soil ID, e.g. XS01230456 (prefix + 4-digit row + 4-digit col)."""
    return f"{prefix}{int(iy):04d}{int(ix):04d}"


def make_profile(row, lat: float, lon: float, sid: str,
                 soc_scale: dict | None = SOC_SCALE) -> SoilProfile:
    """One SoilProfile from a row of ``cell_properties`` (six SoilGrids depths).

    ``soc_scale`` multiplies SoilGrids organic carbon per depth (default
    ``SOC_SCALE``, scaled to WISE); pass None to keep SoilGrids as is.
    """
    g = {p: np.array([row[f"{p}_{d}"] for d in DEPTHS], dtype="float64") for p in PROPS}
    scale = np.array([soc_scale[d] for d in DEPTHS]) if soc_scale else np.ones(len(DEPTHS))
    oc = g["soc"] / 10.0 * scale                             # g/kg -> %, scaled
    sand, clay = g["sand"] / 100.0, g["clay"] / 100.0
    ll, dul, sat, ksat = saxton_rawls(sand, clay, np.minimum(oc * OM_PER_OC, 8.0))
    layers = pd.DataFrame({
        "SLB": LAYER_BOTTOMS, "SLMH": "-99",
        "SLLL": ll, "SDUL": dul, "SSAT": sat,
        "SRGF": root_growth_factor(LAYER_BOTTOMS), "SSKS": ksat,
        "SBDM": g["bdod"], "SLOC": oc, "SLCL": g["clay"], "SLSI": g["silt"],
        "SLCF": g["cfvo"], "SLNI": g["nitrogen"] / 10.0,     # g/kg -> %
        "SLHW": g["phh2o"], "SLHB": -99.0, "SCEC": g["cec"], "SADC": -99.0,
    })
    return SoilProfile(
        id=sid, layers=layers, source="SoilGrids",
        texture=texture_class(g["sand"][0], g["silt"][0], g["clay"][0]),
        depth=float(LAYER_BOTTOMS[-1]),
        description="SoilGrids 2.0, S&R 2006" + (", SOC scaled to WISE" if soc_scale else ""),
        site="-99", country="Indonesia", lat=float(lat), lon=float(lon), family="-99",
        surface=dict(SURFACE_DEFAULTS))


def profiles(props: pd.DataFrame, wlat, wlon, prefix: str = "XS",
             soc_scale: dict | None = SOC_SCALE) -> dict[str, SoilProfile]:
    """Soil profiles keyed by ID for every row of ``props`` (after ``fill_from_nearest``)."""
    out = {}
    for r in props.to_dict("records"):
        sid = profile_id(r["iy"], r["ix"], prefix)
        out[sid] = make_profile(r, wlat[r["iy"]], wlon[r["ix"]], sid, soc_scale)
    return out


# ------------------------------------------------------------ SOC scaling to WISE

SOC_BANDS = {"0-5cm": "0-30", "5-15cm": "0-30", "15-30cm": "0-30",
             "30-60cm": "30-100", "60-100cm": "30-100", "100-200cm": "100-200"}


def wise_soc_pairs(wise_sol: Path, country: str = "Indonesia", bbox=None) -> pd.DataFrame:
    """Pair each WISE layer's organic carbon with SoilGrids at the profile site.

    A WISE layer is matched to the SoilGrids depth interval holding its
    mid-depth (layers deeper than 200 cm are dropped). Needs the regional
    SoilGrids cache. ``bbox`` = (lon_min, lat_min, lon_max, lat_max).
    """
    from sol import read_sol
    rows = []
    for sid, p in read_sol(wise_sol).items():
        if (p.country or "").strip() != country or p.lat is None or p.lon is None:
            continue
        if bbox and not (bbox[0] <= p.lon <= bbox[2] and bbox[1] <= p.lat <= bbox[3]):
            continue
        x, y = (np.asarray(v) for v in _to_igh.transform([p.lon], [p.lat]))
        L = p.layers
        top = np.r_[0.0, L.SLB.to_numpy("float64")[:-1]]
        for t, b, oc in zip(top, L.SLB, L.SLOC):
            mid = (t + b) / 2
            if mid > 200 or not (oc > 0):
                continue
            depth = next(d for d, bot in zip(DEPTHS, LAYER_BOTTOMS) if mid <= bot)
            sg = CachedLayer(cache_path("soc", depth)).sample(x, y)[0]
            if sg == NODATA:
                continue
            rows.append(dict(id=sid, lat=p.lat, lon=p.lon, mid=mid, depth=depth,
                             band=SOC_BANDS[depth], wise_oc=float(oc),
                             sg_oc=sg / PROPS["soc"] / 10.0))
    df = pd.DataFrame(rows)
    df["ratio"] = df.wise_oc / df.sg_oc
    return df


def soc_factors(pairs: pd.DataFrame) -> dict[str, float]:
    """Median WISE/SoilGrids ratio per depth band, expanded to the six SoilGrids depths."""
    band = pairs.groupby("band").ratio.median()
    return {d: float(band[b]) for d, b in SOC_BANDS.items()}

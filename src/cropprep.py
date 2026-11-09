"""Make a crop mask input from your own map: crop area in hectares per pixel.

Any of these sources becomes the standard crop mask (``<cache>/inputs/crop_mask/<name>/``,
one float32 band of crop **ha per pixel**, read by the runner on its own grid
and CRS):

``values: classes``
    A land-cover map with one or more crop classes, e.g. ESA WorldCover (class 40,
    Cropland) or MODIS MCD12Q1 IGBP (12 Croplands, 14 Cropland/Natural Vegetation
    Mosaics). The ha of every source pixel in ``classes`` are summed into the
    output pixel (``aggregate`` source pixels per side). A general cropland class
    does not say which crop grows there: for rice it means "rice may grow on any
    cropland" unless a crop-specific map is used instead.
``values: fraction | percent``
    A raster of the crop's share of each pixel (0-1 or 0-100), e.g. a crop
    fraction product; multiplied by the pixel area.
``values: ha | m2 | km2``
    A raster of crop area per pixel, e.g. MapSPAM physical area (ha).
``values: polygons``
    A vector file of crop fields (shapefile, GeoPackage, GeoJSON ...), burned on
    an equal-area grid (``pixel_m``, default 100 m) with ``all_touched`` and
    ``subpixels`` x ``subpixels`` sub-pixels, as the Java rice map. Optional
    ``class_column`` keeps a field type as the crop class band; ``select`` keeps
    only some polygons.

Pixel area
----------
For a geographic grid (degrees) the area of each source row is exact on the
WGS84 ellipsoid: the cell is mapped to the cylindrical equal-area projection
(ESRI:54034), where a latitude-longitude rectangle stays a rectangle of the same
area. For a projected grid it is the pixel's map area divided by the CRS's areal
scale factor at the output pixel centre (``pyproj`` ``get_factors``), 1 for an
equal-area CRS.

Raster values honour the file's scale, offset and nodata; nodata and NaN count
as no crop. Several files (tiles) must share one grid (CRS, pixel size, aligned
origin); they are read in strips, so a 10 m map of a large area fits in memory
as long as the output does (at most ``MAX_OUTPUT_PIXELS``).
"""
from __future__ import annotations

import glob
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyproj
import rasterio
from rasterio.windows import Window

import config
import inputs
import rasters

VALUES = ("classes", "fraction", "percent", "ha", "m2", "km2", "polygons")
TO_HA = {"ha": 1.0, "m2": 1e-4, "km2": 100.0}
MAX_OUTPUT_PIXELS = 400_000_000          # 1.6 GB of float32
STRIP_PIXELS = 16_000_000                # source pixels read at a time (128 MB as float64)
LATTICE = 16                             # projected grids: areal scale every 16 output pixels
VECTOR_SUFFIXES = (".shp", ".gpkg", ".geojson", ".json", ".parquet", ".fgb", ".gdb", ".kml")


# ----------------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------------
def plan(exp) -> dict:
    """The resolved ``prepare > crop_mask`` settings with defaults filled in."""
    spec = exp.data["prepare"].get("crop_mask")
    if not spec:
        raise config.ConfigError(["setting 'prepare > crop_mask' is not set: say which map holds the crop "
                                  "and what its values mean (configs/rice-java.yml shows how)."], exp.file)
    import weatherprep
    pattern = weatherprep._files(spec["files"], exp)
    files = sorted(Path(f) for f in glob.glob(str(pattern))) if any(ch in str(pattern) for ch in "*?[") \
        else ([pattern] if pattern.exists() else [])
    if not files:
        raise config.ConfigError([f"setting 'prepare > crop_mask > files' ({exp.where('prepare.crop_mask.files')}):\n"
                                  f"  no file matches {pattern}"], exp.file)
    return {"name": spec["name"], "files": files, "pattern": spec["files"], "values": spec["values"],
            "classes": spec.get("classes") or [], "aggregate": int(spec.get("aggregate") or 1),
            "split": int(spec.get("split") or 1), "box": list(spec.get("box") or exp.bbox),
            "layer": spec.get("layer"), "class_column": spec.get("class_column"),
            "select": spec.get("select") or {}, "pixel_m": float(spec.get("pixel_m") or 100.0),
            "subpixels": int(spec.get("subpixels") or 5),
            "source": spec.get("source", ""), "description": spec.get("description", "")}


# ----------------------------------------------------------------------------
# pixel area
# ----------------------------------------------------------------------------
def _is_geographic(crs) -> bool:
    return pyproj.CRS.from_user_input(crs).is_geographic


def row_area_ha(transform, rows: np.ndarray) -> np.ndarray:
    """Exact ellipsoidal area (ha) of one pixel in each row of a geographic (degree) grid."""
    rows = np.asarray(rows, dtype="float64")
    lat_top = transform.f + rows * transform.e
    lat_bot = lat_top + transform.e
    to_ea = rasters.transformer("EPSG:4326", rasters.CRS)
    _, y_top = to_ea.transform(np.zeros_like(lat_top), np.clip(lat_top, -90, 90))
    _, y_bot = to_ea.transform(np.zeros_like(lat_bot), np.clip(lat_bot, -90, 90))
    x0, _ = to_ea.transform(0.0, 0.0)
    x1, _ = to_ea.transform(abs(transform.a), 0.0)
    return np.abs(np.asarray(y_top) - np.asarray(y_bot)) * abs(x1 - x0) / 1e4


def projected_area_ha(crs, transform, rows, cols) -> np.ndarray:
    """Area (ha) of projected pixels at (rows, cols): map area / areal scale at the centre."""
    x = transform.c + (np.asarray(cols) + 0.5) * transform.a
    y = transform.f + (np.asarray(rows) + 0.5) * transform.e
    lon, lat = rasters.transformer(pyproj.CRS.from_user_input(crs).to_string(), "EPSG:4326").transform(x, y)
    k = pyproj.Proj(pyproj.CRS.from_user_input(crs)).get_factors(np.asarray(lon), np.asarray(lat)).areal_scale
    return abs(transform.a * transform.e) / np.asarray(k) / 1e4


# ----------------------------------------------------------------------------
# inspect
# ----------------------------------------------------------------------------
def inspect(path: Path) -> str:
    """A plain description of a raster or vector file, to fill in prepare > crop_mask."""
    path = Path(path)
    if path.suffix.lower() in VECTOR_SUFFIXES or path.is_dir():
        return _inspect_vector(path)
    with rasterio.open(path) as r:
        t = r.transform
        geo = r.crs is not None and r.crs.is_geographic
        unit = "degree" if geo else "m"
        size = f"{t.a:.6g} x {-t.e:.6g} {unit}"
        if geo:
            mid = (r.bounds.bottom + r.bounds.top) / 2
            size += f" (about {t.a * 111_320 * np.cos(np.radians(mid)):.0f} m at {mid:.1f} deg latitude)"
        lines = [f"{path}", f"  raster, {r.count} band(s), {r.width} x {r.height} pixels of {size}",
                 f"  CRS {r.crs.to_string() if r.crs else 'none (set one before using it)'}",
                 f"  bounds {', '.join(f'{v:.6g}' for v in r.bounds)}",
                 f"  type {r.dtypes[0]}, nodata {r.nodata}, scale {r.scales[0]:g}, offset {r.offsets[0]:g}"]
        for i, d in enumerate(r.descriptions, 1):
            if d:
                lines.append(f"  band {i}: {d}")
        # values of a decimated read (at most about 1000 x 1000)
        f = max(1, int(np.ceil(max(r.width, r.height) / 1000)))
        a = r.read(1, out_shape=(max(1, r.height // f), max(1, r.width // f)))
        if r.nodata is not None:
            a = a[a != r.nodata] if np.isfinite(r.nodata) else a[np.isfinite(a)]
        if a.dtype.kind in "iu" and a.size:
            v, n = np.unique(a, return_counts=True)
            top = np.argsort(-n)[:15]
            lines.append(f"  values (sample of {a.size:,} pixels, most common first): "
                         + ", ".join(f"{v[i]} ({100 * n[i] / a.size:.1f}%)" for i in top))
            if len(v) <= 20:
                lines.append("  few distinct values: likely classes (values: classes, with the crop class codes)")
            elif v.max() <= 100:
                lines.append("  values up to 100: a percent share (values: percent) or class codes; "
                             "the product's documentation says which")
        elif a.size:
            a = a[np.isfinite(a)]
            lines.append(f"  values (sample): min {a.min():.4g}, mean {a.mean():.4g}, max {a.max():.4g}"
                         + ("; looks like a fraction (0-1)" if a.max() <= 1 else
                            "; looks like percent (0-100)" if a.max() <= 100 else ""))
    return "\n".join(lines)


def _inspect_vector(path: Path) -> str:
    import geopandas as gpd
    import pyogrio
    layers = pyogrio.list_layers(path)
    lines = [f"{path}", f"  vector, layers: {', '.join(str(n) for n, _ in layers)}"]
    info = pyogrio.read_info(path, layer=layers[0][0])
    lines += [f"  layer {layers[0][0]}: {info['features']:,} features, {info['geometry_type']}, CRS {info['crs']}",
              f"  bounds {', '.join(f'{v:.6g}' for v in info['total_bounds'])}"]
    sample = gpd.read_file(path, layer=layers[0][0], rows=1000, ignore_geometry=True)
    for col in sample.columns:
        vals = sample[col].dropna()
        uniq = vals.unique()
        txt = ", ".join(map(str, uniq[:8])) + (" ..." if len(uniq) > 8 else "")
        lines.append(f"  column {col} ({sample[col].dtype}): {txt}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# rasters -> ha per pixel
# ----------------------------------------------------------------------------
def _source_grid(files: list[Path]):
    """CRS, transform of the first file, and each file's (row, col) offset in source pixels."""
    with rasterio.open(files[0]) as r:
        crs, t0 = r.crs, r.transform
    if crs is None:
        raise ValueError(f"{files[0].name} has no CRS")
    if t0.b != 0 or t0.d != 0 or t0.e >= 0:
        raise ValueError(f"{files[0].name} is not a north-up grid")
    offs = []
    for f in files:
        with rasterio.open(f) as r:
            t = r.transform
            if r.crs != crs or abs(t.a - t0.a) > 1e-9 * abs(t0.a) or abs(t.e - t0.e) > 1e-9 * abs(t0.e):
                raise ValueError(f"{f.name} is not on the grid of {files[0].name} (same CRS and pixel size needed)")
            col, row = (t.c - t0.c) / t0.a, (t.f - t0.f) / t0.e
            if abs(col - round(col)) > 1e-4 or abs(row - round(row)) > 1e-4:
                raise ValueError(f"{f.name} is not aligned with {files[0].name} (origin off by a fraction of a pixel)")
            offs.append((int(round(row)), int(round(col)), r.height, r.width))
    return crs, t0, offs


def _to_ha(a: np.ndarray, p: dict, nodata, scale, offset, area) -> np.ndarray:
    """Crop ha per source pixel from raw values ``a``; ``area`` is ha per pixel (broadcastable)."""
    bad = ~np.isfinite(a) if a.dtype.kind == "f" else np.zeros(a.shape, bool)
    if nodata is not None:
        bad |= (a == nodata) if np.isfinite(nodata) else ~np.isfinite(a)
    v = p["values"]
    if v == "classes":
        out = np.isin(a, p["classes"]) * area
    else:
        x = a.astype("float64") * scale + offset
        if v == "fraction":
            out = x * area
        elif v == "percent":
            out = x / 100.0 * area
        else:
            out = x * TO_HA[v]
    out = np.where(bad | ~np.isfinite(out) | (out < 0), 0.0, out)
    return out


def prepare_raster(p: dict, out_path: Path, log=print) -> dict:
    """Crop ha per pixel from raster file(s) ``p['files']`` over ``p['box']``; returns grid facts."""
    crs, t0, offs = _source_grid(p["files"])
    n, k = p["aggregate"], p["split"]
    if n > 1 and k > 1:
        raise ValueError("aggregate and split cannot both be above 1")
    geo = crs.is_geographic
    x0, y0, x1, y1 = rasters.transformer("EPSG:4326", crs.to_string()).transform_bounds(*p["box"], densify_pts=21)
    # source pixel range of the box, relative to the first file, aligned to the output pixels
    eps = 1e-6                                     # 0.05 / (1 / 1200) may come out as 59.99999...
    c0 = int(np.floor((x0 - t0.c) / t0.a / n + eps)) * n
    c1 = int(np.ceil((x1 - t0.c) / t0.a / n - eps)) * n
    r0 = int(np.floor((y1 - t0.f) / t0.e / n + eps)) * n
    r1 = int(np.ceil((y0 - t0.f) / t0.e / n - eps)) * n
    # not beyond the files (still aligned to whole output pixels)
    lo_r, hi_r = min(o[0] for o in offs), max(o[0] + o[2] for o in offs)
    lo_c, hi_c = min(o[1] for o in offs), max(o[1] + o[3] for o in offs)
    r0, c0 = max(r0, lo_r // n * n), max(c0, lo_c // n * n)
    r1, c1 = min(r1, -(-hi_r // n) * n), min(c1, -(-hi_c // n) * n)
    if r1 <= r0 or c1 <= c0:
        raise ValueError(f"the files do not overlap the box {p['box']}")
    H, W = (r1 - r0) // n * k, (c1 - c0) // n * k
    if H * W > MAX_OUTPUT_PIXELS:
        raise ValueError(f"the output would have {H:,} x {W:,} pixels; use a larger aggregate or a smaller box")
    out = np.zeros((H, W), dtype="float32")
    with rasterio.open(p["files"][0]) as r:
        nodata, scale, offset = r.nodata, r.scales[0], r.offsets[0]
    strip = max(n, (STRIP_PIXELS // max(1, c1 - c0)) // n * n)
    out_t = rasterio.Affine(t0.a * n / k, 0, t0.c + c0 * t0.a, 0, t0.e * n / k, t0.f + r0 * t0.e)
    seen_max = 0.0
    for s0 in range(r0, r1, strip):
        s1 = min(s0 + strip, r1)
        vals = np.zeros((s1 - s0, c1 - c0), dtype="float64")
        if geo:
            area = row_area_ha(t0, np.arange(s0, s1))[:, None]
        else:
            # areal scale at output pixel centres, every LATTICE output pixels (it changes by
            # about 1e-6 over 1.6 km in UTM; exactly 1 in an equal-area CRS); the source
            # pixels inside share it
            L = LATTICE
            orow = np.arange((s0 - r0) // n, (s1 - r0 + n - 1) // n)
            ocol = np.arange(0, (c1 - c0) // n)
            gr, gc = np.meshgrid(orow[::L] + (L - 1) / 2, ocol[::L] + (L - 1) / 2, indexing="ij")
            oa = projected_area_ha(crs, rasterio.Affine(t0.a * n, 0, out_t.c, 0, t0.e * n, out_t.f), gr, gc)
            oa = np.repeat(np.repeat(oa, L, 0), L, 1)[: len(orow), : len(ocol)]
            area = np.repeat(np.repeat(oa / (n * n), n, 0), n, 1)[: s1 - s0, : c1 - c0]
        for f, (fr, fc, fh, fw) in zip(p["files"], offs):
            rr0, rr1 = max(s0, fr), min(s1, fr + fh)
            cc0, cc1 = max(c0, fc), min(c1, fc + fw)
            if rr1 <= rr0 or cc1 <= cc0:
                continue
            with rasterio.open(f) as src:
                a = src.read(1, window=Window(cc0 - fc, rr0 - fr, cc1 - cc0, rr1 - rr0))
            if p["values"] in ("fraction", "percent"):
                ok = np.isfinite(a) if a.dtype.kind == "f" else np.ones(a.shape, bool)
                if nodata is not None and np.isfinite(nodata):
                    ok &= a != nodata
                if ok.any():
                    seen_max = max(seen_max, float(a[ok].max()) * scale + offset)
            ar = area[rr0 - s0: rr1 - s0] if geo else area[rr0 - s0: rr1 - s0, cc0 - c0: cc1 - c0]
            vals[rr0 - s0: rr1 - s0, cc0 - c0: cc1 - c0] = _to_ha(a, p, nodata, scale, offset, ar)
        o0 = (s0 - r0) // n * k
        if n > 1:
            h = (s1 - s0) // n
            block = vals[: h * n].reshape(h, n, -1, n).sum(axis=(1, 3))
        elif k > 1:
            block = np.repeat(np.repeat(vals / (k * k), k, 0), k, 1)
        else:
            block = vals
        out[o0: o0 + block.shape[0]] = block
        log(f"  rows {s0 - r0:,}-{s1 - r0:,} of {r1 - r0:,}: {block.sum():,.0f} ha")
    if p["values"] == "fraction" and seen_max > 1.0001:
        raise ValueError(f"values: fraction, but the file holds values up to {seen_max:g}; "
                         "is it percent (values: percent)?")
    if p["values"] == "percent" and seen_max > 100.01:
        raise ValueError(f"values: percent, but the file holds values up to {seen_max:g}; "
                         "is it an area (values: ha or m2)?")
    profile = dict(driver="GTiff", width=W, height=H, count=1, dtype="float32", crs=crs, transform=out_t,
                   nodata=0, tiled=True, blockxsize=512, blockysize=512, compress="deflate", predictor=3,
                   BIGTIFF="IF_SAFER")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out, 1)
        dst.set_band_description(1, "crop area (ha per pixel)")
    return {"crs": crs.to_string(), "pixel": [abs(out_t.a), abs(out_t.e)], "width": W, "height": H,
            "total_ha": float(out.sum(dtype="float64")), "pixels_with_crop": int((out > 0).sum()),
            "units_per_ha": 1, "class_band": None}


# ----------------------------------------------------------------------------
# polygons -> ha per pixel (share of an equal-area pixel)
# ----------------------------------------------------------------------------
def prepare_polygons(p: dict, out_path: Path, workers: int = 16, log=print) -> dict:
    import geopandas as gpd
    import shapely
    box = gpd.GeoSeries([shapely.box(*p["box"])], crs="EPSG:4326")
    parts = []
    for f in p["files"]:
        g = gpd.read_file(f, layer=p["layer"], bbox=box) if p["layer"] else gpd.read_file(f, bbox=box)
        parts.append(g)
    gdf = pd.concat(parts, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=parts[0].crs)
    for col, keep in p["select"].items():
        if col not in gdf:
            raise ValueError(f"select: the files have no column {col!r} (columns: {', '.join(gdf.columns)})")
        gdf = gdf[gdf[col].isin(keep if isinstance(keep, list) else [keep])]
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if gdf.empty:
        raise ValueError("no polygons inside the box (after select)")
    classes = None
    if p["class_column"]:
        if p["class_column"] not in gdf:
            raise ValueError(f"class_column {p['class_column']!r} is not a column (columns: {', '.join(gdf.columns)})")
        names = sorted(gdf[p["class_column"]].astype(str).unique())
        if len(names) > 254:
            raise ValueError(f"class_column {p['class_column']!r} has {len(names)} values; at most 254")
        codes = {nm: i + 1 for i, nm in enumerate(names)}
        gdf["code"] = gdf[p["class_column"]].astype(str).map(codes)
        classes = pd.DataFrame({"code": list(codes.values()), "name": list(codes)})
    else:
        gdf["code"] = 1
    gdf = rasters.to_grid_crs(gdf)
    grid = rasters.Grid.from_lonlat(*p["box"], res=p["pixel_m"])
    if grid.width * grid.height > MAX_OUTPUT_PIXELS:
        raise ValueError(f"the output would have {grid.height:,} x {grid.width:,} pixels; "
                         "use a larger pixel_m or a smaller box")
    log(f"  {len(gdf):,} polygons -> {grid.width:,} x {grid.height:,} pixels of {p['pixel_m']:g} m")
    rasters.burn(gdf, "code", out_path, grid=grid, factor=p["subpixels"], workers=workers, log=log,
                 descriptions=["crop share of the pixel (percent)", "crop class"])
    units_per_ha = 100.0 / (p["pixel_m"] ** 2 / 1e4)            # percent of a pixel of pixel_m**2 m2
    with rasterio.open(out_path) as r:
        tot = sum(float(r.read(1, window=w).sum(dtype="float64")) for _, w in r.block_windows(1))
        cnt = sum(int((r.read(1, window=w) > 0).sum()) for _, w in r.block_windows(1))
    meta = {"crs": rasters.CRS, "pixel": [p["pixel_m"], p["pixel_m"]], "width": grid.width,
            "height": grid.height, "total_ha": tot / units_per_ha, "pixels_with_crop": cnt,
            "units_per_ha": units_per_ha, "class_band": 2 if classes is not None else None}
    if classes is not None:
        classes.to_csv(out_path.with_name("classes.csv"), index=False)
        meta["classes_path"] = out_path.with_name("classes.csv")
    return meta


# ----------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------
def prepare(exp, overwrite: bool = False, workers: int = 16, log=print) -> Path:
    """Make the crop mask of ``prepare > crop_mask``; write the file and its manifest."""
    p = plan(exp)
    cache = exp.datasets()
    out_dir = inputs.root(cache) / "crop_mask" / p["name"]
    out_path = out_dir / "crop_area.tif"
    if out_path.exists() and not overwrite:
        raise config.ConfigError([f"the crop mask '{p['name']}' exists already ({out_path}); "
                                  "use --overwrite to make it again, or another name."], exp.file)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log(f"crop mask '{p['name']}' from {len(p['files'])} file(s) ({p['pattern']}), values: {p['values']}"
        + (f" {p['classes']}" if p["values"] == "classes" else ""))
    try:
        if p["values"] == "polygons":
            facts = prepare_polygons(p, out_path, workers=workers, log=log)
        else:
            facts = prepare_raster(p, out_path, log=log)
    except ValueError as e:
        raise config.ConfigError([f"setting 'prepare > crop_mask' ({exp.where('prepare.crop_mask')}):\n  {e}"],
                                 exp.file) from None
    files = [f.as_posix() for f in p["files"]]
    meta = {"description": p["description"] or f"crop area (ha per pixel) from {p['values']} of {p['pattern']}",
            "source": p["source"] or ", ".join(Path(f).name for f in files[:3]),
            "made_by": "scripts/prepare_crop_mask.py (prepare > crop_mask)",
            "file": out_path.relative_to(cache).as_posix() if out_path.is_relative_to(cache) else str(out_path),
            "band": 1, "units": "ha per pixel" if facts["units_per_ha"] == 1 else
            f"{facts['units_per_ha']:g} units per ha", "box": p["box"],
            "values": p["values"], "classes_counted": p["classes"] or None,
            "aggregate": p["aggregate"], "split": p["split"], "from_files": files[:50],
            "seconds": round(time.time() - t0, 1), **facts}
    cp = meta.pop("classes_path", None)
    if cp is not None:
        meta["classes"] = cp.relative_to(cache).as_posix() if cp.is_relative_to(cache) else str(cp)
    return inputs.write_manifest("crop_mask", p["name"], meta, cache)

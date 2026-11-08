"""Make the optional class inputs (water regime, region, zones) from your own maps.

Each is ``<cache>/inputs/<kind>/<name>/`` with a GeoTIFF on any grid and CRS (the
runner samples it at the crop pixel centres) and its manifest. Settings in
``prepare > water_regime``, ``prepare > region`` and ``prepare > zones``:

``values: classes``
    A class map. A raster keeps its codes; ``classes`` is an optional CSV of
    ``code, name`` (without it every code is named by its number). A vector file
    (shapefile, GeoPackage ...) is burned on an equal-area grid of ``pixel_m``
    metres (default 100) with ``all_touched``, its ``class_column`` values coded
    1..n in sorted order. For a water regime the codes are what
    ``treatments > water_regimes > irrigation_classes`` lists; for a region the
    names are what ``aoi > region`` may name; for zones what ``by_zone`` uses.
``values: fraction | percent``  (water regime only)
    A raster of the irrigated share of each pixel (0-1 or 0-100).
``values: ratio``  (water regime only)
    The share from two area rasters on one grid: ``irrigated`` / ``total``, e.g.
    MapSPAM's physical area of the crop, irrigated over all technologies (in SPAM
    2020 v2r2: ``spam2020_V2r2_global_A_RICE_I.tif`` / ``..._A_RICE_A.tif``).
    Pixels with no total area get no value.

A share map splits each crop pixel's area: the share to
``treatments > share_regimes > share`` (irrigated), the rest to ``... > rest``
(rainfed). Crop where the map has no value, or no class, takes
``treatments > default_regime``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window

import config
import inputs
import rasters

KINDS = ("water_regime", "region", "zones")
VECTOR_SUFFIXES = (".shp", ".gpkg", ".geojson", ".json", ".parquet", ".fgb", ".gdb", ".kml")
SHARE_VALUES = ("fraction", "percent", "ratio")


def plan(exp, kind: str) -> dict:
    spec = exp.data["prepare"].get(kind)
    if not spec:
        raise config.ConfigError([f"setting 'prepare > {kind}' is not set: say which map to use "
                                  "(configs/rice-java.yml shows how)."], exp.file)
    import weatherprep
    out = {"kind": kind, "name": spec["name"], "values": spec.get("values", "classes"),
           "box": list(spec.get("box") or exp.bbox), "class_column": spec.get("class_column"),
           "layer": spec.get("layer"), "pixel_m": float(spec.get("pixel_m") or 100.0),
           "source": spec.get("source", ""), "description": spec.get("description", "")}
    out["tiles"] = []
    for k in ("files", "classes", "irrigated", "total"):
        out[k] = None
        if spec.get(k):
            p = weatherprep._files(spec[k], exp)
            if k == "files" and any(ch in str(p) for ch in "*?["):       # several tiles of one map
                import glob
                out["tiles"] = sorted(Path(g) for g in glob.glob(str(p)))
                if not out["tiles"]:
                    raise config.ConfigError([f"setting 'prepare > {kind} > files' "
                                              f"({exp.where(f'prepare.{kind}.files')}):\n  no file matches {p}"],
                                             exp.file)
                p = out["tiles"][0]
            if not p.exists():
                raise config.ConfigError([f"setting 'prepare > {kind} > {k}' ({exp.where(f'prepare.{kind}.{k}')}):"
                                          f"\n  {p} does not exist"], exp.file)
            out[k] = p
    return out


def _window(path, box):
    """Grid of a raster file and the window covering a lon/lat box."""
    with rasterio.open(path) as r:
        t = r.transform
        if r.crs is None:
            raise ValueError(f"{Path(path).name} has no CRS")
        g = rasters.Grid(float(t.c), float(t.f), r.width, r.height, float(t.a),
                         rasters.CRS if r.crs == rasterio.crs.CRS.from_user_input(rasters.CRS)
                         else r.crs.to_string(), None if abs(-t.e - t.a) <= 1e-12 * t.a else float(-t.e))
    return g.window(*box)


def _write(path, arr, grid, nodata, description):
    profile = dict(driver="GTiff", width=grid.width, height=grid.height, count=1, dtype=arr.dtype,
                   crs=grid.crs, transform=grid.transform, nodata=nodata, tiled=True, blockxsize=512,
                   blockysize=512, compress="deflate", predictor=3 if arr.dtype.kind == "f" else 2,
                   BIGTIFF="IF_SAFER")
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)
        dst.set_band_description(1, description)


def _read(path, win):
    with rasterio.open(path) as r:
        a = r.read(1, window=win).astype("float64")
        nod, scale, off = r.nodata, r.scales[0], r.offsets[0]
    bad = ~np.isfinite(a)
    if nod is not None:
        bad |= (a == nod) if np.isfinite(nod) else ~np.isfinite(a)
    a = a * scale + off
    a[bad] = np.nan
    return a


def prepare_share(p: dict, out_path: Path, log=print) -> dict:
    if p["values"] == "ratio":
        sub, win = _window(p["irrigated"], p["box"])
        sub_t, win_t = _window(p["total"], p["box"])
        if (sub.crs, sub.res, sub.left, sub.top, sub.width, sub.height) != \
                (sub_t.crs, sub_t.res, sub_t.left, sub_t.top, sub_t.width, sub_t.height):
            raise ValueError(f"{p['irrigated'].name} and {p['total'].name} are not on one grid")
        irr, tot = _read(p["irrigated"], win), _read(p["total"], win_t)
        with np.errstate(invalid="ignore", divide="ignore"):
            share = np.where(np.isfinite(tot) & (tot > 0), np.nan_to_num(irr) / tot, np.nan)
        share = np.clip(share, 0, 1)
    else:
        sub, win = _window(p["files"], p["box"])
        share = _read(p["files"], win)
        if p["values"] == "percent":
            share = share / 100.0
        mx = np.nanmax(share) if np.isfinite(share).any() else 0
        if mx > 1.0001:
            raise ValueError(f"values: {p['values']}, but the file holds shares up to "
                             f"{mx * (100 if p['values'] == 'percent' else 1):g}"
                             + ("; is it percent (values: percent)?" if p["values"] == "fraction" else ""))
        share = np.clip(share, 0, 1)
    arr = share.astype("float32")
    _write(out_path, arr, sub, np.nan, "irrigated share of the pixel (0-1; NaN = no value)")
    ok = np.isfinite(arr)
    log(f"  {sub.width:,} x {sub.height:,} pixels, {ok.mean() * 100:.1f}% with a value, mean share "
        f"{np.nanmean(arr) if ok.any() else float('nan'):.2f}")
    return {"values": "share", "crs": sub.crs, "pixel": [sub.res, sub.ry]}


def _mosaic(tiles, box):
    """Class tiles on one grid (e.g. LGRIP30's 10 x 10 degree tiles), joined over the box."""
    from rasterio.merge import merge
    srcs = [rasterio.open(t) for t in tiles]
    try:
        crs, res = srcs[0].crs, srcs[0].res
        for s in srcs[1:]:
            if s.crs != crs or any(abs(a - b) > 1e-9 * abs(a) for a, b in zip(s.res, res)):
                raise ValueError(f"{Path(s.name).name} is not on the grid of {Path(srcs[0].name).name}")
        x0, y0, x1, y1 = rasters.transformer("EPSG:4326", crs.to_string()).transform_bounds(*box, densify_pts=21)
        keep = [s for s in srcs if not (s.bounds.right <= x0 or s.bounds.left >= x1 or s.bounds.top <= y0
                                        or s.bounds.bottom >= y1)]
        if not keep:
            raise ValueError(f"none of the {len(tiles)} files overlaps the box {box}")
        arr, t = merge(keep, bounds=(x0, y0, x1, y1), nodata=0)
        nod = srcs[0].nodata
    finally:
        for s in srcs:
            s.close()
    crs_s = rasters.CRS if crs == rasterio.crs.CRS.from_user_input(rasters.CRS) else crs.to_string()
    g = rasters.Grid(float(t.c), float(t.f), arr.shape[2], arr.shape[1], float(t.a), crs_s,
                     None if abs(-t.e - t.a) <= 1e-12 * t.a else float(-t.e))
    return g, arr[0], nod


def prepare_classes(p: dict, out_path: Path, workers: int = 16, log=print) -> dict:
    f = p["files"]
    if f.suffix.lower() in VECTOR_SUFFIXES or f.is_dir():
        import geopandas as gpd
        import shapely
        box = gpd.GeoSeries([shapely.box(*p["box"])], crs="EPSG:4326")
        g = gpd.read_file(f, layer=p["layer"], bbox=box) if p["layer"] else gpd.read_file(f, bbox=box)
        col = p["class_column"]
        if col not in g:
            raise ValueError(f"class_column {col!r} is not a column of {f.name} "
                             f"(columns: {', '.join(c for c in g.columns if c != 'geometry')})")
        g = g[g.geometry.notna() & ~g.geometry.is_empty & g[col].notna()]
        if g.empty:
            raise ValueError(f"{f.name} has no polygons inside the box {p['box']}")
        names = sorted(g[col].astype(str).str.strip().unique())
        code = {n: i + 1 for i, n in enumerate(names)}
        g["code"] = g[col].astype(str).str.strip().map(code)
        grid = rasters.Grid.from_lonlat(*p["box"], res=p["pixel_m"])
        rasters.burn(rasters.to_grid_crs(g), "code", out_path, grid=grid, factor=1,
                     dtype="uint8" if len(names) < 255 else "uint16", workers=workers, log=log,
                     descriptions=[f"class ({col})"])
        table = pd.DataFrame({"code": list(code.values()), "name": names})
        return {"values": "classes", "crs": rasters.CRS, "pixel": [p["pixel_m"], p["pixel_m"]]}, table
    if len(p["tiles"]) > 1:
        sub, a, nod = _mosaic(p["tiles"], p["box"])
    else:
        sub, win = _window(f, p["box"])
        with rasterio.open(f) as r:
            a = r.read(1, window=win)
            nod = r.nodata
    if a.dtype.kind == "f":
        raise ValueError(f"{f.name} holds decimal values; a class map holds whole-number codes"
                         + (" (for a share use values: fraction or percent)" if p["kind"] == "water_regime" else ""))
    if nod is not None and nod != 0:
        a = np.where(a == nod, 0, a)
    _write(out_path, a, sub, 0, "class (0 = none)")
    present = sorted(int(v) for v in np.unique(a) if v != 0)
    if p["classes"] is not None:
        table = pd.read_csv(p["classes"])
        if not {"code", "name"} <= set(table.columns):
            raise ValueError(f"{p['classes'].name} needs columns code and name (has {', '.join(table.columns)})")
        unnamed = sorted(set(present) - set(table.code.astype(int)))
        if unnamed:
            log(f"  note: codes {unnamed[:10]} in the map have no name in {p['classes'].name}")
    else:
        table = pd.DataFrame({"code": present, "name": [str(c) for c in present]})
    log(f"  {sub.width:,} x {sub.height:,} pixels, codes {present[:15]}{' ...' if len(present) > 15 else ''}")
    return {"values": "classes", "crs": sub.crs, "pixel": [sub.res, sub.ry]}, table


def prepare(exp, kind: str, overwrite: bool = False, workers: int = 16, log=print) -> Path:
    p = plan(exp, kind)
    cache = exp.path("cache")
    out_dir = inputs.root(cache) / kind / p["name"]
    out_path = out_dir / ("share.tif" if p["values"] in SHARE_VALUES else "classes.tif")
    if out_path.exists() and not overwrite:
        raise config.ConfigError([f"the {kind.replace('_', ' ')} input '{p['name']}' exists already ({out_path}); "
                                  "use --overwrite to make it again, or another name."], exp.file)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = p["files"] or p["irrigated"]
    src_text = src.name
    if len(p["tiles"]) > 1:
        src_text = (f"{len(p['tiles'])} files ({', '.join(t.name for t in p['tiles'][:3])}"
                    + (" ..." if len(p["tiles"]) > 3 else "") + ")")
    log(f"{kind.replace('_', ' ')} '{p['name']}' from {src_text}" + (f" and {p['total']}" if p["total"] else "")
        + f", values: {p['values']}")
    try:
        if p["values"] in SHARE_VALUES:
            facts, table = prepare_share(p, out_path, log=log), None
        else:
            facts, table = prepare_classes(p, out_path, workers=workers, log=log)
    except config.ConfigError:
        raise
    except ValueError as e:
        raise config.ConfigError([f"setting 'prepare > {kind}' ({exp.where(f'prepare.{kind}')}):\n  {e}"],
                                 exp.file) from None
    rel = lambda q: q.relative_to(cache).as_posix() if q.is_relative_to(cache) else str(q)  # noqa: E731
    meta = {"description": p["description"] or f"{kind.replace('_', ' ')} from {src_text}",
            "source": p["source"] or src_text, "made_by": f"scripts/prepare_layers.py --kind {kind}",
            "file": rel(out_path), "band": 1, "box": p["box"], "from_files": [str(q) for q in
                                                                            (p["files"], p["irrigated"], p["total"]) if q],
            **facts}
    if table is not None:
        table.to_csv(out_dir / "classes.csv", index=False)
        meta["classes"] = rel(out_dir / "classes.csv")
    return inputs.write_manifest(kind, p["name"], meta, cache)

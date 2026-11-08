"""Make a soil input: DSSAT profiles plus the map of which weather cell uses which.

The standard soil input (``<cache>/inputs/soil/<name>/``) is

* ``soil.SOL``: the DSSAT soil profiles (at most 20 layers each, ``NL = 20`` in
  ``Utilities/ModuleDefs.for:49``), and
* ``soil_map.csv``: one row per weather cell with crop, ``iy, ix, lat, lon,
  soil_id, ha, share, filled_from``. ``soil_id`` is the profile of the cell,
  ``share`` the part of the cell's crop area on that profile.

One profile per weather cell (``scripts/soil_class_sensitivity.py`` measures what a
profile per soil-map class would change). The runner
gives every cell its own copy of its profile (ID ``XS<iy><ix>``), so settings
per cell or stratum (SLPF) work the same whether cells share a profile or not.

Two methods (``prepare > soil > method``):

``soilgrids``
    The crop-weighted mean of SoilGrids 2.0 under the crop pixels of each cell,
    turned into a profile by pedotransfer (``src/soils.py``). Organic carbon can
    be scaled (``soc_scale``): ``wise_indonesia`` are the factors fitted to WISE
    profiles of Indonesia (``soils.SOC_SCALE``), ``none`` keeps SoilGrids, or six
    factors by depth.
``sol``
    Your own profiles (a soil survey, WISE, HC27 ...) in a ``.SOL`` file, with
    either ``profile``: one profile for every cell, or ``map``: a raster of codes
    (plus ``classes``, a CSV of ``code, soil_id``) or a vector file (plus
    ``id_column``). Each cell takes the profile under most of its crop area
    (crop pixels sampled at their centres); a cell whose crop lies wholly off the
    map takes the profile of the nearest cell that has one.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import numpy as np
import pandas as pd

import config
import inputs
import rasters

METHODS = ("soilgrids", "sol")
MAX_LAYERS = 20                              # NL = 20, Utilities/ModuleDefs.for:49
VECTOR_SUFFIXES = (".shp", ".gpkg", ".geojson", ".json", ".parquet", ".fgb", ".gdb", ".kml")
MAX_MAP_PIXELS = 400_000_000


def soc_scale(value):
    """The SOC factors (dict by SoilGrids depth) of a ``soc_scale`` setting, or None."""
    import soils
    if value in (None, "none"):
        return None
    if value == "wise_indonesia":
        return dict(soils.SOC_SCALE)
    return {d: float(value[d]) for d in soils.DEPTHS}


def plan(exp) -> dict:
    """The resolved ``prepare > soil`` settings with defaults filled in."""
    s = dict(exp.data["prepare"].get("soil") or {})
    method = s.get("method", "soilgrids")
    out = {"name": s.get("name"), "method": method, "description": s.get("description", ""),
           "source": s.get("source", ""), "country": s.get("country", "-99"),
           "soc_scale": s.get("soc_scale", "none"), "profile": s.get("profile"),
           "id_column": s.get("id_column"), "layer": s.get("layer"), "file": None, "map": None,
           "classes": None}
    if method == "sol":
        import weatherprep
        for k in ("file", "map", "classes"):
            if s.get(k):
                out[k] = weatherprep._files(s[k], exp)
                if not out[k].exists():
                    raise config.ConfigError([f"setting 'prepare > soil > {k}' "
                                              f"({exp.where(f'prepare.soil.{k}')}):\n  {out[k]} does not exist"],
                                             exp.file)
    return out


# ----------------------------------------------------------------------------
# reading a .SOL quickly
# ----------------------------------------------------------------------------
_ID = re.compile(r"^\*(?!SOILS)(.{10})", re.IGNORECASE)


def sol_ids(path) -> list[str]:
    """Profile IDs in a .SOL file, in file order (header lines only, no parsing)."""
    out = []
    with open(path, encoding="latin-1") as f:
        for ln in f:
            m = _ID.match(ln)
            if m and len(ln.rstrip("\n")) > 11:
                out.append(m.group(1))
    return out


# ----------------------------------------------------------------------------
# cells with crop, and the profile under each
# ----------------------------------------------------------------------------
def crop_cells(crop, weather, bbox, layers=None) -> pd.DataFrame:
    """Crop area per weather cell (and per layer class), as the runner counts it."""
    import rasters
    lat, lon, valid = weather.grid()
    grid, _ = crop.grid().window(*bbox)
    layers = layers or {}
    st = rasters.aggregate(crop.file(), layers, lat, lon, valid, grid=grid,
                           units_per_ha=crop.units_per_ha, class_band=crop.class_band,
                           sub=rasters.subdivision(grid))
    return st[st.iy >= 0]


def _vector_to_raster(path, layer, id_column, crop, bbox, out_tif):
    """Burn a vector soil map on the crop mask's grid window: codes 1..n and their soil IDs."""
    import geopandas as gpd
    import rasterio
    import shapely
    from rasterio.features import rasterize
    box = gpd.GeoSeries([shapely.box(*bbox)], crs="EPSG:4326")
    g = gpd.read_file(path, layer=layer, bbox=box) if layer else gpd.read_file(path, bbox=box)
    if id_column not in g:
        raise ValueError(f"id_column {id_column!r} is not a column of {Path(path).name} "
                         f"(columns: {', '.join(c for c in g.columns if c != 'geometry')})")
    g = g[g.geometry.notna() & ~g.geometry.is_empty & g[id_column].notna()]
    if g.empty:
        raise ValueError(f"{Path(path).name} has no soil polygons inside the box {bbox}")
    ids = sorted(g[id_column].astype(str).str.strip().unique())
    code = {s: i + 1 for i, s in enumerate(ids)}
    grid, _ = crop.grid().window(*bbox)
    if grid.width * grid.height > MAX_MAP_PIXELS:
        raise ValueError(f"the crop mask window has {grid.height:,} x {grid.width:,} pixels; too many to burn "
                         "the soil map on it (use a raster soil map)")
    g = g.to_crs(grid.crs)
    g = g.iloc[np.argsort(-g.geometry.area.values, kind="stable")]       # small polygons burned last
    arr = rasterize(zip(shapely.force_2d(g.geometry.values), g[id_column].astype(str).str.strip().map(code)),
                    out_shape=(grid.height, grid.width), transform=grid.transform, fill=0,
                    all_touched=True, dtype="uint16" if len(ids) < 65535 else "uint32")
    with rasterio.open(out_tif, "w", driver="GTiff", width=grid.width, height=grid.height, count=1,
                       dtype=arr.dtype, crs=grid.crs, transform=grid.transform, nodata=0,
                       compress="deflate", tiled=True, blockxsize=512, blockysize=512) as dst:
        dst.write(arr, 1)
    return pd.DataFrame({"code": list(code.values()), "soil_id": list(code)})


def assign(cells: pd.DataFrame, wlat, wlon) -> pd.DataFrame:
    """One profile per cell: the soil_id under most of its crop; cells off the map from the nearest cell.

    ``cells``: iy, ix, soil_id ('' where the crop has no soil class), ha.
    """
    from scipy.spatial import cKDTree
    tot = cells.groupby(["iy", "ix"]).ha.sum().rename("cell_ha")
    on = cells[cells.soil_id != ""]
    by = on.groupby(["iy", "ix", "soil_id"], as_index=False).ha.sum()
    by = by.sort_values(["iy", "ix", "ha", "soil_id"], ascending=[True, True, False, True])
    dom = by.drop_duplicates(["iy", "ix"]).set_index(["iy", "ix"])
    out = tot.to_frame().join(dom[["soil_id", "ha"]].rename(columns={"ha": "dom_ha"}))
    out = out.reset_index()
    out["share"] = (out.dom_ha / out.cell_ha).fillna(0.0)
    out["filled_from"] = ""
    lack = out.soil_id.isna()
    if lack.all():
        raise ValueError("no crop pixel lies on a mapped soil class")
    if lack.any():
        have = out[~lack]
        tree = cKDTree(have[["iy", "ix"]].to_numpy())
        _, j = tree.query(out.loc[lack, ["iy", "ix"]].to_numpy())
        src = have.iloc[j]
        out.loc[lack, "soil_id"] = src.soil_id.to_numpy()
        out.loc[lack, "filled_from"] = [f"{a},{b}" for a, b in zip(src.iy, src.ix)]
    out["lat"], out["lon"] = wlat[out.iy], wlon[out.ix]
    return out.rename(columns={"cell_ha": "ha"})[["iy", "ix", "lat", "lon", "soil_id", "ha", "share",
                                                   "filled_from"]]


# ----------------------------------------------------------------------------
# method sol
# ----------------------------------------------------------------------------
def prepare_sol(exp, p: dict, weather_name: str, crop_name: str, overwrite: bool = False, log=print) -> Path:
    """Profiles from ``p['file']`` assigned per weather cell; writes the soil input ``p['name']``."""
    from sol import read_sol, write_sol
    cache = exp.path("cache")
    weather = inputs.open_dataset("weather", weather_name, cache)
    crop = inputs.open_dataset("crop_mask", crop_name, cache)
    out_dir = inputs.root(cache) / "soil" / p["name"]
    if (out_dir / "soil.SOL").exists() and not overwrite:
        raise config.ConfigError([f"the soil input '{p['name']}' exists already ({out_dir}); use --overwrite "
                                  "to make it again, or another name."], exp.file)
    out_dir.mkdir(parents=True, exist_ok=True)
    lat, lon, _ = weather.grid()
    ids_in_file = sol_ids(p["file"])
    if not ids_in_file:
        raise _bad(exp, "file", f"{p['file']} holds no soil profiles (lines starting *<10-character ID>)")
    from collections import Counter
    dup = {i for i, n in Counter(ids_in_file).items() if n > 1}
    try:
        if p["map"] is None:
            if p["profile"] not in ids_in_file:
                near = [i for i in ids_in_file if i[:4] == str(p["profile"])[:4]][:5] or ids_in_file[:5]
                raise _bad(exp, "profile", f"{p['profile']!r} is not a profile of {p['file'].name} "
                                           f"(e.g. {', '.join(near)})")
            cells = crop_cells(crop, weather, exp.bbox)
            cells = cells.groupby(["iy", "ix"], as_index=False).ha.sum()
            cells["soil_id"] = p["profile"]
            log(f"  one profile, {p['profile']}, for all {len(cells):,} weather cells with crop")
        else:
            if p["map"].suffix.lower() in VECTOR_SUFFIXES or p["map"].is_dir():
                tif = out_dir / "soil_codes.tif"
                table = _vector_to_raster(p["map"], p["layer"], p["id_column"], crop, exp.bbox, tif)
                log(f"  {len(table):,} soil IDs in {p['map'].name} burned on the crop mask grid")
            else:
                tif = p["map"]
                table = pd.read_csv(p["classes"])
                miss = {"code", "soil_id"} - set(table.columns)
                if miss:
                    raise _bad(exp, "classes", f"{p['classes'].name} needs columns code and soil_id "
                                               f"(has {', '.join(table.columns)})")
            table["soil_id"] = table.soil_id.astype(str).str.strip()
            code_to_id = dict(zip(table.code.astype(int), table.soil_id))
            st = crop_cells(crop, weather, exp.bbox, layers={"soil": tif})
            st["soil_id"] = st.soil.map(code_to_id).fillna("")
            cells = assign(st[["iy", "ix", "soil_id", "ha"]], lat, lon)
            n_fill = int((cells.filled_from != "").sum())
            off = st.loc[st.soil_id == "", "ha"].sum()
            log(f"  {len(cells):,} weather cells with crop; {off:,.0f} ha of crop ({100 * off / st.ha.sum():.1f}%) "
                f"on no soil class; {n_fill} cells took the nearest cell's profile; dominant profile covers "
                f"{(cells.share * cells.ha).sum() / cells.ha.sum() * 100:.0f}% of the crop (area-weighted)")
        if "share" not in cells:
            cells["lat"], cells["lon"] = lat[cells.iy], lon[cells.ix]
            cells["share"], cells["filled_from"] = 1.0, ""
            cells = cells[["iy", "ix", "lat", "lon", "soil_id", "ha", "share", "filled_from"]]
    except config.ConfigError:
        raise
    except ValueError as e:
        raise _bad(exp, "map" if p["map"] else "profile", str(e)) from None
    used = sorted(set(cells.soil_id))
    lacking = [s for s in used if s not in set(ids_in_file)]
    if lacking:
        raise _bad(exp, "file", f"{len(lacking)} profile(s) of the map are not in {p['file'].name}: "
                                f"{', '.join(lacking[:5])}{' ...' if len(lacking) > 5 else ''}")
    bad_dup = [s for s in used if s in dup]
    if bad_dup:
        raise _bad(exp, "file", f"profile(s) {', '.join(bad_dup[:5])} appear more than once in {p['file'].name}")
    try:
        profs = read_sol(p["file"], ids=used)        # SoilProfile refuses more than MAX_LAYERS layers
    except ValueError as e:
        raise _bad(exp, "file", f"{p['file'].name}: {e} (DSSAT reads at most {MAX_LAYERS} layers "
                                "per profile)") from None
    write_sol(out_dir / "soil.SOL", [profs[s] for s in used], title=f"xdssat soil '{p['name']}' from {p['file'].name}")
    cells.to_csv(out_dir / "soil_map.csv", index=False)
    rel = lambda q: q.relative_to(cache).as_posix() if q.is_relative_to(cache) else str(q)  # noqa: E731
    return inputs.write_manifest("soil", p["name"], {
        "description": p["description"] or f"one profile per weather cell with crop, from {p['file'].name}",
        "source": p["source"] or p["file"].name,
        "made_by": "scripts/prepare_soils.py (prepare > soil, method sol)",
        "method": "sol", "file": rel(out_dir / "soil.SOL"), "map": rel(out_dir / "soil_map.csv"),
        "weather": weather_name, "crop_mask": crop_name, "cell_id": "XS{iy:04d}{ix:04d}",
        "from_file": str(p["file"]), "from_map": str(p["map"]) if p["map"] else None,
        "profiles": len(used), "cells": len(cells),
        "sub_points": rasters.subdivision(crop.grid().window(*exp.bbox)[0])}, cache)


def _bad(exp, key, text):
    return config.ConfigError([f"setting 'prepare > soil > {key}' ({exp.where(f'prepare.soil.{key}')}):\n  {text}"],
                              exp.file)


# ----------------------------------------------------------------------------
# per-cell copies (used by inputs.Soil.profiles)
# ----------------------------------------------------------------------------
def cell_profiles(raw: dict, soil_map: pd.DataFrame, cell_id: str = "XS{iy:04d}{ix:04d}") -> dict:
    """Profile of every mapped cell under the cell's own ID; a profile whose ID already is the
    cell ID is used as is, others are deep copies (so per-cell SLPF changes stay per cell)."""
    out = {}
    for iy, ix, sid in zip(soil_map.iy, soil_map.ix, soil_map.soil_id):
        cid = cell_id.format(iy=int(iy), ix=int(ix))
        p = raw[sid]
        if sid != cid:
            p = copy.deepcopy(p)
            p.id = cid
        out[cid] = p
    return out

"""Crop-area rasters, class layers (water regime, region, zones) and their link
to the weather grid.

Crop mask on any grid
---------------------
The runner reads a crop mask on **any** north-up grid in any CRS: band 1 is the
crop area per pixel (``units_per_ha`` units per hectare; 1 for the standard ha
per pixel that ``src/cropprep.py`` writes). Each pixel goes to the weather cell
holding its centre. Class layers on the same grid are read window by window; a
class layer on another grid or CRS is sampled at the crop pixel centres (the
class of the layer pixel under the centre; 0 outside the layer).

Equal-area grid of the Java case study
--------------------------------------
The rasters that ``scripts/prepare_rasters.py`` burns share one grid: ESRI:54034 (World Cylindrical Equal Area,
``+proj=cea +lat_ts=0 +lon_0=0 +datum=WGS84``) at 100 m. Because the projection
is equal-area, **every pixel is exactly 1 ha**: a pixel at 106.8E 6.6S measures
10,000.0 m2 geodesically, with sides of 99.3 m and 100.7 m (measured with
``pyproj.Geod``, 2026-11-05). With the standard parallel at the equator the
pixels stay within 1.3% of square across Indonesia.

The national grid ``IDN`` covers 94.9E to 141.1E, 11.1S to 6.2N. A smaller
area (Java, say) is a window of it, so every product lines up pixel for pixel.

Rasterising
-----------
Polygons are burned in tiles of 1024 x 1024 pixels, with ``all_touched``: every
pixel that a polygon touches gets a value, so no polygon is lost, however
small. Rice fields are also burned on a finer grid (``factor`` 5, so 20 m) and
counted back to 100 m, which gives each pixel the share of its hectare covered
by rice. Summing the share over pixels gives the area in ha. A pixel that is
touched but has no 20 m sub-pixel centre inside rice gets share 1 (0.01 ha).
Categorical layers (soil, region) take the class of the touching polygon.

Rice area per weather cell
--------------------------
Each 100 m pixel goes to the one 0.1 degree AgERA5 cell that holds its centre.
That splits the pixels into cells with nothing dropped and nothing counted
twice, so the cell totals add up to the raster total.

Link to weather
---------------
``aggregate`` turns the rasters into a table with one row per (AgERA5 cell,
rice class, soil class, region) and the rice area in ha. Rice pixels whose
AgERA5 cell has no data (sea in the land-only AgERA5 grid) are moved to the
nearest cell with data, and the distance is recorded.
"""
from __future__ import annotations

import functools
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyproj
import rasterio
import shapely
from rasterio.features import rasterize
from rasterio.transform import Affine
from rasterio.windows import Window

import config

CRS = "ESRI:54034"
RES = 100.0
TILE = 1024

_to_ll = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
_to_ea = pyproj.Transformer.from_crs("EPSG:4326", CRS, always_xy=True)


@functools.lru_cache(maxsize=32)
def transformer(src: str, dst: str) -> pyproj.Transformer:
    """A cached lon/lat-order (always_xy) transformer between two CRS."""
    if (src, dst) == (CRS, "EPSG:4326"):
        return _to_ll
    if (src, dst) == ("EPSG:4326", CRS):
        return _to_ea
    return pyproj.Transformer.from_crs(src, dst, always_xy=True)


@dataclass(frozen=True)
class Grid:
    """A north-up grid: left/top edge and pixel size in units of ``crs``, size in pixels.

    The default CRS is the equal-area ESRI:54034 of the Java rasters; a crop mask
    on another CRS (geographic degrees, UTM metres ...) carries its own. ``res_y``
    is the pixel height when it differs from the width.
    """
    left: float
    top: float
    width: int
    height: int
    res: float = RES
    crs: str = CRS
    res_y: float | None = None

    @property
    def ry(self) -> float:
        return self.res_y or self.res

    @property
    def transform(self) -> Affine:
        return Affine(self.res, 0, self.left, 0, -self.ry, self.top)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.left, self.top - self.height * self.ry,
                self.left + self.width * self.res, self.top)

    @classmethod
    def from_lonlat(cls, lon_min, lat_min, lon_max, lat_max, res=RES) -> "Grid":
        x0, y0 = _to_ea.transform(lon_min, lat_min)
        x1, y1 = _to_ea.transform(lon_max, lat_max)
        left, bottom = np.floor(x0 / res) * res, np.floor(y0 / res) * res
        right, top = np.ceil(x1 / res) * res, np.ceil(y1 / res) * res
        return cls(float(left), float(top), int(round((right - left) / res)),
                   int(round((top - bottom) / res)), res)

    def window(self, lon_min, lat_min, lon_max, lat_max) -> tuple["Grid", Window]:
        """The sub-grid covering a lon/lat box, aligned to this grid, and its window.

        Raises ValueError if the box does not overlap the grid.
        """
        if self.crs == CRS and self.res_y is None:
            sub = Grid.from_lonlat(lon_min, lat_min, lon_max, lat_max, self.res)
            col = int(round((sub.left - self.left) / self.res))
            row = int(round((self.top - sub.top) / self.res))
            col_end, row_end = col + sub.width, row + sub.height
        else:
            x0, y0, x1, y1 = transformer("EPSG:4326", self.crs).transform_bounds(
                lon_min, lat_min, lon_max, lat_max, densify_pts=21)
            col = int(np.floor((x0 - self.left) / self.res + 1e-9))
            col_end = int(np.ceil((x1 - self.left) / self.res - 1e-9))
            row = int(np.floor((self.top - y1) / self.ry + 1e-9))
            row_end = int(np.ceil((self.top - y0) / self.ry - 1e-9))
        col0, row0 = max(col, 0), max(row, 0)
        col1 = min(col_end, self.width)
        row1 = min(row_end, self.height)
        if col1 <= col0 or row1 <= row0:
            raise ValueError(f"the box {lon_min:g}, {lat_min:g}, {lon_max:g}, {lat_max:g} (lon/lat) does not "
                             f"overlap the raster ({self.crs}, bounds {', '.join(f'{v:g}' for v in self.bounds)})")
        win = Window(col0, row0, col1 - col0, row1 - row0)
        g = Grid(self.left + col0 * self.res, self.top - row0 * self.ry,
                 col1 - col0, row1 - row0, self.res, self.crs, self.res_y)
        return g, win

    def tiles(self, size: int = TILE):
        for r in range(0, self.height, size):
            for c in range(0, self.width, size):
                yield Window(c, r, min(size, self.width - c), min(size, self.height - r))

    def centres_lonlat(self, rows: np.ndarray, cols: np.ndarray):
        x = self.left + (cols + 0.5) * self.res
        y = self.top - (rows + 0.5) * self.ry
        return transformer(self.crs, "EPSG:4326").transform(x, y)


IDN = Grid.from_lonlat(94.9, -11.1, 141.1, 6.2)

# Area of interest of the experiment file (aoi > name and aoi > bbox, lon/lat
# west, south, east, north; default Java 105.0, -9.0, 114.7, -5.0). The box is
# approximate; the region raster decides membership.
AOI = {config.CURRENT.aoi: config.CURRENT.bbox}

# JSWH (rice field type) in the ATR/BPN 2024 rice-field map, coded for the raster.
RICE_CLASSES = {
    "Irigasi Teknis": 1,
    "Irigasi": 2,
    "Irigasi Non-Teknis": 3,
    "Irigasi Permukaan": 4,
    "Non Irigasi": 5,
    "Tadah Hujan": 6,
    "Pasang Surut": 7,
    "Rawa/Lebak": 8,
    None: 9,                     # polygon has no attributes
}
RICE_CLASS_NAMES = {v: (k or "Tidak diketahui") for k, v in RICE_CLASSES.items()}


def to_grid_crs(gdf):
    """Reproject to the grid CRS and drop Z/M so rasterio sees plain 2-D shapes."""
    g = gdf.to_crs(CRS)
    g["geometry"] = shapely.force_2d(g.geometry.values)
    return g


def _tile_raster(wkb, values, left: float, top: float, height: int, width: int,
                 res: float, factor: int, n_classes: int, dtype):
    """Burn one tile from WKB shapes. Returns (share 0-100 or None, class).

    Every pixel a polygon touches is kept (``all_touched``). For ``factor`` > 1
    the share comes from the sub-grid count; a touched pixel whose count is 0
    (the polygon covers no sub-pixel centre) gets share 1, the smallest
    non-zero value, and the class of the touching polygon.
    """
    geoms = shapely.from_wkb(wkb)
    shapes = list(zip(geoms, values.tolist()))
    touched = rasterize(shapes, out_shape=(height, width),
                        transform=Affine(res, 0, left, 0, -res, top),
                        fill=0, dtype=dtype, all_touched=True)
    if factor == 1:
        return None, touched
    fine_res = res / factor
    burned = rasterize(shapes, out_shape=(height * factor, width * factor),
                       transform=Affine(fine_res, 0, left, 0, -fine_res, top),
                       fill=0, dtype=dtype)
    b = burned.reshape(height, factor, width, factor)
    counts = np.stack([(b == c).sum(axis=(1, 3), dtype=np.uint16)
                       for c in range(1, n_classes + 1)])
    total = counts.sum(axis=0)
    share = np.rint(total * (100.0 / factor ** 2)).astype(np.uint8)
    cls = np.where(total > 0, counts.argmax(axis=0) + 1, 0).astype(dtype)
    only_touched = (touched > 0) & (share == 0)
    share[only_touched] = 1
    cls[only_touched] = touched[only_touched]
    return share, cls


def burn(gdf, column: str, path, grid: Grid = IDN, factor: int = 1, dtype="uint8",
         workers: int = 16, log=print, descriptions=None):
    """Rasterise integer codes in ``gdf[column]`` (1..n, 0 = none) to a GeoTIFF.

    ``gdf`` must already be in the grid CRS (``to_grid_crs``). Every pixel a
    polygon touches gets a value (``all_touched``). With ``factor`` > 1 the
    file has two bands: share of the pixel covered (1-100, percent of 1 ha) and
    the majority class; with ``factor`` 1 it has one band, the class.

    Where polygons overlap or meet inside a pixel, the one burned last wins.
    Polygons are burned largest first, so small polygons are never hidden by a
    large neighbour.

    Tiles are burned in separate processes: rasterio converts each shape to
    GeoJSON in Python, which holds the GIL, so threads do not scale (measured:
    788,305 Java rice polygons took 588 s on 16 threads).
    """
    from concurrent.futures import ProcessPoolExecutor

    t0 = time.time()
    gdf = gdf.iloc[np.argsort(-gdf.geometry.area.values, kind="stable")]
    geoms = gdf.geometry.values
    values = gdf[column].to_numpy().astype("int64")
    if values.min() < 1:
        raise ValueError(f"{column}: codes must be >= 1 (0 is reserved for 'none')")
    n_classes = int(values.max())
    tree = shapely.STRtree(geoms)
    count = 2 if factor > 1 else 1
    profile = dict(driver="GTiff", width=grid.width, height=grid.height, count=count,
                   dtype=dtype, crs=CRS, transform=grid.transform, nodata=0,
                   tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                   predictor=2, BIGTIFF="IF_SAFER")
    jobs = []
    for win in grid.tiles():
        left = grid.left + win.col_off * grid.res
        top = grid.top - win.row_off * grid.res
        box = shapely.box(left, top - win.height * grid.res, left + win.width * grid.res, top)
        idx = tree.query(box, predicate="intersects")
        if idx.size:
            jobs.append((win, (shapely.to_wkb(geoms[idx]), values[idx], left, top,
                               win.height, win.width, grid.res, factor, n_classes, dtype)))
    with rasterio.open(path, "w", **profile) as dst:
        for i, d in enumerate(descriptions or []):
            dst.set_band_description(i + 1, d)

        def put(win, share, cls):
            if factor > 1:
                dst.write(share.astype(dtype), 1, window=win)
                dst.write(cls, 2, window=win)
            else:
                dst.write(cls, 1, window=win)

        if workers <= 1:
            for win, args in jobs:
                put(win, *_tile_raster(*args))
        else:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_tile_raster, *args): win for win, args in jobs}
                for fut in futs:
                    put(futs[fut], *fut.result())
    log(f"{path}: {len(jobs)} non-empty tiles, {len(gdf)} polygons, {time.time() - t0:.0f} s")
    return path


def read(path, grid: Grid | None = None, band: int = 1) -> np.ndarray:
    """Read a band, optionally only the window of a sub-grid of IDN."""
    with rasterio.open(path) as src:
        if grid is None:
            return src.read(band)
        col = int(round((grid.left - src.transform.c) / src.res[0]))
        row = int(round((src.transform.f - grid.top) / src.res[1]))
        return src.read(band, window=Window(col, row, grid.width, grid.height))


def nearest_valid(iy: np.ndarray, ix: np.ndarray, valid: np.ndarray, max_cells: int = 5):
    """Move (iy, ix) cells without data to the nearest valid cell.

    Returns new (iy, ix) and the move in cells (0 for cells that were valid).
    Cells with no valid neighbour within ``max_cells`` get -1.
    """
    from scipy.spatial import cKDTree
    iy, ix = np.asarray(iy).copy(), np.asarray(ix).copy()
    ok = valid[iy, ix]
    dist = np.zeros(len(iy))
    if (~ok).any():
        vy, vx = np.nonzero(valid)
        tree = cKDTree(np.column_stack([vy, vx]))
        d, j = tree.query(np.column_stack([iy[~ok], ix[~ok]]), distance_upper_bound=max_cells)
        hit = np.isfinite(d)
        sel = np.nonzero(~ok)[0]
        iy[sel[hit]], ix[sel[hit]] = vy[j[hit]], vx[j[hit]]
        iy[sel[~hit]], ix[sel[~hit]] = -1, -1
        dist[sel] = np.where(hit, d, -1)
    return iy, ix, dist


def weather_step(wlat: np.ndarray, wlon: np.ndarray) -> float:
    """Cell size of a regular weather grid in degrees (rounded: 0.1 stays 0.1, not 0.0999...)."""
    d = wlon[1] - wlon[0] if len(wlon) > 1 else wlat[0] - wlat[1]
    return float(round(abs(d), 6))


def _layer_values(path, grid: Grid, win: Window, r, c, x, y) -> np.ndarray:
    """Class of layer ``path`` at the crop pixels (r, c) of window ``win`` of ``grid``.

    A layer on the same grid (CRS, pixel size, aligned origin) and covering the
    window is read as a window. Otherwise each point (x, y, in the grid CRS: the
    crop pixel centres, or their sub-points when ``r`` is None) takes the class of
    the layer pixel under it; 0 outside the layer.
    """
    with rasterio.open(path) as src:
        t = src.transform
        same = r is not None and (src.crs is not None and src.crs == rasterio.crs.CRS.from_user_input(grid.crs)
                and abs(t.a - grid.res) < 1e-9 * grid.res and abs(-t.e - grid.ry) < 1e-9 * grid.ry
                and t.b == 0 and t.d == 0)
        if same:
            col = (grid.left - t.c) / grid.res + win.col_off
            row = (t.f - grid.top) / grid.ry + win.row_off
            same = abs(col - round(col)) < 1e-6 and abs(row - round(row)) < 1e-6
            col, row = int(round(col)), int(round(row))
            same = same and col >= 0 and row >= 0 and col + win.width <= src.width \
                and row + win.height <= src.height
        if same:
            return src.read(1, window=Window(col, row, win.width, win.height))[r, c]
        if src.crs is None:
            raise ValueError(f"{path} has no CRS, so it cannot be matched to the crop mask")
        lx, ly = transformer(grid.crs, src.crs.to_string()).transform(x, y)
        rr, cc = rasterio.transform.rowcol(t, np.asarray(lx), np.asarray(ly))
        rr, cc = np.asarray(rr), np.asarray(cc)
        dtype = np.dtype(src.dtypes[0])
        fill = np.nan if dtype.kind == "f" else 0          # a share layer: NaN = no value
        out = np.full(len(rr), fill, dtype=dtype)
        inside = (rr >= 0) & (rr < src.height) & (cc >= 0) & (cc < src.width)
        if inside.any():
            r0, c0 = rr[inside].min(), cc[inside].min()
            a = src.read(1, window=Window(c0, r0, cc[inside].max() - c0 + 1, rr[inside].max() - r0 + 1))
            out[inside] = a[rr[inside] - r0, cc[inside] - c0]
        if src.nodata is not None and src.nodata != 0 and np.isfinite(src.nodata):
            out[out == src.nodata] = fill
        return out


def crop_pixels(crop_path, layers: dict, wlat: np.ndarray, wlon: np.ndarray,
                grid: Grid, win: Window, class_band: int | None = 2, sub: int = 1) -> pd.DataFrame | None:
    """Crop pixels of one window of ``grid``: area value (``share``), crop class,
    layer classes, pixel row/col in ``grid``, centre x/y (grid CRS) and lon/lat,
    and the weather cell (iy, ix) that holds the centre. None if the window has no crop.

    A pixel counts when band 1 is above 0; NaN and the file's nodata value count as no crop.

    ``sub`` > 1 splits every crop pixel into sub x sub points, each with 1/sub**2 of
    its area: for a crop mask coarser than its class layers (MODIS 500 m over 100 m
    maps), each part then takes the class under it and goes to the weather cell
    under it, instead of the whole pixel taking the class at its centre.
    """
    with rasterio.open(crop_path) as src:
        col0 = int(round((grid.left - src.transform.c) / grid.res))
        row0 = int(round((src.transform.f - grid.top) / grid.ry))
        w = Window(win.col_off + col0, win.row_off + row0, win.width, win.height)
        share = src.read(1, window=w)
        ok = (np.isfinite(share) & (share > 0)) if share.dtype.kind == "f" else share > 0
        if src.nodata is not None and src.nodata != 0 and np.isfinite(src.nodata):
            ok &= share != src.nodata
        r, c = np.nonzero(ok)
        if r.size == 0:
            return None
        d = {"share": share[r, c],
             "rice_class": src.read(class_band, window=w)[r, c] if class_band else np.zeros(r.size, "uint8")}
    rows, cols = r + win.row_off, c + win.col_off
    x = grid.left + (cols + 0.5) * grid.res
    y = grid.top - (rows + 0.5) * grid.ry
    if sub > 1:
        off = (np.arange(sub) + 0.5) / sub - 0.5               # sub-point offsets, in pixels
        dx, dy = (a.ravel() for a in np.meshgrid(off, off))
        n = sub * sub
        x = (x[:, None] + dx[None, :] * grid.res).ravel()
        y = (y[:, None] - dy[None, :] * grid.ry).ravel()
        rows, cols = np.repeat(rows, n), np.repeat(cols, n)
        d = {"share": np.repeat(d["share"].astype("float64") / n, n),
             "rice_class": np.repeat(d["rice_class"], n)}
        r = c = None
    for k, p in layers.items():
        d[k] = _layer_values(p, grid, win, r, c, x, y)
    d["row"], d["col"] = rows, cols
    d["x"], d["y"] = x, y
    lon, lat = transformer(grid.crs, "EPSG:4326").transform(d["x"], d["y"])
    d["lon"], d["lat"] = lon, lat
    step = weather_step(wlat, wlon)
    step_y = -step if wlat[0] > wlat[-1] else step
    d["iy"] = np.rint((lat - wlat[0]) / step_y).astype(np.int32)
    d["ix"] = np.rint((lon - wlon[0]) / step).astype(np.int32)
    px = pd.DataFrame(d)
    inside = (px.iy >= 0) & (px.iy < len(wlat)) & (px.ix >= 0) & (px.ix < len(wlon))
    if not inside.all():
        raise ValueError(f"{(~inside).sum()} crop pixels fall outside the weather grid")
    return px


rice_pixels = crop_pixels          # the name before crop masks other than rice


def pixel_metres(grid: Grid, lat: float | None = None) -> float:
    """Approximate pixel width of a grid in metres: for degrees, times 111,320 m at ``lat``
    (default the grid's mid-latitude)."""
    if pyproj.CRS.from_user_input(grid.crs).is_geographic:
        if lat is None:
            b = grid.bounds
            lat = (b[1] + b[3]) / 2
        return grid.res * 111_320.0 * float(np.cos(np.radians(lat)))
    return float(grid.res)


def _mid_lat(grid: Grid) -> float:
    b = grid.bounds
    lon, lat = transformer(grid.crs, "EPSG:4326").transform((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    return float(lat)


SUB_SPACING_M = 100.0      # sub-points of a coarse crop pixel are about this far apart


def subdivision(crop_grid: Grid, spacing_m: float = SUB_SPACING_M, cap: int = 10) -> int:
    """Sub-points per side of each crop pixel: ceil(crop pixel size / ``spacing_m``), 1 to ``cap``.

    It depends on the crop mask alone, so the strata, the SoilGrids weights and soil maps
    all see the same sub-points and the same weather cells with crop. 1 for a 100 m mask
    (the Java case study), 5 for MODIS 500 m, 10 (the cap) for MapSPAM's 5 arc-minutes.
    """
    k = int(np.ceil(pixel_metres(crop_grid, _mid_lat(crop_grid)) / spacing_m - 1e-3))
    return int(min(max(k, 1), cap))


def aggregate(rice_path, layers: dict, wlat: np.ndarray, wlon: np.ndarray,
              wvalid: np.ndarray, grid: Grid = IDN, tile: int = 4096, units_per_ha: float = 100.0,
              class_band: int | None = 2, weights: dict | None = None, sub: int = 1) -> pd.DataFrame:
    """Crop area per (weather cell, crop class, layer classes).

    ``grid`` is the crop raster's grid or a window of it (``Grid.window``), in any
    CRS. ``layers`` maps a column name to a single-band class GeoTIFF (e.g.
    ``{"soil": "soil.tif", "region": "region.tif"}``), on the same grid or sampled
    at the pixel centres. ``wlat``, ``wlon``, ``wvalid`` describe the weather grid.
    Band 1 of ``rice_path`` is the crop area, ``units_per_ha`` units per hectare
    (100 for the rice share in percent of a 1 ha pixel, 1 for ha per pixel);
    ``class_band`` its crop class (the rice field type), or None. The crop class
    column keeps its old name, ``rice_class``.

    ``weights`` maps a name to a share layer (values 0-1, e.g. the irrigated share
    of each pixel; NaN or negative = no value). Instead of grouping by it, the
    table gets ``<name>_ha`` (crop ha times the share) and ``<name>_known_ha``
    (crop ha where the layer has a value).

    ``sub``: sub-points per side of each crop pixel (``subdivision``), 1 = centres only.
    """
    weights = weights or {}
    keys = ["iy", "ix", "rice_class", *layers]
    wcols = [c for w in weights for c in (f"{w}_ha", f"{w}_known_ha")]
    parts = []
    for win in grid.tiles(tile):
        px = crop_pixels(rice_path, {**layers, **weights}, wlat, wlon, grid, win, class_band, sub)
        if px is None:
            continue
        px["ha"] = px["share"].astype("float64") / units_per_ha
        aggs = dict(ha=("ha", "sum"), pixels=("ha", "size"))
        for w in weights:
            v = px[w].to_numpy("float64")
            ok = np.isfinite(v) & (v >= 0)
            px[f"{w}_ha"] = np.where(ok, px["ha"] * np.clip(np.nan_to_num(v), 0, 1), 0.0)
            px[f"{w}_known_ha"] = np.where(ok, px["ha"], 0.0)
            aggs[f"{w}_ha"] = (f"{w}_ha", "sum")
            aggs[f"{w}_known_ha"] = (f"{w}_known_ha", "sum")
        parts.append(px.groupby(keys, as_index=False).agg(**aggs))
    out = pd.concat(parts, ignore_index=True)
    out = out.groupby(keys, as_index=False).agg(ha=("ha", "sum"), pixels=("pixels", "sum"),
                                                **{c: (c, "sum") for c in wcols})
    out = out.rename(columns={"iy": "iy_pixel", "ix": "ix_pixel"})
    iy, ix, moved = nearest_valid(out.iy_pixel.values, out.ix_pixel.values, wvalid)
    out["iy"], out["ix"], out["moved_cells"] = iy, ix, moved
    out["lat"] = np.where(iy >= 0, wlat[np.clip(iy, 0, None)], np.nan)
    out["lon"] = np.where(ix >= 0, wlon[np.clip(ix, 0, None)], np.nan)
    keys = ["iy", "ix", "lat", "lon", "rice_class", *layers]
    out = (out.groupby(keys, as_index=False, dropna=False)
              .agg(ha=("ha", "sum"), pixels=("pixels", "sum"),
                   moved_cells=("moved_cells", "max"), **{c: (c, "sum") for c in wcols}))
    return out


def fill_nearest(classes: np.ndarray, fill: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Give pixels in ``fill`` that have class 0 the class of the nearest non-zero pixel.

    Distance is Euclidean in pixels (100 m). Returns (filled classes, distance
    in pixels; 0 where the class was already set or the pixel is not in ``fill``).
    """
    from scipy import ndimage
    if not (classes > 0).any():
        raise ValueError("no classified pixels to fill from")
    dist, (iy, ix) = ndimage.distance_transform_edt(classes == 0, return_indices=True)
    todo = fill & (classes == 0)
    out = classes.copy()
    out[todo] = classes[iy[todo], ix[todo]]
    d = np.where(todo, dist, 0.0)
    return out, d


def write_like(path, grid: Grid, bands: list[np.ndarray], dtype, descriptions=(), window_of=IDN):
    """Write arrays covering ``grid`` into a GeoTIFF the size of ``window_of``."""
    profile = dict(driver="GTiff", width=window_of.width, height=window_of.height,
                   count=len(bands), dtype=dtype, crs=CRS, transform=window_of.transform,
                   nodata=0, tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                   predictor=2, BIGTIFF="IF_SAFER")
    col = int(round((grid.left - window_of.left) / RES))
    row = int(round((window_of.top - grid.top) / RES))
    w = Window(col, row, grid.width, grid.height)
    with rasterio.open(path, "w", **profile) as dst:
        for i, (b, d) in enumerate(zip(bands, list(descriptions) + [None] * len(bands)), 1):
            dst.write(b.astype(dtype), i, window=w)
            if d:
                dst.set_band_description(i, d)
    return path

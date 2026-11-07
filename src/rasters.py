"""100 m equal-area rasters of rice fields, soil and regions, and their link to
the AgERA5 weather grid.

Grid
----
All rasters share one grid: ESRI:54034 (World Cylindrical Equal Area,
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


@dataclass(frozen=True)
class Grid:
    """A north-up grid on the equal-area CRS: left/top edge in metres, size in pixels."""
    left: float
    top: float
    width: int
    height: int
    res: float = RES

    @property
    def transform(self) -> Affine:
        return Affine(self.res, 0, self.left, 0, -self.res, self.top)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.left, self.top - self.height * self.res,
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
        """The sub-grid covering a lon/lat box, aligned to this grid, and its window."""
        sub = Grid.from_lonlat(lon_min, lat_min, lon_max, lat_max, self.res)
        col = int(round((sub.left - self.left) / self.res))
        row = int(round((self.top - sub.top) / self.res))
        col0, row0 = max(col, 0), max(row, 0)
        col1 = min(col + sub.width, self.width)
        row1 = min(row + sub.height, self.height)
        win = Window(col0, row0, col1 - col0, row1 - row0)
        g = Grid(self.left + col0 * self.res, self.top - row0 * self.res,
                 col1 - col0, row1 - row0, self.res)
        return g, win

    def tiles(self, size: int = TILE):
        for r in range(0, self.height, size):
            for c in range(0, self.width, size):
                yield Window(c, r, min(size, self.width - c), min(size, self.height - r))

    def centres_lonlat(self, rows: np.ndarray, cols: np.ndarray):
        x = self.left + (cols + 0.5) * self.res
        y = self.top - (rows + 0.5) * self.res
        return _to_ll.transform(x, y)


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


def rice_pixels(rice_path, layers: dict, wlat: np.ndarray, wlon: np.ndarray,
                grid: Grid, win: Window) -> pd.DataFrame | None:
    """Rice pixels of one window of ``grid``: share, rice class, layer classes,
    pixel row/col in ``grid``, centre x/y (grid CRS) and lon/lat, and the
    weather cell (iy, ix) that holds the centre. None if the window has no rice.
    """
    with rasterio.open(rice_path) as rice:
        col0 = int(round((grid.left - rice.transform.c) / RES))
        row0 = int(round((rice.transform.f - grid.top) / RES))
        w = Window(win.col_off + col0, win.row_off + row0, win.width, win.height)
        share = rice.read(1, window=w)
        r, c = np.nonzero(share)
        if r.size == 0:
            return None
        d = {"share": share[r, c], "rice_class": rice.read(2, window=w)[r, c]}
    for k, p in layers.items():
        with rasterio.open(p) as src:
            d[k] = src.read(1, window=w)[r, c]
    rows, cols = r + win.row_off, c + win.col_off
    d["row"], d["col"] = rows, cols
    d["x"] = grid.left + (cols + 0.5) * grid.res
    d["y"] = grid.top - (rows + 0.5) * grid.res
    lon, lat = _to_ll.transform(d["x"], d["y"])
    d["lon"], d["lat"] = lon, lat
    step_y = -0.1 if wlat[0] > wlat[-1] else 0.1
    d["iy"] = np.rint((lat - wlat[0]) / step_y).astype(np.int32)
    d["ix"] = np.rint((lon - wlon[0]) / 0.1).astype(np.int32)
    px = pd.DataFrame(d)
    inside = (px.iy >= 0) & (px.iy < len(wlat)) & (px.ix >= 0) & (px.ix < len(wlon))
    if not inside.all():
        raise ValueError(f"{(~inside).sum()} rice pixels fall outside the weather grid")
    return px


def aggregate(rice_path, layers: dict, wlat: np.ndarray, wlon: np.ndarray,
              wvalid: np.ndarray, grid: Grid = IDN, tile: int = 4096) -> pd.DataFrame:
    """Rice area per (weather cell, rice class, layer classes).

    ``layers`` maps a column name to a single-band class GeoTIFF on the same
    grid (e.g. ``{"soil": "soil.tif", "region": "region.tif"}``). ``wlat``,
    ``wlon``, ``wvalid`` describe the weather grid (``agera5.grid``).
    """
    keys = ["iy", "ix", "rice_class", *layers]
    parts = []
    for win in grid.tiles(tile):
        px = rice_pixels(rice_path, layers, wlat, wlon, grid, win)
        if px is None:
            continue
        px["ha"] = px["share"].astype("float64") / 100.0
        parts.append(px.groupby(keys, as_index=False).agg(ha=("ha", "sum"), pixels=("ha", "size")))
    out = pd.concat(parts, ignore_index=True)
    out = out.groupby(keys, as_index=False).agg(ha=("ha", "sum"), pixels=("pixels", "sum"))
    out = out.rename(columns={"iy": "iy_pixel", "ix": "ix_pixel"})
    iy, ix, moved = nearest_valid(out.iy_pixel.values, out.ix_pixel.values, wvalid)
    out["iy"], out["ix"], out["moved_cells"] = iy, ix, moved
    out["lat"] = np.where(iy >= 0, wlat[np.clip(iy, 0, None)], np.nan)
    out["lon"] = np.where(ix >= 0, wlon[np.clip(ix, 0, None)], np.nan)
    keys = ["iy", "ix", "lat", "lon", "rice_class", *layers]
    out = (out.groupby(keys, as_index=False, dropna=False)
              .agg(ha=("ha", "sum"), pixels=("pixels", "sum"),
                   moved_cells=("moved_cells", "max")))
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

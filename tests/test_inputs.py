"""Tests for the gridded input layer: AgERA5 cache and 100 m rasters.

These use small synthetic files, so they run without the real archives.
"""
import netCDF4
import numpy as np
import pandas as pd
import pyproj
import pytest
import shapely

import agera5
import config
import rasters


def _fake_source(root, var, year, lat, lon, fill_day=None):
    """A tiny file shaped like an AgERA5 global yearly file."""
    v = agera5.VARIABLES[var]
    d = root / v.folder
    d.mkdir(parents=True, exist_ok=True)
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    with netCDF4.Dataset(d / f"agera5_{v.folder}_{year}.nc", "w") as f:
        f.createDimension("time", len(days))
        f.createDimension("lat", len(lat))
        f.createDimension("lon", len(lon))
        t = f.createVariable("time", "i8", ("time",))
        t.units, t.calendar = "days since 1900-01-01", "proleptic_gregorian"
        t[:] = (days - pd.Timestamp("1900-01-01")).days.values
        f.createVariable("lat", "f8", ("lat",))[:] = lat
        f.createVariable("lon", "f8", ("lon",))[:] = lon
        x = f.createVariable(v.source, "f4", ("time", "lat", "lon"), fill_value=-9999.0)
        rng = np.random.default_rng(year)
        base = {"SRAD": 18e6, "TMAX": 301.0, "TMIN": 296.0, "RAIN": 6.0,
                "DEWP": 295.0, "WIND": 2.0}[var]
        vals = base * (1 + 0.1 * rng.standard_normal((len(days), len(lat), len(lon))))
        vals[:, 1, 1] = -9999.0          # a sea cell (first cell inside the region)
        x[:] = vals.astype("f4")
    return vals


@pytest.fixture
def fake_region(tmp_path, monkeypatch):
    lat = np.round(np.arange(7.0, -12.05, -0.1), 4)[::40]      # coarse, descending
    lon = np.round(np.arange(94.0, 142.05, 0.1), 4)[::60]
    monkeypatch.setitem(agera5.REGIONS, "t", (-11.5, 6.5, 94.5, 141.5))
    src = tmp_path / "src"
    raw = {(v, y): _fake_source(src, v, y, lat, lon) for v in ("SRAD", "TMAX") for y in (2000, 2001)}
    return src, tmp_path / "cache", lat, lon, raw


def test_extract_converts_and_packs_to_one_decimal(fake_region):
    src, cache, lat, lon, raw = fake_region
    p = agera5.extract_year("t", "TMAX", 2000, cache_dir=cache, source_root=src)
    with netCDF4.Dataset(p) as f:
        got = f["TMAX"][:].filled(np.nan)
        glat = f["lat"][:]
    keep = (lat >= -11.5) & (lat <= 6.5)
    assert np.allclose(glat, lat[keep])
    want = raw[("TMAX", 2000)][:, keep][:, :, (lon >= 94.5) & (lon <= 141.5)] - 273.15
    want[want < -9000] = np.nan
    assert np.array_equal(np.isnan(got), np.isnan(want))
    m = ~np.isnan(want)
    assert np.abs(got[m] - want[m]).max() <= 0.05 + 1e-4


def test_series_spans_years_and_cell_frame(fake_region):
    src, cache, *_ = fake_region
    for y in (2000, 2001):
        for v in ("SRAD", "TMAX"):
            agera5.extract_year("t", v, y, cache_dir=cache, source_root=src)
    lat, lon, valid = agera5.grid("t", cache_dir=cache)
    assert not valid[0, 0] and valid[1, 1]
    dates, vals = agera5.series([1, 2], [1, 3], "2000-12-30", "2001-01-02", region="t",
                                variables=("SRAD", "TMAX"), cache_dir=cache)
    assert list(dates) == list(pd.date_range("2000-12-30", "2001-01-02"))
    assert vals["SRAD"].shape == (4, 2)
    df = agera5.cell_frame(dates, vals, 1)
    assert list(df.columns) == ["SRAD", "TMAX"]
    assert 10 < df.SRAD.mean() < 30 and 20 < df.TMAX.mean() < 35


def test_cell_index_round_trip():
    lat = np.round(np.arange(6.5, -11.55, -0.1), 4)
    lon = np.round(np.arange(94.5, 141.55, 0.1), 4)
    iy, ix = agera5.cell_index(lat, lon, [-6.62, 5.0], [106.81, 141.5])
    assert np.allclose(lat[iy], [-6.6, 5.0]) and np.allclose(lon[ix], [106.8, 141.5])


def test_grid_pixel_is_one_hectare():
    g = rasters.IDN
    geod = pyproj.Geod(ellps="WGS84")
    for row, col in [(0, 0), (g.height // 2, g.width // 3), (g.height - 1, g.width - 1)]:
        x0 = g.left + col * g.res
        y0 = g.top - row * g.res
        xs, ys = [x0, x0 + 100, x0 + 100, x0], [y0, y0, y0 - 100, y0 - 100]
        lon, lat = rasters._to_ll.transform(xs, ys)
        area, _ = geod.polygon_area_perimeter(lon, lat)
        assert abs(abs(area) - 1e4) < 0.01


def test_window_is_aligned_with_national_grid():
    sub, win = rasters.IDN.window(*config.DEFAULTS["aoi"]["bbox"])
    assert (sub.left - rasters.IDN.left) % 100 == 0
    assert (rasters.IDN.top - sub.top) % 100 == 0
    assert win.col_off == (sub.left - rasters.IDN.left) / 100


def test_burn_share_sums_to_polygon_area(tmp_path):
    import geopandas as gpd
    g = rasters.Grid(0.0, 2000.0, 20, 20)
    # 2.4 ha rectangle cutting across pixels (edges on the 20 m sub-grid, so the
    # count is exact), a 1 ha square on a pixel, and a 0.4 ha class-2 strip
    polys = [shapely.box(120, 1000, 360, 1100), shapely.box(1000, 1000, 1100, 1100),
             shapely.box(500, 500, 540, 600)]
    gdf = gpd.GeoDataFrame({"code": [1, 1, 2]}, geometry=polys, crs=rasters.CRS)
    p = rasters.burn(gdf, "code", tmp_path / "r.tif", g, factor=5, workers=2, log=lambda m: None)
    share = rasters.read(p, band=1).astype(float)
    cls = rasters.read(p, band=2)
    assert share.sum() / 100 == pytest.approx(2.4 + 1.0 + 0.4, abs=1e-9)
    assert share[9, 10] == 100 and cls[9, 10] == 1
    assert cls[14, 5] == 2


def test_nearest_valid_moves_only_invalid_cells():
    valid = np.zeros((5, 5), bool)
    valid[2, 2] = valid[0, 4] = True
    iy, ix, d = rasters.nearest_valid(np.array([2, 2, 4]), np.array([2, 3, 0]), valid, max_cells=2)
    assert (iy[:2] == 2).all() and (ix[:2] == 2).all() and d[0] == 0 and d[1] == 1
    assert iy[2] == -1 and d[2] == -1


def test_tiny_polygon_is_kept_by_all_touched(tmp_path):
    import geopandas as gpd
    g = rasters.Grid(0.0, 1000.0, 10, 10)
    # 4 m x 4 m field inside pixel (row 2, col 3), away from every 20 m sub-pixel centre
    tiny = shapely.box(301, 781, 305, 785)
    gdf = gpd.GeoDataFrame({"code": [3]}, geometry=[tiny], crs=rasters.CRS)
    p = rasters.burn(gdf, "code", tmp_path / "t.tif", g, factor=5, workers=1, log=lambda m: None)
    share, cls = rasters.read(p, band=1), rasters.read(p, band=2)
    assert share[2, 3] == 1 and cls[2, 3] == 3 and share.sum() == 1
    # categorical layer: a thin strip touching a column of pixels without covering centres
    strip = shapely.box(510, 0, 512, 1000)
    gdf = gpd.GeoDataFrame({"code": [7]}, geometry=[strip], crs=rasters.CRS)
    p = rasters.burn(gdf, "code", tmp_path / "c.tif", g, workers=1, log=lambda m: None)
    assert (rasters.read(p)[:, 5] == 7).all()


def test_fill_nearest_only_fills_requested_gaps():
    cls = np.zeros((5, 6), np.uint8)
    cls[0, 0], cls[4, 5] = 1, 4
    fill = np.zeros_like(cls, bool)
    fill[1, 1] = fill[3, 5] = fill[0, 0] = True        # two gaps and one already classed
    out, d = rasters.fill_nearest(cls, fill)
    assert out[1, 1] == 1 and out[3, 5] == 4 and out[0, 0] == 1
    assert d[1, 1] == pytest.approx(np.sqrt(2)) and d[3, 5] == 1 and d[0, 0] == 0
    assert out[2, 2] == 0                                 # not requested: left empty

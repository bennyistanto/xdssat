"""Tests for crop masks from any source, grid and CRS (src/cropprep.py, rasters.crop_pixels).

Synthetic maps only: a land-cover map in degrees split into two tiles, crop
shares and areas in UTM, a polygon field map, and class layers on another grid
than the crop mask. Pixel areas are checked against pyproj.Geod.
"""
import numpy as np
import pandas as pd
import pyproj
import pytest
import rasterio
import yaml
from rasterio.transform import Affine

import config
import cropprep
import inputs
import rasters

GEOD = pyproj.Geod(ellps="WGS84")
BOX = [107.0, -7.0, 107.5, -6.5]


def _geod_area_ha(lon0, lat0, dlon, dlat):
    lons = [lon0, lon0 + dlon, lon0 + dlon, lon0]
    lats = [lat0, lat0, lat0 - dlat, lat0 - dlat]
    return abs(GEOD.polygon_area_perimeter(lons, lats)[0]) / 1e4


def _tif(path, data, transform, crs, nodata=None, dtype=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = data if data.ndim == 3 else data[None]
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[-1], height=data.shape[-2],
                       count=data.shape[0], dtype=dtype or data.dtype, crs=crs, transform=transform,
                       nodata=nodata) as r:
        r.write(data)
    return path


def _exp(tmp_path, crop, **extra):
    d = {"paths": {"cache": str(tmp_path / "cache"), "inputs": str(tmp_path / "src")},
         "aoi": {"name": "t", "bbox": BOX, "region": "Jawa"}, "years": [2015, 2015],
         "prepare": {"crop_mask": crop}, **extra}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return config.load(p, env={})


def _ha(tmp_path, name):
    with rasterio.open(tmp_path / "cache" / "inputs" / "crop_mask" / name / "crop_area.tif") as r:
        return r.read(1).astype("float64"), r.transform, r.crs


# ---------------------------------------------------------------- pixel areas
@pytest.mark.parametrize("lat, step", [(-6.5, 1 / 12000), (60.0, 0.1), (0.05, 0.1)])
def test_geographic_pixel_area_is_exact_on_the_ellipsoid(lat, step):
    t = Affine(step, 0, 107.0, 0, -step, lat)
    got = cropprep.row_area_ha(t, np.array([0]))[0]
    assert got == pytest.approx(_geod_area_ha(107.0, lat, step, step), rel=1e-6)


def test_projected_pixel_area():
    # equal-area grid: 100 m pixels are exactly 1 ha
    t = Affine(100, 0, 11_900_000, 0, -100, -660_000)
    assert cropprep.projected_area_ha(rasters.CRS, t, np.array([0, 5]), np.array([0, 9])) == pytest.approx(1.0, abs=1e-9)
    # UTM 48S, 300 km from the central meridian: the map area overstates the true area
    crs = "EPSG:32748"
    x0, y0 = 200_000.0, 9_250_000.0
    a = cropprep.projected_area_ha(crs, Affine(100, 0, x0, 0, -100, y0), np.array([0]), np.array([0]))[0]
    to_ll = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    xs, ys = [x0, x0 + 100, x0 + 100, x0], [y0, y0, y0 - 100, y0 - 100]
    lon, lat = to_ll.transform(xs, ys)
    true = abs(GEOD.polygon_area_perimeter(lon, lat)[0]) / 1e4
    assert a == pytest.approx(true, rel=1e-5) and abs(a - 1.0) > 1e-4


# ---------------------------------------------------------------- land cover
def _landcover_tiles(src, step=1 / 1200):
    """A 0.6 x 0.6 degree land-cover map in two tiles split at a column that is not a multiple of 10."""
    n = int(round(0.6 / step))
    rng = np.random.default_rng(1)
    lc = rng.choice(np.array([10, 40, 50, 80], "uint8"), size=(n, n), p=[0.3, 0.4, 0.2, 0.1])
    lc[:5, :5] = 0                                    # nodata corner
    split = 333
    t = Affine(step, 0, 106.95, 0, -step, -6.45)
    _tif(src / "lc_a.tif", lc[:, :split], t, "EPSG:4326", nodata=0)
    _tif(src / "lc_b.tif", lc[:, split:], Affine(step, 0, 106.95 + split * step, 0, -step, -6.45), "EPSG:4326",
         nodata=0)
    return lc, t


def test_landcover_classes_become_ha_per_pixel(tmp_path):
    lc, t = _landcover_tiles(tmp_path / "src")
    exp = _exp(tmp_path, {"name": "lc", "files": "{inputs}/lc_*.tif", "values": "classes", "classes": [40],
                          "aggregate": 10, "box": [106.95, -7.05, 107.55, -6.45]})
    m = cropprep.prepare(exp, log=lambda s: None)
    ha, ot, crs = _ha(tmp_path, "lc")
    assert crs.to_string() == "EPSG:4326" and ot.a == pytest.approx(10 * t.a) and ha.shape == (72, 72)
    # expected: every class-40 source pixel's exact area, summed
    area = cropprep.row_area_ha(t, np.arange(lc.shape[0]))[:, None]
    want = ((lc == 40) * area).reshape(72, 10, 72, 10).sum(axis=(1, 3))
    assert np.allclose(ha, want, rtol=1e-6)
    meta = inputs.open_dataset("crop_mask", "lc", exp.path("cache")).meta
    assert meta["units_per_ha"] == 1 and meta["class_band"] is None
    assert meta["total_ha"] == pytest.approx(want.sum(), rel=1e-6)
    assert m.name == "input.json"
    # the box cuts the map: only the pixels inside count
    exp = _exp(tmp_path, {"name": "lc2", "files": "{inputs}/lc_*.tif", "values": "classes", "classes": [40],
                          "aggregate": 10, "box": [107.0, -6.7, 107.2, -6.5]})
    cropprep.prepare(exp, log=lambda s: None)
    ha2, t2, _ = _ha(tmp_path, "lc2")
    assert ha2.shape == (24, 24) and (t2.c, t2.f) == pytest.approx((107.0, -6.5))
    assert np.allclose(ha2, want[6:30, 6:30], rtol=1e-6)


# ---------------------------------------------------------------- shares and areas
def test_fraction_percent_area_and_nodata(tmp_path):
    crs = "EPSG:32748"
    t = Affine(250, 0, 730_000, 0, -250, 9_270_000)           # about 107.1E 6.6S, 225 km east of 105E
    f = np.full((40, 40), 0.5, "float32")
    f[0, 0] = -1.0                                    # nodata
    f[1, 1] = np.nan
    _tif(tmp_path / "src" / "frac.tif", f, t, crs, nodata=-1.0)
    _tif(tmp_path / "src" / "pct.tif", (f * 100).astype("float32"), t, crs, nodata=-100.0)
    _tif(tmp_path / "src" / "area.tif", np.where(np.isfinite(f) & (f > 0), f * 6.25, -1).astype("float32"), t,
         crs, nodata=-1.0)
    box = [107.0, -7.0, 107.5, -6.5]
    totals = {}
    for v, fname in (("fraction", "frac"), ("percent", "pct"), ("ha", "area")):
        cropprep.prepare(_exp(tmp_path, {"name": v, "files": f"{{inputs}}/{fname}.tif", "values": v,
                                         "box": box}), log=lambda s: None)
        ha, ot, _ = _ha(tmp_path, v)
        assert ha[0, 0] == 0 and ha[1, 1] == 0
        totals[v] = ha.sum()
    # 1598 pixels of half 6.25 ha (UTM scale makes fraction/percent a little smaller than the map area)
    assert totals["ha"] == pytest.approx(1598 * 3.125, rel=1e-6)
    assert totals["fraction"] == pytest.approx(totals["percent"], rel=1e-6)
    assert totals["fraction"] == pytest.approx(totals["ha"], rel=2e-3)
    # wrong values setting: a plain message
    with pytest.raises(config.ConfigError, match="is it percent"):
        cropprep.prepare(_exp(tmp_path, {"name": "bad", "files": "{inputs}/pct.tif", "values": "fraction",
                                         "box": box}), log=lambda s: None)


def test_split_spreads_a_coarse_pixel_evenly(tmp_path):
    t = Affine(0.25, 0, 107.0, 0, -0.25, -6.5)
    _tif(tmp_path / "src" / "spam.tif", np.array([[100.0, 0.0], [40.0, 8.0]], "float32"), t, "EPSG:4326")
    cropprep.prepare(_exp(tmp_path, {"name": "s", "files": "{inputs}/spam.tif", "values": "ha", "split": 5,
                                     "box": [107.0, -7.0, 107.5, -6.5]}), log=lambda s: None)
    ha, ot, _ = _ha(tmp_path, "s")
    assert ha.shape == (10, 10) and ot.a == pytest.approx(0.05)
    assert ha.sum() == pytest.approx(148.0) and ha[0, 0] == pytest.approx(4.0)


# ---------------------------------------------------------------- polygons
def test_polygons_are_burned_to_ha(tmp_path):
    import geopandas as gpd
    import shapely
    to_ea = pyproj.Transformer.from_crs("EPSG:4326", rasters.CRS, always_xy=True)
    x, y = to_ea.transform(107.2, -6.8)
    # edges 40 m into a pixel: between 20 m sub-pixel centres (an edge on a centre counts it)
    x, y = round(x / 100) * 100 + 40, round(y / 100) * 100 + 40
    fields = gpd.GeoDataFrame({"use": ["sawah", "sawah", "kebun"],
                               "type": ["irigasi", "tadah hujan", "x"]},
                              geometry=[shapely.box(x, y, x + 1000, y + 500),        # 50 ha
                                        shapely.box(x + 2000, y, x + 2200, y + 100),  # 2 ha
                                        shapely.box(x - 3000, y, x - 2000, y + 1000)],  # 100 ha, not selected
                              crs=rasters.CRS).to_crs("EPSG:4326")
    (tmp_path / "src").mkdir()
    fields.to_file(tmp_path / "src" / "fields.gpkg")
    exp = _exp(tmp_path, {"name": "p", "files": "{inputs}/fields.gpkg", "values": "polygons",
                          "select": {"use": ["sawah"]}, "class_column": "type"})
    cropprep.prepare(exp, workers=1, log=lambda s: None)
    c = inputs.open_dataset("crop_mask", "p", exp.path("cache"))
    assert c.meta["units_per_ha"] == 100 and c.class_band == 2
    assert c.meta["total_ha"] == pytest.approx(52.0, rel=0.005)
    assert pd.read_csv(c.file("classes")).name.tolist() == ["irigasi", "tadah hujan"]


# ---------------------------------------------------------------- runner side
def test_crop_mask_in_degrees_with_class_layers_on_another_grid(tmp_path):
    """The runner reads a crop mask in EPSG:4326 and samples equal-area class layers at its pixels."""
    step = 0.01
    t = Affine(step, 0, 107.0, 0, -step, -6.5)
    crop = np.full((50, 50), 2.0, "float32")                      # 2 ha in every 0.01 degree pixel
    p = _tif(tmp_path / "crop.tif", crop, t, "EPSG:4326")
    # region layer: equal-area 100 m, west half 1, east half 2
    g = rasters.Grid.from_lonlat(106.9, -7.1, 107.6, -6.4)
    lon, _ = g.centres_lonlat(np.zeros(g.width), np.arange(g.width))
    reg = np.broadcast_to(np.where(np.asarray(lon) < 107.25, 1, 2).astype("uint8"), (g.height, g.width))
    rp = _tif(tmp_path / "region.tif", np.ascontiguousarray(reg), g.transform, rasters.CRS)
    wlat, wlon = np.array([-6.55, -6.65, -6.75, -6.85, -6.95]), np.array([107.05, 107.15, 107.25, 107.35, 107.45])
    grid = rasters.Grid(107.0, -6.5, 50, 50, step, "EPSG:4326")
    sub, win = grid.window(107.0, -6.8, 107.5, -6.5)
    assert (sub.width, sub.height, win.row_off) == (50, 30, 0)
    out = rasters.aggregate(p, {"region": rp}, wlat, wlon, np.ones((5, 5), bool), grid=grid, tile=16,
                            units_per_ha=1.0, class_band=None)
    assert out.ha.sum() == pytest.approx(2.0 * 2500)
    by = out.groupby("region").ha.sum()
    assert by[1] == pytest.approx(2.0 * 50 * 25) and by[2] == pytest.approx(2.0 * 50 * 25)
    assert set(out.ix) == {0, 1, 2, 3, 4} and (out.groupby(["iy", "ix"]).pixels.sum() == 100).all()


def test_checks_accept_any_crs_and_name_coverage_and_soil(tmp_path):
    cache = tmp_path / "cache"
    from test_input_datasets import _exp as dexp, _layers, _weather
    _weather(cache, "w_x")
    _layers(cache)
    # a crop mask in degrees over the area passes
    _tif(cache / "r" / "deg.tif", np.ones((60, 110), "float32"), Affine(0.1, 0, 104.0, 0, -0.1, -4.0), "EPSG:4326")
    inputs.write_manifest("crop_mask", "deg", {"file": "r/deg.tif", "units_per_ha": 1}, cache)
    # with a soil made for it (0.1 degree pixels are read as 10 x 10 sub-points)
    inputs.write_manifest("soil", "soil_deg", {"file": "s/x.SOL", "weather": "w_x", "crop_mask": "deg",
                                               "sub_points": 10}, cache)
    assert inputs.check(dexp(tmp_path, cache, crop_mask="deg", soil="soil_deg")) == []
    # one that misses the area
    _tif(cache / "r" / "far.tif", np.ones((10, 10), "float32"), Affine(0.1, 0, 10.0, 0, -0.1, 50.0), "EPSG:4326")
    inputs.write_manifest("crop_mask", "far", {"file": "r/far.tif", "units_per_ha": 1}, cache)
    msg = "\n".join(inputs.check(dexp(tmp_path, cache, crop_mask="far")))
    assert "does not cover the area" in msg
    # soil made for another crop mask
    inputs.write_manifest("soil", "soil_c", {"file": "s/x.SOL", "weather": "w_x", "crop_mask": "crop_x"}, cache)
    msg = "\n".join(inputs.check(dexp(tmp_path, cache, crop_mask="deg", soil="soil_c")))
    assert "made for the crop mask 'crop_x'" in msg and "prepare_soils.py" in msg


def test_prepare_settings_are_checked_in_plain_words(tmp_path):
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, {"name": "x", "files": "a.tif", "values": "classes"})
    assert "needs the list of crop class codes" in str(e.value)
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, {"name": "x", "files": "a.tif", "values": "share", "aggregate": 4, "split": 2,
                        "pixel_m": 50})
    msg = str(e.value)
    assert "'share' should be one of" in msg and "not both" in msg and "only with values: polygons" in msg
    exp = _exp(tmp_path, {"name": "x", "files": "{inputs}/none_*.tif", "values": "ha"})
    with pytest.raises(config.ConfigError, match="no file matches"):
        cropprep.prepare(exp, log=lambda s: None)


def test_inspect_describes_rasters_and_vectors(tmp_path):
    lc, _ = _landcover_tiles(tmp_path / "src")
    txt = cropprep.inspect(tmp_path / "src" / "lc_a.tif")
    assert "EPSG:4326" in txt and "40 (" in txt and "likely classes" in txt and "about 92 m" in txt

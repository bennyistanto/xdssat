"""Tests for optional water regime, region and zone inputs (phase 5).

Strata without a region or water regime map, with a default regime for crop off
the map, with an irrigated-share map, and the preparation of class and share
layers from rasters and polygons (src/layerprep.py). Synthetic data only.
"""
import numpy as np
import pandas as pd
import pytest
import rasterio
import yaml
from rasterio.transform import Affine

import config
import inputs
import layerprep
import management as mg
import rasters
from test_input_datasets import _layers, _weather

REGIME = {1: "irrigated", 2: "irrigated", 3: "irrigated", 4: "rainfed"}


def _stats(**cols):
    base = {"iy": [0, 0, 1], "ix": [0, 1, 1], "lat": [-6.0, -6.0, -6.1], "lon": [107.0, 107.1, 107.1],
            "crop_class": [0, 0, 0], "ha": [10.0, 20.0, 30.0]}
    return pd.DataFrame({**base, **cols})


# ---------------------------------------------------------------- strata
def test_class_map_with_crop_off_the_map():
    st = _stats(irr=[1, 0, 4])
    with pytest.raises(ValueError, match="default_regime"):
        mg.strata(st, None, zone_col=None, regime=REGIME)
    out = mg.strata(st, None, zone_col=None, regime=REGIME, default_regime="rainfed")
    assert out.set_index(["iy", "ix"]).water.to_dict() == {(0, 0): "irrigated", (0, 1): "rainfed", (1, 1): "rainfed"}
    assert out.ha.sum() == pytest.approx(60.0)


def test_no_water_regime_map_needs_a_default():
    with pytest.raises(ValueError, match="inputs > water_regime is null"):
        mg.strata(_stats(), None, zone_col=None)
    out = mg.strata(_stats(), None, zone_col=None, default_regime="rainfed")
    assert set(out.water) == {"rainfed"} and out.ha.sum() == pytest.approx(60.0) and len(out) == 3


def test_share_map_splits_the_area():
    # cell (0,0): 40% irrigated; (0,1): 10 of its 20 ha have no value; (1,1): all irrigated
    st = _stats(irr_ha=[4.0, 5.0, 30.0], irr_known_ha=[10.0, 10.0, 30.0])
    with pytest.raises(ValueError, match="has no value"):
        mg.strata(st, None, zone_col=None)
    out = mg.strata(st, None, zone_col=None, default_regime="rainfed",
                    share_regimes={"share": "irrigated", "rest": "rainfed"})
    ha = out.set_index(["iy", "ix", "water"]).ha
    assert ha[(0, 0, "irrigated")] == pytest.approx(4.0) and ha[(0, 0, "rainfed")] == pytest.approx(6.0)
    assert ha[(0, 1, "irrigated")] == pytest.approx(5.0) and ha[(0, 1, "rainfed")] == pytest.approx(15.0)
    assert (1, 1, "rainfed") not in ha.index and ha.sum() == pytest.approx(60.0)


def test_region_filter_is_optional():
    st = _stats(region=[1, 2, 1], irr=[1, 1, 4])
    assert len(mg.strata(st, {"A": 1, "B": 2}, "A", zone_col=None, regime=REGIME)) == 2
    assert len(mg.strata(st, None, zone_col=None, regime=REGIME)) == 3


# ---------------------------------------------------------------- share layer in aggregate
def test_aggregate_weights_a_share_layer_on_another_grid(tmp_path):
    crop = tmp_path / "crop.tif"
    with rasterio.open(crop, "w", driver="GTiff", width=20, height=10, count=1, dtype="float32",
                       crs="EPSG:4326", transform=Affine(0.01, 0, 107.0, 0, -0.01, -6.0)) as r:
        r.write(np.full((1, 10, 20), 2.0, "float32"))
    share = np.full((5, 10), 0.25, "float32")
    share[:, 5:] = np.nan                                         # east half: no value
    sp = tmp_path / "share.tif"
    with rasterio.open(sp, "w", driver="GTiff", width=10, height=5, count=1, dtype="float32",
                       crs="EPSG:4326", transform=Affine(0.02, 0, 107.0, 0, -0.02, -6.0), nodata=np.nan) as r:
        r.write(share[None])
    grid = rasters.Grid(107.0, -6.0, 20, 10, 0.01, "EPSG:4326")
    wlat, wlon = np.array([-6.05]), np.array([107.05, 107.15])
    out = rasters.aggregate(crop, {}, wlat, wlon, np.ones((1, 2), bool), grid=grid, units_per_ha=1.0,
                            class_band=None, weights={"irr": sp})
    c = out.set_index("ix")
    assert c.loc[0, "ha"] == pytest.approx(200.0) and c.loc[0, "irr_known_ha"] == pytest.approx(200.0)
    assert c.loc[0, "irr_ha"] == pytest.approx(50.0)
    assert c.loc[1, "irr_known_ha"] == pytest.approx(0.0) and c.loc[1, "irr_ha"] == pytest.approx(0.0)


# ---------------------------------------------------------------- preparation
def _exp(tmp_path, prepare, inputs_=None, **extra):
    d = {"paths": {"cache": str(tmp_path / "cache"), "inputs": str(tmp_path / "src")},
         "aoi": {"name": "t", "bbox": [107.0, -6.4, 107.5, -6.0], "region": None},
         "inputs": {"region": None, **(inputs_ or {})}, "years": [2015, 2015], "prepare": prepare, **extra}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return config.load(p, env={})


def _tif(path, arr, t, crs="EPSG:4326", nodata=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=arr.dtype, crs=crs, transform=t, nodata=nodata) as r:
        r.write(arr[None])


def test_share_from_irrigated_and_total_area(tmp_path):
    t = Affine(1 / 12, 0, 106.5, 0, -1 / 12, -5.5)              # MapSPAM-like 5 arc-minute grid
    tot = np.full((24, 24), 100.0, "float32")
    tot[0, 0] = 0.0                                               # no crop: no value
    irr = np.full((24, 24), 30.0, "float32")
    irr[1, 1] = -1.0                                              # nodata in irrigated: counts as 0
    _tif(tmp_path / "src" / "tot.tif", tot, t, nodata=-1.0)
    _tif(tmp_path / "src" / "irr.tif", irr, t, nodata=-1.0)
    exp = _exp(tmp_path, {"water_regime": {"name": "spam_share", "values": "ratio",
                                           "irrigated": "{inputs}/irr.tif", "total": "{inputs}/tot.tif",
                                           "box": [106.5, -7.5, 108.5, -5.5]}})
    layerprep.prepare(exp, "water_regime", log=lambda s: None)
    d = inputs.open_dataset("water_regime", "spam_share", exp.path("cache"))
    assert d.is_share
    with rasterio.open(d.file()) as r:
        a = r.read(1)
    assert np.isnan(a[0, 0]) and a[1, 1] == 0.0 and a[5, 5] == pytest.approx(0.3)


def test_share_values_are_checked(tmp_path):
    t = Affine(0.1, 0, 107.0, 0, -0.1, -6.0)
    _tif(tmp_path / "src" / "pct.tif", np.full((4, 5), 60.0, "float32"), t)
    exp = _exp(tmp_path, {"water_regime": {"name": "w", "values": "fraction", "files": "{inputs}/pct.tif"}})
    with pytest.raises(config.ConfigError, match="is it percent"):
        layerprep.prepare(exp, "water_regime", log=lambda s: None)
    exp = _exp(tmp_path, {"water_regime": {"name": "w", "values": "percent", "files": "{inputs}/pct.tif"}})
    layerprep.prepare(exp, "water_regime", log=lambda s: None)
    with rasterio.open(inputs.open_dataset("water_regime", "w", exp.path("cache")).file()) as r:
        assert np.allclose(r.read(1), 0.6)


def test_class_maps_from_raster_and_polygons(tmp_path):
    import geopandas as gpd
    import shapely
    t = Affine(0.01, 0, 106.9, 0, -0.01, -5.9)
    codes = np.zeros((60, 70), "uint8")
    codes[:, :35], codes[:, 35:] = 1, 4
    _tif(tmp_path / "src" / "irr.tif", codes, t)
    pd.DataFrame({"code": [1, 4], "name": ["technical", "rainfed"]}).to_csv(tmp_path / "src" / "irr.csv", index=False)
    gpd.GeoDataFrame({"PROV": ["Lampung", "Banten"]},
                     geometry=[shapely.box(106.9, -6.5, 107.2, -5.9), shapely.box(107.2, -6.5, 107.6, -5.9)],
                     crs="EPSG:4326").to_file(tmp_path / "src" / "prov.gpkg")
    exp = _exp(tmp_path, {"water_regime": {"name": "irr", "files": "{inputs}/irr.tif", "classes": "{inputs}/irr.csv"},
                          "region": {"name": "prov", "files": "{inputs}/prov.gpkg", "class_column": "PROV",
                                     "pixel_m": 500}})
    layerprep.prepare(exp, "water_regime", log=lambda s: None)
    layerprep.prepare(exp, "region", workers=1, log=lambda s: None)
    c = exp.path("cache")
    w = inputs.open_dataset("water_regime", "irr", c)
    assert not w.is_share and w.codes() == {"technical": 1, "rainfed": 4}
    reg = inputs.open_dataset("region", "prov", c)
    assert reg.codes() == {"Banten": 1, "Lampung": 2}
    with rasterio.open(reg.file()) as r:
        a = r.read(1)
        assert r.crs.to_string() == rasters.CRS and set(np.unique(a)) <= {0, 1, 2} and (a == 1).any() and (a == 2).any()


def test_settings_in_plain_words(tmp_path):
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, {"water_regime": {"name": "w", "values": "share", "files": "a.tif"},
                        "zones": {"name": "z", "values": "percent", "files": "z.tif"}})
    msg = str(e.value)
    assert "'share' should be classes or fraction or percent or ratio" in msg and "'percent' should be classes" in msg
    with pytest.raises(config.ConfigError, match="values: ratio needs irrigated"):
        _exp(tmp_path, {"water_regime": {"name": "w", "values": "ratio", "total": "t.tif"}})
    with pytest.raises(config.ConfigError, match="no region input"):
        _exp(tmp_path, {}, aoi={"name": "t", "bbox": [107.0, -6.4, 107.5, -6.0], "region": "Jawa"})
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, {}, treatments={"default_regime": "flooded", "share_regimes": {"share": "irrigated",
                                                                                      "rest": "irrigated"}})
    assert "'flooded' should be one of the water regimes" in str(e.value) and "two different" in str(e.value)


def test_optional_inputs_pass_the_checks(tmp_path):
    cache = tmp_path / "cache"
    _weather(cache, "w_x")
    _layers(cache)
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump({"paths": {"cache": str(cache)}, "aoi": {"region": None},
                                 "inputs": {"weather": "w_x", "crop_mask": "crop_x", "soil": "soil_x",
                                            "water_regime": None, "region": None, "zones": None},
                                 "treatments": {"default_regime": "rainfed"}, "years": [2015, 2015]}))
    exp = config.load(p, env={})
    assert inputs.check(exp) == []
    ins = inputs.open_all(exp)
    assert ins["water_regime"] is None and ins["region"] is None


def test_unlisted_classes_take_the_default_regime():
    # an LGRIP30-like map: 0 water, 1 non-cropland, 2 irrigated, 3 rainfed
    lgrip = {2: "irrigated", 3: "rainfed"}
    st = _stats(irr=[2, 1, 0])
    with pytest.raises(ValueError):
        mg.strata(st, None, zone_col=None, regime=lgrip)
    out = mg.strata(st, None, zone_col=None, regime=lgrip, default_regime="rainfed")
    assert out.set_index(["iy", "ix"]).water.to_dict() == {(0, 0): "irrigated", (0, 1): "rainfed", (1, 1): "rainfed"}


def test_class_map_in_tiles_is_joined_over_the_box(tmp_path):
    step = 0.01
    a = np.full((50, 50), 2, "uint8")
    b = np.full((50, 50), 3, "uint8")
    _tif(tmp_path / "src" / "lg_E107.tif", a, Affine(step, 0, 107.0, 0, -step, -6.0))
    _tif(tmp_path / "src" / "lg_E107h.tif", b, Affine(step, 0, 107.5, 0, -step, -6.0))
    exp = _exp(tmp_path, {"water_regime": {"name": "lg", "files": "{inputs}/lg_*.tif",
                                           "box": [107.3, -6.4, 107.7, -6.1]}})
    layerprep.prepare(exp, "water_regime", log=lambda s: None)
    d = inputs.open_dataset("water_regime", "lg", exp.path("cache"))
    with rasterio.open(d.file()) as r:
        arr, t = r.read(1), r.transform
    assert arr.shape == (30, 40) and t.c == pytest.approx(107.3) and t.f == pytest.approx(-6.1)
    assert (arr[:, :20] == 2).all() and (arr[:, 20:] == 3).all()
    assert d.codes() == {"2": 2, "3": 3}

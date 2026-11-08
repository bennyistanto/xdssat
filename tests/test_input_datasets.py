"""Tests for the prepared inputs (src/inputs.py): manifests, the generic weather reader,
the requirement checks and their messages, and registering the original layout.

Everything is synthetic and small, in a temporary cache folder.
"""
import json

import netCDF4
import numpy as np
import pandas as pd
import pytest
import rasterio
import yaml
from rasterio.transform import Affine

import config
import inputs
import rasters

LAT = np.round(np.arange(-6.0, -6.35, -0.1), 4)      # 4 rows, north to south
LON = np.round(np.arange(107.0, 107.45, 0.1), 4)     # 5 columns


def _weather_file(path, var, year, packed, value, hours=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31")
    with netCDF4.Dataset(path, "w") as g:
        g.createDimension("time", len(days))
        g.createDimension("lat", len(LAT))
        g.createDimension("lon", len(LON))
        t = g.createVariable("time", "f8" if hours else "i4", ("time",))
        t.units = "hours since 2000-01-01 00:00:00" if hours else "days since 1900-01-01"
        t.calendar = "standard"
        base = pd.Timestamp("2000-01-01") if hours else pd.Timestamp("1900-01-01")
        t[:] = ((days - base).days * (24 if hours else 1)).values
        g.createVariable("lat", "f8", ("lat",))[:] = LAT
        g.createVariable("lon", "f8", ("lon",))[:] = LON
        vals = np.full((len(days), len(LAT), len(LON)), value, dtype="float64")
        vals[:, 0, 0] = np.nan                       # a sea cell
        if packed:
            x = g.createVariable(var, "i2", ("time", "lat", "lon"), fill_value=np.int16(-32768))
            x.set_auto_maskandscale(False)
            x.scale_factor, x.add_offset = np.float32(0.1), np.float32(0.0)
            x[:] = np.where(np.isnan(vals), -32768, np.round(vals / 0.1)).astype("int16")
        else:
            x = g.createVariable(var, "f4", ("time", "lat", "lon"), fill_value=np.float32(-9999.0))
            x[:] = np.where(np.isnan(vals), -9999.0, vals)
        x.units = inputs.WEATHER_UNITS[var]


def _weather(cache, name, years=(2015, 2016), packed=True, variables=inputs.WEATHER_REQUIRED, hours=False):
    for var in variables:
        for y in years:
            _weather_file(cache / "w" / name / var / f"{var}_{y}.nc", var, y, packed,
                          {"SRAD": 18.3, "TMAX": 31.2, "TMIN": 23.4, "RAIN": 6.5}[var], hours)
    inputs.write_manifest("weather", name, {"files": f"w/{name}/{{var}}/{{var}}_{{year}}.nc",
                                            "variables": list(variables), "wind_height_m": 2.0}, cache)


def _tif(path, data, transform, crs=rasters.CRS, count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[-1], height=data.shape[-2], count=count,
                       dtype=data.dtype, crs=crs, transform=transform) as r:
        r.write(data if data.ndim == 3 else data[None])


def _layers(cache, shift=0.0):
    t = Affine(100.0, 0, 11_900_000.0, 0, -100.0, -660_000.0)
    _tif(cache / "r" / "crop.tif", np.full((2, 6, 6), 50, "uint8"), t, count=2)
    t2 = Affine(100.0, 0, 11_900_000.0 + shift, 0, -100.0, -660_000.0)
    for kind in ("water_regime", "region", "zones"):
        _tif(cache / "r" / f"{kind}.tif", np.ones((6, 6), "uint8"), t2)
        pd.DataFrame({"code": [1], "name": ["Jawa" if kind == "region" else "one"]}).to_csv(
            cache / "r" / f"{kind}.csv", index=False)
        inputs.write_manifest(kind, f"{kind}_x", {"file": f"r/{kind}.tif", "classes": f"r/{kind}.csv"}, cache)
    inputs.write_manifest("crop_mask", "crop_x", {"file": "r/crop.tif", "units_per_ha": 100, "class_band": 2}, cache)
    (cache / "s").mkdir(exist_ok=True)
    (cache / "s" / "x.SOL").write_text("*SOILS\n")
    inputs.write_manifest("soil", "soil_x", {"file": "s/x.SOL", "weather": "w_x"}, cache)


def _exp(tmp_path, cache, **inputs_):
    names = {"weather": "w_x", "crop_mask": "crop_x", "soil": "soil_x", "water_regime": "water_regime_x",
             "region": "region_x", "zones": "zones_x", **inputs_}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump({"paths": {"cache": str(cache)}, "inputs": names, "years": [2015, 2015]}))
    return config.load(p, env={})


# ---------------------------------------------------------------- weather


@pytest.mark.parametrize("packed, hours", [(True, False), (False, False), (False, True)])
def test_weather_reader_honours_packing_fill_and_time_units(tmp_path, packed, hours):
    _weather(tmp_path, "w_x", packed=packed, hours=hours)
    w = inputs.open_dataset("weather", "w_x", tmp_path)
    lat, lon, valid = w.grid()
    assert np.allclose(lat, LAT) and np.allclose(lon, LON) and not valid[0, 0] and valid[1, 1]
    assert w.years("TMAX") == [2015, 2016] and abs(w.step - 0.1) < 1e-9 and w.wind_height == 2.0
    d, v = w.series([1, 2, 0], [1, 3, 0], "2015-12-30", "2016-01-02")
    assert list(d) == list(pd.date_range("2015-12-30", "2016-01-02"))
    assert np.allclose(v["TMAX"][:, :2], 31.2, atol=1e-4) and np.isnan(v["TMAX"][:, 2]).all()
    assert w.last_common_date() == pd.Timestamp("2016-12-31")


def test_agera5_cache_reads_through_the_generic_reader(tmp_path):
    import agera5
    for var in ("TMAX", "RAIN"):
        for y in (2000, 2001):
            _weather_file(agera5.cache_path("tst", var, y, tmp_path), var, y, True, 25.0)
    lat, lon, valid = agera5.grid("tst", tmp_path)
    assert np.allclose(lat, LAT) and not valid[0, 0]
    d, v = agera5.series([1], [1], "2000-12-31", "2001-01-01", "tst", ("TMAX", "RAIN"), tmp_path)
    assert len(d) == 2 and np.allclose(v["RAIN"], 25.0)
    assert agera5.last_common_date("tst", ("TMAX", "RAIN"), tmp_path) == pd.Timestamp("2001-12-31")


# ---------------------------------------------------------------- checks


def test_complete_inputs_pass(tmp_path):
    cache = tmp_path / "cache"
    _weather(cache, "w_x")
    _layers(cache)
    exp = _exp(tmp_path, cache)
    assert inputs.check(exp) == []
    ins = inputs.open_all(exp)
    assert ins["crop_mask"].units_per_ha == 100 and ins["crop_mask"].class_band == 2
    assert ins["region"].codes() == {"Jawa": 1}
    assert ins["soil"].ids([3, 12], [7, 1234]) == ["XS00030007", "XS00121234"]
    g = ins["crop_mask"].grid()
    assert (g.left, g.top, g.width, g.height, g.res) == (11_900_000.0, -660_000.0, 6, 6, 100.0)


def test_problems_are_named_in_plain_words(tmp_path):
    cache = tmp_path / "cache"
    _weather(cache, "w_x", years=(2015,), variables=("SRAD", "TMAX", "TMIN"))
    _layers(cache, shift=100.0)
    exp = _exp(tmp_path, cache, soil="nope")
    msg = "\n".join(inputs.check(exp))
    assert "lacks RAIN" in msg
    assert "same grid" not in msg            # since phase 3 a shifted class layer is sampled, not refused
    assert "'nope'" in msg and "register_inputs.py" in msg and "soil_x" in msg   # lists what exists
    exp = _exp(tmp_path, cache)
    _weather(cache, "w_x", years=(2015,))                                        # all variables, one year
    msg = "\n".join(inputs.check(exp))
    assert "no weather for 2016" in msg and "the year after" not in msg


def test_soil_made_for_another_weather_grid(tmp_path):
    cache = tmp_path / "cache"
    _weather(cache, "w_x")
    _weather(cache, "w_y")
    _layers(cache)
    msg = "\n".join(inputs.check(_exp(tmp_path, cache, weather="w_y")))
    assert "one profile per cell of the weather grid 'w_x'" in msg


def test_experiment_file_names_the_inputs(tmp_path):
    with pytest.raises(config.ConfigError, match="null is allowed only for zones"):
        config.load(tmp_path / "x.yml", env={}) if False else config.load(
            _write(tmp_path, "inputs:\n  crop_mask: null\n"), env={})
    with pytest.raises(config.ConfigError, match="Did you mean 'weather'"):
        config.load(_write(tmp_path, "inputs:\n  wether: x\n"), env={})
    exp = config.load(_write(tmp_path, "inputs:\n  zones: null\n"), env={})
    assert exp.data["inputs"]["zones"] is None and exp.data["inputs"]["weather"] == "agera5_idn"


def _write(tmp_path, text):
    p = tmp_path / "f.yml"
    p.write_text(text)
    return p


# ---------------------------------------------------------------- registering


def test_register_existing_writes_manifests_for_what_is_there(tmp_path):
    import agera5
    _weather_file(agera5.cache_path("idn", "TMAX", 2000, tmp_path), "TMAX", 2000, True, 30.0)
    t = Affine(100.0, 0, 0.0, 0, -100.0, 0.0)
    _tif(tmp_path / "rasters" / "idn_rice.tif", np.zeros((2, 3, 3), "uint8"), t, count=2)
    done = dict(((k, n), w) for k, n, w in inputs.register_existing(tmp_path, log=lambda m: None))
    assert done[("weather", "agera5_idn")] == "registered"
    assert done[("crop_mask", "rice_atrbpn_idn")] == "registered"
    assert done[("soil", "soilgrids_java")] == "not found, skipped"
    m = json.loads(inputs.manifest_path("weather", "agera5_idn", tmp_path).read_text())
    assert m["format"] == inputs.FORMAT and m["variables"] == ["TMAX"]
    assert set(inputs.JAVA.values()) == set(config.DEFAULTS["inputs"].values())   # the names the configs use

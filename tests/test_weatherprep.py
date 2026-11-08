"""Tests for weather preparation from any gridded source (src/weatherprep.py).

Synthetic sources only: two products at different grids and in source units,
one multi-year file with 0-360 longitudes and south-to-north latitudes, wind as
u and v components, prepared onto one 0.1 degree grid.
"""
import netCDF4
import numpy as np
import pandas as pd
import pytest
import yaml

import config
import inputs
import rasters
import weatherprep as wp

BOX = [107.0, -7.0, 107.5, -6.5]


def _src(path, names_units, lat, lon, years, fn, lon360=False, multi=True, time_hours=False):
    """A source file (or one per year) with the given variables; fn(name, t, lat2d, lon2d) -> values."""
    path.parent.mkdir(parents=True, exist_ok=True)
    groups = [list(years)] if multi else [[y] for y in years]
    out = []
    for ys in groups:
        p = path if multi else path.with_name(path.name.format(year=ys[0]))
        days = pd.date_range(f"{ys[0]}-01-01", f"{ys[-1]}-12-31")
        with netCDF4.Dataset(p, "w") as g:
            g.createDimension("valid_time", len(days))
            g.createDimension("latitude", len(lat))
            g.createDimension("longitude", len(lon))
            t = g.createVariable("valid_time", "f8", ("valid_time",))
            if time_hours:
                t.units, t[:] = "hours since 1970-01-01", ((days - pd.Timestamp("1970-01-01")).days * 24).values
            else:
                t.units, t[:] = "days since 1970-01-01", (days - pd.Timestamp("1970-01-01")).days.values
            g.createVariable("latitude", "f8", ("latitude",))[:] = lat
            g.createVariable("longitude", "f8", ("longitude",))[:] = np.where(lon < 0, lon + 360, lon) if lon360 else lon
            la, lo = np.meshgrid(lat, lon, indexing="ij")
            for name, units in names_units.items():
                x = g.createVariable(name, "f4", ("valid_time", "latitude", "longitude"), fill_value=-9999.0)
                x.units = units
                x[:] = np.stack([fn(name, k, la, lo) for k in range(len(days))])
        out.append(p)
    return out


def _sources(tmp_path):
    lat25 = np.arange(-7.5, -5.99, 0.25)                    # south to north
    lon25 = np.arange(106.5, 108.01, 0.25)
    era = tmp_path / "src" / "era.nc"
    _src(era, {"t2m_max": "K", "t2m_min": "K", "ssrd": "J m**-2", "u10": "m s**-1", "v10": "m s**-1"},
         lat25, lon25, (2015, 2016),
         lambda n, k, la, lo: {"t2m_max": 300.0 + 2 * lo - 200.0 + 0 * la, "t2m_min": 295.0 + 0 * la,
                               "ssrd": 1.8e7 + 0 * la, "u10": 3.0 + 0 * la, "v10": 4.0 + 0 * la}[n],
         lon360=True)
    lat05 = np.round(np.arange(-7.475, -6.0, 0.05), 3)[::-1]
    lon05 = np.round(np.arange(106.525, 108.0, 0.05), 3)
    rain = tmp_path / "src" / "rain_{year}.nc"
    _src(rain, {"precip": "mm/day"}, lat05, lon05, (2015, 2016),
         lambda n, k, la, lo: np.where(np.round((lo - 106.525) / 0.05) % 2 == 0, 4.0, 8.0), multi=False)
    return era, tmp_path / "src"


def _exp(tmp_path, weather, **extra):
    d = {"paths": {"cache": str(tmp_path / "cache")}, "aoi": {"name": "t", "bbox": BOX, "region": "Jawa"},
         "years": [2015, 2015], "prepare": {"weather": weather}, **extra}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return config.load(p, env={})


def _spec(src):
    return {"name": "mix", "grid_step": 0.1, "variables": {
        "TMAX": {"files": f"{src}/era.nc", "variable": "t2m_max"},
        "TMIN": {"files": f"{src}/era.nc", "variable": "t2m_min"},
        "SRAD": {"files": f"{src}/era.nc", "variable": "ssrd"},
        "RAIN": {"files": f"{src}/rain_{{year}}.nc", "variable": "precip"},
        "WIND": {"files": f"{src}/era.nc", "variable": ["u10", "v10"], "height_m": 10}}}


def test_units_are_read_from_the_file_and_converted():
    assert wp.conversion("TMAX", "K") == (1.0, -273.15)
    assert wp.conversion("SRAD", "J m**-2") == (1e-6, 0.0)
    assert wp.conversion("SRAD", "W m-2") == (0.0864, 0.0)
    assert wp.conversion("RAIN", "m") == (1000.0, 0.0)
    assert wp.conversion("RAIN", "kg m-2 s-1") == (86400.0, 0.0)
    assert wp.conversion("WIND", "m/s") == (86.4, 0.0)
    with pytest.raises(ValueError, match="units:"):
        wp.conversion("RAIN", "inches")


def test_two_sources_become_one_weather_input(tmp_path):
    era, src = _sources(tmp_path)
    exp = _exp(tmp_path, _spec(src))
    wp.prepare(exp, log=lambda m: None)
    w = inputs.open_dataset("weather", "mix", exp.path("cache"))
    lat, lon, valid = w.grid()
    assert abs(w.step - 0.1) < 1e-9 and lat[0] > lat[-1] and valid.all()
    assert lon[0] == pytest.approx(106.9) and lon[-1] == pytest.approx(107.6)   # box plus one cell
    iy, ix = [3, 3], [1, 4]                                  # lat -6.8; lon 107.0 and 107.3
    d, v = w.series(iy, ix, "2015-01-01", "2015-12-31")
    assert len(d) == 365
    # TMAX = 2 lon - 173.15 C, linear in lon, so bilinear from 0.25 degree is exact
    assert np.allclose(v["TMAX"][0], [2 * 107.0 - 173.15, 2 * 107.3 - 173.15], atol=0.051)
    assert np.allclose(v["TMIN"], 21.85, atol=0.051) and np.allclose(v["SRAD"], 18.0)
    assert np.allclose(v["WIND"], 5.0 * 86.4, atol=0.051)    # |(3, 4)| m/s in km/d
    assert np.allclose(v["RAIN"], 6.0)                       # mean of the alternating 4 and 8 mm columns
    assert w.wind_height == 10.0 and w.station_code == "XDWX"
    exp2 = _exp(tmp_path, _spec(src), inputs={"weather": "mix", "zones": None})
    msgs = [m for m in inputs.check(exp2) if "inputs > weather" in m]
    assert msgs == []


def test_same_grid_source_is_copied_exactly(tmp_path):
    lat = np.round(np.arange(-6.5, -7.05, -0.1), 4)
    lon = np.round(np.arange(107.0, 107.55, 0.1), 4)
    _src(tmp_path / "src" / "a.nc", {"TMAX": "degC", "TMIN": "degC", "SRAD": "MJ m-2 d-1", "RAIN": "mm"},
         lat, lon, (2015, 2016), lambda n, k, la, lo: 10 * la + lo + k * 0.01, time_hours=True)
    spec = {"name": "same", "variables": {v: {"files": f"{tmp_path}/src/a.nc", "variable": v}
                                          for v in ("TMAX", "TMIN", "SRAD", "RAIN")}}
    exp = _exp(tmp_path, spec)
    wp.prepare(exp, log=lambda m: None)
    w = inputs.open_dataset("weather", "same", exp.path("cache"))
    glat, glon, valid = w.grid()
    inside = (glat[:, None] <= -6.5 + 1e-9) & (glat[:, None] >= -7.0 - 1e-9) & \
             (glon[None, :] >= 107.0 - 1e-9) & (glon[None, :] <= 107.5 + 1e-9)
    assert valid[inside].all() and not valid[~inside].any()  # outside the source: no data
    d, v = w.series([1], [1], "2015-01-02", "2015-01-02")
    assert v["TMAX"][0, 0] == pytest.approx(round(10 * glat[1] + glon[1] + 0.01, 1))


def test_regrid_methods():
    lat = np.array([0.15, 0.05])
    lon = np.array([0.05, 0.15])
    fine = wp.Block(pd.DatetimeIndex(["2015-01-01"]), np.array([0.175, 0.125, 0.075, 0.025]),
                    np.array([0.025, 0.075, 0.125, 0.175]),
                    np.array([[[1, 3, 5, 7], [1, 3, 5, np.nan], [2, 2, 2, 2], [2, 2, 2, 2]]], float), "")
    m = wp.regrid(fine, lat, lon, "mean")
    assert np.allclose(m, [[[2.0, 17 / 3], [2.0, 2.0]]])
    coarse = wp.Block(fine.dates, np.array([0.2, 0.0]), np.array([0.0, 0.2]),
                      np.array([[[0.0, 2.0], [np.nan, 4.0]]]), "")
    li = wp.regrid(coarse, np.array([0.1]), np.array([0.1]), "linear")
    assert li[0, 0, 0] == pytest.approx(2.0)                 # mean of the three corners with data
    assert wp.default_method(fine, lat, lon) == "mean" and wp.default_method(coarse, lat, lon) == "linear"


def test_inspect_and_plain_problems(tmp_path):
    era, src = _sources(tmp_path)
    text = wp.inspect(era)
    assert "t2m_max" in text and "'K'" in text and "step 0.25" in text and "step 1 days" in text
    bad = _spec(src)
    del bad["variables"]["RAIN"]
    bad["variables"]["TMAX"]["regrid"] = "cubic"
    bad["variables"]["SNOW"] = {"files": "x", "variable": "y"}
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, bad)
    msg = str(e.value)
    assert "RAIN is missing" in msg and "'cubic' is not a method" in msg and "'SNOW' is not a setting" in msg
    spec = _spec(src)
    spec["variables"]["RAIN"]["files"] = f"{src}/nope_{{year}}.nc"
    with pytest.raises(config.ConfigError, match="no file .*nope_2015.nc"):
        wp.prepare(_exp(tmp_path, spec), log=lambda m: None)


def test_pixels_find_their_cell_on_any_weather_step(tmp_path):
    """rasters.aggregate on a 0.25 degree weather grid (it assumed 0.1 before phase 2)."""
    from rasterio.transform import Affine
    import rasterio
    grid = rasters.Grid.from_lonlat(107.0, -7.0, 107.5, -6.5)
    share = np.full((grid.height, grid.width), 100, "uint8")
    p = tmp_path / "crop.tif"
    with rasterio.open(p, "w", driver="GTiff", width=grid.width, height=grid.height, count=2, dtype="uint8",
                       crs=rasters.CRS, transform=Affine(100, 0, grid.left, 0, -100, grid.top)) as r:
        r.write(np.stack([share, np.ones_like(share)]))
    wlat, wlon = np.array([-6.5, -6.75, -7.0]), np.array([107.0, 107.25, 107.5])
    out = rasters.aggregate(p, {}, wlat, wlon, np.ones((3, 3), bool), grid=grid, tile=256)
    assert set(zip(out.iy, out.ix)) == {(i, j) for i in range(3) for j in range(3)}
    assert out.ha.sum() == pytest.approx(grid.width * grid.height, rel=1e-9)
    assert rasters.weather_step(wlat, wlon) == 0.25

"""Weather from sub-daily gridded files and from point sources (src/weatherprep.py).

Synthetic files only. The hourly files follow ERA5-Land's conventions as ECMWF
describes them (instantaneous 2 m temperature; precipitation and radiation
accumulated since 00 UTC, the 00 UTC value holding the previous day's total).
The CSV follows the layout of a NASA POWER daily point download as this code
reads it (header block, parameter lines ending in their unit, YEAR,DOY columns,
-999 for no data); it was not checked against a file downloaded from POWER.
"""
import netCDF4
import numpy as np
import pandas as pd
import pytest
import yaml

import config
import inputs
import weatherprep as wp
from wth import write_wth

BOX = [107.0, -7.0, 107.3, -6.7]
LAT = np.round(np.arange(-6.6, -7.15, -0.1), 4)
LON = np.round(np.arange(106.9, 107.45, 0.1), 4)


def _hourly(path, start, end, fields):
    """Hourly file from start to end (inclusive), fields: name -> (units, fn(times) -> values per hour)."""
    t = pd.date_range(start, end, freq="h")
    path.parent.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(path, "w") as g:
        g.createDimension("valid_time", len(t))
        g.createDimension("latitude", len(LAT))
        g.createDimension("longitude", len(LON))
        tv = g.createVariable("valid_time", "i8", ("valid_time",))
        tv.units = "seconds since 1970-01-01"
        tv[:] = ((t - pd.Timestamp("1970-01-01")).total_seconds()).astype("int64")
        g.createVariable("latitude", "f8", ("latitude",))[:] = LAT
        g.createVariable("longitude", "f8", ("longitude",))[:] = LON
        for name, (units, fn) in fields.items():
            x = g.createVariable(name, "f4", ("valid_time", "latitude", "longitude"))
            x.units = units
            x[:] = np.broadcast_to(np.asarray(fn(t), dtype="float32")[:, None, None], (len(t), len(LAT), len(LON)))


def _accumulated(rate_per_hour):
    """ERA5-Land style running totals since 00 UTC from a function of time -> amount in the hour ending then."""
    def fn(t):
        amt = rate_per_hour(t)
        day = (t - pd.Timedelta(hours=1)).normalize()
        return pd.Series(amt).groupby(day.values).cumsum().to_numpy()
    return fn


def test_to_daily_instantaneous_and_local_day():
    t = pd.date_range("2015-01-01 00:00", "2015-01-03 23:00", freq="h")
    v = np.asarray(t.hour, dtype="float64")[:, None, None] * np.ones((1, 2, 2))
    b = wp.Block(t, np.array([0.0, -0.1]), np.array([107.0, 107.1]), v, "K")
    d = wp.to_daily(b, "max")
    assert list(d.dates.day) == [1, 2, 3] and (d.values == 23).all()
    d7 = wp.to_daily(b, "max", utc_offset_hours=7)
    # local days of UTC+7: only 2 Jan and 3 Jan are complete (1 Jan local starts at 17:00 UTC of 31 Dec)
    assert list(d7.dates.day) == [2, 3]
    assert d7.values[0, 0, 0] == 23                         # UTC 17-23 of 1 Jan then 00-16 of 2 Jan: max 23
    assert wp.to_daily(b, "min", 7).values[0, 0, 0] == 0
    assert wp.to_daily(b, "mean").values[0, 0, 0] == pytest.approx(11.5)


def test_to_daily_accumulated_since_00_utc():
    t = pd.date_range("2015-01-01 00:00", "2015-01-04 00:00", freq="h")
    rate = np.where((t - pd.Timedelta(hours=1)).normalize() == pd.Timestamp("2015-01-02"), 2.0, 1.0)
    acc = _accumulated(lambda tt: rate)(t)
    b = wp.Block(t, np.array([0.0]), np.array([107.0]), acc[:, None, None].astype("float64"), "m")
    d = wp.to_daily(b, "accumulated")
    got = dict(zip(d.dates.strftime("%m-%d"), d.values[:, 0, 0]))
    # 1 Jan: the hours ending 01:00 to 00:00 next day are all there (the 00 UTC value of 1 Jan
    # belongs to 31 Dec, which is dropped)
    assert got == pytest.approx({"01-01": 24.0, "01-02": 48.0, "01-03": 24.0})
    d7 = wp.to_daily(b, "accumulated", utc_offset_hours=7)
    got7 = dict(zip(d7.dates.strftime("%m-%d"), d7.values[:, 0, 0]))
    # local 2 Jan = UTC 1 Jan 17:00-2 Jan 17:00: 7 h at 1 mm, 17 h at 2 mm
    assert got7["01-02"] == pytest.approx(7 * 1 + 17 * 2)


def _hourly_sources(src, years):
    """One file per year, cut from one continuous series (so each file's 00 UTC value of 1 January
    holds the previous day's total, as in ERA5-Land); the last year only to 2 January."""
    full = pd.date_range(f"{years[0]}-01-01 00:00", f"{years[-1]}-01-02 00:00", freq="h")
    series = {"t2m": ("K", 295.0 + 10 * np.sin(2 * np.pi * (full.hour + 7) / 24)),
              "ssrd": ("J m**-2", _accumulated(lambda t: np.where(((t.hour + 7) % 24 >= 7) & ((t.hour + 7) % 24 < 18),
                                                                 1.6e6, 0.0))(full)),
              "tp": ("m", _accumulated(lambda t: np.full(len(t), 0.0005))(full)),
              "u10": ("m s**-1", np.full(len(full), 3.0)),
              "v10": ("m s**-1", np.full(len(full), 4.0))}
    for y in years:
        sel = full.year == y
        f = {k: (u, (lambda vv: (lambda t: vv))(np.asarray(v)[sel])) for k, (u, v) in series.items()}
        _hourly(src / f"e5l_{y}.nc", full[sel][0], full[sel][-1], f)


def _exp(tmp_path, weather, years=(2015, 2015)):
    d = {"paths": {"cache": str(tmp_path / "cache"), "inputs": str(tmp_path / "src")},
         "aoi": {"name": "t", "bbox": BOX, "region": "Jawa"}, "years": list(years), "prepare": {"weather": weather}}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return config.load(p, env={})


def _hourly_spec(files="{inputs}/e5l_{year}.nc"):
    return {"name": "e5l", "grid_step": 0.1, "years": [2015, 2015], "utc_offset_hours": 7, "variables": {
        "TMAX": {"files": files, "variable": "t2m", "daily": "max"},
        "TMIN": {"files": files, "variable": "t2m", "daily": "min"},
        "SRAD": {"files": files, "variable": "ssrd", "daily": "accumulated"},
        "RAIN": {"files": files, "variable": "tp", "daily": "accumulated"},
        "WIND": {"files": files, "variable": ["u10", "v10"], "daily": "mean", "height_m": 10}}}


def test_hourly_files_become_daily_weather(tmp_path):
    _hourly_sources(tmp_path / "src", [2014, 2015, 2016])
    exp = _exp(tmp_path, _hourly_spec())
    wp.prepare(exp, log=lambda s: None)
    w = inputs.open_dataset("weather", "e5l", exp.path("cache"))
    lat, lon, valid = w.grid()
    dates, v = w.series([2], [2], pd.Timestamp("2015-01-01"), pd.Timestamp("2015-12-31"))
    assert len(dates) == 365
    # 295 + 10 sin(local hour): max 305 K = 31.85 C, min 285 K = 11.85 C (packed to 0.1)
    assert v["TMAX"].max() == pytest.approx(31.85, abs=0.06) and v["TMIN"].min() == pytest.approx(11.85, abs=0.06)
    # 11 local hours of 1.6 MJ: 17.6 MJ m-2 d-1; 0.5 mm per hour: 12 mm d-1; wind 5 m/s = 432 km/d
    assert np.allclose(v["SRAD"], 17.6, atol=0.06) and np.allclose(v["RAIN"], 12.0, atol=0.06)
    assert np.allclose(v["WIND"], 432.0, atol=0.06)


def test_a_missing_neighbour_year_is_named(tmp_path):
    _hourly_sources(tmp_path / "src", [2015, 2016])          # no 2014: 1 Jan 2015 local is incomplete
    exp = _exp(tmp_path, _hourly_spec())
    with pytest.raises(config.ConfigError, match="only 364 of 365 days of 2015"):
        wp.prepare(exp, log=lambda s: None)


def test_amounts_per_step_are_not_taken_for_running_totals():
    t = pd.date_range("2015-01-01 00:00", "2015-01-03 00:00", freq="h")
    b = wp.Block(t, np.array([0.0]), np.array([107.0]), (1.0 + np.asarray(t.hour % 3))[:, None, None].astype("float64"), "mm")
    with pytest.raises(ValueError, match="not totals since 00 UTC"):
        wp.to_daily(b, "accumulated")
    # as amounts per hour: 1 Jan has 8 x (1 + 2 + 3) = 48, 2 Jan the same
    assert wp.to_daily(b, "sum").values[:, 0, 0].tolist() == pytest.approx([48.0, 48.0])


def test_daily_setting_is_required_for_hourly_files(tmp_path):
    _hourly_sources(tmp_path / "src", [2014, 2015, 2016])
    spec = _hourly_spec()
    del spec["variables"]["TMAX"]["daily"]
    with pytest.raises(config.ConfigError, match="set  daily: max"):
        wp.prepare(_exp(tmp_path, spec), log=lambda s: None)


# ---------------------------------------------------------------- points
POWER = """-BEGIN HEADER-
NASA/POWER Source Native Resolution Daily Data
Dates (month/day/year): 01/01/2015 through 12/31/2016 in LST
Location: Latitude  -6.7500   Longitude 107.2500
Elevation from MERRA-2: Average for 0.5 x 0.625 degree lat/lon region = 700.0 meters
The value for missing source data that cannot be computed or is outside of the sources availability range: -999
Parameter(s):
T2M_MAX           MERRA-2 Temperature at 2 Meters Maximum (C)
T2M_MIN           MERRA-2 Temperature at 2 Meters Minimum (C)
PRECTOTCORR       MERRA-2 Precipitation Corrected (mm/day)
ALLSKY_SFC_SW_DWN CERES SYN1deg All Sky Surface Shortwave Downward Irradiance (kW-hr/m^2/day)
WS2M              MERRA-2 Wind Speed at 2 Meters (m/s)
-END HEADER-
YEAR,DOY,T2M_MAX,T2M_MIN,PRECTOTCORR,ALLSKY_SFC_SW_DWN,WS2M
"""


def _power(path, missing_day=None):
    days = pd.date_range("2015-01-01", "2016-12-31")
    rows = [f"{d.year},{d.dayofyear},31.5,22.0,{-999 if d == missing_day else 7.5},5.0,1.5" for d in days]
    path.write_text(POWER + "\n".join(rows) + "\n")


def _station(path):
    days = pd.date_range("2015-01-01", "2016-12-31")
    df = pd.DataFrame({"SRAD": 16.0, "TMAX": 30.0, "TMIN": 20.0, "RAIN": 4.0}, index=days)
    write_wth(path, df, "STAT", -7.05, 106.95, tav=25.0, amp=2.0)


def test_points_spread_to_the_nearest_cells(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _power(src / "power.csv")
    _station(src / "STAT.WTH")
    spec = {"name": "pts", "grid_step": 0.1, "years": [2015, 2016],
            "points": [{"file": "{inputs}/power.csv"}, {"file": "{inputs}/STAT.WTH"}]}
    exp = _exp(tmp_path, spec, years=(2015, 2015))
    wp.prepare(exp, log=lambda s: None)
    w = inputs.open_dataset("weather", "pts", exp.path("cache"))
    lat, lon, valid = w.grid()
    # WIND only where every point has it (the station file has none)
    assert valid.all() and w.variables == ["SRAD", "TMAX", "TMIN", "RAIN"]
    iy_ne, ix_ne = int(np.argmin(abs(lat + 6.7))), int(np.argmin(abs(lon - 107.3)))
    iy_sw, ix_sw = int(np.argmin(abs(lat + 7.0))), int(np.argmin(abs(lon - 107.0)))
    _, v = w.series([iy_ne, iy_sw], [ix_ne, ix_sw], pd.Timestamp("2015-03-01"), pd.Timestamp("2015-03-01"))
    assert v["TMAX"][0].tolist() == pytest.approx([31.5, 30.0])
    assert v["SRAD"][0, 0] == pytest.approx(18.0)              # 5 kWh m-2 = 18 MJ m-2
    assert v["RAIN"][0].tolist() == pytest.approx([7.5, 4.0])


def test_points_with_gaps_or_no_location(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _power(src / "gap.csv", missing_day=pd.Timestamp("2015-06-01"))
    spec = {"name": "pts", "grid_step": 0.1, "years": [2015, 2015], "points": [{"file": "{inputs}/gap.csv"}]}
    with pytest.raises(config.ConfigError, match="no RAIN for 1 day.*2015-06-01"):
        wp.prepare(_exp(tmp_path, spec), log=lambda s: None)
    (src / "plain.csv").write_text("date,tx,tn,rr,rs\n" + "\n".join(
        f"{d:%Y-%m-%d},30,20,5,15" for d in pd.date_range("2015-01-01", "2015-12-31")) + "\n")
    spec["points"] = [{"file": "{inputs}/plain.csv", "columns": {"TMAX": "tx", "TMIN": "tn", "RAIN": "rr", "SRAD": "rs"},
                       "units": {"TMAX": "C", "TMIN": "C", "RAIN": "mm", "SRAD": "MJ m-2"}}]
    with pytest.raises(config.ConfigError, match="give lat and lon"):
        wp.prepare(_exp(tmp_path, spec), log=lambda s: None)
    spec["points"][0].update(lat=-6.8, lon=107.1)
    wp.prepare(_exp(tmp_path, spec), overwrite=True, log=lambda s: None)
    txt = wp.inspect_table(src / "gap.csv")
    assert "-6.75, 107.25" in txt and "T2M_MAX" in txt and "(C)" in txt and "-> TMAX" in txt


def test_settings_in_plain_words(tmp_path):
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, {"name": "x", "points": [{"file": "a.csv", "colums": {}}],
                        "variables": {"TMAX": {"files": "a", "variable": "b"}}})
    msg = str(e.value)
    assert "not both" in msg and "need grid_step" in msg and "'colums' is not a setting" in msg
    with pytest.raises(config.ConfigError, match="'median' should be one of max, min, mean"):
        spec = _hourly_spec()
        spec["variables"]["TMAX"]["daily"] = "median"
        _exp(tmp_path, spec)

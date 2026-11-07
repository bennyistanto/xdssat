"""Gridded netCDF of a run (src/outputs.py), on a synthetic run."""
import json
import re

import numpy as np
import pandas as pd
import xarray as xr

import outputs

LAT = np.round(np.arange(6.5, -11.55, -0.1), 1)
LON = np.round(np.arange(94.5, 141.55, 0.1), 1)


def _run():
    strata = pd.DataFrame({"iy": [120, 120, 121], "ix": [200, 200, 202],
                           "water": ["irrigated", "rainfed", "irrigated"], "ha": [100.0, 50.0, 10.0]})
    strata["lat"], strata["lon"] = LAT[strata.iy], LON[strata.ix]
    rows = []
    for y in (2000, 2001):
        for st in strata.itertuples():
            seasons = ["wet", "dry"] if st.water == "irrigated" else ["wet"]
            for s in seasons:
                pd_ = pd.Timestamp(y, 12, 1) if s == "wet" else pd.Timestamp(y + 1, 4, 1)
                md = pd_ + pd.Timedelta(days=100)
                hwah = 5000 + 1000 * (s == "dry") + 10 * st.ix + (y - 2000)
                rows.append(dict(season=s, season_year=y, iy=st.iy, ix=st.ix, water=st.water, ha=st.ha,
                                 lat=st.lat, lon=st.lon, pdate=pd_.strftime("%Y-%m-%d"),
                                 HWAH=hwah, CWAM=2 * hwah, LAIX=5.0, PRCM=500.0, IRCM=0.0, ETCM=400.0,
                                 NUCM=120.0, PDAT=int(pd_.strftime("%Y%j")), ADAT=-99,
                                 MDAT=int(md.strftime("%Y%j"))))
    s = pd.DataFrame(rows)
    s.loc[0, "HWAH"] = -99            # one failed yield
    return s, strata


def test_dataset_layout_and_values():
    s, st = _run()
    ds = outputs.seasonal_dataset(s, st, LAT, LON)
    assert list(ds.system.values) == ["wet_irrigated", "wet_rainfed", "dry_irrigated"]
    assert ds["yield"].dims == ("system", "time", "lat", "lon")
    assert ds.sizes["lat"] == 2 and ds.sizes["lon"] == 3
    assert list(ds.season_year.values) == [2000, 2001]
    # every season lands in its cell, nowhere else
    assert int(ds["yield"].notnull().sum()) == (s.HWAH > 0).sum()
    v = ds["yield"].sel(system="dry_irrigated").sel(lat=LAT[121], lon=LON[202]).isel(time=1)
    assert float(v) == 5000 + 1000 + 10 * 202 + 1
    assert np.isnan(float(ds["yield"].sel(system="wet_irrigated").isel(time=0, lat=0, lon=0)))
    # dates and season length
    pdate = ds.planting_date.sel(system="wet_rainfed").isel(time=0, lat=0, lon=0).values
    assert str(pdate)[:10] == "2000-12-01"
    assert np.isnat(ds.anthesis_date).all()
    assert float(ds.season_length.max()) == 100
    # areas: production / harvested area is the area-weighted mean yield
    ok = s.HWAH > 0
    expect = (s.HWAH[ok] * s.ha[ok]).sum() / s.ha[ok].sum()
    got = float(ds.production.sum() / ds.harvested_area.sum() * 1000)
    assert abs(got - expect) < 0.01
    assert float(ds.rice_area_total.sum()) == st.ha.sum()
    assert float(ds.rice_area.sel(system="wet_rainfed").sum()) == 50.0


def test_rejects_two_seasons_per_slot():
    s, st = _run()
    s = pd.concat([s, s.iloc[[1]]])
    try:
        outputs.seasonal_dataset(s, st, LAT, LON)
    except ValueError:
        return
    raise AssertionError("duplicate season not rejected")


def test_write_round_trip(tmp_path):
    s, st = _run()
    s.to_csv(tmp_path / "summary.csv", index=False)
    st.to_csv(tmp_path / "strata.csv", index=False)
    info = {"args": {"aoi": "test", "mode": "uniform", "years": "[2000, 2001]"}, "template": "X.RIX"}
    (tmp_path / "run_info.json").write_text(json.dumps(info))
    ds = outputs.seasonal_dataset(s, st, LAT, LON, attrs=outputs.run_attrs(tmp_path, info))
    p = outputs.write(ds, tmp_path / "out.nc")
    with xr.open_dataset(p) as r:
        assert r.attrs["Conventions"].startswith("CF-")
        assert r["yield"].dtype == np.float32
        np.testing.assert_allclose(r["yield"].values, ds["yield"].values)
        assert (r.planting_date.values == ds.planting_date.values).sum() == ds.planting_date.notnull().sum()
        assert list(r.system.values) == list(ds.system.values)
        assert r.time_bnds.values[0, 1] == np.datetime64("2001-08-01")
        assert list(r.season_year.values) == [2000, 2001]
    import netCDF4
    with netCDF4.Dataset(p) as f:
        assert "coordinates" not in f["time_bnds"].ncattrs()        # CF 7.1: bounds carry no coordinates
        for v in f.variables.values():
            assert getattr(v, "coordinates", "x") != ""
        assert f["yield"].coordinates == "season water_regime season_year"
        local = re.compile(r"\b[A-Za-z]:[\\/]")                    # no local drive paths published
        assert not local.search(f.xdssat_run) and not local.search(f.source)

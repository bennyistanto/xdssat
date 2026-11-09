"""Seasonal analysis tables (src/seasonal.py) on a synthetic run with known answers."""
import numpy as np
import pandas as pd
import pytest

import seasonal


def _summary():
    rows = []
    # two cells, one irrigated (100 ha) and one rainfed (300 ha), wet season, 4 years
    for y, (a, b) in zip(range(2000, 2004), [(6000, 2000), (7000, 4000), (8000, 3000), (5000, -99)]):
        rows.append(dict(iy=1, ix=1, lat=-7.0, lon=110.0, water="irrigated", season="wet", season_year=y,
                         ha=100.0, HWAH=a, NICM=100, IRCM=300))
        rows.append(dict(iy=1, ix=2, lat=-7.0, lon=110.1, water="rainfed", season="wet", season_year=y,
                         ha=300.0, HWAH=b, NICM=100, IRCM=0))
    return pd.DataFrame(rows)


def test_cell_stats_over_years():
    s = seasonal.season_table(_summary())
    c = seasonal.cell_stats(s, below=4500).set_index("water")
    assert c.loc["irrigated", "n"] == 4 and c.loc["irrigated", "mean"] == 6500
    assert c.loc["irrigated", "p50"] == 6500 and c.loc["irrigated", "share_below"] == 0
    assert c.loc["rainfed", "n"] == 3                                   # -99 is missing, not 0
    assert c.loc["rainfed", "mean"] == 3000 and c.loc["rainfed", "share_below"] == 1
    assert c.loc["irrigated", "cv"] == pytest.approx(np.std([6000, 7000, 8000, 5000], ddof=1) / 6500)


def test_region_series_is_area_weighted_and_skips_missing():
    series = seasonal.region_series(seasonal.season_table(_summary()))
    allv = series[series.system == "all"].set_index("season_year")["value"]
    assert allv[2000] == pytest.approx((6000 * 100 + 2000 * 300) / 400)
    assert allv[2003] == pytest.approx(5000)                             # only the irrigated cell
    assert set(series.system) == {"wet_irrigated", "wet_rainfed", "all"}


def test_cdf_and_mean_variance():
    series = seasonal.region_series(seasonal.season_table(_summary()))
    c = seasonal.cdf(series)
    ir = c[c.system == "wet_irrigated"]
    assert list(ir["value"]) == [5000, 6000, 7000, 8000]
    assert list(ir["probability"]) == pytest.approx([0.2, 0.4, 0.6, 0.8])
    more = series.assign(value=series["value"] * 1.1)
    mv = seasonal.mean_variance({"a": series, "b": more}).set_index(["run", "system"])
    assert mv.loc[("b", "all"), "mean"] == pytest.approx(1.1 * mv.loc[("a", "all"), "mean"])


def test_gross_margin():
    eco = seasonal.Economics(price=0.3, fixed_cost=500, n_cost=1.0, irrigation_cost=0.1)
    s = seasonal.season_table(_summary(), economics=eco)
    first = s.iloc[0]                                                   # irrigated, 6000 kg
    assert first["margin"] == pytest.approx(0.3 * 6000 - 500 - 100 - 30)

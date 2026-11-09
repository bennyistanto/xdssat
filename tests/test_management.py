"""Tests for management.py: strata, the three modes, rainfall onset."""
import numpy as np
import pandas as pd
import pytest

import management as mg


def _stats():
    # two cells; cell A has irrigated (classes 1 and 3) and rainfed rice, cell B irrigated only
    return pd.DataFrame({
        "iy": [10, 10, 10, 11, 12], "ix": [20, 20, 20, 21, 22],
        "lat": [-6.0, -6.0, -6.0, -6.1, -6.2], "lon": [107.0, 107.0, 107.0, 107.1, 107.2],
        "crop_class": 9, "region": [2, 2, 2, 2, 1], "irr": [1, 3, 4, 2, 1],
        "aez": [5, 5, 7, 6, 6], "ha": [100.0, 50.0, 30.0, 80.0, 999.0]})


def test_strata_merge_irrigated_classes_and_drop_other_regions():
    s = mg.strata(_stats(), {"Jawa": 2, "Sumatera": 1})
    assert len(s) == 3 and s.ha.sum() == 260
    a = s[(s.iy == 10) & (s.water == "irrigated")].iloc[0]
    assert a.ha == 150 and a.aez == 5                     # dominant zone by rice area
    assert a.wsta == "W0100020" and a.soil_id == "XS00100020" and len(a.wsta) == 8


def test_strata_refuse_missing_irrigation_class():
    st = _stats()
    st.loc[0, "irr"] = 0
    with pytest.raises(ValueError):
        mg.strata(st, {"Jawa": 2})


def test_date_in_agricultural_year():
    assert mg.date_in_year(2015, "11-15") == pd.Timestamp("2015-11-15")
    assert mg.date_in_year(2015, "04-01") == pd.Timestamp("2016-04-01")
    assert mg.date_in_year(2015, "08-01") == pd.Timestamp("2015-08-01")


def test_uniform_gives_dry_season_to_irrigated_only():
    s = mg.strata(_stats(), {"Jawa": 2})
    u = mg.uniform(s, [2015, 2016])
    # 2 irrigated strata x 2 seasons x 2 years + 1 rainfed x 1 season x 2 years
    assert len(u) == 10
    assert set(u[u.water == "rainfed"].season) == {"wet"}
    assert {"cell", "wsta", "soil_id", "pdate", "ha"} <= set(u.columns)


def test_template_treatment_found_by_levels():
    from filex import FileX
    t = FileX.read(mg.TEMPLATE)
    assert mg.template_cultivars(t) == {"IR 58": (1, "IB0012"), "IR 64": (2, "IB0015")}
    # the generated grid rows (12 onward) that the runner used before the experiment file
    assert mg.template_treatment(t, 1, 4, "IB0012") == (16, {})
    assert mg.template_treatment(t, 3, 4, "IB0012") == (21, {})
    assert mg.template_treatment(t, 3, 3, "IB0015") == (30, {})
    k, over = mg.template_treatment(t, 1, 2, "IB0099", "My rice")
    assert over == {"CU.INGENO": "IB0099", "CU.CNAME": "My rice"}
    with pytest.raises(ValueError):
        mg.template_treatment(t, 2, 9)


def test_seasons_and_regime_from_experiment():
    import config
    exp = config.load_defaults()
    assert mg.seasons_from(exp) == (mg.Season("wet", ("irrigated", "rainfed"), "11-15"),
                                    mg.Season("dry", ("irrigated",), "04-01"))
    assert exp.regime_of_class() == {1: "irrigated", 2: "irrigated", 3: "irrigated", 4: "rainfed"}
    s = mg.strata(_stats(), {"Jawa": 2}, "Jawa", regime={1: "rainfed", 2: "rainfed", 3: "rainfed",
                                                         4: "rainfed"})
    assert set(s.water) == {"rainfed"}


def test_by_zone_uses_table_and_falls_back():
    s = mg.strata(_stats(), {"Jawa": 2})
    table = pd.DataFrame({"zone": [5], "season": ["wet"], "month_day": ["12-10"]})
    u = mg.by_zone(s, [2015], table)
    w = u[u.season == "wet"].set_index("aez").pdate
    assert (w.loc[[5]] == pd.Timestamp("2015-12-10")).all()   # both strata of cell A
    assert (w.loc[[6]] == pd.Timestamp("2015-11-15")).all()   # zone not in table: default


def test_per_cell_days_after_anchor():
    s = mg.strata(_stats(), {"Jawa": 2})
    days = {"wet": pd.Series([100.0], index=pd.MultiIndex.from_tuples([(10, 20)])),
            "dry": pd.Series([220.0], index=pd.MultiIndex.from_tuples([(10, 20)]))}
    u = mg.per_cell(s, [2015], days)
    a = u[(u.iy == 10) & (u.season == "wet")].pdate.unique()
    assert list(a) == [pd.Timestamp("2015-08-01") + pd.Timedelta(days=100)]
    b = u[(u.iy == 11) & (u.season == "wet")].pdate.iloc[0]
    assert b == pd.Timestamp("2015-11-15")                 # cell without data: fallback


def _rain(dates, start_wet, false_start=None):
    r = np.zeros((len(dates), 2))
    r[dates >= pd.Timestamp(start_wet), 0] = 8.0           # 80 mm per full dasarian
    if false_start:
        f = (dates >= pd.Timestamp(false_start)) & (dates < pd.Timestamp(false_start) + pd.Timedelta(days=10))
        r[f, 0] = 8.0
    r[:, 1] = 1.0                                          # never reaches 50 mm
    return r


def test_dasarian_totals():
    d = pd.date_range("2015-02-01", "2015-02-28")
    starts, tot = mg.dasarian_totals(d, np.ones((len(d), 1)))
    assert list(starts.day) == [1, 11, 21] and list(tot[:, 0]) == [10, 10, 8]


def test_onset_needs_three_wet_dasarian():
    d = pd.date_range("2015-08-01", "2016-07-31")
    r = _rain(d, "2015-10-21", false_start="2015-09-01")
    days = mg.onset(d, r, 2015)
    assert days[0] == (pd.Timestamp("2015-10-21") - pd.Timestamp("2015-08-01")).days
    assert np.isnan(days[1])
    per_year, med = mg.onset_climatology(d, r, [2015])
    assert med[0] == days[0]

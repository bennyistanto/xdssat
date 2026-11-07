"""Cleaning rules of scripts/clean_moa_yields.py on small synthetic tables."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location(
    "clean_moa_yields", Path(__file__).resolve().parents[1] / "scripts" / "clean_moa_yields.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)


def _long(rows):
    d = pd.DataFrame(rows, columns=["sheet", "code", "name", "year", "raw"])
    d["value"] = d.raw.map(cm.parse_value)
    return d


def test_parse_value():
    assert cm.parse_value("45,62") == 45.62
    assert cm.parse_value("1.234,50") == 1234.5
    assert np.isnan(cm.parse_value("0,00")) and np.isnan(cm.parse_value(None))
    assert cm.parse_value(51.2) == 51.2


def test_column_rules():
    rows = []
    for i, c in enumerate(["3501", "3502", "3503", "3504"]):
        rows += [("35", c, f"Kab. {c}", 2014, f"{300000 + i},00"),       # production-sized column
                 ("35", c, f"Kab. {c}", 1998, f"{4000 + 100 * i},00"),     # lost decimal separator
                 ("35", c, f"Kab. {c}", 2021, "10,00"),                    # constant placeholder
                 ("35", c, f"Kab. {c}", 2016, f"{4 + i / 10:.2f}".replace(".", ",")),  # t/ha
                 ("35", c, f"Kab. {c}", 2010, f"{50 + i},00")]             # fine
    rows += [("35", "3501", "Kab. 3501", 2011, "250,00"),                  # single implausible value
             ("35", "3502", "Kab. 3502", 2011, "50,00"), ("35", "3503", "Kab. 3503", 2011, "51,00")]
    rows[0] = ("35", "3501", "Kab. 3501", 2014, "59,55")                   # a plausible value in a bad column
    flags = []
    out = cm.column_rules(_long(rows), flags).set_index(["code", "year"]).value
    assert out[("3501", 2014)] == 59.55 and out.loc[pd.IndexSlice[["3502", "3503"], 2014]].isna().all()
    assert out[("3502", 1998)] == 41.0
    assert out.xs(2021, level="year").isna().all() and out.xs(2016, level="year").isna().all()
    assert out.xs(2010, level="year").tolist() == [50, 51, 52, 53]
    assert np.isnan(out[("3501", 2011)])
    rules = pd.concat(flags).rule.value_counts()
    assert rules["column_production_scale"] == 3 and rules["decimal_shift_x100"] == 4
    assert rules["implausible_range"] == 1


def test_relative_rule():
    vals = [50, 51, 52, 90, 53, 52, 54]
    d = _long([("33", "3301", "Kab. A", 2000 + i, f"{v},00") for i, v in enumerate(vals)])
    flags = []
    out = cm.relative_rule(d, flags)
    assert np.isnan(out.loc[out.year == 2003, "value"].iloc[0])
    assert out.value.notna().sum() == 6 and len(flags) == 1


def test_harmonise_codes():
    d = _long([("94", "9401", "Kab. Merauke", 2020, "31,40"),
               ("94", "9401", "Kab. Merauke", 2024, "40,00"),
               ("95", "9501", "Kab. Merauke", 2024, "46,17"),
               ("94", "9403", "Kab. Jayapura", 2020, "33,62"),
               ("91", "9171", "Kota. Sorong", 2020, "40,00"),
               ("92", "9202", "Kab. Sorong", 2024, "45,00"),
               ("92", "9271", "Kota Sorong", 2024, "44,00")])
    flags = []
    out, mapping = cm.harmonise_codes(d, {"9403", "9501", "9202", "9271"}, flags)
    s = out.set_index(["code", "year"]).value
    assert s[("9501", 2020)] == 31.40 and s[("9501", 2024)] == 46.17      # new code wins
    assert s[("9271", 2020)] == 40.0                                       # Kota stays Kota
    assert ("9401", 2020) not in s.index and s[("9403", 2020)] == 33.62
    assert dict(zip(mapping.old_code, mapping.new_code)) == {"9401": "9501", "9171": "9271"}
    assert pd.concat(flags).rule.tolist() == ["old_code_overlaps_new_code"]


def test_bdsp_reconcile():
    spec2 = importlib.util.spec_from_file_location(
        "clean_bdsp", Path(__file__).resolve().parents[1] / "scripts" / "clean_bdsp.py")
    cb = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(cb)
    base = pd.DataFrame({"sheet": "35", "code": ["3501", "3502", "3503", "3504"], "name": "x", "year": 2017})
    y = base.assign(raw="", value=[np.nan, 50.0, 40.0, 45.0])       # missing, disagrees, agrees, A/P broken
    a = base.assign(raw="", value=[40000.0, 1000.0, 1000.0, 1000.0])
    p = base.assign(raw="", value=[185248.0, 6000.0, 4100.0, 450000.0])
    flags = []
    y2, a2, p2 = cb.reconcile(y, a, p, flags)
    assert abs(y2.value[0] - 46.312) < 1e-3                         # filled from P/A
    assert y2.value[1] == 50.0 and y2.value[2] == 40.0 and y2.value[3] == 45.0
    assert np.isnan(a2.value[3]) and np.isnan(p2.value[3]) and a2.value[0] == 40000.0
    rules = pd.concat([f for f in flags if len(f)]).rule.tolist()
    assert rules.count("yield_from_production_over_area") == 1
    assert rules.count("yield_disagrees_with_production_over_area") == 1
    assert sum(r.startswith("area_production_inconsistent") for r in rules) == 2

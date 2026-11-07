"""Clean the BDSP district rice tables (harvested area, production, yield) into CSVs.

    python scripts/clean_bdsp.py

Input: <XDSSAT_CACHE>/obs/bdsp/bdsp_padi_adm2_long.csv from scripts/fetch_bdsp.py
(LUAS PANEN ha, PRODUKSI t, PRODUKTIVITAS ku/ha GKG, per district and year).

Its yield is the same series as the MoA workbook (identical in all 12,770
district-years both have), minus the workbook's production-sized Jawa Timur
values, so the yield rules of scripts/clean_moa_yields.py apply. Area and
production add a check: yield should equal production / area x 10.

Steps:
1. Reported yield: clean_moa_yields column rules, plausible range 10-100 ku/ha.
2. Yc = production / area x 10 where both are reported.
   - Yc within 10-100 and the reported yield missing or removed in step 1:
     yield = Yc (flag yield_from_production_over_area). This recovers Jawa
     Timur 2014 and 2017-2022, Sulawesi Selatan 2021-22 and others.
   - Yc within 10-100 and reported yield more than 5% away: reported yield
     kept, flagged yield_disagrees_with_production_over_area (Yc in the flag).
   - Yc outside 10-100: area and production of that year are removed
     (area_production_inconsistent); the reported yield is kept if it passed 1.
3. Yield against the district's 7-year running median (clean_moa_yields rule 4).
4. Codes moved to the latest boundaries as in clean_moa_yields (Papua and
   Papua Barat splits), for all three variables.

Writes to <XDSSAT_CACHE>/obs:
    bdsp_rice_yield_adm2.csv        ku/ha GKG
    bdsp_rice_area_adm2.csv         harvested area, ha
    bdsp_rice_production_adm2.csv   production, t GKG
    bdsp_rice_adm2_flags.csv        every change, with raw values
Rows: code (a2bps), name, name_moa, province_code, province, in_boundary; columns: years.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

import config  # noqa: E402
import clean_moa_yields as cm  # noqa: E402

AGREE = 0.05
VARS = {"PRODUKTIVITAS": "yield", "LUAS PANEN": "area", "PRODUKSI": "production"}


def reconcile(y: pd.DataFrame, a: pd.DataFrame, p: pd.DataFrame, flags: list):
    """Steps 2: yield from production / area, and inconsistent area/production removed.

    All three frames have one row per (sheet, code, name, year) in the same
    order, columns raw and value.
    """
    y, a, p = y.copy(), a.copy(), p.copy()
    yc = p.value / a.value * 10
    ok = yc.between(cm.LOW, cm.HIGH)
    fill = ok & y.value.isna()
    f = y.loc[fill, ["code", "year", "raw", "value"]].rename(columns={"value": "before"})
    f["after"], f["rule"] = yc[fill], "yield_from_production_over_area"
    flags.append(f)
    y.loc[fill, "value"] = yc[fill]
    dis = ok & ~fill & y.value.notna() & ((y.value / yc - 1).abs() > AGREE)
    f = y.loc[dis, ["code", "year", "raw", "value"]].rename(columns={"value": "before"})
    f["after"], f["rule"], f["production_over_area"] = y.value[dis], "yield_disagrees_with_production_over_area", yc[dis]
    flags.append(f)
    bad = a.value.notna() & p.value.notna() & ~ok
    for d, name in ((a, "area"), (p, "production")):
        f = d.loc[bad, ["code", "year", "raw", "value"]].rename(columns={"value": "before"})
        f["after"], f["rule"], f["production_over_area"] = np.nan, f"area_production_inconsistent ({name})", yc[bad]
        flags.append(f)
        d.loc[bad, "value"] = np.nan
    return y, a, p


def wide(d: pd.DataFrame, bnd: pd.DataFrame, decimals: int) -> pd.DataFrame:
    w = d.pivot_table(index="code", columns="year", values="value", aggfunc="first", dropna=False)
    w = w.reindex(index=sorted(d.code.unique()))
    w = w[[c for c in w.columns if w[c].notna().any()]]
    info = bnd.set_index("a2bps")
    names = d.drop_duplicates("code").set_index("code").name
    meta = pd.DataFrame({
        "code": w.index,
        "name": [info.a2name.get(c, names[c]) for c in w.index],
        "name_moa": [names[c] for c in w.index],
        "province_code": [info.a1bps.get(c, c[:2]) for c in w.index],
        "province": [info.a1name.get(c, "") for c in w.index],
        "in_boundary": [c in info.index for c in w.index],
    }).set_index("code")
    return meta.join(w.round(decimals))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--long", type=Path, default=config.CACHE_DIR / "obs" / "bdsp" / "bdsp_padi_adm2_long.csv")
    ap.add_argument("--boundary", type=Path, default=cm.BOUNDARY)
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "obs")
    a = ap.parse_args()
    import pyogrio

    d = pd.read_csv(a.long, dtype={"code": str, "sheet": str, "raw": str})
    keys = ["sheet", "code", "name", "year"]
    frames = {}
    base = d[keys].drop_duplicates().sort_values(keys).reset_index(drop=True)
    for ind, var in VARS.items():
        frames[var] = base.merge(d[d.indicator == ind][keys + ["raw", "value"]], on=keys, how="left")
    flags: list = []
    y = cm.column_rules(frames["yield"], flags)
    y, ar, pr = reconcile(y, frames["area"], frames["production"], flags)
    y = cm.relative_rule(y, flags)

    bnd = pyogrio.read_dataframe(a.boundary, read_geometry=False, columns=["a1bps", "a1name", "a2bps", "a2name"])
    bnd = bnd[bnd.a2bps.astype(str) != "0"].astype({"a2bps": str, "a1bps": str}).drop_duplicates("a2bps")
    out = {}
    for var, fr, dec in (("yield", y, 2), ("area", ar, 2), ("production", pr, 2)):
        h, mapping = cm.harmonise_codes(fr, set(bnd.a2bps), flags)
        out[var] = wide(h, bnd, dec)
        out[var].to_csv(a.out / f"bdsp_rice_{var}_adm2.csv")
    fl = pd.concat([f for f in flags if len(f)], ignore_index=True)
    fl.to_csv(a.out / "bdsp_rice_adm2_flags.csv", index=False)

    yrs = [c for c in out["yield"].columns if isinstance(c, (int, np.integer))]
    print(f"flags: {fl.rule.value_counts().to_dict()}")
    for var, t in out.items():
        cols = [c for c in t.columns if isinstance(c, (int, np.integer))]
        print(f"{var}: {len(t)} districts, years {cols[0]}-{cols[-1]}, {int(t[cols].notna().sum().sum())} values")
    yy = out["yield"][yrs].to_numpy(dtype=float)
    print(f"yield median {np.nanmedian(yy):.2f} ku/ha; Jawa Timur values per year 2013-2023: "
          f"{out['yield'][out['yield'].province_code == '35'][[c for c in yrs if 2013 <= c <= 2023]].notna().sum().to_dict()}")
    print(f"wrote {a.out}/bdsp_rice_{{yield,area,production}}_adm2.csv")


if __name__ == "__main__":
    main()

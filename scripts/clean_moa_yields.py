"""Clean the Ministry of Agriculture district rice productivity workbook into a CSV.

    python scripts/clean_moa_yields.py
    python scripts/clean_moa_yields.py --xlsx E:/temp/xdssat/temp/padi-produktivitas-moa.xlsx

Input: one worksheet per province (sheet name = BPS province code), columns
No, Lokasi ("<BPS code> - <name>"), 1970 ... 2026. Values are text with a
comma decimal separator, in quintal per hectare (ku/ha); "0,00" means no data.

Output, in --out (default <XDSSAT_CACHE>/obs):
    moa_rice_yield_adm2.csv        one row per district: code (BPS, as in the
                                   admin2 boundaries a2bps), name, province_code,
                                   province, then one column per year with data
                                   (ku/ha, empty = missing)
    moa_rice_yield_adm2_flags.csv  every value changed or removed: code, year,
                                   raw text, value before and after, rule

Cleaning rules, in order (thresholds measured on this workbook):
1. "0,00" and blanks are missing. Years with no data at all are dropped.
2. Rules on a whole province-year column (sheet x year), from its non-missing values:
   - median >= 10,000: production-sized numbers, not ku/ha (Jawa Timur 2014 and
     2017-2022). Values outside 10-100 are removed; plausible ones are kept.
   - 1,000 <= median < 10,000 and every value >= 1,000 is a whole number: the
     decimal separator was lost (1998, e.g. "3647,00" for 36.47). Divided by 100.
   - three or more values, all identical: placeholder (Sulawesi Selatan 2021-22,
     all 10.0). Removed.
   - median < 10: another unit, likely t/ha (Papua 2016-17, Bangka Belitung
     2013, Maluku Utara 2013). Removed, not rescaled.
   - more than half the values outside 10-100: column unusable (Kalimantan
     Selatan 2021-22). Removed.
3. Any remaining value outside 10-100 ku/ha is removed.
4. A value below 0.6 or above 1.6 times the district's 7-year centred running
   median (at least 3 values) is removed. Over the plausible values, 98% lie
   within 0.83-1.17 of that median and 99.8% within 0.48-1.92.
5. District codes follow the latest admin2 boundaries (a2bps). Papua districts
   that moved to Papua Selatan, Papua Tengah and Papua Pegunungan (sheet 94 ->
   95, 96, 97), and Papua Barat districts that moved to Papua Barat Daya
   (sheet 91 -> 92), are matched by type (Kab./Kota) and name; the old-code
   series continues under the new code. Where both have a value for a year the
   new-code value is kept and the old one flagged.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

XLSX = config.CACHE_DIR / "temp" / "padi-produktivitas-moa.xlsx"
BOUNDARY = "idn_bnd_adm2.shp"      # in paths > boundaries (BPS-coded admin2 districts)
LOW, HIGH = 10.0, 100.0               # plausible district rice productivity, ku/ha
REL_LOW, REL_HIGH, WINDOW = 0.6, 1.6, 7
SPLITS = {"94": ("95", "96", "97"), "91": ("92",)}   # old province sheet -> new province sheets


def parse_value(v) -> float:
    """Workbook cell to ku/ha. Text uses a comma decimal separator; 0 means missing."""
    if v is None:
        return np.nan
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).strip()
        if not s or s == "-":
            return np.nan
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        x = float(s)
    return np.nan if x == 0 else x


def read_workbook(path: Path) -> pd.DataFrame:
    """Long table: sheet, code, name, year, raw, value."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = []
    for sheet in wb.sheetnames:
        it = wb[sheet].iter_rows(values_only=True)
        hdr = next(it)
        years = [int(y) for y in hdr[2:] if y is not None]
        for r in it:
            if r[1] is None:
                continue
            m = re.match(r"^\s*(\d+)\s*-\s*(.+?)\s*$", str(r[1]))
            if not m:
                raise ValueError(f"sheet {sheet}: cannot read district {r[1]!r}")
            for y, raw in zip(years, r[2:2 + len(years)]):
                rows.append((sheet, m.group(1), m.group(2), y, raw))
    out = pd.DataFrame(rows, columns=["sheet", "code", "name", "year", "raw"])
    out["value"] = out.raw.map(parse_value)
    return out


def _flag(flags, d, mask, new, rule):
    if mask.any():
        f = d.loc[mask, ["code", "year", "raw", "value"]].rename(columns={"value": "before"})
        f["after"] = new[mask] if isinstance(new, pd.Series) else new
        f["rule"] = rule
        flags.append(f)


def column_rules(d: pd.DataFrame, flags: list) -> pd.DataFrame:
    """Rules 2 and 3: whole province-year columns, then the plausible range."""
    d = d.copy()
    for (sheet, year), g in d[d.value.notna()].groupby(["sheet", "year"]):
        v = g.value
        med = v.median()
        idx = g.index
        out = (v < LOW) | (v > HIGH)
        if med >= 10000:
            _flag(flags, d.loc[idx], out, np.nan, "column_production_scale")
            d.loc[idx[out.to_numpy()], "value"] = np.nan
        elif med >= 1000 and (v[v >= 1000] % 1 == 0).all():
            big = v >= 1000
            new = v.where(~big, v / 100)
            _flag(flags, d.loc[idx], big, new, "decimal_shift_x100")
            d.loc[idx, "value"] = new
        elif len(v) >= 3 and v.nunique() == 1:
            _flag(flags, d.loc[idx], v.notna(), np.nan, "column_constant_placeholder")
            d.loc[idx, "value"] = np.nan
        elif med < LOW:
            _flag(flags, d.loc[idx], v.notna(), np.nan, "column_small_scale")
            d.loc[idx, "value"] = np.nan
        elif out.mean() > 0.5:
            _flag(flags, d.loc[idx], v.notna(), np.nan, "column_mostly_implausible")
            d.loc[idx, "value"] = np.nan
    bad = d.value.notna() & ((d.value < LOW) | (d.value > HIGH))
    _flag(flags, d, bad, np.nan, "implausible_range")
    d.loc[bad, "value"] = np.nan
    return d


def relative_rule(d: pd.DataFrame, flags: list) -> pd.DataFrame:
    """Rule 4: against the district's running median."""
    d = d.copy()
    wide = d.pivot_table(index="code", columns="year", values="value", aggfunc="first")
    med = wide.T.rolling(WINDOW, center=True, min_periods=3).median().T
    ratio = (wide / med).stack().rename("ratio").reset_index()
    d = d.merge(ratio, on=["code", "year"], how="left")
    bad = d.value.notna() & d.ratio.notna() & ((d.ratio < REL_LOW) | (d.ratio > REL_HIGH))
    _flag(flags, d, bad, np.nan, "outlier_vs_district")
    d.loc[bad, "value"] = np.nan
    return d.drop(columns="ratio")


def _key(name: str) -> str:
    n = name.lower().strip()
    kind = "kota" if n.startswith("kota") else "kab"
    n = re.sub(r"^(kab(upaten)?|kota)\.?\s*", "", n).replace("peg.", "pegunungan")
    return kind + ":" + re.sub(r"[^a-z]", "", n)


def harmonise_codes(d: pd.DataFrame, boundary_codes: set, flags: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rule 5: old Papua / Papua Barat codes to the new provinces' codes."""
    names = d.drop_duplicates("code")[["sheet", "code", "name"]]
    rows = []
    for old_sheet, new_sheets in SPLITS.items():
        new = names[names.sheet.isin(new_sheets)].assign(key=lambda x: x.name.map(_key))
        old = names[(names.sheet == old_sheet) & ~names.code.isin(boundary_codes)]
        for r in old.itertuples():
            hit = new[new.key == _key(r.name)]
            rows.append(dict(old_code=r.code, old_name=r.name,
                             new_code=hit.code.iloc[0] if len(hit) == 1 else None,
                             new_name=hit.name.iloc[0] if len(hit) == 1 else None))
    mapping = pd.DataFrame(rows)
    m = dict(mapping.dropna(subset=["new_code"])[["old_code", "new_code"]].to_numpy())
    d = d.copy()
    d["old_code"] = d.code.where(d.code.isin(m))
    d["code"] = d.code.replace(m)
    has = d.value.notna()
    dup = d[has].duplicated(["code", "year"], keep=False)
    clash = d[has][dup]
    drop = clash[clash.old_code.notna()].index         # keep the new-code value
    if len(drop):
        _flag(flags, d, d.index.isin(drop), np.nan, "old_code_overlaps_new_code")
    d = d.drop(index=drop)
    d = (d.sort_values("value", na_position="last")
           .drop_duplicates(["code", "year"]).sort_values(["code", "year"]))
    return d.drop(columns="old_code"), mapping


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", type=Path, default=XLSX)
    ap.add_argument("--boundary", type=Path,
                    help="admin2 boundaries (default <paths > boundaries>/idn_bnd_adm2.shp)")
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "obs")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    import pyogrio

    d = read_workbook(a.xlsx)
    n_cells = len(d)
    print(f"read {d.code.nunique()} districts in {d.sheet.nunique()} sheets, years {d.year.min()}-{d.year.max()}, "
          f"{d.value.notna().sum()} non-zero values of {n_cells}")
    flags: list = []
    d = column_rules(d, flags)
    d = relative_rule(d, flags)

    a.boundary = a.boundary or config.BOUNDARIES_DIR / BOUNDARY
    bnd = pyogrio.read_dataframe(a.boundary, read_geometry=False, columns=["a1bps", "a1name", "a2bps", "a2name"])
    bnd = bnd[bnd.a2bps.astype(str) != "0"].astype({"a2bps": str, "a1bps": str}).drop_duplicates("a2bps")
    d, mapping = harmonise_codes(d, set(bnd.a2bps), flags)
    mapping.to_csv(a.out / "moa_rice_yield_adm2_code_map.csv", index=False)

    wide = d.pivot_table(index="code", columns="year", values="value", aggfunc="first", dropna=False)
    wide = wide.reindex(index=sorted(d.code.unique()))      # districts without data stay as empty rows
    wide = wide.reindex(columns=[y for y in sorted(d.year.unique()) if wide.get(y, pd.Series()).notna().any()])
    info = bnd.set_index("a2bps")
    names = d.drop_duplicates("code").set_index("code").name
    unmatched = sorted(set(wide.index) - set(info.index))
    table = pd.DataFrame({
        "code": wide.index,
        "name": [info.a2name.get(c, names[c]) for c in wide.index],
        "name_moa": [names[c] for c in wide.index],
        "province_code": [info.a1bps.get(c, c[:2]) for c in wide.index],
        "province": [info.a1name.get(c, "") for c in wide.index],
        "in_boundary": [c in info.index for c in wide.index],
    }).set_index("code").join(wide.round(2))
    table.to_csv(a.out / "moa_rice_yield_adm2.csv")
    fl = pd.concat(flags, ignore_index=True) if flags else pd.DataFrame()
    fl.to_csv(a.out / "moa_rice_yield_adm2_flags.csv", index=False)

    years = [c for c in table.columns if isinstance(c, (int, np.integer))]
    print(f"rules: {fl.groupby('rule').size().to_dict() if len(fl) else {}}")
    print(f"code map: {mapping.new_code.notna().sum()} old codes moved, unmatched old codes: "
          f"{mapping[mapping.new_code.isna()].old_code.tolist()}")
    print(f"districts {len(table)} ({table.in_boundary.sum()} in the boundaries; not in them: {unmatched}); "
          f"boundary districts without a row: {sorted(set(info.index) - set(table.index))}")
    print(f"years {years[0]}-{years[-1]}; values {int(table[years].notna().sum().sum())}; "
          f"median {np.nanmedian(table[years].to_numpy(dtype=float)):.2f} ku/ha")
    print(f"wrote {a.out / 'moa_rice_yield_adm2.csv'}")


if __name__ == "__main__":
    main()

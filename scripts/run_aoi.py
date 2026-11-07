"""Run rice seasons for every rice stratum of an area of interest.

    python scripts/run_aoi.py --aoi java --mode uniform  --years 1991 2020
    python scripts/run_aoi.py --aoi java --mode per_cell --years 1991 2020
    python scripts/run_aoi.py --aoi java --mode by_zone  --years 1991 2020 --zone-table zones.csv
    python scripts/run_aoi.py --aoi java --mode uniform  --years 2015 2016 --sample 30   # quick test
    python scripts/run_aoi.py --aoi java --mode per_cell --cultivar "IR 64" --n 105

Needs the AgERA5 cache, the rasters (with the filled irrigation layer) and the
soil profiles of the AOI (docs/getting-started.md). Strata, modes and calendars
are described in src/management.py.

Modes:
    uniform   wet season 15 Nov (all rice), dry season 1 Apr (irrigated only)
    by_zone   --zone-table CSV with columns zone, season, month_day (MM-DD),
              zone = agro-ecological zone code (rasters/idn_aez_classes.csv).
              Without --zone-table: each zone gets the median onset of its
              cells (as per_cell), written to zone_table.csv
    per_cell  wet-season onset per cell from AgERA5 rain (BMKG dasarian rule),
              median over --onset-years; transplant 20 days after onset, dry
              season 120 days after the wet season

Writes to --out (default <XDSSAT_CACHE>/runs/<aoi>_<mode>_<y0>-<y1>):
    strata.csv     one row per weather cell and water regime, with rice ha
    units.csv      one row per simulated season
    summary.csv    DSSAT seasonal summary joined to the units (HWAH, MDAT ...)
    failed.csv     seasons the engine rejected, with its error message
    onset.csv      per_cell only: onset per cell (days after 1 Aug, date)
    run_info.json  arguments, counts, timings, engine build
    <out name>.nc  gridded CF netCDF of the seasonal results (src/outputs.py)
"""
import argparse
import copy
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agera5  # noqa: E402
import config  # noqa: E402
import management as mg  # noqa: E402
import outputs  # noqa: E402
import rasters  # noqa: E402
from filex import FileX  # noqa: E402
from genotype import write_cultivar_files  # noqa: E402
from runner import run_units  # noqa: E402
from sol import read_sol, write_sol  # noqa: E402
from wth import tav_amp, write_wth  # noqa: E402

REGION_NAME = {"java": "Jawa"}
SEASON_DAYS = 220          # weather needed after planting (longest season seen: 162 d)


def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def build_strata(aoi, sample, seed):
    R = config.CACHE_DIR / "rasters"
    lat, lon, valid = agera5.grid()
    grid, _ = rasters.IDN.window(*rasters.AOI[aoi])
    stats = rasters.aggregate(R / "idn_rice.tif",
                              {"region": R / "idn_region.tif",
                               "irr": R / "idn_irrigation_filled.tif",
                               "aez": R / "idn_aez.tif"}, lat, lon, valid, grid=grid)
    codes = pd.read_csv(R / "idn_region_classes.csv").set_index("name").code.to_dict()
    st = mg.strata(stats, codes, REGION_NAME[aoi])
    if sample:
        cells = st.groupby("cell").ha.sum()
        keep = cells.sample(sample, weights=cells, random_state=seed).index
        st = st[st.cell.isin(keep)].reset_index(drop=True)
    return st


def onset_days(st, years):
    cells = st.drop_duplicates("cell")[["iy", "ix", "lat", "lon"]].reset_index(drop=True)
    start, end = mg.anchor_date(years[0]), mg.anchor_date(years[-1] + 1) - pd.Timedelta(days=1)
    dates, v = agera5.series(cells.iy, cells.ix, start, end, variables=("RAIN",))
    per_year, med = mg.onset_climatology(dates, v["RAIN"], years)
    cells["onset_days"] = med
    cells["onset_date_md"] = [(pd.Timestamp(2001, 8, 1) + pd.Timedelta(days=int(d))).strftime("%m-%d")
                              if np.isfinite(d) else "" for d in med]
    cells["years_with_onset"] = np.isfinite(per_year).sum(axis=0)
    return cells


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aoi", default="java", choices=sorted(REGION_NAME))
    ap.add_argument("--mode", default="uniform", choices=["uniform", "by_zone", "per_cell"])
    ap.add_argument("--years", nargs=2, type=int, default=[1991, 2020], metavar=("FIRST", "LAST"),
                    help="agricultural years (1 Aug to 31 Jul), inclusive")
    ap.add_argument("--zone-table", type=Path, help="by_zone: CSV zone,season,month_day")
    ap.add_argument("--onset-years", nargs=2, type=int, metavar=("FIRST", "LAST"),
                    help="per_cell: years for the onset climatology (default --years)")
    ap.add_argument("--sample", type=int, help="run only this many cells (sampled by rice area)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--block", type=int, default=5, help="agricultural years per batch")
    ap.add_argument("--chunk-size", type=int, default=200)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--cultivar", default="IR 58", choices=sorted(mg.CULTIVARS))
    ap.add_argument("--n", type=int, default=140, choices=sorted(mg.N_LEVELS),
                    help="kg N/ha as urea, 2/3 at 18 and 1/3 at 41 days after transplanting")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--tcldf", type=float,
                    help="cold sterility threshold (C) of the cultivar: canopy temperature below it during "
                         "heading reduces spikelet fertility (RI_Phenol.for:600-648); default: the CUL value")
    ap.add_argument("--slpf", type=float, default=1.0,
                    help="soil fertility factor (0-1], multiplies daily crop carbohydrate in CERES-Rice "
                         "(RI_Grosub.for:648-649); the yield-gap calibration factor")
    ap.add_argument("--slpf-rainfed", type=float,
                    help="SLPF for rainfed strata (default: --slpf); rainfed strata then get their own "
                         "copies of the cell profiles, ID XR<iy><ix>")
    ap.add_argument("--slpf-table", type=Path,
                    help="CSV iy, ix, water, slpf (scripts/fit_slpf.py slpf_table.csv): SLPF per stratum; "
                         "replaces --slpf and --slpf-rainfed")
    a = ap.parse_args()
    if a.slpf_table and (a.slpf != 1.0 or a.slpf_rainfed is not None):
        raise SystemExit("--slpf-table replaces --slpf and --slpf-rainfed; give only one")
    if a.slpf_rainfed == a.slpf:
        a.slpf_rainfed = None
    for v in (a.slpf, a.slpf_rainfed):
        if v is not None and not 0 < v <= 1:
            raise SystemExit("--slpf and --slpf-rainfed must be in (0, 1]")
    years = list(range(a.years[0], a.years[1] + 1))
    tag = "" if (a.cultivar, a.n) == ("IR 58", 140) else f"_{a.cultivar.replace(' ', '').lower()}_n{a.n}"
    if a.slpf != 1.0 or a.slpf_rainfed is not None:
        tag += f"_slpf{a.slpf:g}"
    if a.slpf_rainfed is not None:
        tag += f"_rf{a.slpf_rainfed:g}"
    if a.slpf_table:
        tag += f"_{a.slpf_table.stem}"
    if a.tcldf is not None:
        tag += f"_tcldf{a.tcldf:g}"
    cul_over = {mg.CULTIVARS[a.cultivar][1]: {"TCLDF": a.tcldf}} if a.tcldf is not None else None
    out = a.out or config.CACHE_DIR / "runs" / f"{a.aoi}_{a.mode}_{years[0]}-{years[-1]}{tag}"
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    st = build_strata(a.aoi, a.sample, a.seed)
    st.to_csv(out / "strata.csv", index=False)
    log(f"{len(st)} strata in {st.cell.nunique()} cells, {st.ha.sum():,.0f} ha "
        f"({st.groupby('water').ha.sum().round(0).to_dict()})")

    if a.mode == "uniform":
        units = mg.uniform(st, years)
    else:
        cells = None
        if a.mode == "per_cell" or not a.zone_table:
            oy = a.onset_years or a.years
            cells = onset_days(st, list(range(oy[0], oy[1] + 1)))
            cells.to_csv(out / "onset.csv", index=False)
            log(f"onset: median {np.nanmedian(cells.onset_days):.0f} days after 1 Aug, "
                f"{int(np.isnan(cells.onset_days).sum())} cells without onset (they use the default date)")
        if a.mode == "per_cell":
            units = mg.per_cell(st, years, mg.planting_from_onset(cells, cells.onset_days.to_numpy()))
        else:
            if a.zone_table:
                table = pd.read_csv(a.zone_table, dtype={"month_day": str})
            else:
                cz = cells.merge(st.drop_duplicates(["iy", "ix"])[["iy", "ix", "aez"]], on=["iy", "ix"])
                table = mg.zone_table_from_onset(cz)
            table.to_csv(out / "zone_table.csv", index=False)
            log(f"zone calendar for {table.zone.nunique()} zones")
            units = mg.by_zone(st, years, table)

    last = agera5.last_common_date()
    late = units.pdate + pd.Timedelta(days=SEASON_DAYS) > last
    if late.any():
        log(f"dropping {late.sum()} seasons that need weather after {last:%Y-%m-%d}")
        units = units[~late].reset_index(drop=True)
    units["trno_template"] = [mg.trno(w, a.cultivar, a.n) for w in units.water]
    units["cultivar"], units["n_kg"] = a.cultivar, a.n

    profiles = read_sol(config.CACHE_DIR / "soils" / f"{a.aoi}.SOL")
    for _p in profiles.values():
        _p.surface["SLPF"] = a.slpf
    missing = set(st.soil_id) - set(profiles)
    if missing:
        raise SystemExit(f"{len(missing)} soil profiles missing from {a.aoi}.SOL, e.g. {sorted(missing)[:3]}")
    if a.slpf_rainfed is not None:          # same soil, own SLPF: a copy per cell with ID XR<iy><ix>
        rf = units.water == "rainfed"
        units.loc[rf, "soil_id"] = "XR" + units.loc[rf, "soil_id"].str[2:]
        for sid in sorted(set(units.loc[rf, "soil_id"])):
            p = copy.deepcopy(profiles["XS" + sid[2:]])
            p.id = sid
            p.surface["SLPF"] = a.slpf_rainfed
            profiles[sid] = p
    if a.slpf_table:                        # SLPF per stratum: irrigated on XS, rainfed on an XR copy
        tab = pd.read_csv(a.slpf_table).set_index(["iy", "ix", "water"]).slpf
        need = units.drop_duplicates(["iy", "ix", "water"]).set_index(["iy", "ix", "water"]).index
        lack = need.difference(tab.index)
        if len(lack):
            raise SystemExit(f"{len(lack)} strata missing from {a.slpf_table}, e.g. {list(lack[:3])}")
        rf = units.water == "rainfed"
        units.loc[rf, "soil_id"] = "XR" + units.loc[rf, "soil_id"].str[2:]
        for iy, ix, water in need:
            sid = f"{'XR' if water == 'rainfed' else 'XS'}{iy:04d}{ix:04d}"
            p = profiles[sid] if water == "irrigated" else copy.deepcopy(profiles["XS" + sid[2:]])
            p.id = sid
            p.surface["SLPF"] = float(tab[(iy, ix, water)])
            profiles[sid] = p
        log(f"SLPF from {a.slpf_table.name}: {tab.reindex(need).min():.3f}-{tab.reindex(need).max():.3f}")
    units.to_csv(out / "units.csv", index=False)
    template = FileX.read(mg.TEMPLATE)
    log(f"{len(units)} seasons to run ({units.groupby(['water', 'season']).size().to_dict()})")

    weather = {}      # filled per block: dates, values {VAR: (time, cell)}, column of each cell

    def write_inputs(run_dir, u):
        dates, w, col = weather["dates"], weather["values"], weather["col"]
        cells = u.drop_duplicates("cell")[["cell", "wsta", "iy", "ix", "lat", "lon"]]
        for c in cells.itertuples():
            df = agera5.cell_frame(dates, w, col[c.cell])
            tav, amp = tav_amp(df.TMAX, df.TMIN, df.index)
            write_wth(run_dir / f"{c.wsta}.WTH", df, "AGE5", c.lat, c.lon, tav=tav, amp=amp,
                      refht=2.0, wndht=agera5.WIND_HEIGHT, title=f"AgERA5 cell {c.iy},{c.ix}")
        write_sol(run_dir / "SOIL.SOL", [profiles[s] for s in sorted(set(u.soil_id))])
        write_cultivar_files(run_dir, cul_over)

    summaries, failures = [], []
    blocks = [years[i:i + a.block] for i in range(0, len(years), a.block)]
    for b in blocks:
        ub = units[units.season_year.isin(b)]
        if ub.empty:
            continue
        tb = time.time()
        cells = ub.drop_duplicates("cell")[["cell", "iy", "ix"]].reset_index(drop=True)
        start = ub.pdate.min() - pd.Timedelta(days=40)
        end = min(ub.pdate.max() + pd.Timedelta(days=SEASON_DAYS), last)
        dates, w = agera5.series(cells.iy, cells.ix, start, end)
        weather.update(dates=dates, values=w, col=dict(zip(cells.cell, range(len(cells)))))
        res = run_units(ub, template, write_inputs, chunk_size=a.chunk_size, workers=a.workers,
                        soils=profiles, ic_water=0.9)
        s = pd.concat([r.summary for r in res], ignore_index=True)
        f = pd.concat([r.failed for r in res], ignore_index=True)
        summaries.append(s)
        failures.append(f)
        log(f"years {b[0]}-{b[-1]}: {len(s)} done, {len(f)} failed, {len(res)} chunks, "
            f"{time.time() - tb:.0f} s")

    summary = pd.concat(summaries, ignore_index=True)
    failed = pd.concat(failures, ignore_index=True)
    summary.to_csv(out / "summary.csv", index=False)
    failed.to_csv(out / "failed.csv", index=False)
    hwah = pd.to_numeric(summary.HWAH, errors="coerce")
    wmean = (summary.assign(w=summary.ha * hwah).groupby(["water", "season"])
             .apply(lambda g: g.w.sum() / g.ha[hwah[g.index].notna()].sum(), include_groups=False))
    log("area-weighted mean yield HWAH (kg/ha):\n" + wmean.round(0).to_string())
    build = (config.ENGINE_DIR / "BUILD_INFO.txt")
    info = dict(args={k: str(v) for k, v in vars(a).items()}, strata=len(st), cells=int(st.cell.nunique()),
                ha=float(st.ha.sum()), seasons=len(units), done=len(summary), failed=len(failed),
                minutes=round((time.time() - t0) / 60, 1), template=str(mg.TEMPLATE),
                engine=build.read_text().strip() if build.exists() else str(config.ENGINE_DIR),
                weather_last_day=f"{last:%Y-%m-%d}")
    (out / "run_info.json").write_text(json.dumps(info, indent=2))
    log(f"netCDF -> {outputs.write_run(out)}")
    log(f"done in {info['minutes']} min -> {out}")


if __name__ == "__main__":
    main()

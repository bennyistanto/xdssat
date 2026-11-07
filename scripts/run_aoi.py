"""Run rice seasons for every rice stratum of an area of interest.

    python scripts/run_aoi.py                                   # everything from configs/xdssat.yml
    python scripts/run_aoi.py --config configs/co2-java.yml     # another experiment file
    python scripts/run_aoi.py --sample 30 --years 2015 2016     # quick test of the same file
    python scripts/run_aoi.py --mode per_cell --cultivar "IR 64" --n 105

All settings come from one experiment file (default configs/xdssat.yml, else
the built-in defaults, which equal configs/rice-java.yml; that file
explains every setting). Flags given here win over the file. The run covers
every cultivar x N rate x scenario of the file, one run folder each.

Needs the AgERA5 cache, the rasters (with the filled irrigation layer) and the
soil profiles of the AOI (docs/getting-started.md). Strata, modes and calendars
are described in src/management.py.

Modes (seasons > mode):
    uniform   one calendar everywhere (seasons > calendar; default wet season
              15 Nov for all rice, dry season 1 Apr for irrigated rice only)
    by_zone   seasons > zone_table (or --zone-table CSV with columns zone,
              season, month_day (MM-DD), zone = agro-ecological zone code,
              rasters/idn_aez_classes.csv). Without a table: each zone gets
              the median onset of its cells (as per_cell), written to
              zone_table.csv
    per_cell  wet-season onset per cell from AgERA5 rain (BMKG dasarian rule,
              seasons > onset), median over the onset years; transplant 20
              days after onset, the next season 120 days later

Writes one folder per combination, in run > output_folder (default
<cache>/runs), named <aoi>_<mode>_<y0>-<y1> plus tags for the cultivar, N rate
and scenario (--out: that exact folder, for an experiment with one combination):
    experiment.yml the resolved settings of this run (re-run it with --config)
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
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

SEASON_DAYS = 220          # default of run > season_days (longest season seen: 162 d)


def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def build_strata(aoi=None, sample=None, seed=1, exp=None):
    """Strata of the experiment's AOI (``aoi`` must be its aoi > name)."""
    import agera5
    import management as mg
    import rasters
    exp = exp or config.CURRENT
    if aoi not in (None, exp.aoi):
        raise ValueError(f"aoi {aoi!r} is not the experiment's aoi ({exp.aoi})")
    R = exp.path("cache") / "rasters"
    lat, lon, valid = agera5.grid()
    grid, _ = rasters.IDN.window(*exp.bbox)
    stats = rasters.aggregate(R / "idn_rice.tif",
                              {"region": R / "idn_region.tif",
                               "irr": R / "idn_irrigation_filled.tif",
                               "aez": R / "idn_aez.tif"}, lat, lon, valid, grid=grid)
    codes = pd.read_csv(R / "idn_region_classes.csv").set_index("name").code.to_dict()
    if exp.region not in codes:
        raise SystemExit(f"aoi > region '{exp.region}' ({exp.where('aoi.region')}) is not in "
                         f"{R / 'idn_region_classes.csv'}; use one of: {', '.join(map(str, codes))}")
    st = mg.strata(stats, codes, exp.region, regime=exp.regime_of_class())
    if sample:
        cells = st.groupby("cell").ha.sum()
        keep = cells.sample(sample, weights=cells, random_state=seed).index
        st = st[st.cell.isin(keep)].reset_index(drop=True)
    return st


def onset_days(st, years, threshold=50.0, n=3):
    import agera5
    import management as mg
    cells = st.drop_duplicates("cell")[["iy", "ix", "lat", "lon"]].reset_index(drop=True)
    start, end = mg.anchor_date(years[0]), mg.anchor_date(years[-1] + 1) - pd.Timedelta(days=1)
    dates, v = agera5.series(cells.iy, cells.ix, start, end, variables=("RAIN",))
    per_year, med = mg.onset_climatology(dates, v["RAIN"], years, threshold=threshold, n=n)
    cells["onset_days"] = med
    cells["onset_date_md"] = [(mg.anchor_date(2001) + pd.Timedelta(days=int(d))).strftime("%m-%d")
                              if np.isfinite(d) else "" for d in med]
    cells["years_with_onset"] = np.isfinite(per_year).sum(axis=0)
    return cells


# ----------------------------------------------------------------------------
# command line -> experiment
# ----------------------------------------------------------------------------
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path,
                    help="experiment file (default: XDSSAT_CONFIG, else configs/xdssat.yml, "
                         "else the built-in defaults = configs/rice-java.yml)")
    ap.add_argument("--check", action="store_true", help="check the experiment file, list the runs, stop")
    g = ap.add_argument_group("overrides (each wins over the experiment file)")
    g.add_argument("--aoi", help="must equal aoi > name of the file (to run another area, edit the aoi section)")
    g.add_argument("--mode", choices=list(config.MODES), help="seasons > mode")
    g.add_argument("--years", nargs=2, type=int, metavar=("FIRST", "LAST"),
                   help="years: agricultural years (1 Aug to 31 Jul), inclusive")
    g.add_argument("--zone-table", type=Path, help="seasons > zone_table: by_zone CSV zone,season,month_day")
    g.add_argument("--onset-years", nargs=2, type=int, metavar=("FIRST", "LAST"),
                   help="seasons > onset > years: years for the onset climatology (default: years)")
    g.add_argument("--sample", type=int, help="run > sample: run only this many cells (sampled by rice area)")
    g.add_argument("--seed", type=int, help="run > seed")
    g.add_argument("--block", type=int, help="run > years_per_batch: agricultural years per batch")
    g.add_argument("--chunk-size", type=int, help="run > chunk_size")
    g.add_argument("--workers", type=int, help="run > workers")
    g.add_argument("--cultivar", help="run only this cultivar: a name under cultivars, or one the template lists")
    g.add_argument("--n", type=int,
                   help="treatments > nitrogen_kg: run only this kg N/ha (urea, 2/3 at 18 and 1/3 at 41 "
                        "days after transplanting)")
    g.add_argument("--out", type=Path, help="exact run folder (experiments with one combination only)")
    g.add_argument("--tcldf", type=float,
                   help="every scenario: cold sterility threshold (C) of the cultivar: canopy temperature below "
                        "it during heading reduces spikelet fertility (RI_Phenol.for:600-648)")
    g.add_argument("--slpf", type=float,
                   help="every scenario: soil fertility factor (0-1], multiplies daily crop carbohydrate in "
                        "CERES-Rice (RI_Grosub.for:648-649); the yield-gap calibration factor")
    g.add_argument("--slpf-rainfed", type=float,
                   help="every scenario: SLPF for rainfed strata (default: --slpf); rainfed strata then get "
                        "their own copies of the cell profiles, ID XR<iy><ix>")
    g.add_argument("--slpf-table", type=Path,
                   help="every scenario: CSV iy, ix, water, slpf (scripts/fit_slpf.py slpf_table.csv): SLPF "
                        "per stratum; replaces --slpf and --slpf-rainfed")
    return ap.parse_args(argv)


def experiment_from_args(a) -> config.Experiment:
    """Load the experiment file and apply the command-line overrides."""
    if a.slpf_table and (a.slpf is not None or a.slpf_rainfed is not None):
        raise SystemExit("--slpf-table replaces --slpf and --slpf-rainfed; give only one")
    base = config.load(a.config)          # file as written (paths resolved), for the cultivar lookup
    o: dict = {}
    if a.mode:
        o.setdefault("seasons", {})["mode"] = a.mode
    if a.years:
        o["years"] = list(a.years)
    if a.zone_table:
        o.setdefault("seasons", {})["zone_table"] = str(a.zone_table.resolve())
    if a.onset_years:
        o.setdefault("seasons", {})["onset"] = {**base.data["seasons"]["onset"], "years": list(a.onset_years)}
    for flag, key in (("sample", "sample"), ("seed", "seed"), ("block", "years_per_batch"),
                      ("chunk_size", "chunk_size"), ("workers", "workers")):
        if getattr(a, flag) is not None:
            o.setdefault("run", {})[key] = getattr(a, flag)
    if a.cultivar:
        cvs = base.data["cultivars"]
        if a.cultivar not in cvs:
            import management as mg
            from filex import FileX
            known = {n: code for n, (_, code) in mg.template_cultivars(FileX.read(base.template_file)).items()}
            if a.cultivar not in known:
                raise SystemExit(f"--cultivar {a.cultivar!r}: not under cultivars in the experiment file and not "
                                 f"in the template; known: {', '.join(sorted({**known, **cvs}))}")
            cvs = known
        o["cultivars"] = {a.cultivar: cvs[a.cultivar]}
    if a.n is not None:
        o.setdefault("treatments", {})["nitrogen_kg"] = [a.n]
    if any(v is not None for v in (a.tcldf, a.slpf, a.slpf_rainfed, a.slpf_table)):
        scs = copy.deepcopy(base.data["scenarios"])
        for s in scs:
            if a.slpf is not None:
                s["slpf"] = a.slpf
                s.pop("slpf_table", None)
            if a.slpf_rainfed is not None:
                s["slpf_rainfed"] = a.slpf_rainfed
                s.pop("slpf_table", None)
            if a.slpf_table:
                s["slpf_table"] = str(a.slpf_table.resolve())
                s.pop("slpf", None)
                s.pop("slpf_rainfed", None)
            if a.tcldf is not None:
                co = dict(s.get("cultivar_coefficients") or {})
                co["TCLDF"] = a.tcldf
                s["cultivar_coefficients"] = co
        o["scenarios"] = scs
    exp = config.load(a.config, overrides=o) if o else base
    if a.aoi and a.aoi != exp.aoi:
        raise SystemExit(f"--aoi {a.aoi}: the experiment file is set up for aoi '{exp.aoi}' "
                         f"({exp.where('aoi.name')}). To run another area, edit the aoi section of the file "
                         "(name, bbox, region), or use another experiment file.")
    problems = config.check_files(exp)
    if problems:
        raise config.ConfigError(problems, exp.file)
    return exp


def scenario_settings(sc: dict) -> dict:
    """SLPF and coefficient settings of a scenario, with the defaults filled in."""
    slpf = float(sc.get("slpf") if sc.get("slpf") is not None else 1.0)
    rf = sc.get("slpf_rainfed")
    rf = None if rf is None or float(rf) == slpf else float(rf)
    table = Path(sc["slpf_table"]) if sc.get("slpf_table") else None
    return dict(slpf=slpf, slpf_rainfed=rf, slpf_table=table, coefficients=sc.get("cultivar_coefficients") or {})


def run_tag(cultivar: str, n: int, sc: dict) -> str:
    """Folder-name tag, as the flag-only runs named them; a scenario not named
    'baseline' adds its name."""
    s = scenario_settings(sc)
    tag = "" if (cultivar, n) == ("IR 58", 140) else f"_{cultivar.replace(' ', '').lower()}_n{n}"
    if s["slpf"] != 1.0 or s["slpf_rainfed"] is not None:
        tag += f"_slpf{s['slpf']:g}"
    if s["slpf_rainfed"] is not None:
        tag += f"_rf{s['slpf_rainfed']:g}"
    if s["slpf_table"]:
        tag += f"_{s['slpf_table'].stem}"
    co = s["coefficients"]
    for k, v in co.items():
        if not isinstance(v, dict):
            tag += f"_{k.lower()}{float(v):g}"
    if sc["name"] != "baseline":
        tag += f"_{sc['name']}"
    return tag


def cultivar_overrides(exp, cultivar: str, sc: dict) -> dict | None:
    """{INGENO: {coefficient: value}} for this run's cultivar."""
    co = scenario_settings(sc)["coefficients"]
    flat = {k: v for k, v in co.items() if not isinstance(v, dict)}
    own = co.get(cultivar) if isinstance(co.get(cultivar), dict) else {}
    both = {**flat, **own}
    return {exp.data["cultivars"][cultivar]: both} if both else None


def environment_changes(sc: dict) -> dict:
    """Scenario environment -> FileX columns {"ECO2": ("R", 550), ...}."""
    out = {}
    for var, how in (sc.get("environment") or {}).items():
        (change, amount), = how.items()
        out[config.ENVIRONMENT[var]] = (config.CHANGE[change], amount)
    return out


def legacy_args(exp, cultivar, n, sc, out) -> dict:
    """run_info.json "args": the keys the flag-only runner wrote, read by outputs.py,
    fit_slpf.py, compare_districts.py and docs/scripts/export_data.py."""
    s = scenario_settings(sc)
    d, se, rn = exp.data, exp.data["seasons"], exp.data["run"]
    tcldf = s["coefficients"].get("TCLDF")
    return {k: str(v) for k, v in dict(
        aoi=exp.aoi, mode=se["mode"], years=list(d["years"]), zone_table=se["zone_table"],
        onset_years=se["onset"]["years"], sample=rn["sample"], seed=rn["seed"], block=rn["years_per_batch"],
        chunk_size=rn["chunk_size"], workers=rn["workers"], cultivar=cultivar, n=n, out=out,
        tcldf=None if tcldf is None else float(tcldf), slpf=s["slpf"], slpf_rainfed=s["slpf_rainfed"],
        slpf_table=s["slpf_table"], scenario=sc["name"], environment=sc.get("environment"),
        config=exp.file).items()}


def one_combination(exp, cultivar, n, sc) -> config.Experiment:
    """The experiment restricted to one cultivar, N rate and scenario (for experiment.yml)."""
    d = copy.deepcopy(exp.data)
    d["cultivars"] = {cultivar: d["cultivars"][cultivar]}
    d["treatments"]["nitrogen_kg"] = [n]
    d["scenarios"] = [copy.deepcopy(sc)]
    return config.Experiment(d, exp.file, exp.lines, exp.sources)


# ----------------------------------------------------------------------------
# run
# ----------------------------------------------------------------------------
def main(argv=None):
    a = parse_args(argv)
    try:
        exp = experiment_from_args(a)
    except config.ConfigError as e:
        print(f"\n{e}\n\nNothing was run. Fix the file and try again "
              "(python scripts/run_aoi.py --check only checks it).", file=sys.stderr)
        sys.exit(2)
    config.use(exp)

    import agera5
    import management as mg
    import outputs
    from filex import FileX, with_environment
    from genotype import write_cultivar_files
    from runner import run_units
    from sol import read_sol, write_sol
    from wth import tav_amp, write_wth

    mg.ANCHOR = exp.anchor
    d, se, rn = exp.data, exp.data["seasons"], exp.data["run"]
    years, mode = exp.years, se["mode"]
    combos = exp.combinations()
    runs_dir = Path(rn["output_folder"]) if rn["output_folder"] else exp.path("cache") / "runs"
    if a.out and len(combos) > 1:
        raise SystemExit(f"--out names one run folder, but the experiment has {len(combos)} combinations "
                         "(cultivars x nitrogen_kg x scenarios); set run > output_folder instead")
    folders = [a.out or runs_dir / f"{exp.aoi}_{mode}_{years[0]}-{years[-1]}{run_tag(cv, n, sc)}"
               for cv, n, sc in combos]
    if len(set(folders)) != len(folders):
        raise SystemExit("two combinations would write to the same folder; give the scenarios different names")
    log(f"experiment {exp.file or 'built-in defaults'}: {len(combos)} run(s), {mode}, {years[0]}-{years[-1]}")
    for (cv, n, sc), out in zip(combos, folders):
        log(f"  {cv}, {n} kg N, scenario {sc['name']} -> {out}")
    if a.check:
        log("settings OK (--check: nothing run)")
        return
    t0 = time.time()
    for out in folders:
        out.mkdir(parents=True, exist_ok=True)

    st = build_strata(exp.aoi, rn["sample"], rn["seed"], exp)
    log(f"{len(st)} strata in {st.cell.nunique()} cells, {st.ha.sum():,.0f} ha "
        f"({st.groupby('water').ha.sum().round(0).to_dict()})")

    seasons = mg.seasons_from(exp)
    on = se["onset"]
    extra = {}
    if mode == "uniform":
        units = mg.uniform(st, years, seasons)
    else:
        cells = None
        if mode == "per_cell" or not se["zone_table"]:
            oy = on["years"] or d["years"]
            cells = onset_days(st, list(range(oy[0], oy[1] + 1)), on["rain_mm"], on["dasarian"])
            extra["onset.csv"] = cells
            log(f"onset: median {np.nanmedian(cells.onset_days):.0f} days after the year start, "
                f"{int(np.isnan(cells.onset_days).sum())} cells without onset (they use the default date)")
        if mode == "per_cell":
            units = mg.per_cell(st, years, mg.planting_from_onset(
                cells, cells.onset_days.to_numpy(), on["transplant_after_days"], on["next_season_after_days"],
                seasons), seasons)
        else:
            zt = se["zone_table"]
            if isinstance(zt, str):
                table = pd.read_csv(zt, dtype={"month_day": str})
            elif isinstance(zt, list):
                table = pd.DataFrame([{"zone": r["zone"], "season": r["season"], "month_day": r["date"]}
                                      for r in zt])
            else:
                zc = se["zone_column"]
                cz = cells.merge(st.drop_duplicates(["iy", "ix"])[["iy", "ix", zc]], on=["iy", "ix"])
                table = mg.zone_table_from_onset(cz, zc, on["transplant_after_days"],
                                                 on["next_season_after_days"], seasons=seasons)
            extra["zone_table.csv"] = table
            log(f"zone calendar for {table.zone.nunique()} zones")
            units = mg.by_zone(st, years, table, se["zone_column"], seasons)

    last = agera5.last_common_date()
    season_days = rn["season_days"]
    late = units.pdate + pd.Timedelta(days=season_days) > last
    if late.any():
        log(f"dropping {late.sum()} seasons that need weather after {last:%Y-%m-%d}")
        units = units[~late].reset_index(drop=True)

    base_template = FileX.read(exp.template_file)
    soil_file = exp.path("cache") / "soils" / f"{exp.aoi}.SOL"
    base_profiles = read_sol(soil_file)
    missing = set(st.soil_id) - set(base_profiles)
    if missing:
        raise SystemExit(f"{len(missing)} soil profiles missing from {soil_file.name}, e.g. {sorted(missing)[:3]}")

    # one job per combination: units with their template treatment, soils, cultivar file, template
    jobs = []
    for (cv, n, sc), out in zip(combos, folders):
        s = scenario_settings(sc)
        u = units.copy()
        ingeno = d["cultivars"][cv]
        trno_w, over = {}, {}
        for w in sorted(set(u.water)):
            trno_w[w], over = mg.template_treatment(base_template, exp.water_level(w), exp.n_level(n), ingeno, cv)
        u["trno_template"] = u.water.map(trno_w)
        for k, v in over.items():
            u[k] = v
        u["cultivar"], u["n_kg"] = cv, n
        profiles = copy.deepcopy(base_profiles)
        for _p in profiles.values():
            _p.surface["SLPF"] = s["slpf"]
        if s["slpf_rainfed"] is not None:      # same soil, own SLPF: a copy per cell with ID XR<iy><ix>
            rf = u.water == "rainfed"
            u.loc[rf, "soil_id"] = "XR" + u.loc[rf, "soil_id"].str[2:]
            for sid in sorted(set(u.loc[rf, "soil_id"])):
                p = copy.deepcopy(profiles["XS" + sid[2:]])
                p.id = sid
                p.surface["SLPF"] = s["slpf_rainfed"]
                profiles[sid] = p
        if s["slpf_table"]:                    # SLPF per stratum: irrigated on XS, rainfed on an XR copy
            tab = pd.read_csv(s["slpf_table"]).set_index(["iy", "ix", "water"]).slpf
            need = u.drop_duplicates(["iy", "ix", "water"]).set_index(["iy", "ix", "water"]).index
            lack = need.difference(tab.index)
            if len(lack):
                raise SystemExit(f"{len(lack)} strata missing from {s['slpf_table']}, e.g. {list(lack[:3])}")
            rf = u.water == "rainfed"
            u.loc[rf, "soil_id"] = "XR" + u.loc[rf, "soil_id"].str[2:]
            for iy, ix, water in need:
                sid = f"{'XR' if water == 'rainfed' else 'XS'}{iy:04d}{ix:04d}"
                p = profiles[sid] if water != "rainfed" else copy.deepcopy(profiles["XS" + sid[2:]])
                p.id = sid
                p.surface["SLPF"] = float(tab[(iy, ix, water)])
                profiles[sid] = p
            log(f"SLPF from {s['slpf_table'].name}: {tab.reindex(need).min():.3f}-{tab.reindex(need).max():.3f}")
        env = environment_changes(sc)
        template = with_environment(base_template, env, sc["name"]) if env else base_template
        if env:
            log(f"scenario {sc['name']}: environment {env}")
        st.to_csv(out / "strata.csv", index=False)
        for name, df in extra.items():
            df.to_csv(out / name, index=False)
        u.to_csv(out / "units.csv", index=False)
        header = ("# Resolved settings of this run folder: one cultivar, one N rate, one scenario.\n"
                  "# configs/rice-java.yml explains every setting. Re-run exactly this folder with\n"
                  f"#   python scripts/run_aoi.py --config \"{(out / 'experiment.yml').as_posix()}\" "
                  f"--out \"{out.as_posix()}\"\n")
        (out / "experiment.yml").write_text(one_combination(exp, cv, n, sc).to_yaml(header), encoding="utf-8")
        jobs.append(dict(cv=cv, n=n, sc=sc, out=out, units=u, profiles=profiles, template=template,
                         cul_over=cultivar_overrides(exp, cv, sc), summaries=[], failures=[], t0=time.time()))
    log(f"{len(units)} seasons per run ({units.groupby(['water', 'season']).size().to_dict()})")

    weather = {}      # filled per block: dates, values {VAR: (time, cell)}, column of each cell
    crop = d["template"]["crop_model"]

    def writer(job):
        def write_inputs(run_dir, u):
            dates, w, col = weather["dates"], weather["values"], weather["col"]
            cells = u.drop_duplicates("cell")[["cell", "wsta", "iy", "ix", "lat", "lon"]]
            for c in cells.itertuples():
                df = agera5.cell_frame(dates, w, col[c.cell])
                tav, amp = tav_amp(df.TMAX, df.TMIN, df.index)
                write_wth(run_dir / f"{c.wsta}.WTH", df, "AGE5", c.lat, c.lon, tav=tav, amp=amp,
                          refht=2.0, wndht=agera5.WIND_HEIGHT, title=f"AgERA5 cell {c.iy},{c.ix}")
            write_sol(run_dir / "SOIL.SOL", [job["profiles"][s] for s in sorted(set(u.soil_id))])
            write_cultivar_files(run_dir, job["cul_over"], crop)
        return write_inputs

    # Own folder under the work root, so two runs at the same time (two experiment files, say)
    # never share a chunk folder: chunk folders are numbered c00001, c00002 ... in every run.
    work_root = exp.path("work") / f"p{os.getpid()}"
    blocks = [years[i:i + rn["years_per_batch"]] for i in range(0, len(years), rn["years_per_batch"])]
    for b in blocks:
        ub = units[units.season_year.isin(b)]
        if ub.empty:
            continue
        cells = ub.drop_duplicates("cell")[["cell", "iy", "ix"]].reset_index(drop=True)
        start = ub.pdate.min() - pd.Timedelta(days=40)
        end = min(ub.pdate.max() + pd.Timedelta(days=season_days), last)
        dates, w = agera5.series(cells.iy, cells.ix, start, end)
        weather.update(dates=dates, values=w, col=dict(zip(cells.cell, range(len(cells)))))
        for job in jobs:
            tb = time.time()
            uj = job["units"][job["units"].season_year.isin(b)]
            res = run_units(uj, job["template"], writer(job), chunk_size=rn["chunk_size"], workers=rn["workers"],
                            soils=job["profiles"], ic_water=d["treatments"]["initial_soil_water"],
                            work_root=work_root)
            s = pd.concat([r.summary for r in res], ignore_index=True)
            f = pd.concat([r.failed for r in res], ignore_index=True)
            job["summaries"].append(s)
            job["failures"].append(f)
            log(f"{job['out'].name} years {b[0]}-{b[-1]}: {len(s)} done, {len(f)} failed, {len(res)} chunks, "
                f"{time.time() - tb:.0f} s")

    shutil.rmtree(work_root, ignore_errors=True)
    build = exp.path("engine") / "BUILD_INFO.txt"
    for job in jobs:
        out = job["out"]
        summary = pd.concat(job["summaries"], ignore_index=True)
        failed = pd.concat(job["failures"], ignore_index=True)
        summary.to_csv(out / "summary.csv", index=False)
        failed.to_csv(out / "failed.csv", index=False)
        hwah = pd.to_numeric(summary.HWAH, errors="coerce")
        wmean = (summary.assign(w=summary.ha * hwah).groupby(["water", "season"])
                 .apply(lambda g: g.w.sum() / g.ha[hwah[g.index].notna()].sum(), include_groups=False))
        log(f"{out.name}: area-weighted mean yield HWAH (kg/ha):\n" + wmean.round(0).to_string())
        info = dict(args=legacy_args(exp, job["cv"], job["n"], job["sc"], a.out),
                    strata=len(st), cells=int(st.cell.nunique()),
                    ha=float(st.ha.sum()), seasons=len(job["units"]), done=len(summary), failed=len(failed),
                    minutes=round((time.time() - t0) / 60, 1), template=str(exp.template_file),
                    engine=build.read_text().strip() if build.exists() else str(exp.path("engine")),
                    weather_last_day=f"{last:%Y-%m-%d}")
        (out / "run_info.json").write_text(json.dumps(info, indent=2))
        log(f"netCDF -> {outputs.write_run(out)}")
    log(f"done in {round((time.time() - t0) / 60, 1)} min -> {', '.join(str(j['out']) for j in jobs)}")


if __name__ == "__main__":
    main()

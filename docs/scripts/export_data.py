"""Export small CSV/JSON extracts and static PNGs for the xdssat documentation site.

    conda run --no-capture-output -n climate python docs/scripts/export_data.py
    ... export_data.py --run java_per_cell_1991-2020_slpf_temp_v1_tcldf19   # the run the pages call "current"
    ... export_data.py --skip-rasters                             # no 100 m PNGs (faster)

Reads (never writes) the xdssat cache (``XDSSAT_CACHE``, src/config.py):
    runs/<run>/{run_info.json, strata.csv, onset.csv, summary.csv, <run>.nc}
    checks/districts/<run>_{districts,years}.csv     (scripts/compare_districts.py)
    checks/n_cultivar_sensitivity*.csv               (spikes/n_cultivar_sensitivity.py)
    obs/bdsp_rice_{yield,area,production}_adm2.csv, obs/bdsp_rice_adm2_flags.csv
    soils/java.SOL, rasters/idn_{rice,irrigation_filled,region}.tif
and the admin2 boundaries (--adm2) and the engine's RICER048.CUL.

Writes into docs/java/data/ and docs/assets/java/. Every number on the site
comes from these files, so rerun this after a new run or calibration and then
``quarto render docs``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

SITE = Path(__file__).resolve().parents[1]
ROOT = SITE.parent
sys.path.insert(0, str(ROOT / "src"))
import config  # noqa: E402

CACHE = config.CACHE_DIR
DATA = SITE / "java" / "data"
IMAGES = SITE / "assets" / "java"
JAVA_PROVINCES = {"31": "DKI Jakarta", "32": "Jawa Barat", "33": "Jawa Tengah",
                  "34": "DI Yogyakarta", "35": "Jawa Timur", "36": "Banten"}
SYSTEMS = ["wet_irrigated", "wet_rainfed", "dry_irrigated"]
MOISTURE = 0.14  # GKG moisture, the same assumption as scripts/compare_districts.py

# Runs known to be superseded (HANDOFF.md, 2026-11-06): made before the initial
# soil N fix in filex._ic_from_soil. Shown on the site with this label.
STATUS = {
    "java_uniform_1991-2020": "stale: before the initial-N fix",
    "java_by_zone_1991-2020": "stale: before the initial-N fix",
    "java_per_cell_1991-2020_old_ic": "superseded: initial-N bug",
    "java_per_cell_1991-2020": "after the initial-N fix, SLPF 1",
    "java_per_cell_1991-2020_slpf0.73": "earlier calibration: one SLPF",
    "java_per_cell_1991-2020_slpf0.73_tcldf19": "TCLDF test (one SLPF)",
    "java_per_cell_1991-2020_slpf0.53_tcldf19": "SLPF sweep for scripts/fit_slpf.py",
    "java_per_cell_1991-2020_slpf0.63_tcldf19": "SLPF sweep for scripts/fit_slpf.py",
    "java_per_cell_1991-2020_slpf0.83_tcldf19": "SLPF sweep for scripts/fit_slpf.py",
    "java_per_cell_1991-2020_slpf0.76_rf0.58_tcldf19": "earlier calibration: irrigated/rain-fed SLPF pair",
    "java_uniform_1991-2020_slpf_temp_v1_tcldf19": "uniform calendar, current calibration",
    "java_by_zone_1991-2020_slpf_temp_v1_tcldf19": "by-zone calendar, current calibration",
}

CLI_SCRIPTS = ["fit_slpf", "check_setup", "build_engine", "build_agera5_cache", "prepare_rasters", "fetch_soilgrids",
               "build_soilgrids_cache", "prepare_soils", "make_templates", "run_aoi", "make_netcdf",
               "compare_bps", "fetch_bdsp", "clean_bdsp", "clean_moa_yields", "compare_districts"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wavg(x, w):
    x, w = np.asarray(x, float), np.asarray(w, float)
    ok = np.isfinite(x) & np.isfinite(w) & (w > 0)
    return float(np.sum(x[ok] * w[ok]) / np.sum(w[ok])) if ok.any() else np.nan


def r6(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 4)


def write_csv(df: pd.DataFrame, name: str, digits: int = 3):
    df = df.copy()
    for c in df.select_dtypes("float").columns:
        df[c] = df[c].round(digits)
    df.to_csv(DATA / name, index=False)
    log(f"  {name}: {len(df)} rows, {(DATA / name).stat().st_size / 1024:.0f} KB")


def write_json(obj, name: str):
    (DATA / name).write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    log(f"  {name}: {(DATA / name).stat().st_size / 1024:.0f} KB")


def run_dirs():
    return sorted(p for p in (CACHE / "runs").iterdir() if (p / "run_info.json").is_file())


def run_nc(run: Path):
    named = run / f"{run.name}.nc"
    if named.is_file():
        return named
    ncs = sorted(run.glob("*.nc"))
    return ncs[0] if ncs else None


# --------------------------------------------------------------------- runs
def export_runs(current: str):
    import xarray as xr
    log("runs")
    runs, years = [], []
    for run in run_dirs():
        info = json.loads((run / "run_info.json").read_text())
        rec = {"run": run.name, "current": run.name == current,
               "status": "current" if run.name == current else STATUS.get(run.name, ""),
               "mode": info["args"].get("mode"), "cultivar": info["args"].get("cultivar", "IR 58"),
               "n": info["args"].get("n", "140"), "slpf": info["args"].get("slpf", "None"), "tcldf": info["args"].get("tcldf", "None"),
               "slpf_rainfed": info["args"].get("slpf_rainfed", "None"),
               "slpf_table": Path(str(info["args"].get("slpf_table", "None"))).name
               if info["args"].get("slpf_table", "None") not in ("None", None) else "None",
               "years": info["args"].get("years"), "strata": info.get("strata"), "cells": info.get("cells"),
               "ha": info.get("ha"), "seasons": info.get("seasons"), "done": info.get("done"),
               "failed": info.get("failed"), "minutes": info.get("minutes"),
               "workers": info["args"].get("workers"), "chunk_size": info["args"].get("chunk_size")}
        nc = run_nc(run)
        if nc is not None:
            ds = xr.open_dataset(nc)
            rec["nc_file"] = nc.name
            rec["nc_mb"] = round(nc.stat().st_size / 2**20, 2)
            rec["nc_created"] = ds.attrs.get("date_created", "")
            for s in ds.system.values:
                p = ds["production"].sel(system=s)
                h = ds["harvested_area"].sel(system=s)
                rec[f"yield_{s}"] = r6(float(p.sum()) / float(h.sum()) * 1000)
                py = p.sum(["lat", "lon"]).values
                hy = h.sum(["lat", "lon"]).values
                for t, a, b in zip(ds.season_year.values, py, hy):
                    years.append({"run": run.name, "system": str(s), "season_year": int(t),
                                  "yield_kg_ha": a / b * 1000 if b > 0 else np.nan})
            ds.close()
        runs.append(rec)
    write_json(runs, "runs.json")
    write_csv(pd.DataFrame(years), "run_years.csv", 1)
    return runs


# ------------------------------------------------------------------- grid
def export_grid(run: Path):
    import xarray as xr
    import sol  # noqa: F401  (src/sol.py, read only)
    log(f"grid of {run.name}")
    strata = pd.read_csv(run / "strata.csv")
    aez_names = pd.read_csv(CACHE / "rasters" / "idn_aez_classes.csv").set_index("code").name
    cells = (strata.groupby(["iy", "ix"])
             .agg(lat=("lat", "first"), lon=("lon", "first"), aez=("aez", "first")).reset_index())
    ha = strata.pivot_table(index=["iy", "ix"], columns="water", values="ha", aggfunc="sum").fillna(0)
    cells = cells.join(ha.rename(columns=lambda c: f"ha_{c}"), on=["iy", "ix"])
    for c in ("ha_irrigated", "ha_rainfed"):
        if c not in cells:
            cells[c] = 0.0
    cells["ha"] = cells.ha_irrigated + cells.ha_rainfed
    cells["irr_share"] = cells.ha_irrigated / cells.ha
    cells["aez_name"] = cells.aez.map(aez_names)
    on = run / "onset.csv"
    if on.is_file():
        o = pd.read_csv(on)[["iy", "ix", "onset_days", "onset_date_md", "years_with_onset"]]
        cells = cells.merge(o, on=["iy", "ix"], how="left")

    s = pd.read_csv(run / "summary.csv", low_memory=False,
                    usecols=["iy", "ix", "season", "water", "HWAH", "PDAT", "MDAT", "TMINA", "TMAXA", "SRADA",
                             "PRCP", "ha"])
    for c in ("PDAT", "MDAT"):
        s[c] = pd.to_datetime(s[c].astype(str), format="%Y%j", errors="coerce")
    s["len"] = (s.MDAT - s.PDAT).dt.days
    s["system"] = s.season + "_" + s.water
    m = s.groupby(["iy", "ix", "system"])[["HWAH", "len", "TMINA", "TMAXA", "SRADA", "PRCP"]].mean()
    for sysn in SYSTEMS:
        if sysn in m.index.get_level_values("system"):
            t = m.xs(sysn, level="system")
            cells = cells.join(t.rename(columns=lambda c: f"{c.lower()}_{sysn}"), on=["iy", "ix"])
    # cell means over all seasons, area-weighted by stratum ha (for drivers)
    allm = s.groupby(["iy", "ix"]).apply(
        lambda g: pd.Series({"hwah_all": wavg(g.HWAH, g.ha), "len_all": wavg(g.len, g.ha),
                             "tmina_all": wavg(g.TMINA, g.ha), "srada_all": wavg(g.SRADA, g.ha)}),
        include_groups=False)
    cells = cells.join(allm, on=["iy", "ix"])

    # soil: top layer of the profile that enters the engine
    profs = sol.read_sol(CACHE / "soils" / "java.SOL")
    rows, layers = [], []
    for pid, p in profs.items():
        iy, ix = int(pid[2:6]), int(pid[6:10])
        L = p.layers
        top = L.iloc[0]
        rows.append({"iy": iy, "ix": ix, "texture": p.texture, "sloc_top": top.SLOC, "slcl_top": top.SLCL,
                     "slsi_top": top.SLSI, "slll_top": top.SLLL, "sdul_top": top.SDUL, "ssat_top": top.SSAT,
                     "sbdm_top": top.SBDM, "ssks_top": top.SSKS,
                     "paw_mm": float(((L.SDUL - L.SLLL) * np.diff(np.r_[0, L.SLB]) * 10).sum())})
        prev = 0.0
        for _, r in L.iterrows():
            layers.append({"slb": r.SLB, "mid": (prev + r.SLB) / 2, "slll": r.SLLL, "sdul": r.SDUL,
                           "ssat": r.SSAT, "srgf": r.SRGF, "sloc": r.SLOC, "sbdm": r.SBDM, "ssks": r.SSKS,
                           "slcl": r.SLCL})
            prev = r.SLB
    cells = cells.merge(pd.DataFrame(rows), on=["iy", "ix"], how="left")
    write_csv(cells, "grid_cells.csv", 3)
    lay = pd.DataFrame(layers)
    q = lay.groupby("slb").agg(**{f"{v}_{k}": (v, f) for v in ("slll", "sdul", "ssat", "srgf", "sloc", "sbdm",
                                                                  "ssks", "slcl")
                                 for k, f in (("p10", lambda x: x.quantile(.1)), ("p50", "median"),
                                              ("p90", lambda x: x.quantile(.9)))})
    q["mid"] = lay.groupby("slb").mid.first()
    q["n"] = lay.groupby("slb").size()
    write_csv(q.reset_index(), "soil_layers.csv", 4)

    # yearly yield per cell and system, compact JSON for the netCDF explorer
    ds = xr.open_dataset(run_nc(run))
    lat = np.round(ds.lat.values, 1)
    lon = np.round(ds.lon.values, 1)
    li = {v: i for i, v in enumerate(lat)}
    lo = {v: i for i, v in enumerate(lon)}
    iy_ = [li[round(v, 1)] for v in cells.lat]
    ix_ = [lo[round(v, 1)] for v in cells.lon]
    out = {"run": run.name, "years": [int(v) for v in ds.season_year.values], "lat": cells.lat.tolist(),
           "lon": cells.lon.tolist(), "systems": {}}
    for sysn in ds.system.values:
        a = ds["yield"].sel(system=sysn).values[:, iy_, ix_]  # (time, cell)
        out["systems"][str(sysn)] = [[None if not np.isfinite(v) else int(round(v)) for v in row] for row in a]
    schema = []
    for name, v in list(ds.data_vars.items()) + list(ds.coords.items()):
        schema.append({"name": name, "dims": ", ".join(v.dims), "dtype": str(v.dtype),
                       "units": v.attrs.get("units", ""), "long_name": v.attrs.get("long_name", ""),
                       "standard_name": v.attrs.get("standard_name", ""),
                       "kind": "coordinate" if name in ds.coords else "data"})
    attrs = {k: str(v) for k, v in ds.attrs.items() if k != "xdssat_run"}
    ds.close()
    write_json(out, "grid_yearly.json")
    write_csv(pd.DataFrame(schema), "nc_schema.csv")
    write_json(attrs, "nc_attrs.json")
    return cells


# -------------------------------------------------------------- boundaries
# Box the admin lines are cut to: the Java AOI (config.DEFAULTS aoi bbox, 105.0, -9.0, 114.7, -5.0) plus
# 1 degree, so the cut edges fall outside every map frame (the OJS maps also clip to the frame).
ADMIN_BOX = (104.0, -10.0, 115.7, -4.0)
SIMPLIFY_DEG = 0.002


def export_boundaries(adm1: Path, adm2: Path):
    """Admin1 and admin2 lines for every map, plus the Java districts and outline.

    Writes provinces.geojson (admin1) and districts.geojson (admin2), both cut to
    ADMIN_BOX and simplified by SIMPLIFY_DEG; districts carry code, name,
    province and java (inside the six Java provinces). Returns both as
    GeoDataFrames for the static PNGs.
    """
    import geopandas as gpd
    import shapely
    from shapely.geometry import MultiPolygon, Polygon, mapping
    from shapely.geometry.polygon import orient
    from shapely.ops import unary_union
    log("boundaries")

    def load(path):
        g = gpd.read_file(path, bbox=ADMIN_BOX).to_crs(4326)
        g["geometry"] = g.geometry.force_2d().clip_by_rect(*ADMIN_BOX)
        g = g[~g.geometry.is_empty].copy()
        g["geometry"] = g.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)
        return g[~g.geometry.is_empty]

    def cw(geom):
        # d3-geo wants clockwise exterior rings (the opposite of RFC 7946). Snap to
        # 1e-4 degrees first, so the rounding in rnd() cannot flip a tiny ring.
        geom = shapely.set_precision(shapely.force_2d(geom), 1e-4)
        if geom.geom_type == "Polygon":
            return orient(geom, sign=-1.0)
        parts = [p for p in getattr(geom, "geoms", []) if p.geom_type == "Polygon" and p.area > 1e-6]
        return MultiPolygon([orient(p, sign=-1.0) for p in parts])

    def rnd(obj):
        if isinstance(obj, (list, tuple)):
            if obj and isinstance(obj[0], float):
                return [round(obj[0], 4), round(obj[1], 4)]
            return [rnd(o) for o in obj]
        return obj

    def feature(geom, props):
        gm = mapping(cw(geom))
        return {"type": "Feature", "properties": props,
                "geometry": {"type": gm["type"], "coordinates": rnd(gm["coordinates"])}}

    def name(v):
        return v if isinstance(v, str) else ""

    g2 = load(adm2)
    g2["a1bps"] = g2.a1bps.astype(str)
    feats = [feature(r.geometry, {"code": str(r.a2bps), "name": name(r.a2name), "province": name(r.a1name),
                                  "java": r.a1bps in JAVA_PROVINCES})
             for _, r in g2.iterrows() if not cw(r.geometry).is_empty]
    write_json({"type": "FeatureCollection", "features": feats}, "districts.geojson")

    g1 = load(adm1)
    namecol = "a1name" if "a1name" in g1 else ("WADMPR" if "WADMPR" in g1 else None)
    feats = [feature(r.geometry, {"name": name(r[namecol]) if namecol else ""})
             for _, r in g1.iterrows() if not cw(r.geometry).is_empty]
    write_json({"type": "FeatureCollection", "features": feats}, "provinces.geojson")

    java = g2[g2.a1bps.isin(JAVA_PROVINCES)]
    u = unary_union(java.geometry.buffer(0.002)).simplify(0.01)
    parts = [Polygon(p.exterior) for p in getattr(u, "geoms", [u]) if p.area > 0.002]  # drop islets and holes
    gm = mapping(cw(MultiPolygon(parts)))
    write_json({"type": "Feature", "properties": {"name": "Java"},
                "geometry": {"type": gm["type"], "coordinates": rnd(gm["coordinates"])}}, "java_outline.geojson")
    return g1, g2


# ---------------------------------------------------------------- districts
def _cd_module():
    spec = importlib.util.spec_from_file_location("compare_districts", ROOT / "scripts" / "compare_districts.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def highpass(x: pd.Series, window=5):
    return x - x.rolling(window, center=True, min_periods=3).mean()


def export_districts(current: str, cells: pd.DataFrame | None, drivers: bool):
    log("district comparisons")
    D = CACHE / "checks" / "districts"
    dist, yrs, java, summ = [], [], [], {}
    for f in sorted(D.glob("*_districts.csv")):
        run = f.name[: -len("_districts.csv")]
        d = pd.read_csv(f, dtype={"code": str})
        d["run"] = run
        dist.append(d)
        yf = D / f"{run}_years.csv"
        if not yf.is_file():
            continue
        y = pd.read_csv(yf, dtype={"code": str})
        y["run"] = run
        yrs.append(y[["run", "code", "year", "sim_ku_ha", "obs_ku_ha", "harvested_ha", "obs_harvested_ha",
                      "rice_ha"]])
        # Java series as printed by compare_districts.py: rice-ha weighted over districts with data
        jj = y.dropna(subset=["obs_ku_ha"])
        tot = jj.groupby("year").apply(lambda g: pd.Series({
            "sim": wavg(g.sim_ku_ha, g.rice_ha), "obs": wavg(g.obs_ku_ha, g.rice_ha),
            "districts": g.code.nunique()}), include_groups=False).reset_index()
        tot["run"] = run
        java.append(tot)
        t = tot.set_index("year")
        r_dt = _detr_r(t.sim, t.obs)
        hp = pd.concat([highpass(t.sim), highpass(t.obs)], axis=1).dropna()
        w = d.rice_ha
        ri = CACHE / "runs" / run / "run_info.json"
        args = json.loads(ri.read_text())["args"] if ri.is_file() else {}
        slpf = args.get("slpf", "None")
        tcldf = args.get("tcldf", "None")
        slpf_rf = args.get("slpf_rainfed", "None")
        table = args.get("slpf_table", "None")
        table = None if table in ("None", None) else Path(str(table)).stem
        if table:
            sl = f"SLPF table {table}"
        else:
            sl = (f"SLPF {slpf if slpf not in ('None', None) else '1 (default)'}"
                  + (f", rain-fed {slpf_rf}" if slpf_rf not in ("None", None) else ""))
        label = f"{run} ({sl}" + (f", TCLDF {float(tcldf):g}" if tcldf not in ("None", None) else "") + ")"
        summ[run] = {
            "label": label, "slpf": None if slpf in ("None", None) else float(slpf),
            "tcldf": None if tcldf in ("None", None) else float(tcldf),
            "slpf_rainfed": None if slpf_rf in ("None", None) else float(slpf_rf),
            "slpf_table": table,
            "within10": r6((np.abs(d.ratio - 1) <= 0.10).mean()),
            "rmse_log": r6(np.sqrt(wavg(np.log(d.ratio) ** 2, w))),
            "cultivar": args.get("cultivar"), "n": args.get("n"), "mode": args.get("mode"),
            "districts": int(len(d)), "rice_ha": float(d.rice_ha.sum()),
            "year_min": int(y.year.min()), "year_max": int(y.year.max()),
            "level_area_weighted": r6(wavg(d.ratio, w)),
            "ratio_p10": r6(d.ratio.quantile(.1)), "ratio_p50": r6(d.ratio.median()),
            "ratio_p90": r6(d.ratio.quantile(.9)),
            "java_obs": r6(t.obs.mean()), "java_sim": r6(t.sim.mean()),
            "java_obs_trend_decade": r6(np.polyfit(t.index, t.obs, 1)[0] * 10),
            "java_sim_trend_decade": r6(np.polyfit(t.index, t.sim, 1)[0] * 10),
            "java_r_detrended": r6(r_dt), "java_r_highpass": r6(hp.iloc[:, 0].corr(hp.iloc[:, 1])),
            "spatial_r": r6(d[["obs_mean", "sim_mean"]].corr().iloc[0, 1]),
            "spatial_spearman": r6(d[["obs_mean", "sim_mean"]].rank().corr().iloc[0, 1]),
            "obs_sd_districts": r6(d.obs_mean.std()), "sim_sd_districts": r6(d.sim_mean.std()),
            "r_detrended_median": r6(d.r_detrended.median()), "r_detrended_pos": r6((d.r_detrended.dropna() > 0).mean()),
            "r_highpass_median": r6(d.r_highpass.median()), "r_highpass_pos": r6((d.r_highpass.dropna() > 0).mean()),
            "area_ratio_median": r6(d.area_ratio.median()),
            "province_ratio": {p: r6(wavg(g.ratio, g.rice_ha)) for p, g in d.groupby("province")},
        }
    dist = pd.concat(dist, ignore_index=True)

    if drivers:
        log("  district drivers via compare_districts.pieces() (reads 100 m rasters)")
        try:
            pc = _cd_module().pieces()
            drvs = []
            for run in sorted(set(dist.run)):
                sf = CACHE / "runs" / run / "summary.csv"
                if not sf.is_file():
                    continue
                s = pd.read_csv(sf, low_memory=False,
                                usecols=["iy", "ix", "water", "PDAT", "MDAT", "TMINA", "SRADA", "HWAH"])
                for k in ("PDAT", "MDAT"):
                    s[k] = pd.to_datetime(s[k].astype(str), format="%Y%j", errors="coerce")
                s["len"] = (s.MDAT - s.PDAT).dt.days
                cw = s.groupby(["iy", "ix", "water"])[["len", "TMINA", "SRADA", "HWAH"]].mean().reset_index()
                m = pc.merge(cw, on=["iy", "ix", "water"])
                drv = m.groupby("code").apply(lambda g: pd.Series({
                    "season_len": wavg(g.len, g.ha), "tmina": wavg(g.TMINA, g.ha), "srada": wavg(g.SRADA, g.ha),
                    "irr_share": g.ha[g.water == "irrigated"].sum() / g.ha.sum()}), include_groups=False)
                drv = drv.reset_index()
                drv["run"] = run
                drvs.append(drv)
            dist = dist.merge(pd.concat(drvs), on=["run", "code"], how="left")
        except Exception as e:  # the scripts may be mid-edit in another session
            log(f"  drivers skipped: {e!r}")
    write_csv(dist, "districts.csv", 4)
    write_csv(pd.concat(yrs, ignore_index=True), "district_years.csv", 2)
    write_csv(pd.concat(java, ignore_index=True), "java_years.csv", 3)
    return summ


def _detr_r(x: pd.Series, y: pd.Series):
    t = x.index.to_numpy(float)
    if len(t) < 8:
        return np.nan
    rx = x - np.polyval(np.polyfit(t, x, 1), t)
    ry = y - np.polyval(np.polyfit(t, y, 1), t)
    return float(np.corrcoef(rx, ry)[0, 1])


# --------------------------------------------------------------- observed
def export_obs():
    log("BDSP observations, Java")
    O = CACHE / "obs"
    tabs = {}
    for v in ("yield", "area", "production"):
        t = pd.read_csv(O / f"bdsp_rice_{v}_adm2.csv", dtype={"code": str, "province_code": str})
        t = t[t.province_code.isin(JAVA_PROVINCES)]
        yrs = [c for c in t.columns if c.isdigit()]
        tabs[v] = t.melt(id_vars=["code", "name", "province_code"], value_vars=yrs, var_name="year",
                         value_name=v).astype({"year": int})
    o = tabs["yield"].merge(tabs["area"][["code", "year", "area"]], on=["code", "year"], how="outer") \
                     .merge(tabs["production"][["code", "year", "production"]], on=["code", "year"], how="outer")
    o["province"] = o.code.str[:2].map(JAVA_PROVINCES)
    prov = o.groupby(["province", "year"]).apply(lambda g: pd.Series({
        "yield_p_over_a": (g.production.sum() / g.area.sum() * 10) if g[["production", "area"]].notna().all(axis=1).any() else np.nan,
        "yield_mean": g["yield"].mean(),
        "districts_yield": int(g["yield"].notna().sum()),
        "districts_area": int(g["area"].notna().sum()),
        "area_ha": g.area.sum(min_count=1), "production_t": g.production.sum(min_count=1)}),
        include_groups=False).reset_index()
    # province P/A only over districts where both are reported
    both = o.dropna(subset=["production", "area"])
    pa = both.groupby(["province", "year"]).apply(lambda g: g.production.sum() / g.area.sum() * 10,
                                                   include_groups=False).rename("yield_p_over_a")
    prov = prov.drop(columns="yield_p_over_a").join(pa, on=["province", "year"])
    write_csv(prov, "obs_province_years.csv", 2)
    w = o[(o.year >= 1992) & (o.year <= 2020)]
    dm = w.groupby(["code", "name", "province"]).agg(obs_mean=("yield", "mean"), obs_years=("yield", "count"),
                                                      area_mean=("area", "mean")).reset_index()
    write_csv(dm, "obs_district_means.csv", 2)
    f = pd.read_csv(O / "bdsp_rice_adm2_flags.csv", dtype={"code": str})
    f = f[f.code.str[:2].isin(JAVA_PROVINCES)]
    f["province"] = f.code.str[:2].map(JAVA_PROVINCES)
    fl = f.groupby(["rule", "province"]).size().rename("n").reset_index()
    write_csv(fl, "obs_flags_java.csv")
    allf = pd.read_csv(O / "bdsp_rice_adm2_flags.csv", dtype={"code": str}).groupby("rule").size()
    write_csv(allf.rename("n").reset_index(), "obs_flags_all.csv")
    yy = o.dropna(subset=["yield"]).year
    return {"java_districts": int(o.code.nunique()), "years": [int(yy.min()), int(yy.max())],
            "district_years_yield": int(o["yield"].notna().sum())}


# ------------------------------------------------------------ sensitivity
def export_sensitivity():
    log("sensitivity checks")
    rows, cells = [], []
    for f in sorted((CACHE / "checks").glob("n_cultivar_sensitivity*.csv")):
        tag = f.stem.replace("n_cultivar_sensitivity", "").strip("_")
        p = {"soc": np.nan, "slpf": np.nan, "tcldf": np.nan}
        for k, v in re.findall(r"(soc|slpf|tcldf)([0-9.]+)", tag):
            p[k] = float(v)
        s = pd.read_csv(f, low_memory=False, usecols=["iy", "ix", "cultivar", "n_kg", "water", "ha", "HWAH",
                                                      "season_days", "TMINA", "season_year"])
        for (cv, n, wtr), g in s.groupby(["cultivar", "n_kg", "water"]):
            rows.append({"file": f.name, "tag": tag or "first", **p, "cultivar": cv, "n_kg": n, "water": wtr,
                         "hwah": wavg(g.HWAH, g.ha), "season_days": wavg(g.season_days, g.ha),
                         "seasons": len(g), "cells": g[["iy", "ix"]].drop_duplicates().shape[0],
                         "years": f"{g.season_year.min()}-{g.season_year.max()}"})
        for (cv, n), g in s.groupby(["cultivar", "n_kg"]):
            rows.append({"file": f.name, "tag": tag or "first", **p, "cultivar": cv, "n_kg": n, "water": "all",
                         "hwah": wavg(g.HWAH, g.ha), "season_days": wavg(g.season_days, g.ha),
                         "seasons": len(g), "cells": g[["iy", "ix"]].drop_duplicates().shape[0],
                         "years": f"{g.season_year.min()}-{g.season_year.max()}"})
        if np.isfinite(p["tcldf"]) or (p["soc"] == 1 and p["slpf"] == 0.73):
            c = s[(s.cultivar == "IR 58") & (s.n_kg == 140)].groupby(["iy", "ix", "water"]).agg(
                hwah=("HWAH", "mean"), tmina=("TMINA", "mean"), season_days=("season_days", "mean"),
                ha=("ha", "first")).reset_index()
            c["tcldf"] = p["tcldf"]
            cells.append(c)
    write_csv(pd.DataFrame(rows), "sensitivity.csv", 2)
    if cells:
        write_csv(pd.concat(cells, ignore_index=True), "tcldf_cells.csv", 2)


# ---------------------------------------------------------------- SLPF fit
def export_fit():
    """scripts/fit_slpf.py results: one SLPF vs an irrigated/rain-fed pair, and the objective surface."""
    D = CACHE / "checks" / "districts"
    if not (D / "fit_slpf.json").is_file():
        log("  no fit_slpf.json, skipped")
        return
    log("SLPF fit")
    write_json(json.loads((D / "fit_slpf.json").read_text()), "fit_slpf.json")
    if (D / "fit_slpf_grid.csv").is_file():
        write_csv(pd.read_csv(D / "fit_slpf_grid.csv"), "fit_slpf_grid.csv", 5)


def export_slpf_table(run: Path):
    """SLPF per stratum of a run made with --slpf-table, with cell coordinates (for maps)."""
    info = json.loads((run / "run_info.json").read_text())
    t = info["args"].get("slpf_table", "None")
    if t in ("None", None) or not Path(t).is_file():
        return
    log("SLPF table of the current run")
    tab = pd.read_csv(t)
    st = pd.read_csv(run / "strata.csv")[["iy", "ix", "lat", "lon", "water", "ha"]]
    tab = tab.merge(st, on=["iy", "ix", "water"], how="inner")
    tab["table"] = Path(t).name
    write_csv(tab, "slpf_cells.csv", 4)


# --------------------------------------------------------------- cultivars
def export_cultivars():
    log("cultivars")
    cul = Path(config.ENGINE_DIR) / "Genotype" / "RICER048.CUL"
    if not cul.is_file():
        cul = ROOT.parent / "dssat-csm-os" / "Data" / "Genotype" / "RICER048.CUL"
    lines = cul.read_text(errors="replace").splitlines()
    head = next(l for l in lines if l.startswith("@VAR#"))
    names = head[23:].split()
    rows = []
    for l in lines:
        if l[:6] in ("IB0012", "IB0015"):
            vals = l[23:].split()
            rows.append({"var": l[:6], "name": l[7:23].strip(), **dict(zip(names, vals))})
    # definitions from the comment block (! NAME  text, continued on indented lines)
    defs, cur = {}, None
    for l in lines:
        if not l.startswith("!"):
            if l.startswith("@"):
                break
            continue
        m = re.match(r"!\s(\S+)\s{2,}(.*)", l)
        if m and m.group(1) in names + ["VAR#", "VAR-NAME"]:
            cur = m.group(1)
            defs[cur] = m.group(2).strip()
        elif cur and l[1:].strip() and l.startswith("!   "):
            defs[cur] += " " + l[1:].strip()
        elif not l[1:].strip():
            cur = None
    t = pd.DataFrame(rows).set_index("var").T.reset_index().rename(columns={"index": "coefficient"})
    t["definition"] = t.coefficient.map(defs).fillna("")
    t["definition"] = t.definition.str.replace("oC", "°C", regex=False)
    t.attrs["source"] = str(cul)
    write_csv(t, "cultivars.csv")
    return str(cul)


# ---------------------------------------------------------- settings, CLI
def export_settings_cli():
    log("settings and CLI help")
    import os
    rows = [{"setting": k, "variable": env, "value": str(v), "from": "environment" if env in os.environ else "default"}
            for k, (env, v) in config.SETTINGS.items()]
    write_csv(pd.DataFrame(rows), "settings.csv")
    out = DATA / "cli"
    out.mkdir(exist_ok=True)
    for name in CLI_SCRIPTS:
        p = ROOT / "scripts" / f"{name}.py"
        if not p.is_file():
            continue
        try:
            r = subprocess.run([sys.executable, str(p), "--help"], cwd=ROOT, capture_output=True, text=True,
                               timeout=180)
            txt = r.stdout if r.returncode == 0 else f"(--help failed: exit {r.returncode})\n{r.stderr[-800:]}"
        except Exception as e:
            txt = f"(--help failed: {e!r})"
        (out / f"{name}.txt").write_text(txt.replace("\r\n", "\n"), encoding="utf-8")
    log(f"  cli/: {len(list(out.glob('*.txt')))} files")


# --------------------------------------------------------------- rasters
def export_rasters(adm1=None, adm2=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    import rasters  # src/rasters.py, read only
    log("100 m rasters (Java window)")
    R = CACHE / "rasters"
    grid, _ = rasters.IDN.window(*config.DEFAULTS["aoi"]["bbox"])
    region = rasters.read(R / "idn_region.tif", grid)
    jawa = int(pd.read_csv(R / "idn_region_classes.csv").set_index("name").code["Jawa"])
    share = rasters.read(R / "idn_rice.tif", grid).astype(np.float32)
    share[region != jawa] = 0
    irr = rasters.read(R / "idn_irrigation_filled.tif", grid, band=1)
    dist = rasters.read(R / "idn_irrigation_filled.tif", grid, band=2)
    ha = share / 100.0
    names = pd.read_csv(R / "idn_irrigation_classes.csv").drop_duplicates("code").set_index("code").name
    rows = []
    for c in sorted(np.unique(irr[ha > 0])):
        sel = (irr == c) & (ha > 0)
        rows.append({"code": int(c), "class": names.get(int(c), "none"), "ha": float(ha[sel].sum()),
                     "ha_own": float(ha[sel & (dist == 0)].sum()), "ha_filled": float(ha[sel & (dist > 0)].sum()),
                     "pixels": int(sel.sum())})
    t = pd.DataFrame(rows)
    t["share"] = t.ha / t.ha.sum()
    write_csv(t, "java_irrigation_classes.csv", 4)

    k = 5  # 500 m display blocks
    h, w = (ha.shape[0] // k) * k, (ha.shape[1] // k) * k
    blk = ha[:h, :w].reshape(h // k, k, w // k, k)
    mean_share = blk.mean(axis=(1, 3)) * 100
    left, bottom, right, top = grid.bounds
    from pyproj import Transformer
    tr = Transformer.from_crs(rasters.CRS, 4326, always_xy=True)
    lon0, lat0 = tr.transform(left, bottom)
    lon1, lat1 = tr.transform(right, top)
    ext = [lon0, lon1, lat0, lat1]

    def admin(ax):
        # same simplified lines as the interactive maps: thin admin2, bold admin1
        if adm2 is not None:
            adm2.boundary.plot(ax=ax, color="#555555", linewidth=0.25)
        if adm1 is not None:
            adm1.boundary.plot(ax=ax, color="#111111", linewidth=0.8)
        ax.set_xlim(ext[0], ext[1])
        ax.set_ylim(ext[2], ext[3])

    def save(fig, name):
        fig.savefig(IMAGES / name, dpi=110, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        log(f"  assets/java/{name}: {(IMAGES / name).stat().st_size / 1024:.0f} KB")

    fig, ax = plt.subplots(figsize=(14, 5.2))
    img = np.where(mean_share > 0, mean_share, np.nan)
    im = ax.imshow(img, extent=ext, cmap="YlGn", vmin=0, vmax=100, interpolation="nearest")
    ax.set_xlabel("longitude (°E)")
    ax.set_ylabel("latitude (°)")
    admin(ax)
    ax.set_title("Rice field share of each 500 m block (mean of 25 one-hectare pixels), Java, ATR/BPN 2024",
                 fontsize=11, loc="left")
    fig.colorbar(im, ax=ax, shrink=0.7, label="% of area under rice")
    save(fig, "java_rice_share.png")

    classes = sorted(c for c in names.index if c in t.code.values)
    stack = np.stack([np.where(irr[:h, :w] == c, ha[:h, :w], 0).reshape(h // k, k, w // k, k).sum(axis=(1, 3))
                      for c in classes])
    dom = np.array(classes)[stack.argmax(axis=0)].astype(float)
    dom[stack.sum(axis=0) == 0] = np.nan
    colors = ["#1f5f99", "#3a8fd0", "#8cc3ea", "#e0a030"][: len(classes)]
    fig, ax = plt.subplots(figsize=(14, 5.2))
    cmap = ListedColormap(colors)
    ax.imshow(dom, extent=ext, cmap=cmap, vmin=min(classes) - 0.5, vmax=max(classes) + 0.5,
              interpolation="nearest")
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=col, label=names[c]) for c, col in zip(classes, colors)],
              loc="lower left", fontsize=9, frameon=True)
    ax.set_xlabel("longitude (°E)")
    ax.set_ylabel("latitude (°)")
    admin(ax)
    ax.set_title("Dominant MoA irrigation class of the rice in each 500 m block (gaps filled from the nearest class)",
                 fontsize=11, loc="left")
    save(fig, "java_irrigation_class.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="java_per_cell_1991-2020_slpf_temp_v1_tcldf19",
                    help="run folder under XDSSAT_CACHE/runs that the pages treat as current")
    ap.add_argument("--adm1", type=Path, help="default <paths > boundaries>/idn_bnd_adm1.shp")
    ap.add_argument("--adm2", type=Path, help="default <paths > boundaries>/idn_bnd_adm2.shp")
    ap.add_argument("--skip-rasters", action="store_true", help="skip the 100 m PNGs and the irrigation class table")
    ap.add_argument("--skip-cli", action="store_true", help="do not refresh the --help texts")
    a = ap.parse_args()
    a.adm1 = a.adm1 or config.BOUNDARIES_DIR / "idn_bnd_adm1.shp"
    a.adm2 = a.adm2 or config.BOUNDARIES_DIR / "idn_bnd_adm2.shp"
    DATA.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(exist_ok=True)
    t0 = time.time()
    run = CACHE / "runs" / a.run
    runs = export_runs(a.run)
    cells = export_grid(run)
    adm1, adm2 = export_boundaries(a.adm1, a.adm2)
    summ = export_districts(a.run, cells, drivers=True)
    obs = export_obs()
    export_sensitivity()
    export_fit()
    export_slpf_table(run)
    cul = export_cultivars()
    if not a.skip_cli:
        export_settings_cli()
    if not a.skip_rasters:
        export_rasters(adm1, adm2)
    cur = next(r for r in runs if r["run"] == a.run)
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M"), "cache": str(CACHE), "current_run": a.run,
            "current": cur, "districts": summ, "obs": obs, "cultivar_file": cul, "moisture": MOISTURE,
            "adm1": str(a.adm1), "adm2": str(a.adm2), "export_script": "docs/scripts/export_data.py"}
    write_json(json.loads(json.dumps(meta, default=lambda o: None)), "summary.json")
    log(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

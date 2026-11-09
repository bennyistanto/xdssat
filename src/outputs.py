"""Gridded CF netCDF of a run's seasonal results, on the AgERA5 grid.

A run (``scripts/run_aoi.py``) simulates one stratum per weather cell and water
regime, so every (cell, season, regime) has at most one simulated season per
agricultural year. ``seasonal_dataset`` places those values on the 0.1 degree
AgERA5 grid:

    dims  system (season x water regime that exists), time (agricultural year),
          lat, lon (bounding box of the run's cells, AgERA5 order: lat north to south)

Each ``system`` is one (season, water_regime) pair, e.g. wet/irrigated, so no
slab is empty by construction (in the Java rice case, rainfed rice has no dry season).

Per-season values are the DSSAT seasonal summary (summary.csv) of the
stratum: yield (HWAH), biomass, dates, water and nitrogen. Area variables make
regional totals a plain sum:

    crop_area        static crop area of the stratum (ha, from the crop mask)
    harvested_area   crop_area where the season was simulated and has a yield
    production       yield x harvested_area (t)

Rice runs also carry ``rice_area`` and ``rice_area_total``, copies of
``crop_area`` and ``crop_area_total`` under the names files had before other
crops (kept for scripts that read them).

The crop comes from the run folder's run_info.json (``crop``: code, name,
model); folders written before it existed are rice (``genotype.RICE``).

So the area-weighted mean yield of any region and year is
``production.sum() / harvested_area.sum() * 1000`` (kg/ha), the same weighting
``run_aoi.py`` prints. Values are per stratum, not per pixel: one soil profile,
one weather series and one calendar per cell and regime.

``time`` is the start of the agricultural year (1 Aug, see management.ANCHOR),
with bounds to the next 1 Aug; actual dates are in ``planting_date`` etc.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from pyproj import CRS

import agera5
import rasters
import management as mg
from genotype import RICE

FILL = np.float32(1.0e20)
SYSTEM_ORDER = [("wet", "irrigated"), ("wet", "rainfed"), ("dry", "irrigated"), ("dry", "rainfed")]

# name: (summary.csv column, units, long_name, extra attributes)
# Definitions from dssat-csm-os/Data/DATA.CDE. PRCM and ETCM run from the
# simulation start (SDAT, 3 days before planting in the template) to harvest.
SEASON_VARS = {
    "yield": ("HWAH", "kg ha-1", "harvested grain yield, dry matter", {}),
    "biomass": ("CWAM", "kg ha-1", "above-ground biomass at maturity, dry matter", {}),
    "lai_max": ("LAIX", "1", "maximum leaf area index during the season",
                {"standard_name": "leaf_area_index", "cell_methods": "time: maximum"}),
    "rain": ("PRCM", "mm", "precipitation from simulation start to harvest",
             {"standard_name": "lwe_thickness_of_precipitation_amount", "cell_methods": "time: sum"}),
    "irrigation": ("IRCM", "mm", "applied irrigation including losses, simulation start to harvest",
                   {"cell_methods": "time: sum"}),
    "evapotranspiration": ("ETCM", "mm", "evapotranspiration from simulation start to harvest",
                           {"cell_methods": "time: sum"}),
    "n_uptake": ("NUCM", "kg ha-1", "crop nitrogen uptake during the season", {}),
    "n_fertilizer": ("NICM", "kg ha-1", "inorganic N applied during the season", {}),
    "n_leached": ("NLCM", "kg ha-1", "N leached during the season", {}),
    "n_mineralized": ("NMINC", "kg ha-1", "cumulative net N mineralisation during the season", {}),
    "n2o_emitted": ("N2OEM", "kg ha-1", "cumulative N2O emissions at maturity, as N", {}),
    "n_fixed": ("NFXM", "kg ha-1", "N fixed during the season (legumes)", {}),
    "runoff": ("ROCM", "mm", "surface runoff during the season", {"cell_methods": "time: sum"}),
    "drainage": ("DRCM", "mm", "water drained below the profile during the season", {"cell_methods": "time: sum"}),
    "soil_evaporation": ("ESCM", "mm", "soil evaporation during the season", {"cell_methods": "time: sum"}),
    "transpiration": ("EPCM", "mm", "transpiration during the season", {"cell_methods": "time: sum"}),
    "water_at_maturity": ("SWXM", "mm", "plant-extractable soil water at maturity", {}),
}
DATE_VARS = {
    "planting_date": ("PDAT", "planting (transplanting) date"),
    "anthesis_date": ("ADAT", "anthesis date"),
    "maturity_date": ("MDAT", "physiological maturity date"),
}
SEASONAL = ("accumulated over the simulated growing season (simulation start to harvest), "
            "not over the whole agricultural year that time_bnds spans")
for _k in ("lai_max", "rain", "irrigation", "evapotranspiration", "n_uptake", "n_fertilizer", "n_leached",
           "n_mineralized", "runoff", "drainage", "soil_evaporation", "transpiration"):
    SEASON_VARS[_k][3]["comment"] = SEASONAL.replace("accumulated", "maximum") if _k == "lai_max" else SEASONAL
TIME_UNITS = "days since 1970-01-01"
CALENDAR = "proleptic_gregorian"


def _yrdoy(v: pd.Series) -> pd.Series:
    """DSSAT YYYYDDD dates to Timestamps; -99 and blanks to NaT."""
    n = pd.to_numeric(v, errors="coerce")
    n = n.where(n > 1000000)
    return pd.to_datetime(n.astype("Int64").astype(str), format="%Y%j", errors="coerce")


def _value(v: pd.Series) -> pd.Series:
    """Numeric summary values; DSSAT missing (-99 and other negatives) to NaN."""
    n = pd.to_numeric(v, errors="coerce")
    return n.where(n >= 0)


def systems(summary: pd.DataFrame) -> list[tuple[str, str]]:
    present = set(map(tuple, summary[["season", "water"]].drop_duplicates().to_numpy()))
    order = [s for s in SYSTEM_ORDER if s in present]
    return order + sorted(present - set(order))


def seasonal_dataset(summary: pd.DataFrame, strata: pd.DataFrame, lat: np.ndarray, lon: np.ndarray,
                     years=None, attrs: dict | None = None, crop: dict | None = None) -> xr.Dataset:
    """Grid the seasonal summary of a run.

    ``summary`` is run_aoi's summary.csv (DSSAT columns plus the unit columns
    ``season``, ``season_year``, ``iy``, ``ix``, ``water``, ``ha``); ``strata``
    its strata.csv; ``lat``/``lon`` the full AgERA5 region grid that ``iy``/``ix``
    index (``agera5.grid``). ``years`` defaults to every season_year in summary.
    ``crop`` (``genotype.crop_info``) names the crop in the attributes; default rice.
    """
    crop = crop or RICE
    cname = crop["name"]
    sy = systems(summary)
    years = list(range(int(summary.season_year.min()), int(summary.season_year.max()) + 1)) \
        if years is None else list(years)
    iy0, iy1 = int(strata.iy.min()), int(strata.iy.max())
    ix0, ix1 = int(strata.ix.min()), int(strata.ix.max())
    glat, glon = lat[iy0:iy1 + 1], lon[ix0:ix1 + 1]
    shape = (len(sy), len(years), len(glat), len(glon))

    s_of = {s: k for k, s in enumerate(sy)}
    t_of = {y: k for k, y in enumerate(years)}
    keep = summary.season_year.isin(t_of)
    sm = summary[keep]
    si = np.array([s_of[(a, b)] for a, b in zip(sm.season, sm.water)], dtype=int)
    ti = sm.season_year.map(t_of).to_numpy()
    yi = sm.iy.to_numpy() - iy0
    xi = sm.ix.to_numpy() - ix0
    if pd.DataFrame({"s": si, "t": ti, "y": yi, "x": xi}).duplicated().any():
        raise ValueError("more than one season per (system, year, cell): summary is not one stratum per regime")

    dims = ("system", "time", "lat", "lon")
    data = {}

    def grid(values, dtype=np.float32, fill=np.nan):
        a = np.full(shape, fill, dtype=dtype)
        a[si, ti, yi, xi] = values
        return a

    hwah = _value(sm.HWAH)
    for name, (col, units, long_name, extra) in SEASON_VARS.items():
        if col not in sm:                 # e.g. summaries of runs made before the column was kept
            continue
        data[name] = (dims, grid(_value(sm[col]).to_numpy()),
                      {"long_name": long_name, "units": units, "dssat_variable": col, **extra})
    dates = {}
    for name, (col, long_name) in DATE_VARS.items():
        d = _yrdoy(sm[col])
        dates[col] = d
        data[name] = (dims, grid(d.to_numpy(), dtype="datetime64[ns]", fill=np.datetime64("NaT")),
                      {"long_name": long_name, "dssat_variable": col})
    length = (dates["MDAT"] - dates["PDAT"]).dt.days
    data["season_length"] = (dims, grid(length.to_numpy(dtype=float)),
                             {"long_name": "days from planting to physiological maturity", "units": "d"})
    harvested = sm.ha.where(hwah.notna())
    data["harvested_area"] = (dims, grid(harvested.to_numpy()),
                              {"long_name": f"{cname} area of the stratum where the season has a simulated yield",
                               "units": "ha", "cell_methods": "area: sum"})
    data["production"] = (dims, grid((hwah * harvested / 1000.0).to_numpy()),
                          {"long_name": "simulated grain production, yield x harvested_area, dry matter",
                           "units": "t", "cell_methods": "area: sum"})

    # static crop area per system (the area of the system's water regime)
    ra = np.full((len(sy), len(glat), len(glon)), np.nan, dtype=np.float32)
    for k, (season, water) in enumerate(sy):
        st = strata[strata.water == water]
        ra[k, st.iy.to_numpy() - iy0, st.ix.to_numpy() - ix0] = st.ha.to_numpy()
    data["crop_area"] = (("system", "lat", "lon"), ra,
                         {"long_name": f"{cname} area of the water regime in the cell, from the crop mask",
                          "units": "ha", "cell_methods": "area: sum",
                          "comment": "static; a regime planted in two seasons appears under both systems "
                                     "(e.g. wet_irrigated and dry_irrigated), so do not sum over system "
                                     "(use crop_area_total)"})
    tot = np.full((len(glat), len(glon)), np.nan, dtype=np.float32)
    cells = strata.groupby(["iy", "ix"]).ha.sum()
    tot[cells.index.get_level_values(0) - iy0, cells.index.get_level_values(1) - ix0] = cells.to_numpy()
    data["crop_area_total"] = (("lat", "lon"), tot,
                               {"long_name": f"{cname} area in the cell, all water regimes",
                                "units": "ha", "cell_methods": "area: sum"})
    if crop["code"] == "RI":            # the names of files written before other crops
        data["rice_area"] = (data["crop_area"][0], ra,
                             {**data["crop_area"][2], "comment": data["crop_area"][2]["comment"]
                              + "; same values as crop_area, kept under its older name"})
        data["rice_area_total"] = (data["crop_area_total"][0], tot,
                                   {**data["crop_area_total"][2],
                                    "comment": "same values as crop_area_total, kept under its older name"})

    t0 = [mg.anchor_date(y) for y in years]
    t1 = [mg.anchor_date(y + 1) for y in years]
    half = rasters.weather_step(lat, lon) / 2
    coords = {
        "system": ("system", np.array([f"{a}_{b}" for a, b in sy], dtype=object),
                   {"long_name": "cropping system: season and water regime"}),
        "season": ("system", np.array([a for a, _ in sy], dtype=object),
                   {"long_name": "season of the agricultural year (seasons > calendar of experiment.yml)"}),
        "water_regime": ("system", np.array([b for _, b in sy], dtype=object),
                         {"long_name": "water regime (treatments > water_regimes of experiment.yml: the "
                                       "template irrigation level it ran with)"}),
        "time": ("time", pd.DatetimeIndex(t0).to_numpy(),
                 {"standard_name": "time", "long_name": "start of the agricultural year (1 Aug)",
                  "axis": "T", "bounds": "time_bnds"}),
        "lat": ("lat", glat.astype(np.float64),
                {"standard_name": "latitude", "long_name": "latitude of the AgERA5 cell centre",
                 "units": "degrees_north", "axis": "Y", "bounds": "lat_bnds"}),
        "lon": ("lon", glon.astype(np.float64),
                {"standard_name": "longitude", "long_name": "longitude of the AgERA5 cell centre",
                 "units": "degrees_east", "axis": "X", "bounds": "lon_bnds"}),
    }
    ds = xr.Dataset(data, coords=coords)
    # a data variable, not a coordinate: xarray would otherwise list it in time_bnds' coordinates
    ds["season_year"] = ("time", np.array(years, dtype=np.int32),
                         {"long_name": "agricultural year: 1 Aug of this year to 31 Jul of the next"})
    ds["time_bnds"] = (("time", "nv"), np.stack([pd.DatetimeIndex(t0).to_numpy(),
                                                  pd.DatetimeIndex(t1).to_numpy()], axis=1))
    ds["lat_bnds"] = (("lat", "nv"), np.stack([glat + half, glat - half], axis=1))
    ds["lon_bnds"] = (("lon", "nv"), np.stack([glon - half, glon + half], axis=1))
    ds["crs"] = ((), np.int32(0), {"grid_mapping_name": "latitude_longitude",
                                   "semi_major_axis": 6378137.0, "inverse_flattening": 298.257223563,
                                   "longitude_of_prime_meridian": 0.0,
                                   "crs_wkt": CRS.from_epsg(4326).to_wkt(),
                                   "long_name": "WGS 84 (AgERA5 grid)"})
    for v in ds.data_vars:
        if {"lat", "lon"} <= set(ds[v].dims) and v not in ("lat_bnds", "lon_bnds"):
            ds[v].attrs["grid_mapping"] = "crs"
            c = " ".join(c for c in ("season", "water_regime", "season_year")
                         if set(ds[c].dims) <= set(ds[v].dims))
            if c:
                ds[v].attrs["coordinates"] = c
    ds.attrs.update(attrs or {})
    return ds


def encoding(ds: xr.Dataset) -> dict:
    enc = {}
    for v in ds.variables:
        x = ds[v]
        if np.issubdtype(x.dtype, np.datetime64):
            e = {"units": TIME_UNITS, "calendar": CALENDAR}
            if v in ds.data_vars and v != "time_bnds":
                e.update(dtype="int32", _FillValue=np.int32(-2147483647), zlib=True, complevel=4)
            else:
                e.update(dtype="int32", _FillValue=None)
            enc[v] = e
        elif x.dtype == object:
            enc[v] = {"dtype": str}
        elif v in ds.data_vars and x.ndim >= 2 and np.issubdtype(x.dtype, np.floating) \
                and v not in ("lat_bnds", "lon_bnds"):
            enc[v] = {"dtype": "float32", "_FillValue": FILL, "zlib": True, "complevel": 4}
        else:
            enc[v] = {"_FillValue": None}
    return enc


def write(ds: xr.Dataset, path: Path) -> Path:
    path = Path(path)
    tmp = path.with_suffix(".nc.part")
    ds.to_netcdf(tmp, format="NETCDF4", encoding=encoding(ds))
    tmp.replace(path)
    return path


def _git_version() -> str:
    try:
        return subprocess.check_output(["git", "describe", "--always", "--dirty"], text=True,
                                       cwd=Path(__file__).resolve().parent, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _name(v) -> str:
    """Last path component of a local path (no local folders in published files)."""
    s = str(v or "?")
    return Path(s).name if ("\\" in s or "/" in s) else s


def run_attrs(run_dir: Path, info: dict) -> dict:
    args = info.get("args", {})
    crop = info.get("crop") or RICE
    rice = crop["code"] == "RI"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean = {**info, "template": _name(info.get("template")), "engine": _name(info.get("engine"))}
    clean["args"] = {k: (_name(v) if k == "zone_table" else v) for k, v in args.items() if k != "out"}
    ins = info.get("inputs")
    if ins:                             # the sources of the prepared inputs of this run
        data = "; ".join(f"{k.replace('_', ' ')} {d.get('source') or d.get('name')}" for k, d in ins.items())
    else:                               # run folders written before input datasets existed
        data = ("weather AgERA5 (https://doi.org/10.24381/cds.6c68c9bb); soil SoilGrids 2.0 with "
                "Saxton-Rawls (2006) hydraulics; rice area ATR/BPN 2024; irrigation class Ministry of Agriculture")
    cultivar = args.get("cultivar", "IR 58" if rice else "?")
    out = {
        "Conventions": "CF-1.10",
        "title": f"xdssat {crop['name']} simulation, {args.get('aoi', '?')}, {args.get('mode', '?')} calendar",
        "summary": (f"Seasonal DSSAT-CSM {crop['name']} results on the weather grid, one simulation per "
                    f"weather cell and water regime, with {crop['name']} area for area-weighted totals."),
        "source": (f"DSSAT-CSM 4.8 {crop['model_name']} ({crop['model']}), engine {clean['engine']}; "
                   f"template {clean['template']}; {data}"),
        "date_created": now,
        "history": f"{now} written by xdssat outputs.py from {Path(run_dir).name}/summary.csv",
        "references": "https://doi.org/10.24381/cds.6c68c9bb; https://doi.org/10.5194/soil-7-217-2021",
        "comment": (f"Uncalibrated: one cultivar ({cultivar}), {args.get('n', 140 if rice else '?')} kg N, "
                    "no pests or diseases; not comparable with farm statistics until calibrated."),
        "crop": crop["name"],
        "crop_model": crop["model"],
        "cultivar": f"{cultivar} (template {clean['template'] if info.get('template') else 'XDRI8601.RIX'})",
        "nitrogen_kg_ha": str(args.get("n", 140 if rice else "?")),
        "soil_fertility_factor_SLPF": str(args.get("slpf", 1.0)),
        "soil_fertility_factor_SLPF_rainfed": str(args.get("slpf_rainfed", "None")).replace("None", "same as SLPF"),
        "soil_fertility_factor_SLPF_table": Path(str(args.get("slpf_table", "None"))).name.replace("None", "none"),
    }
    if rice:
        out["cold_sterility_threshold_TCLDF"] = str(args.get("tcldf", "None")).replace("None", "cultivar file value")
    out.update({
        "xdssat_version": _git_version(),
        "xdssat_aoi": str(args.get("aoi", "")),
        "xdssat_mode": str(args.get("mode", "")),
        "xdssat_run": json.dumps(clean),
    })
    return out


def write_run(run_dir: Path, region: str = "idn", path: Path | None = None, lat_lon=None) -> Path:
    """Read a run folder (summary.csv, strata.csv, run_info.json) and write its netCDF.

    The grid is ``lat_lon`` (lat, lon) when given; otherwise that of the weather
    input named in the folder's experiment.yml; for runs made before input
    datasets existed, the AgERA5 cache of ``region``.
    """
    run_dir = Path(run_dir)
    summary = pd.read_csv(run_dir / "summary.csv", low_memory=False)
    strata = pd.read_csv(run_dir / "strata.csv")
    info = json.loads((run_dir / "run_info.json").read_text()) if (run_dir / "run_info.json").exists() else {}
    years = None
    if "years" in info.get("args", {}):
        y0, y1 = json.loads(info["args"]["years"])
        years = range(y0, y1 + 1)
    if lat_lon is None and (run_dir / "experiment.yml").is_file():
        import config
        import inputs
        exp = config.load(run_dir / "experiment.yml")
        if "inputs" in config.read_yaml(run_dir / "experiment.yml")[0]:
            lat_lon = inputs.open_dataset("weather", exp.data["inputs"]["weather"], exp.datasets()).grid()[:2]
    lat, lon = lat_lon if lat_lon is not None else agera5.grid(region)[:2]
    ds = seasonal_dataset(summary, strata, lat, lon, years=years, attrs=run_attrs(run_dir, info),
                          crop=info.get("crop"))
    return write(ds, path or run_dir / f"{run_dir.name}.nc")

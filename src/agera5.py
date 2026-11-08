"""AgERA5 daily weather: regional cache in DSSAT units, and per-cell series.

The source is the global AgERA5 archive, one netCDF per variable per year,
``<AGERA5_DIR>/<folder>/agera5_<folder>_<YYYY>.nc``, on a 0.1 degree
latitude-longitude grid (lat 90 to -90, lon -180 to 179.9), land only (sea
cells are NaN). Reading the Indonesia box from one global file takes about 5 s
(measured 2026-11-05), so a region is extracted once into a cache and every
later read comes from the cache.

Cache layout: ``<CACHE_DIR>/agera5/<region>/<VAR>/agera5_<region>_<VAR>_<YYYY>.nc``,
one file per DSSAT variable per year, already in DSSAT units and packed as
int16 with ``scale_factor`` 0.1. The WTH writer prints these variables with one
decimal (``%6.1f``), so 0.1 is exactly the precision that reaches the engine.

Units (source to DSSAT):

======  ==========  ===================================  ==================
DSSAT   folder      source variable, unit                conversion
======  ==========  ===================================  ==================
SRAD    srad        Solar_Radiation_Flux, J m-2 d-1      / 1e6 -> MJ m-2 d-1
TMAX    t2mmax      Temperature_Air_2m_Max_24h, K        - 273.15 -> degC
TMIN    t2mmin      Temperature_Air_2m_Min_24h, K        - 273.15 -> degC
RAIN    precip      Precipitation_Flux, mm d-1           none
DEWP    t2mdp       Dew_Point_Temperature_2m_Mean_24h, K - 273.15 -> degC
WIND    ws10m       Wind_Speed_10m_Mean_24h, m s-1       * 86.4 -> km d-1
======  ==========  ===================================  ==================

AgERA5 days run 00-00 local time (the source ``long_name`` says
"(00-00LT)"). Wind is at 10 m, so the WTH header needs ``WNDHT`` 10.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

import config

# The AgERA5 archive folder is config.AGERA5_DIR (paths > agera5 in the experiment file,
# or XDSSAT_AGERA5), looked up at each call so a --config file takes effect.
RES = 0.1
WIND_HEIGHT = 10.0
PACK_SCALE = 0.1
PACK_FILL = np.int16(-32768)
# netCDF4/HDF5 is not thread-safe; the runner calls write_inputs from threads.
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Var:
    folder: str
    source: str
    units: str
    long_name: str
    scale: float = 1.0
    offset: float = 0.0

    def convert(self, x: np.ndarray) -> np.ndarray:
        return x * self.scale + self.offset


VARIABLES: dict[str, Var] = {
    "SRAD": Var("srad", "Solar_Radiation_Flux", "MJ m-2 d-1",
                "daily surface solar radiation", scale=1e-6),
    "TMAX": Var("t2mmax", "Temperature_Air_2m_Max_24h", "degC",
                "daily maximum 2 m air temperature", offset=-273.15),
    "TMIN": Var("t2mmin", "Temperature_Air_2m_Min_24h", "degC",
                "daily minimum 2 m air temperature", offset=-273.15),
    "RAIN": Var("precip", "Precipitation_Flux", "mm d-1", "daily precipitation"),
    "DEWP": Var("t2mdp", "Dew_Point_Temperature_2m_Mean_24h", "degC",
                "daily mean 2 m dew point temperature", offset=-273.15),
    "WIND": Var("ws10m", "Wind_Speed_10m_Mean_24h", "km d-1",
                "daily mean 10 m wind run", scale=86.4),
}
REQUIRED = ("SRAD", "TMAX", "TMIN", "RAIN")

# Regions as cell-centre bounds (lat_min, lat_max, lon_min, lon_max), inclusive.
REGIONS = {
    "idn": (-11.5, 6.5, 94.5, 141.5),
}


def source_path(var: str, year: int, root: Path | None = None) -> Path:
    v = VARIABLES[var]
    return Path(root or config.AGERA5_DIR) / v.folder / f"agera5_{v.folder}_{year}.nc"


def cache_path(region: str, var: str, year: int, cache_dir: Path | None = None) -> Path:
    root = Path(cache_dir or config.CACHE_DIR) / "agera5" / region / var
    return root / f"agera5_{region}_{var}_{year}.nc"


def _index(coord: np.ndarray, lo: float, hi: float) -> slice:
    """Index slice of the cells whose centres lie in [lo, hi] (either axis order)."""
    sel = np.nonzero((coord >= lo - RES / 4) & (coord <= hi + RES / 4))[0]
    if sel.size == 0:
        raise ValueError(f"no cells between {lo} and {hi}")
    return slice(int(sel[0]), int(sel[-1]) + 1)


def available_years(var: str, root: Path | None = None) -> list[int]:
    folder = Path(root or config.AGERA5_DIR) / VARIABLES[var].folder
    return sorted(int(p.stem.rsplit("_", 1)[1]) for p in folder.glob("agera5_*_*.nc"))


def extract_year(region: str, var: str, year: int, cache_dir: Path | None = None,
                 source_root: Path | None = None, overwrite: bool = False) -> Path:
    """Cut one region from one global yearly file, convert and pack it into the cache.

    Writes to a temporary name and renames at the end, so an interrupted run
    never leaves a file that looks complete.
    """
    out = cache_path(region, var, year, cache_dir)
    if out.exists() and not overwrite:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    v = VARIABLES[var]
    src = source_path(var, year, source_root)
    lat_min, lat_max, lon_min, lon_max = REGIONS[region]
    with netCDF4.Dataset(src) as f:
        lat = f["lat"][:].data
        lon = f["lon"][:].data
        iy, ix = _index(lat, lat_min, lat_max), _index(lon, lon_min, lon_max)
        tvar = f["time"]
        days = netCDF4.num2date(tvar[:], tvar.units, tvar.calendar,
                                only_use_cftime_datetimes=False,
                                only_use_python_datetimes=True)
        data = f[v.source]
        data.set_auto_maskandscale(True)
        raw = np.ma.filled(data[:, iy, ix].astype("float64"), np.nan)
        history = getattr(f, "history", "")
    vals = v.convert(raw)
    packed = np.where(np.isnan(vals), PACK_FILL,
                      np.round(vals / PACK_SCALE)).astype("int16")
    dates = pd.DatetimeIndex(days)

    tmp = out.with_suffix(".nc.part")
    with netCDF4.Dataset(tmp, "w", format="NETCDF4") as g:
        g.createDimension("time", len(dates))
        g.createDimension("lat", raw.shape[1])
        g.createDimension("lon", raw.shape[2])
        t = g.createVariable("time", "i4", ("time",))
        t.units = "days since 1900-01-01"
        t.calendar = "proleptic_gregorian"
        t.standard_name = "time"
        t.axis = "T"
        t[:] = (dates - pd.Timestamp("1900-01-01")).days.values
        la = g.createVariable("lat", "f8", ("lat",))
        la.units, la.standard_name, la.axis = "degrees_north", "latitude", "Y"
        la[:] = np.round(lat[iy], 4)
        lo = g.createVariable("lon", "f8", ("lon",))
        lo.units, lo.standard_name, lo.axis = "degrees_east", "longitude", "X"
        lo[:] = np.round(lon[ix], 4)
        crs = g.createVariable("crs", "i4")
        crs.grid_mapping_name = "latitude_longitude"
        crs.semi_major_axis = 6378137.0
        crs.inverse_flattening = 298.257223563
        x = g.createVariable(var, "i2", ("time", "lat", "lon"), zlib=True, complevel=4,
                             shuffle=True, chunksizes=(len(dates), min(16, raw.shape[1]), min(16, raw.shape[2])),
                             fill_value=PACK_FILL)
        x.set_auto_maskandscale(False)
        x.scale_factor = np.float32(PACK_SCALE)
        x.add_offset = np.float32(0.0)
        x.units = v.units
        x.long_name = v.long_name
        x.grid_mapping = "crs"
        x.source_variable = v.source
        x.conversion = f"value = source * {v.scale:g} + {v.offset:g}"
        x[:] = packed
        g.Conventions = "CF-1.8"
        g.title = f"AgERA5 {var} for region {region}, in DSSAT units"
        g.source = f"{src.name} ({v.source}); AgERA5 https://doi.org/10.24381/cds.6c68c9bb"
        g.history = (f"{pd.Timestamp.now():%Y-%m-%dT%H:%M:%S} extracted by xdssat agera5.py "
                     f"(region {REGIONS[region]}); source history: {history}")
    os.replace(tmp, out)
    return out


def _extract_job(args):
    t0 = time.time()
    p = extract_year(*args)
    return args, p, time.time() - t0


def build_cache(region: str = "idn", variables=tuple(VARIABLES), years=None,
                cache_dir: Path | None = None, workers: int = 4, log=print) -> list[Path]:
    """Extract every (variable, year) for a region. Existing files are skipped."""
    jobs = []
    for var in variables:
        ys = years if years is not None else available_years(var)
        for y in ys:
            if not cache_path(region, var, y, cache_dir).exists():
                jobs.append((region, var, int(y), cache_dir))
    log(f"{len(jobs)} files to extract")
    done = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_extract_job, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            args, p, dt = fut.result()
            done.append(p)
            log(f"[{i}/{len(jobs)}] {args[1]} {args[2]} {dt:.1f}s")
    return done


# ---------------------------------------------------------------- reading
# The cache is a standard weather input (src/inputs.py); reading goes through it.


def store(region: str = "idn", cache_dir: Path | None = None) -> "inputs.Weather":
    """The cached region as a standard weather dataset (``inputs.Weather``)."""
    import inputs
    folder = Path(cache_dir or config.CACHE_DIR) / "agera5" / region
    return inputs.Weather.from_layout(folder, f"{{var}}/agera5_{region}_{{var}}_{{year}}.nc",
                                      variables=tuple(VARIABLES), wind_height=WIND_HEIGHT)


def grid(region: str = "idn", cache_dir: Path | None = None, var: str = "TMAX",
         year: int = 2000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(lat, lon, valid) of a cached region; valid = cell has data on day 1."""
    return store(region, cache_dir).grid(var, year)


def cell_index(lat: np.ndarray, lon: np.ndarray, glat, glon) -> tuple[np.ndarray, np.ndarray]:
    """Row and column of the grid cells containing the points (glat, glon)."""
    iy = np.rint((lat[0] - np.asarray(glat)) / RES).astype(int) if lat[0] > lat[-1] \
        else np.rint((np.asarray(glat) - lat[0]) / RES).astype(int)
    ix = np.rint((np.asarray(glon) - lon[0]) / RES).astype(int)
    return iy, ix


def series(iy, ix, start, end, region: str = "idn", variables=tuple(VARIABLES),
           cache_dir: Path | None = None) -> tuple[pd.DatetimeIndex, dict[str, np.ndarray]]:
    """Daily values of selected cells, each variable as an array (time, n_cells).

    ``iy`` and ``ix`` are row and column indices into the cached region grid.
    Only the bounding block of the cells is read from each yearly file.
    """
    return store(region, cache_dir).series(iy, ix, start, end, variables)


def cell_frame(dates: pd.DatetimeIndex, values: dict[str, np.ndarray], k: int) -> pd.DataFrame:
    """One cell's daily weather as the DataFrame that ``wth.write_wth`` takes."""
    return pd.DataFrame({v: a[:, k] for v, a in values.items()}, index=dates)


def last_common_date(region: str = "idn", variables=tuple(VARIABLES),
                     cache_dir: Path | None = None) -> pd.Timestamp:
    """Last day that every cached variable has (the newest year is often partial)."""
    return store(region, cache_dir).last_common_date(variables)

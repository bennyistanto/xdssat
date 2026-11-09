"""Prepare daily weather from any gridded source into the standard weather input.

A user brings daily gridded files from whatever source they have: ERA5-Land daily
statistics, AgERA5, NASA POWER, CHIRPS, a national product. Each DSSAT variable
can come from its own source (rain from CHIRPS, temperature from ERA5-Land, say).
``prepare`` reads every variable, converts it to DSSAT units, puts all of them on
one target grid and writes the standard form of ``src/inputs.py`` with its
manifest, then runs the input checks. ``inspect`` lists what a file holds, so the
names for the experiment file can be read off it.

Units come from each file's own ``units`` attribute (``UNIT_RULES``); a file
without one, or with one not in the table, needs ``units:`` in the experiment
file. Amounts are daily: a radiation or rain value is the day's total, a flux is
the day's mean.

Regridding to the target grid (``regrid`` per variable):

``exact``   the source grid is the target grid (default when they match);
``mean``    each target cell is the mean of the source cells whose centres lie in
            it (default when the source is finer, e.g. CHIRPS 0.05 degree to 0.1);
``linear``  bilinear from the four surrounding source cells, using only those with
            data, so coastal cells keep a value (default when the source is
            coarser, e.g. a 0.5 degree product to 0.1);
``nearest`` the source cell nearest each target cell centre.

Values are stored packed as int16 with scale 0.1, as the weather files reach the
engine with one decimal (``wth.write_wth``).
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

import config
import inputs

PACK_SCALE = 0.1
PACK_FILL = np.int16(-32768)

# DSSAT variable -> quantity; quantity -> list of (normalised unit, scale, offset): dssat = x * scale + offset
QUANTITY = {"SRAD": "radiation", "TMAX": "temperature", "TMIN": "temperature", "DEWP": "temperature",
            "RAIN": "rain", "WIND": "wind"}
UNIT_RULES = {
    "temperature": [("k", 1.0, -273.15), ("kelvin", 1.0, -273.15), ("degc", 1.0, 0.0), ("c", 1.0, 0.0),
                    ("degreesc", 1.0, 0.0), ("degrees_celsius", 1.0, 0.0), ("celsius", 1.0, 0.0),
                    ("degree_celsius", 1.0, 0.0)],
    "radiation": [("jm-2d-1", 1e-6, 0.0), ("jm-2", 1e-6, 0.0), ("mjm-2d-1", 1.0, 0.0), ("mjm-2", 1.0, 0.0),
                  ("wm-2", 0.0864, 0.0), ("kwhm-2d-1", 3.6, 0.0), ("kwhm-2", 3.6, 0.0),
                  ("kw-hrm-2d-1", 3.6, 0.0)],
    "rain": [("mmd-1", 1.0, 0.0), ("mm", 1.0, 0.0), ("kgm-2d-1", 1.0, 0.0), ("kgm-2", 1.0, 0.0),
             ("md-1", 1000.0, 0.0), ("m", 1000.0, 0.0), ("kgm-2s-1", 86400.0, 0.0), ("mms-1", 86400.0, 0.0)],
    "wind": [("ms-1", 86.4, 0.0), ("kmh-1", 24.0, 0.0), ("kmd-1", 1.0, 0.0)],
}
DSSAT_UNIT = {"radiation": "MJ m-2 d-1", "temperature": "degC", "rain": "mm d-1", "wind": "km d-1"}


def normalise_unit(u: str) -> str:
    """'J m**-2 day-1', 'J/m^2/day', 'J m-2 d-1' -> 'jm-2d-1'."""
    s = str(u).strip().lower().replace("**", "").replace("^", "").replace(" ", "").replace(".", "")
    for a, b in (("/m2", "m-2"), ("/day", "d-1"), ("/d", "d-1"), ("/s", "s-1"), ("/h", "h-1"),
                 ("day-1", "d-1"), ("hr-1", "h-1"), ("sec-1", "s-1"), ("m2", "m-2"), ("°", "deg")):
        s = s.replace(a, b)
    s = s.replace("m--2", "m-2")
    return s


def conversion(var: str, units: str) -> tuple[float, float]:
    """(scale, offset) that turn ``units`` into the DSSAT unit of ``var``; ValueError if unknown."""
    q = QUANTITY[var]
    n = normalise_unit(units)
    for unit, scale, offset in UNIT_RULES[q]:
        if n == unit:
            return scale, offset
    known = ", ".join(sorted({u for u, _, _ in UNIT_RULES[q]}))
    raise ValueError(f"{var}: unit {units!r} is not one xdssat converts for {q} (known, written without "
                     f"spaces: {known}). Give the unit with 'units:' in the experiment file, e.g. "
                     f"units: \"{DSSAT_UNIT[q]}\"")


# ----------------------------------------------------------------------------
# reading a source file
# ----------------------------------------------------------------------------
TIME_NAMES = ("time", "valid_time", "date", "t")
LAT_NAMES = ("lat", "latitude", "y", "nav_lat")
LON_NAMES = ("lon", "longitude", "x", "nav_lon")


def _find(names, available):
    for n in names:
        if n in available:
            return n
    return None


@dataclass
class Block:
    """Daily values of one variable on a regular lat/lon grid, lat north to south, lon west to east."""
    dates: pd.DatetimeIndex
    lat: np.ndarray
    lon: np.ndarray
    values: np.ndarray           # (time, lat, lon), float64, NaN = no data
    units: str

    @property
    def step(self) -> float:
        return float(round(abs(self.lon[1] - self.lon[0]), 6)) if len(self.lon) > 1 else float("nan")


def read_block(path: Path, variable: str, box=None, margin: float = 0.0, years=None,
               time_range=None, subdaily: bool = False) -> Block:
    """One variable of one file, cut to ``box`` (west, south, east, north) plus ``margin``.

    ``years`` keeps those calendar years, ``time_range`` (start, end) the time steps in it.
    Sub-daily steps are an error unless ``subdaily`` (then ``to_daily`` aggregates them);
    daily steps are dated by their day.
    """
    with netCDF4.Dataset(path) as f:
        if variable not in f.variables:
            raise ValueError(f"{path.name} has no variable {variable!r}; it has "
                             f"{', '.join(k for k, v in f.variables.items() if v.ndim >= 2)}")
        x = f[variable]
        dims = x.dimensions
        td, yd, xd = _find(TIME_NAMES, dims), _find(LAT_NAMES, dims), _find(LON_NAMES, dims)
        if None in (td, yd, xd) or len(dims) != 3:
            raise ValueError(f"{path.name}: {variable} has dimensions {dims}; need time, latitude and "
                             "longitude (3 dimensions)")
        dates = inputs._dates(f[td], keep_time=True)
        lat = np.asarray(f[yd][:], dtype="float64")
        lon = np.asarray(f[xd][:], dtype="float64")
        units = getattr(x, "units", "")
        x.set_auto_maskandscale(True)
        order = [dims.index(td), dims.index(yd), dims.index(xd)]
        lon_wrapped = lon.max() > 180.0
        if lon_wrapped:
            lon = np.where(lon > 180.0, lon - 360.0, lon)
        if time_range is not None:
            ti = np.nonzero((dates >= time_range[0]) & (dates < time_range[1]))[0]
        else:
            ti = np.arange(len(dates)) if years is None else np.nonzero(dates.year.isin(list(years)))[0]
        if ti.size == 0:
            raise ValueError(f"{path.name}: no days of {variable} in "
                             f"{sorted(years) if time_range is None else [str(t) for t in time_range]}")
        yi = np.arange(len(lat))
        xi = np.arange(len(lon))
        if box is not None:
            w, s, e, n = box
            yi = np.nonzero((lat >= s - margin) & (lat <= n + margin))[0]
            xi = np.nonzero((lon >= w - margin) & (lon <= e + margin))[0]
            if yi.size == 0 or xi.size == 0:
                raise ValueError(f"{path.name}: no cells of {variable} inside the box {box}")
        sl = {td: slice(ti.min(), ti.max() + 1), yd: slice(yi.min(), yi.max() + 1),
              xd: slice(xi.min(), xi.max() + 1)}
        raw = x[tuple(sl[d] for d in dims)]
        data = np.ma.filled(np.ma.asarray(raw).astype("float64"), np.nan)
    data = np.transpose(data, order)
    dates = dates[sl[td]]
    if time_range is not None:
        tkeep = (dates >= time_range[0]) & (dates < time_range[1])
        dates, data = dates[tkeep], data[tkeep]
    elif years is not None:
        tkeep = dates.year.isin(list(years))
        dates, data = dates[tkeep], data[tkeep]
    lat, lon = lat[sl[yd]], lon[sl[xd]]
    if box is not None:                                  # drop rows/cols of the slice outside the box
        ky = (lat >= box[1] - margin) & (lat <= box[3] + margin)
        kx = (lon >= box[0] - margin) & (lon <= box[2] + margin)
        lat, lon, data = lat[ky], lon[kx], data[:, ky][:, :, kx]
    oy, ox = np.argsort(-lat), np.argsort(lon)           # north to south, west to east
    lat, lon, data = lat[oy], lon[ox], data[:, oy][:, :, ox]
    if len(dates) > 1:
        dd = np.diff(dates.values).astype("timedelta64[m]").astype(int)
        if (dd < 24 * 60).any() and not subdaily:
            raise ValueError(f"{path.name}: {variable} has more than one value per day (every "
                             f"{int(np.median(dd))} minutes); set  daily: max, min, mean, sum or accumulated  "
                             "for this variable to make daily values from it")
        if (dd >= 24 * 60).all():
            dates = pd.DatetimeIndex(dates).normalize()
    elif not subdaily:
        dates = pd.DatetimeIndex(dates).normalize()
    return Block(pd.DatetimeIndex(dates), lat, lon, data, units)


# ----------------------------------------------------------------------------
# sub-daily sources -> daily values
# ----------------------------------------------------------------------------
DAILY = ("max", "min", "mean", "sum", "accumulated")


def to_daily(b: Block, how: str, utc_offset_hours: float = 0.0) -> Block:
    """Daily values from sub-daily steps, over local days (UTC + ``utc_offset_hours``).

    ``max``, ``min``, ``mean``: of the values whose time falls in the day (instantaneous
    values such as ERA5-Land 2 m temperature). ``sum``: of amounts that each cover the
    step ending at their time (e.g. an hourly rain total stamped at the end of its hour).
    ``accumulated``: running totals since 00 UTC, as ERA5-Land's precipitation and radiation
    (ECMWF: the 00 UTC value is the previous day's total, 01 UTC holds 00-01 UTC); they are
    differenced back to step amounts, then summed. Days with fewer steps than a full day
    (at the start and end of the data) are dropped.
    """
    if how not in DAILY:
        raise ValueError(f"daily: {how!r} should be one of {', '.join(DAILY)}")
    t = pd.DatetimeIndex(b.dates)
    if len(t) < 2:
        raise ValueError("too few time steps to make daily values")
    step = pd.Timedelta(np.median(np.diff(t.values)))
    per_day = int(round(pd.Timedelta(days=1) / step))
    if per_day < 2 or abs(per_day * step - pd.Timedelta(days=1)) > pd.Timedelta(minutes=1):
        raise ValueError(f"time step {step} does not divide a day")
    v = b.values
    if how == "accumulated":
        prev = np.concatenate([np.full((1,) + v.shape[1:], np.nan), v[:-1]])
        reset = np.asarray((t - step).hour == 0) & np.asarray((t - step).minute == 0)   # step after 00 UTC
        amt = np.where(reset[:, None, None], v, v - prev)
        amt[0] = np.nan                                       # first step: its start is unknown
        scale = np.nanmax(np.abs(amt)) if np.isfinite(amt).any() else 0.0
        drop = amt < -max(1e-9, 0.01 * scale)
        if drop.any():
            ti = int(np.nonzero(drop.reshape(len(t), -1).any(axis=1))[0][0])
            raise ValueError(f"the running total falls at {t[ti]:%Y-%m-%d %H:%M} UTC other than after 00 UTC, so "
                             "these values are not totals since 00 UTC (as ERA5-Land's); for an amount per "
                             "time step use daily: sum")
        v = np.where(amt < 0, 0.0, amt)                       # rounding noise just below zero
        how_ = "sum"
    else:
        how_ = how
    off = pd.Timedelta(hours=utc_offset_hours)
    stamp = (t - step / 2 + off) if how_ == "sum" else (t + off)   # amounts belong to the step they end
    day = stamp.normalize()
    days, inv, counts = np.unique(day.values, return_inverse=True, return_counts=True)
    out = np.full((len(days),) + v.shape[1:], np.nan)
    for i in range(len(days)):
        sel = inv == i
        x = v[sel]
        with np.errstate(invalid="ignore"):
            if how_ == "max":
                out[i] = np.nanmax(x, axis=0) if np.isfinite(x).any() else np.nan
            elif how_ == "min":
                out[i] = np.nanmin(x, axis=0) if np.isfinite(x).any() else np.nan
            elif how_ == "mean":
                out[i] = np.mean(x, axis=0)
            else:
                out[i] = np.sum(x, axis=0)                      # NaN if a step is missing
    full = counts == per_day
    if how == "accumulated":
        full &= np.array([np.isfinite(v[inv == i]).all(axis=0).any() for i in range(len(days))])
    return Block(pd.DatetimeIndex(days[full]), b.lat, b.lon, out[full], b.units)


def inspect(path: Path) -> str:
    """What a netCDF file holds: variables with dimensions and units, grid, time steps."""
    lines = [f"{path}"]
    with netCDF4.Dataset(path) as f:
        dims = {k: len(v) for k, v in f.dimensions.items()}
        lines.append("  dimensions: " + ", ".join(f"{k} {n}" for k, n in dims.items()))
        tn = _find(TIME_NAMES, f.variables)
        if tn:
            try:
                d = inputs._dates(f[tn])
                step = (pd.Series(d).diff().median() if len(d) > 1 else pd.Timedelta(0))
                lines.append(f"  time: {d[0]:%Y-%m-%d} to {d[-1]:%Y-%m-%d}, {len(d)} steps, step {step}")
            except ValueError as e:
                lines.append(f"  time: {e}")
        for names, what in ((LAT_NAMES, "latitude"), (LON_NAMES, "longitude")):
            n = _find(names, f.variables)
            if n:
                c = np.asarray(f[n][:])
                st = abs(c[1] - c[0]) if c.size > 1 else 0
                lines.append(f"  {what} ({n}): {c.min():g} to {c.max():g}, step {st:g}")
        lines.append("  variables:")
        for k, v in f.variables.items():
            if v.ndim >= 2:
                lines.append(f"    {k:28s}{str(v.dimensions):34s}units {getattr(v, 'units', '(none)')!r}"
                             f"  {getattr(v, 'long_name', '')}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# regridding
# ----------------------------------------------------------------------------
def target_grid(box, step: float) -> tuple[np.ndarray, np.ndarray]:
    """Cell centres on multiples of ``step`` covering ``box``; lat north to south."""
    w, s, e, n = box
    k = lambda v: np.round(v / step).astype(int)  # noqa: E731
    lat = np.round(np.arange(k(n), k(s) - 1, -1) * step, 6)
    lon = np.round(np.arange(k(w), k(e) + 1) * step, 6)
    return lat, lon


def _aligned(src: Block, lat, lon) -> bool:
    """Same cell size, and target centres on source centres (the target may extend beyond the source)."""
    tstep = round(abs(lon[1] - lon[0]), 6) if len(lon) > 1 else src.step
    if abs(tstep - src.step) > 1e-6:
        return False
    off = lambda a, b: abs((a - b) / tstep - round((a - b) / tstep)) < 1e-4  # noqa: E731
    return off(lon[0], src.lon[0]) and off(lat[0], src.lat[0])


def _match(src_c, tgt_c, step):
    """Index of each target coordinate in the source coordinates, -1 where the source has none."""
    order = np.argsort(src_c)
    sc = src_c[order]
    pos = np.clip(np.searchsorted(sc, tgt_c), 0, len(sc) - 1)
    best = np.where(np.abs(sc[pos] - tgt_c) <= np.abs(sc[np.maximum(pos - 1, 0)] - tgt_c), pos, np.maximum(pos - 1, 0))
    hit = np.abs(sc[best] - tgt_c) < step * 1e-3
    return np.where(hit, order[best], -1)


def default_method(src: Block, lat, lon) -> str:
    if _aligned(src, lat, lon):
        return "exact"
    tstep = abs(lon[1] - lon[0]) if len(lon) > 1 else src.step
    return "mean" if src.step < 0.75 * tstep else "linear"


def regrid(src: Block, lat: np.ndarray, lon: np.ndarray, method: str | None = None) -> np.ndarray:
    """``src`` on the target cell centres (lat north to south, lon west to east)."""
    method = method or default_method(src, lat, lon)
    v = src.values
    if method == "exact":
        if not _aligned(src, lat, lon):
            raise ValueError("regrid 'exact': the source cells are not the target cells (other size or "
                             "offset); use mean, linear or nearest")
        iy, ix = _match(src.lat, lat, src.step), _match(src.lon, lon, src.step)
        out = v[:, np.maximum(iy, 0)][:, :, np.maximum(ix, 0)]
        out[:, iy < 0] = np.nan
        out[:, :, ix < 0] = np.nan
        return out
    if method == "nearest":
        iy = np.array([np.argmin(np.abs(src.lat - a)) for a in lat])
        ix = np.array([np.argmin(np.abs(src.lon - b)) for b in lon])
        out = v[:, iy][:, :, ix]
        half = src.step / 2 + 1e-9
        out[:, np.abs(src.lat[iy] - lat) > half] = np.nan
        out[:, :, np.abs(src.lon[ix] - lon) > half] = np.nan
        return out
    if method == "mean":
        step_y = abs(lat[0] - lat[1]) if len(lat) > 1 else src.step
        step_x = abs(lon[1] - lon[0]) if len(lon) > 1 else src.step
        ty = np.floor((lat[0] + step_y / 2 - src.lat) / step_y + 1e-9).astype(int)
        tx = np.floor((src.lon - (lon[0] - step_x / 2)) / step_x + 1e-9).astype(int)
        out = np.full((v.shape[0], len(lat), len(lon)), np.nan)
        okx = (tx >= 0) & (tx < len(lon))
        for j in range(len(lat)):
            rows = np.nonzero(ty == j)[0]
            if rows.size == 0:
                continue
            block = v[:, rows][:, :, okx]                          # (t, rows, cols in range)
            cols = tx[okx]
            sums = np.zeros((v.shape[0], len(lon)))
            counts = np.zeros((v.shape[0], len(lon)))
            fin = np.isfinite(block)
            np.add.at(sums, (slice(None), cols), np.where(fin, block, 0.0).sum(axis=1))
            np.add.at(counts, (slice(None), cols), fin.sum(axis=1))
            with np.errstate(invalid="ignore", divide="ignore"):
                out[:, j] = np.where(counts > 0, sums / counts, np.nan)
        return out
    if method == "linear":
        sl, so = src.lat[::-1], src.lon                             # ascending for searchsorted
        vv = v[:, ::-1]

        def weights(c, a):
            i = np.clip(np.searchsorted(c, a) - 1, 0, len(c) - 2)
            f = np.clip((a - c[i]) / (c[i + 1] - c[i]), 0.0, 1.0)
            return i, f
        iy, fy = weights(sl, lat)
        ix, fx = weights(so, lon)
        num = np.zeros((v.shape[0], len(lat), len(lon)))
        den = np.zeros_like(num)
        for dy, wy in ((0, 1 - fy), (1, fy)):
            for dx, wx in ((0, 1 - fx), (1, fx)):
                corner = vv[:, iy + dy][:, :, ix + dx]
                w = wy[:, None] * wx[None, :]
                ok = np.isfinite(corner)
                num += np.where(ok, corner, 0.0) * w
                den += ok * w
        with np.errstate(invalid="ignore", divide="ignore"):
            out = np.where(den > 1e-12, num / den, np.nan)
        half = src.step / 2 + 1e-9                                   # no values far outside the source
        out[:, (lat < sl.min() - half) | (lat > sl.max() + half)] = np.nan
        out[:, :, (lon < so.min() - half) | (lon > so.max() + half)] = np.nan
        return out
    raise ValueError(f"regrid method {method!r}: use exact, mean, linear or nearest")


# ----------------------------------------------------------------------------
# writing the standard form
# ----------------------------------------------------------------------------
def write_year(path: Path, var: str, dates: pd.DatetimeIndex, lat, lon, values: np.ndarray,
               title: str, source: str, history: str = "") -> Path:
    """One variable-year of the standard weather form (CF netCDF, packed int16, scale 0.1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = np.where(np.isnan(values), PACK_FILL, np.round(values / PACK_SCALE)).astype("int16")
    tmp = path.with_suffix(".nc.part")
    with netCDF4.Dataset(tmp, "w", format="NETCDF4") as g:
        g.createDimension("time", len(dates))
        g.createDimension("lat", len(lat))
        g.createDimension("lon", len(lon))
        t = g.createVariable("time", "i4", ("time",))
        t.units, t.calendar, t.standard_name, t.axis = "days since 1900-01-01", "proleptic_gregorian", "time", "T"
        t[:] = (dates - pd.Timestamp("1900-01-01")).days.values
        la = g.createVariable("lat", "f8", ("lat",))
        la.units, la.standard_name, la.axis = "degrees_north", "latitude", "Y"
        la[:] = np.round(lat, 6)
        lo = g.createVariable("lon", "f8", ("lon",))
        lo.units, lo.standard_name, lo.axis = "degrees_east", "longitude", "X"
        lo[:] = np.round(lon, 6)
        crs = g.createVariable("crs", "i4")
        crs.grid_mapping_name = "latitude_longitude"
        crs.semi_major_axis = 6378137.0
        crs.inverse_flattening = 298.257223563
        x = g.createVariable(var, "i2", ("time", "lat", "lon"), zlib=True, complevel=4, shuffle=True,
                             chunksizes=(len(dates), min(16, len(lat)), min(16, len(lon))), fill_value=PACK_FILL)
        x.set_auto_maskandscale(False)
        x.scale_factor = np.float32(PACK_SCALE)
        x.add_offset = np.float32(0.0)
        x.units = inputs.WEATHER_UNITS[var]
        x.grid_mapping = "crs"
        x[:] = packed
        g.Conventions = "CF-1.8"
        g.title = title
        g.source = source
        g.history = f"{pd.Timestamp.now():%Y-%m-%dT%H:%M:%S} {history}".strip()
    os.replace(tmp, path)
    return path


# ----------------------------------------------------------------------------
# the whole preparation
# ----------------------------------------------------------------------------
def _files(pattern: str, exp, year=None) -> Path:
    keys = {k: (exp.path(k).as_posix() if exp.has_path(k) else f"<paths > {k} not set>")
            for k in config.PATH_KEYS}
    s = pattern.format(**keys, year=year if year is not None else "{year}")
    if "<paths >" in s:
        missing = re.findall(r"<paths > (\w+) not set>", s)[0]
        exp.path(missing)                                   # raises PathNotSet with the line to fix
    p = Path(s)
    return p if p.is_absolute() else config.ROOT / p


def plan(exp) -> dict:
    """The resolved preparation settings (``prepare > weather``) with defaults filled in."""
    spec = exp.data["prepare"]["weather"]
    if not spec:
        raise config.ConfigError(["setting 'prepare > weather' is not set: say where each weather "
                                  "variable comes from (configs/rice-java.yml shows how)."], exp.file)
    years = spec.get("years") or [exp.years[0], exp.years[-1] + 1]
    box = spec.get("box") or list(exp.bbox)
    return {"name": spec["name"], "years": list(range(years[0], years[1] + 1)), "box": box,
            "grid_step": spec.get("grid_step"), "variables": spec.get("variables") or {},
            "points": spec.get("points") or [], "utc_offset_hours": float(spec.get("utc_offset_hours") or 0.0),
            "wind_height_m": spec.get("wind_height_m"),
            "station_code": spec.get("station_code", "XDWX"),
            "temperature_height_m": float(spec.get("temperature_height_m", 2.0)),
            "description": spec.get("description", ""), "source": spec.get("source") or ""}


def prepare(exp, overwrite: bool = False, log=print) -> Path:
    """Read, convert and regrid every weather variable; write the dataset and its manifest."""
    p = plan(exp)
    cache = exp.datasets()
    out_dir = inputs.root(cache) / "weather" / p["name"]
    if p["points"]:
        return prepare_points(exp, p, out_dir, overwrite=overwrite, log=log)
    first = p["variables"].get("TMAX") or next(iter(p["variables"].values()))
    # target grid: grid_step on multiples of the step over the box, or the TMAX source grid cut to the box
    # One cell beyond the box on every side, so crop pixels at the box edge always have a cell.
    if p["grid_step"]:
        step = float(p["grid_step"])
    else:
        fs = source_files(first["files"], exp, p["years"][0])
        if not fs:
            raise config.ConfigError([f"prepare > weather: no file {_files(first['files'], exp, p['years'][0])} "
                                      "to take the grid from (or set grid_step)."], exp.file)
        step = read_block(fs[0], _names(first)[0], p["box"], 0.0, subdaily=True).step
    w, s_, e, n = p["box"]
    lat, lon = target_grid((w - step, s_ - step, e + step, n + step), step)
    margin = 2 * step
    if len(lat) > 999 or len(lon) > 9999:
        raise config.ConfigError([f"the weather grid would be {len(lat)} x {len(lon)} cells; xdssat names "
                                  "weather cells with 3-digit rows and 4-digit columns (at most 999 x 9999). "
                                  "Use a smaller box or a coarser grid_step."], exp.file)
    log(f"weather '{p['name']}': grid {len(lat)} x {len(lon)} cells of {step:g} degree, "
        f"years {p['years'][0]}-{p['years'][-1]}")
    sources = {}
    for var, src in p["variables"].items():
        names = _names(src)
        for y in p["years"]:
            path = out_dir / var / f"{p['name']}_{var}_{y}.nc"
            if path.exists() and not overwrite:
                continue
            fs = source_files(src["files"], exp, y, neighbours=bool(src.get("daily")))
            if not fs:
                raise config.ConfigError([f"prepare > weather > variables > {var}: no file "
                                          f"{_files(src['files'], exp, y)} for {y}."], exp.file)
            f = fs[0]
            try:
                written = _one(exp, p, var, src, names, f, y, lat, lon, margin, path)
            except ValueError as e:
                raise config.ConfigError([f"setting 'prepare > weather > variables > {var}' ({f.name}, {y}):\n"
                                          f"  {e}"], exp.file) from None
            sources[var] = written
            log(f"  {var} {y}: {f.name} [{', '.join(names)}] -> {step:g} degree ({written.split('regrid ')[-1]})")
    wind = p["variables"].get("WIND") or {}

    meta = {"description": p["description"] or f"daily weather prepared for {exp.aoi}",
            "source": "; ".join(([p["source"]] if p.get("source") else [])
                                + [f"{v}: {s}" for v, s in sources.items()]) or "see the files",
            "made_by": "scripts/prepare_weather.py", "made": dt.date.today().isoformat(),
            "files": f"inputs/weather/{p['name']}/{{var}}/{p['name']}_{{var}}_{{year}}.nc",
            "variables": [v for v in inputs.WEATHER_UNITS if v in p["variables"]],
            "wind_height_m": float(wind.get("height_m", 10.0)),
            "temperature_height_m": p["temperature_height_m"], "station_code": p["station_code"],
            "grid_step": step, "box": [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())],
            "grid_reference": {"var": "TMAX" if "TMAX" in p["variables"] else next(iter(p["variables"])),
                               "year": p["years"][0]}}
    old = inputs.manifest_path("weather", p["name"], cache)
    if old.is_file():                                       # keep the sources of years written earlier
        prev = __import__("json").loads(old.read_text(encoding="utf-8"))
        if not sources:
            meta["source"] = prev.get("source", meta["source"])
    return inputs.write_manifest("weather", p["name"], meta, cache)


def _names(src) -> list[str]:
    v = src["variable"]
    return list(v) if isinstance(v, (list, tuple)) else [v]


def source_files(pattern: str, exp, year: int, neighbours: bool = False) -> list[Path]:
    """The files of one variable for ``year``: the {year} file (with ``neighbours`` also the years
    before and after, whose edges complete the first and last local day), every file a * pattern
    matches, or the one multi-year file."""
    import glob
    out = []
    years = [year - 1, year, year + 1] if (neighbours and "{year}" in pattern) else [year]
    for y in years:
        f = _files(pattern, exp, y)
        if any(ch in str(f) for ch in "*?["):
            out += sorted(Path(g) for g in glob.glob(str(f)))
        elif f.is_file():
            out.append(f)
        if "{year}" not in pattern:
            break
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    if "{year}" in pattern and neighbours and _files(pattern, exp, year) not in seen and \
            not any(ch in str(_files(pattern, exp, year)) for ch in "*?["):
        return []                                     # the year's own file is missing
    return uniq


def _read_source(fs, name, box, margin, y, daily):
    """One variable for year ``y`` from files ``fs``, as daily values (or sub-daily if ``daily``)."""
    if daily:
        rng = (pd.Timestamp(y, 1, 1) - pd.Timedelta(days=1), pd.Timestamp(y + 1, 1, 1) + pd.Timedelta(days=1))
    else:
        rng = (pd.Timestamp(y, 1, 1), pd.Timestamp(y + 1, 1, 1))
    parts = []
    for f in fs:
        try:
            parts.append(read_block(f, name, box, margin, time_range=rng, subdaily=bool(daily)))
        except ValueError as e:
            if "no days of" in str(e):
                continue
            raise
    if not parts:
        raise ValueError(f"no time steps of {y} in {', '.join(f.name for f in fs)}")
    b0 = parts[0]
    if len(parts) > 1:
        for b in parts[1:]:
            if b.values.shape[1:] != b0.values.shape[1:] or not (np.allclose(b.lat, b0.lat) and np.allclose(b.lon, b0.lon)):
                raise ValueError(f"the files of {name} are not on one grid")
        dates = pd.DatetimeIndex(np.concatenate([b.dates.values for b in parts]))
        vals = np.concatenate([b.values for b in parts])
        order = np.argsort(dates.values, kind="stable")
        dates, vals = dates[order], vals[order]
        keep = ~dates.duplicated()
        b0 = Block(dates[keep], b0.lat, b0.lon, vals[keep], b0.units)
    return b0


def _one(exp, p, var, src, names, f, y, lat, lon, margin, path) -> str:
    """Read, convert, regrid and write one variable-year; returns its source description."""
    daily = src.get("daily")
    fs = source_files(src["files"], exp, y, neighbours=bool(daily))
    blocks = [_read_source(fs, n, p["box"], margin, y, daily) for n in names]
    if len(blocks) == 2:                              # wind from u and v components: speed per step
        u, v = blocks
        if len(u.dates) != len(v.dates) or not (u.dates == v.dates).all():
            raise ValueError(f"{names[0]} and {names[1]} do not have the same time steps")
        blocks = [Block(u.dates, u.lat, u.lon, np.sqrt(u.values ** 2 + v.values ** 2), u.units)]
    b = blocks[0]
    if daily:
        b = to_daily(b, daily, float(p.get("utc_offset_hours") or 0.0))
    sel = b.dates.year == y
    b = Block(b.dates[sel], b.lat, b.lon, b.values[sel], b.units)
    if len(b.dates) == 0:
        raise ValueError(f"no days of {y} in the file")
    n_days = 366 if pd.Timestamp(y, 12, 31).dayofyear == 366 else 365
    if daily and len(b.dates) < n_days:
        have = set(b.dates.dayofyear)
        miss = [d for d in range(1, n_days + 1) if d not in have]
        raise ValueError(f"only {len(b.dates)} of {n_days} days of {y} are complete (e.g. day {miss[0]}); the "
                         "first and last local day of a year need the time steps of the years before and after")
    scale, offset = conversion(var, src.get("units") or b.units)
    vals = b.values * scale + offset
    method = src.get("regrid") or default_method(b, lat, lon)
    out = regrid(Block(b.dates, b.lat, b.lon, vals, b.units), lat, lon, method)
    write_year(path, var, b.dates, lat, lon, out,
               title=f"{var} for {p['name']}, in DSSAT units ({inputs.WEATHER_UNITS[var]})",
               source=f"{f.name}: {', '.join(names)} ({b.units or src.get('units')}), regrid {method}",
               history="prepared by xdssat weatherprep.py")
    return f"{src['files']} [{', '.join(names)}, {b.units or src.get('units')}], regrid {method}"


# ----------------------------------------------------------------------------
# point sources: weather stations, NASA POWER points
# ----------------------------------------------------------------------------
# NASA POWER parameter -> DSSAT variable, for tables that use POWER's column names
POWER_COLUMNS = {"T2M_MAX": "TMAX", "T2M_MIN": "TMIN", "PRECTOTCORR": "RAIN", "PRECTOT": "RAIN",
                 "ALLSKY_SFC_SW_DWN": "SRAD", "T2MDEW": "DEWP", "WS2M": "WIND"}
MISSING = (-999.0, -99.0, -9999.0, -999.9)
_UNIT_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s+.*\(([^()]+)\)\s*$")
_LATLON = re.compile(r"lat[a-z]*\s*[:=]?\s*(-?\d+(?:\.\d+)?).{0,40}?lon[a-z]*\s*[:=]?\s*(-?\d+(?:\.\d+)?)",
                     re.IGNORECASE)


def read_table(path: Path, spec: dict | None = None) -> tuple[pd.DataFrame, dict, tuple | None]:
    """A daily point table (CSV): (values by date, units by column, (lat, lon) if the header says).

    Lines before the column header are read for ``NAME ... (unit)`` lines (as NASA POWER writes
    its parameters) and a ``latitude ... longitude ...`` line. The header is the first line
    naming a date: a DATE column, YEAR + DOY, or YEAR + MO + DY (or MONTH, DAY). Values in
    ``missing`` (default -999, -99, -9999, -999.9) are no data.
    """
    spec = spec or {}
    lines = Path(path).read_text(encoding="latin-1").splitlines()
    units, loc, head = {}, None, None
    for i, ln in enumerate(lines):
        cells = [c.strip().upper() for c in ln.replace(";", ",").split(",")]
        if "DATE" in cells or ("YEAR" in cells and ("DOY" in cells or "MO" in cells or "MONTH" in cells)):
            head = i
            break
        m = _UNIT_LINE.match(ln)
        if m:
            units[m.group(1).upper()] = m.group(2).strip()
        if loc is None:
            m = _LATLON.search(ln)
            if m:
                loc = (float(m.group(1)), float(m.group(2)))
    if head is None:
        raise ValueError(f"{Path(path).name}: no column header with a date (DATE, or YEAR with DOY or MO and DY)")
    df = pd.read_csv(path, skiprows=head, sep=None, engine="python")
    df.columns = [str(c).strip() for c in df.columns]
    up = {c.upper(): c for c in df.columns}
    if "DATE" in up:
        dates = pd.to_datetime(df[up["DATE"]].astype(str), format=spec.get("date_format"))
    elif "DOY" in up:
        dates = pd.to_datetime(df[up["YEAR"]].astype(int).astype(str), format="%Y") + \
            pd.to_timedelta(df[up["DOY"]].astype(int) - 1, unit="D")
    else:
        mo, dy = up.get("MO") or up.get("MONTH"), up.get("DY") or up.get("DAY")
        dates = pd.to_datetime(dict(year=df[up["YEAR"]], month=df[mo], day=df[dy]))
    df.index = pd.DatetimeIndex(dates).normalize()
    missing = spec.get("missing", MISSING)
    df = df.replace(list(missing), np.nan)
    return df, units, loc


def read_point(path: Path, spec: dict) -> tuple[float, float, pd.DataFrame, str]:
    """One point source in DSSAT units: (lat, lon, daily DataFrame of SRAD TMAX TMIN RAIN [DEWP WIND], text)."""
    path = Path(path)
    if path.suffix.upper() == ".WTH":
        import wth
        st, df = wth.read_wth(path)
        lat, lon = spec.get("lat", st.get("LAT")), spec.get("lon", st.get("LONG"))
        keep = {v: v for v in inputs.WEATHER_UNITS if v in df}
        if "TDEW" in df and "DEWP" not in keep:                 # the WTH name of dew point
            keep["DEWP"] = "TDEW"
        out = pd.DataFrame({v: df[c] for v, c in keep.items()}, index=df.index)
        return float(lat), float(lon), out, f"{path.name} (DSSAT weather file)"
    df, units, loc = read_table(path, spec)
    cols = spec.get("columns")
    if not cols:
        up = {c.upper(): c for c in df.columns}
        cols = {}
        for k, v in POWER_COLUMNS.items():              # PRECTOTCORR (bias-corrected) before PRECTOT
            if k in up:
                cols.setdefault(v, up[k])
        if not cols:
            raise ValueError(f"{path.name}: say which column holds which variable with columns: "
                             f"{{TMAX: ..., TMIN: ..., RAIN: ..., SRAD: ...}} (columns: {', '.join(df.columns)})")
    lat = spec.get("lat", loc[0] if loc else None)
    lon = spec.get("lon", loc[1] if loc else None)
    if lat is None or lon is None:
        raise ValueError(f"{path.name}: give lat and lon for this point (the file does not say)")
    out = {}
    for var, col in cols.items():
        if col not in df:
            raise ValueError(f"{path.name}: no column {col!r} for {var} (columns: {', '.join(df.columns)})")
        unit = (spec.get("units") or {}).get(var) or units.get(str(col).upper())
        if not unit:
            raise ValueError(f"{path.name}: the unit of {col} ({var}) is not in the file; give it with "
                             f"units: {{{var}: ...}}")
        sc, of = conversion(var, unit)
        out[var] = pd.to_numeric(df[col], errors="coerce") * sc + of
    return float(lat), float(lon), pd.DataFrame(out, index=df.index), f"{path.name} ({', '.join(f'{v}: {c}' for v, c in cols.items())})"


def nearest_point(lat, lon, plat, plon) -> np.ndarray:
    """Index of the nearest point for every cell of a (lat, lon) grid (distance in km, flat-earth)."""
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    k = np.cos(np.radians(la))[..., None]
    d = (la[..., None] - np.asarray(plat)) ** 2 + ((lo[..., None] - np.asarray(plon)) * k) ** 2
    return np.argmin(d, axis=-1)


def prepare_points(exp, p, out_dir: Path, overwrite: bool = False, log=print) -> Path:
    """Weather from point sources: every cell of the grid takes the series of its nearest point."""
    cache = exp.datasets()
    if not p["grid_step"]:
        raise config.ConfigError(["setting 'prepare > weather > grid_step' is needed with points: the cell "
                                  "size of the weather grid, e.g. 0.1."], exp.file)
    step = float(p["grid_step"])
    w, s_, e, n = p["box"]
    lat, lon = target_grid((w - step, s_ - step, e + step, n + step), step)
    pts = []
    for i, ps in enumerate(p["points"], 1):
        f = _files(ps["file"], exp)
        try:
            if not f.is_file():
                raise ValueError(f"{f} does not exist")
            pts.append(read_point(f, ps))
        except ValueError as err:
            raise config.ConfigError([f"setting 'prepare > weather > points > {i}' "
                                      f"({exp.where(f'prepare.weather.points.{i}')}):\n  {err}"], exp.file) from None
    have = set.intersection(*(set(df.columns) for _, _, df, _ in pts))
    need = [v for v in inputs.WEATHER_REQUIRED if v not in have]
    if need:
        raise config.ConfigError([f"prepare > weather > points: every point needs {', '.join(inputs.WEATHER_REQUIRED)}; "
                                  f"{', '.join(need)} is missing in at least one."], exp.file)
    variables = [v for v in inputs.WEATHER_UNITS if v in have]
    idx = nearest_point(lat, lon, [q[0] for q in pts], [q[1] for q in pts])
    log(f"weather '{p['name']}' from {len(pts)} point(s): grid {len(lat)} x {len(lon)} cells of {step:g} degree, "
        f"each cell from its nearest point; years {p['years'][0]}-{p['years'][-1]}")
    for var in variables:
        for y in p["years"]:
            path = out_dir / var / f"{p['name']}_{var}_{y}.nc"
            if path.exists() and not overwrite:
                continue
            days = pd.date_range(f"{y}-01-01", f"{y}-12-31")
            series = []
            for (plat, plon, df, txt), ps in zip(pts, p["points"]):
                s = df[var].reindex(days)
                if s.isna().any():
                    bad = s.index[s.isna()]
                    raise config.ConfigError([f"prepare > weather > points: {txt} has no {var} for {len(bad)} day(s) "
                                              f"of {y} (first {bad[0]:%Y-%m-%d}); fill them or shorten years."],
                                             exp.file)
                series.append(s.to_numpy("float64"))
            arr = np.stack(series, axis=1)                 # (time, point)
            vals = arr[:, idx]                             # (time, lat, lon)
            write_year(path, var, days, lat, lon, vals,
                       title=f"{var} for {p['name']}, in DSSAT units ({inputs.WEATHER_UNITS[var]})",
                       source="; ".join(t for _, _, _, t in pts), history="prepared by xdssat weatherprep.py (points)")
        log(f"  {var}: {len(p['years'])} year(s)")
    meta = {"description": p["description"] or f"daily weather from {len(pts)} point source(s)",
            "source": "; ".join(f"{t} at {a:.4f}, {b:.4f}" for a, b, _, t in pts),
            "made_by": "scripts/prepare_weather.py (points)", "made": dt.date.today().isoformat(),
            "files": f"inputs/weather/{p['name']}/{{var}}/{p['name']}_{{var}}_{{year}}.nc",
            "variables": variables, "wind_height_m": float(p["wind_height_m"] or 2.0),
            "temperature_height_m": p["temperature_height_m"], "station_code": p["station_code"],
            "grid_step": step, "box": [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())],
            "grid_reference": {"var": "TMAX", "year": p["years"][0]},
            "points": [{"lat": a, "lon": b, "source": t} for a, b, _, t in pts]}
    return inputs.write_manifest("weather", p["name"], meta, cache)


def inspect_table(path: Path) -> str:
    """What a point file holds: location, columns with units, dates."""
    path = Path(path)
    if path.suffix.upper() == ".WTH":
        import wth
        st, df = wth.read_wth(path)
        return (f"{path}\n  DSSAT weather file, station {st.get('INSI', '?')} at {st.get('LAT')}, {st.get('LONG')}\n"
                f"  {df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}, {len(df)} days; columns {', '.join(df.columns)}")
    df, units, loc = read_table(path)
    lines = [f"{path}", f"  table, {df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}, {len(df)} days",
             f"  location: {loc if loc else 'not in the file (give lat and lon)'}"]
    for c in df.columns:
        u = units.get(c.upper(), "")
        guess = POWER_COLUMNS.get(c.upper(), "")
        lines.append(f"  column {c:22s} {('(' + u + ')') if u else '':22s} {('-> ' + guess) if guess else ''}")
    return "\n".join(lines)

"""Ready-to-use inputs: what a run needs, in one standard form per kind, whatever the source.

xdssat runs from six kinds of prepared input. Each prepared dataset lives under
``<paths > cache>/inputs/<kind>/<name>/`` as a small manifest, ``input.json``,
that says where its files are and how to read them. The experiment file names
the dataset of each kind (``inputs:`` section); the runner reads only through
this module, so it never depends on where the data came from (AgERA5, ERA5-Land,
NASA POWER, CHIRPS, a national survey ...). Preparing a source means writing its
files in the form below and a manifest; ``check`` then says whether the result
meets the requirements, in plain words, before any analysis.

=============  ========  =====================================================
Kind           Needed    Standard form
=============  ========  =====================================================
weather        yes       daily netCDF, one file per variable per year, regular
                         lat/lon grid, dims (time, lat, lon), DSSAT variable
                         names and units (``WEATHER_UNITS``): SRAD, TMAX, TMIN,
                         RAIN required, DEWP and WIND optional. Values may be
                         floats or packed (``scale_factor``, ``add_offset``,
                         ``_FillValue`` are honoured). Time ``days``, ``hours``,
                         ``minutes`` or ``seconds since`` a date, Gregorian.
crop_mask      yes       GeoTIFF on any north-up grid and CRS; band 1 = crop area,
                         ``units_per_ha`` pixel units per hectare (100: percent of
                         a 1 ha pixel; 1: hectares). Optional ``class_band``.
soil           yes       a DSSAT ``.SOL`` file plus ``soil_map.csv`` (the profile
                         of each weather cell with crop); made for one weather
                         input and crop mask (``weather``, ``crop_mask``,
                         ``sub_points``). Older soils: ``cell_id`` per cell.
water_regime   no        GeoTIFF of classes + class table (CSV code, name), the
                         classes mapped to regimes by ``treatments >
                         water_regimes``; or of the irrigated share (``values:
                         share``). Crop off it: ``treatments > default_regime``.
region         no        GeoTIFF of classes + class table; ``aoi > region`` names
                         the class to keep. Without it the bbox decides.
zones          no        GeoTIFF of classes + class table, for ``by_zone``.
=============  ========  =====================================================

Class layers may be on any grid and CRS: they are read at the crop pixels (or the
sub-points of coarse crop pixels, ``rasters.subdivision``). The weather grid may
have any step; cells are named with 3-digit rows and 4-digit columns, so it has at
most 999 x 9999 cells. The prepare scripts make each kind from your own files:
``prepare_weather.py``, ``prepare_crop_mask.py``, ``prepare_soils.py``,
``prepare_layers.py``.

Manifest paths are relative to the cache folder unless absolute, so a cache can
move. ``register_existing`` writes manifests for files made by the original
xdssat scripts, without copying or rebuilding anything.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

import config

FORMAT = 1
KINDS = ("weather", "crop_mask", "soil", "water_regime", "region", "zones")
OPTIONAL = ("zones", "water_regime", "region")
WEATHER_REQUIRED = ("SRAD", "TMAX", "TMIN", "RAIN")
WEATHER_UNITS = {"SRAD": "MJ m-2 d-1", "TMAX": "degC", "TMIN": "degC", "RAIN": "mm d-1",
                 "DEWP": "degC", "WIND": "km d-1"}
MAX_ROWS, MAX_COLS = 999, 9999   # weather station codes W<iy:03d><ix:04d> (8 characters)
CLASS_KINDS = ("water_regime", "region", "zones")
_LOCK = threading.Lock()    # netCDF4/HDF5 is not thread-safe; the runner reads from threads


class InputError(config.ConfigError):
    """A prepared input is missing or does not meet the requirements."""


# ----------------------------------------------------------------------------
# manifests
# ----------------------------------------------------------------------------
def root(cache: Path | None = None) -> Path:
    return Path(cache or config.CACHE_DIR) / "inputs"


def manifest_path(kind: str, name: str, cache: Path | None = None) -> Path:
    return root(cache) / kind / name / "input.json"


def available(kind: str, cache: Path | None = None) -> list[str]:
    d = root(cache) / kind
    return sorted(p.parent.name for p in d.glob("*/input.json")) if d.is_dir() else []


def write_manifest(kind: str, name: str, meta: dict, cache: Path | None = None) -> Path:
    """Write ``input.json`` for a dataset (``meta`` without kind, name, format)."""
    if kind not in KINDS:
        raise ValueError(f"unknown input kind {kind!r}; one of {KINDS}")
    p = manifest_path(kind, name, cache)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": FORMAT, "kind": kind, "name": name,
            "written": dt.datetime.now().isoformat(timespec="seconds"), **meta}
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def open_dataset(kind: str, name: str, cache: Path | None = None):
    """The prepared dataset ``name`` of ``kind``; raises InputError if it is not there."""
    p = manifest_path(kind, name, cache)
    if not p.is_file():
        have = available(kind, cache)
        raise InputError([_missing_message(kind, name, have, cache)])
    meta = json.loads(p.read_text(encoding="utf-8"))
    cls = {"weather": Weather, "crop_mask": CropMask, "soil": Soil}.get(kind, ClassLayer)
    return cls(kind, name, meta, Path(cache or config.CACHE_DIR))


def open_all(exp) -> dict:
    """Every input the experiment names, by kind (None for an optional kind set to null)."""
    cache = exp.path("cache")
    return {k: (open_dataset(k, n, cache) if n else None) for k, n in exp.data["inputs"].items()}


def _missing_message(kind, name, have, cache) -> str:
    where = root(cache) / kind
    lst = f"Datasets of this kind there: {', '.join(have)}." if have else "There are none yet."
    return (f"The {kind.replace('_', ' ')} input '{name}' (setting 'inputs > {kind}') is not in {where}.\n"
            f"  {lst}\n  Prepare it with the matching script in scripts/, or, for files made by the "
            "original xdssat scripts, run  python scripts/register_inputs.py")


class Dataset:
    def __init__(self, kind: str, name: str, meta: dict, cache: Path):
        self.kind, self.name, self.meta, self.cache = kind, name, meta, cache

    def file(self, key: str = "file") -> Path:
        p = Path(self.meta[key])
        return p if p.is_absolute() else self.cache / p

    def summary(self) -> dict:
        return {"name": self.name, "source": self.meta.get("source", ""),
                "made_by": self.meta.get("made_by", "")}


# ----------------------------------------------------------------------------
# weather
# ----------------------------------------------------------------------------
def _decimal(v) -> float:
    """A float32 attribute as the decimal it was written as (np.float32(0.1) -> 0.1)."""
    return float(np.format_float_positional(np.float32(v))) if isinstance(v, np.float32) else float(v)


_TIME = re.compile(r"^\s*(days|hours|minutes|seconds)\s+since\s+(\S+)(?:\s+(\S+))?", re.I)
_TIME_UNIT = {"days": "D", "hours": "h", "minutes": "min", "seconds": "s"}


def _dates(tvar, keep_time: bool = False) -> pd.DatetimeIndex:
    """Dates of a CF time variable (days, hours, minutes or seconds since ...); the day only,
    unless ``keep_time`` (for sub-daily sources)."""
    m = _TIME.match(getattr(tvar, "units", ""))
    if not m:
        raise ValueError(f"time units {getattr(tvar, 'units', '')!r}: need 'days', 'hours', 'minutes' or "
                         "'seconds since <date>'")
    cal = getattr(tvar, "calendar", "standard").lower()
    if cal not in ("standard", "gregorian", "proleptic_gregorian"):
        raise ValueError(f"calendar {cal!r}: need a Gregorian calendar")
    origin = pd.Timestamp(m.group(2) + (" " + m.group(3) if m.group(3) else ""))
    t = pd.DatetimeIndex(origin + pd.to_timedelta(np.asarray(tvar[:], dtype="float64"),
                                                    unit=_TIME_UNIT[m.group(1).lower()]))
    return t if keep_time else t.normalize()


def _coord(f, *names):
    for n in names:
        if n in f.variables:
            return np.asarray(f[n][:])
    raise ValueError(f"no coordinate named {' or '.join(names)}")


class Weather(Dataset):
    """Daily gridded weather. ``files`` is a pattern with ``{var}`` and ``{year}``."""

    @classmethod
    def from_layout(cls, folder: Path, pattern: str, variables=tuple(WEATHER_UNITS), wind_height=10.0,
                    grid_reference=("TMAX", 2000)):
        meta = {"files": str(Path(folder) / pattern), "variables": list(variables),
                "wind_height_m": wind_height,
                "grid_reference": {"var": grid_reference[0], "year": grid_reference[1]}}
        return cls("weather", Path(folder).name, meta, Path(folder))

    @property
    def variables(self) -> list[str]:
        return list(self.meta["variables"])

    @property
    def wind_height(self) -> float:
        return float(self.meta.get("wind_height_m", 10.0))

    @property
    def temperature_height(self) -> float:
        return float(self.meta.get("temperature_height_m", 2.0))

    @property
    def station_code(self) -> str:
        """4-character institute code written into the WTH files."""
        return str(self.meta.get("station_code", "XDWX"))[:4]

    def path(self, var: str, year: int) -> Path:
        p = Path(self.meta["files"].format(var=var, year=year))
        return p if p.is_absolute() else self.cache / p

    def years(self, var: str) -> list[int]:
        pat = self.path(var, 1111)
        rx = re.compile(re.escape(pat.name).replace("1111", r"(\d{4})"))
        if not pat.parent.is_dir():
            return []
        return sorted(int(m.group(1)) for p in pat.parent.iterdir() if (m := rx.fullmatch(p.name)))

    def _read_year(self, var, year, iy, ix):
        with _LOCK, netCDF4.Dataset(self.path(var, year)) as f:
            x = f[var]
            x.set_auto_maskandscale(False)
            raw = x[:, iy, ix]
            fill = getattr(x, "_FillValue", getattr(x, "missing_value", None))
            scale, offset = _decimal(getattr(x, "scale_factor", 1.0)), _decimal(getattr(x, "add_offset", 0.0))
            dates = _dates(f["time"])
        vals = raw * scale if scale != 1.0 else raw.astype("float64")
        if offset:
            vals = vals + offset
        if fill is not None:
            vals = np.where(raw == fill, np.nan, vals)
        return dates, vals

    def grid(self, var: str | None = None, year: int | None = None):
        """(lat, lon, valid) of the weather grid; valid = cell has data on day 1."""
        ref = self.meta.get("grid_reference", {})
        var = var or ref.get("var", "TMAX")
        year = year or ref.get("year") or self.years(var)[0]
        with _LOCK, netCDF4.Dataset(self.path(var, year)) as f:
            lat, lon = _coord(f, "lat", "latitude"), _coord(f, "lon", "longitude")
            x = f[var]
            x.set_auto_maskandscale(False)
            day1 = x[0, :, :]
            fill = getattr(x, "_FillValue", getattr(x, "missing_value", None))
            valid = (day1 != fill) if fill is not None else np.isfinite(day1)
        return lat, lon, np.asarray(valid)

    @property
    def step(self) -> float:
        lat, lon, _ = self.grid()
        return float(abs(np.round(lon[1] - lon[0], 6)))

    def series(self, iy, ix, start, end, variables=None):
        """Daily values of selected cells, each variable an array (time, n_cells).

        Only the bounding block of the cells is read from each yearly file.
        """
        variables = tuple(variables or self.variables)
        iy, ix = np.asarray(iy), np.asarray(ix)
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        by, bx = slice(iy.min(), iy.max() + 1), slice(ix.min(), ix.max() + 1)
        out, dates = {}, None
        for var in variables:
            parts, dparts = [], []
            for y in range(start.year, end.year + 1):
                d, v = self._read_year(var, y, by, bx)
                parts.append(v[:, iy - by.start, ix - bx.start])
                dparts.append(d)
            d = pd.DatetimeIndex(np.concatenate(dparts))
            v = np.concatenate(parts)
            keep = (d >= start) & (d <= end)
            d, v = d[keep], v[keep]
            if dates is None:
                dates = d
            elif not d.equals(dates):
                missing = dates.symmetric_difference(d)
                raise ValueError(f"{var}: dates differ from {variables[0]} on {len(missing)} days, "
                                 f"e.g. {missing[:3].tolist()}")
            out[var] = v
        expected = pd.date_range(start, min(end, dates[-1]), freq="D")
        if not dates.equals(expected):
            raise ValueError(f"daily series has gaps between {start:%Y-%m-%d} and {end:%Y-%m-%d}")
        return dates, out

    def last_common_date(self, variables=None) -> pd.Timestamp:
        """Last day that every variable has (the newest year is often partial)."""
        ends = []
        for var in variables or self.variables:
            with netCDF4.Dataset(self.path(var, self.years(var)[-1])) as f:
                ends.append(_dates(f["time"])[-1])
        return min(ends)


def cell_frame(dates: pd.DatetimeIndex, values: dict[str, np.ndarray], k: int) -> pd.DataFrame:
    """One cell's daily weather as the DataFrame that ``wth.write_wth`` takes."""
    return pd.DataFrame({v: a[:, k] for v, a in values.items()}, index=dates)


# ----------------------------------------------------------------------------
# rasters: crop mask and class layers
# ----------------------------------------------------------------------------
class Raster(Dataset):
    @property
    def band(self) -> int:
        return int(self.meta.get("band", 1))

    def grid(self):
        """The raster's own grid (``rasters.Grid``), in its own CRS."""
        import rasterio
        import rasters
        with rasterio.open(self.file()) as r:
            t = r.transform
            if r.crs is None:
                raise InputError([f"The {self.kind} input '{self.name}' ({self.file()}) has no CRS."])
            if t.b != 0 or t.d != 0 or t.a <= 0 or t.e >= 0:
                raise InputError([f"The {self.kind} input '{self.name}' ({self.file()}) is not a north-up "
                                  "grid (rotated or flipped); write it north-up."])
            crs = rasters.CRS if r.crs == rasterio.crs.CRS.from_user_input(rasters.CRS) else r.crs.to_string()
            res_y = None if abs(-t.e - t.a) <= 1e-12 * t.a else float(-t.e)
            return rasters.Grid(float(t.c), float(t.f), r.width, r.height, float(t.a), crs, res_y)

    def profile(self) -> dict:
        import rasterio
        with rasterio.open(self.file()) as r:
            return {"crs": r.crs.to_string() if r.crs else None, "transform": tuple(r.transform)[:6],
                    "width": r.width, "height": r.height, "count": r.count}


class CropMask(Raster):
    @property
    def units_per_ha(self) -> float:
        return float(self.meta.get("units_per_ha", 1))

    @property
    def class_band(self) -> int | None:
        b = self.meta.get("class_band")
        return int(b) if b else None


class ClassLayer(Raster):
    @property
    def is_share(self) -> bool:
        """A water regime given as the irrigated share of each pixel (0-1), not as classes."""
        return self.meta.get("values") == "share"

    def classes(self) -> pd.DataFrame:
        """The class table: columns code and name (and any others)."""
        return pd.read_csv(self.file("classes"))

    def codes(self) -> dict:
        """Class name (always text, so aoi > region "2" matches a class named 2) -> raster code."""
        c = self.classes()
        return dict(zip(c["name"].astype(str), c["code"].astype(int)))


# ----------------------------------------------------------------------------
# soil
# ----------------------------------------------------------------------------
class Soil(Dataset):
    @property
    def weather(self) -> str:
        return self.meta.get("weather", "")

    @property
    def crop_mask(self) -> str:
        return self.meta.get("crop_mask", "")

    @property
    def cell_id(self) -> str:
        return self.meta.get("cell_id", "XS{iy:04d}{ix:04d}")

    def ids(self, iy, ix) -> list[str]:
        """The profile ID the runner uses for each cell (its own copy, ``XS<iy><ix>``)."""
        return [self.cell_id.format(iy=int(a), ix=int(b)) for a, b in zip(iy, ix)]

    def soil_map(self) -> pd.DataFrame | None:
        """iy, ix, soil_id (and ha, share ...) per weather cell; None for a soil written before maps."""
        return pd.read_csv(self.file("map"), dtype={"soil_id": str}) if self.meta.get("map") else None

    def profiles(self) -> dict:
        """Profiles by the cell IDs of ``ids`` (a cell's own copy where cells share a profile)."""
        from sol import read_sol
        m = self.soil_map()
        if m is None:
            return read_sol(self.file())
        import soilprep
        raw = read_sol(self.file(), ids=set(m.soil_id))
        return soilprep.cell_profiles(raw, m, self.cell_id)


# ----------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------
def check(exp, years=None) -> list[str]:
    """Plain-language problems with the inputs the experiment names (empty when all is well)."""
    if not exp.has_path("cache"):
        return []                       # config.check_files already says the cache is not set
    out = []

    def bad(kind, text):
        ln = exp.lines.get(f"inputs.{kind}")
        where = f"{exp.file.name} line {ln}, " if (exp.file and ln) else ""
        out.append(f"{where}setting 'inputs > {kind}':\n  {text}")

    ds = {}
    for kind, name in exp.data["inputs"].items():
        if not name:
            continue
        try:
            ds[kind] = open_dataset(kind, name, exp.path("cache"))
        except InputError as e:
            out.append(e.problems[0])
            continue
        if ds[kind].meta.get("format") != FORMAT:
            bad(kind, f"'{name}' was written in format {ds[kind].meta.get('format')}, this xdssat reads "
                      f"format {FORMAT}. Prepare it again.")
    w = ds.get("weather")
    if w is not None:
        miss = [v for v in WEATHER_REQUIRED if v not in w.variables]
        if miss:
            bad("weather", f"'{w.name}' lacks {', '.join(miss)}; a run needs {', '.join(WEATHER_REQUIRED)}.")
        for v in w.variables:
            if not w.years(v):
                bad("weather", f"'{w.name}' has no files for {v} (looked for {w.path(v, 2000)} and other years).")
        if not miss and all(w.years(v) for v in w.variables):
            have = set.intersection(*(set(w.years(v)) for v in WEATHER_REQUIRED))
            need = set(range((years or exp.years)[0], (years or exp.years)[-1] + 2))
            gap = sorted(need - have)
            if gap:
                bad("weather", f"'{w.name}' has no weather for {_ranges(gap)}, which the run years "
                               f"{exp.years[0]}-{exp.years[-1]} need (each season runs into the next "
                               "calendar year). Shorten 'years' or prepare those years.")
            try:
                lat, lon, _ = w.grid()
                if len(lat) > MAX_ROWS or len(lon) > MAX_COLS:
                    bad("weather", f"'{w.name}' has {len(lat)} x {len(lon)} cells; xdssat names weather cells "
                                   f"with 3-digit rows and 4-digit columns, so at most {MAX_ROWS} x {MAX_COLS}. "
                                   "Prepare it for a smaller box or a coarser grid.")
                steps = np.round(np.abs(np.diff(lon)), 6)
                if len(lon) > 2 and steps.max() - steps.min() > 1e-6:
                    bad("weather", f"'{w.name}' is not a regular grid (longitude steps vary).")
            except Exception as e:  # noqa: BLE001
                bad("weather", f"'{w.name}' cannot be read: {e}")
    # rasters: any north-up grid in any CRS, overlapping the area. Class layers on
    # another grid than the crop mask are sampled at the crop pixel centres.
    for kind, d in ((k, d) for k, d in ds.items() if k == "crop_mask" or k in CLASS_KINDS):
        if not d.file().is_file():
            bad(kind, f"'{d.name}' points at {d.file()}, which does not exist.")
            continue
        try:
            d.grid().window(*exp.bbox)
        except InputError as e:
            bad(kind, e.problems[0])
            continue
        except ValueError as e:
            bad(kind, f"'{d.name}' does not cover the area: {e}. Prepare it for aoi > bbox.")
            continue
        if kind == "crop_mask":
            prof = d.profile()
            if d.units_per_ha <= 0:
                bad(kind, f"'{d.name}' has units_per_ha {d.units_per_ha:g} in its input.json; it should be "
                          "the number of raster units in one hectare (1 for ha per pixel).")
            if d.class_band and d.class_band > prof["count"]:
                bad(kind, f"'{d.name}' names band {d.class_band} as its crop class, but the file has "
                          f"{prof['count']} band(s).")
        if kind in CLASS_KINDS and not getattr(d, "is_share", False) and not d.file("classes").is_file():
            bad(kind, f"'{d.name}' has no class table at {d.file('classes')}.")
    s = ds.get("soil")
    if s is not None:
        if not s.file().is_file():
            bad("soil", f"'{s.name}' points at {s.file()}, which does not exist.")
        if w is not None and s.weather and s.weather != w.name:
            bad("soil", f"'{s.name}' has one profile per cell of the weather grid '{s.weather}', but "
                        f"'inputs > weather' is '{w.name}'. Prepare the soil for that weather grid.")
        elif s.meta.get("map"):
            if not s.file("map").is_file():
                bad("soil", f"'{s.name}' has no soil map at {s.file('map')}.")
            else:
                import soilprep
                lack = sorted(set(s.soil_map().soil_id) - set(soilprep.sol_ids(s.file())))
                if lack:
                    bad("soil", f"'{s.name}': {len(lack)} profile(s) of its soil map are not in "
                                f"{s.file().name}, e.g. {', '.join(lack[:3])}. Prepare it again.")
        c = ds.get("crop_mask")
        made_for = s.meta.get("crop_mask")
        if c is not None and c.file().is_file() and (not made_for or made_for == c.name):
            import rasters as rmod
            try:
                k = rmod.subdivision(c.grid().window(*exp.bbox)[0])
            except (ValueError, InputError):
                k = None
            have = s.meta.get("sub_points", 1)
            if k is not None and k != have:
                bad("soil", f"'{s.name}' was made reading each crop pixel as {have} x {have} point(s), but the "
                            f"crop mask '{c.name}' is now read as {k} x {k} sub-points, which can put crop in "
                            "weather cells that have no profile. Prepare the soil again: "
                            f"python scripts/prepare_soils.py --aoi {exp.aoi} --overwrite")
        if c is not None and made_for and made_for != c.name:
            bad("soil", f"'{s.name}' was made for the crop mask '{made_for}', but 'inputs > crop_mask' is "
                        f"'{c.name}'. Profiles exist only for cells with crop in that mask, weighted by it; "
                        f"prepare the soil for this crop mask: python scripts/prepare_soils.py --aoi {exp.aoi}")
    if exp.data["seasons"]["mode"] == "by_zone" and not exp.data["inputs"].get("zones") \
            and not exp.data["seasons"]["zone_table"]:
        bad("zones", "seasons > mode is by_zone, but there is no zone layer and no zone table.")
    return out


def _ranges(years: list[int]) -> str:
    parts, start = [], years[0]
    for a, b in zip(years, years[1:] + [None]):
        if b != a + 1:
            parts.append(f"{start}" if start == a else f"{start}-{a}")
            start = b
    return ", ".join(parts)


# ----------------------------------------------------------------------------
# registering files made by the original scripts
# ----------------------------------------------------------------------------
# Names of the datasets the original Java workflow makes; configs/rice-java.yml uses them.
JAVA = {"weather": "agera5_idn", "crop_mask": "rice_atrbpn_idn", "soil": "soilgrids_java",
        "water_regime": "irrigation_moa_idn", "region": "islands_bbsdlp_idn", "zones": "aez_bbsdlp_idn"}


def register_existing(cache: Path | None = None, region: str = "idn", aoi: str = "java",
                      log=print) -> list[tuple[str, str, str]]:
    """Write manifests for the files of the original layout that exist; nothing is copied.

    Returns (kind, name, what happened) for every kind.
    """
    cache = Path(cache or config.CACHE_DIR)
    done = []

    def reg(kind, name, files_exist, meta):
        if not files_exist:
            done.append((kind, name, "not found, skipped"))
            return
        write_manifest(kind, name, meta, cache)
        done.append((kind, name, "registered"))

    wdir = cache / "agera5" / region
    reg("weather", f"agera5_{region}", (wdir / "TMAX").is_dir(), {
        "description": f"AgERA5 daily weather, region {region}, 0.1 degree, DSSAT units",
        "source": "AgERA5 (Copernicus), https://doi.org/10.24381/cds.6c68c9bb",
        "made_by": "scripts/build_agera5_cache.py",
        "files": f"agera5/{region}/{{var}}/agera5_{region}_{{var}}_{{year}}.nc",
        "variables": [v for v in WEATHER_UNITS if (wdir / v).is_dir()],
        "wind_height_m": 10.0, "temperature_height_m": 2.0, "station_code": "AGE5", "grid_step": 0.1,
        "grid_reference": {"var": "TMAX", "year": 2000}})
    rd = cache / "rasters"
    reg("crop_mask", f"rice_atrbpn_{region}", (rd / f"{region}_rice.tif").is_file(), {
        "description": "rice fields 2024, share of each 1 ha pixel (percent), and the field type",
        "source": "ATR/BPN Luas Baku Sawah 2024", "made_by": "scripts/prepare_rasters.py --layers rice",
        "file": f"rasters/{region}_rice.tif", "band": 1, "units_per_ha": 100, "class_band": 2})
    for kind, name, layer, what, made in (
            ("water_regime", f"irrigation_moa_{region}", "irrigation_filled", "irrigation",
             "MoA irrigation class (1-3 irrigated, 4 rainfed), gaps filled from the nearest class"),
            ("region", f"islands_bbsdlp_{region}", "region", "region", "island groups of the BBSDLP "
             "agro-ecological zone map"),
            ("zones", f"aez_bbsdlp_{region}", "aez", "aez", "BBSDLP agro-ecological zones")):
        reg(kind, name, (rd / f"{region}_{layer}.tif").is_file(), {
            "description": made, "source": "Ministry of Agriculture (MoA)" if kind == "water_regime" else "BBSDLP",
            "made_by": f"scripts/prepare_rasters.py --layers {layer}",
            "file": f"rasters/{region}_{layer}.tif", "band": 1, "classes": f"rasters/{region}_{what}_classes.csv"})
    reg("soil", f"soilgrids_{aoi}", (cache / "soils" / f"{aoi}.SOL").is_file(), {
        "description": f"one profile per weather cell with rice, {aoi}, from SoilGrids 2.0",
        "source": "SoilGrids 2.0 (ISRIC), CC-BY 4.0", "made_by": f"scripts/prepare_soils.py --aoi {aoi}",
        "crop_mask": f"rice_atrbpn_{region}",
        "file": f"soils/{aoi}.SOL", "weather": f"agera5_{region}", "cell_id": "XS{iy:04d}{ix:04d}"})
    for kind, name, what in done:
        log(f"  {kind:13s}{name:22s}{what}")
    return done

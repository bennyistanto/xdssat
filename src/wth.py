"""Read and write DSSAT daily weather files (``.WTH``).

Layout (``Weather/IPWTH_alt.for`` in dssat-csm-os)::

    $WEATHER : title

    @ INSI      LAT     LONG  ELEV   TAV   AMP REFHT WNDHT
      GR01   -6.200  106.800    10  27.0   1.5   -99   -99
    @  DATE  SRAD  TMAX  TMIN  RAIN
    1986001  21.2  32.5  21.8   0.0

* The station and record lines are read through their ``@`` headers
  (``PARSE_HEADERS``), see :mod:`fixedwidth`.
* The record date is always read from the first characters of the line, not
  through the header: ``I5`` (``YYDDD``) by default, or ``I7`` (``YYYYDDD``)
  when the file contains the ``$WEATHER`` keyword and the first record after a
  ``@...DATE`` header has seven digits (``IPWTH_alt.for:1004-1008``,
  ``InputModule/MAKEFILEW.f90:352-375``). We write seven-digit dates so one
  file can span any number of years without century guessing.
* ``TAV`` is the mean of daily mean temperature. ``AMP`` is the *full* range of
  monthly mean temperature (warmest month minus coolest): the soil temperature
  routine halves it itself (``SPAM/STEMP.for:351``,
  ``TA = TAV + TAMP * COS(ALX) / 2.0``).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from fixedwidth import format_row, parse_header

STATION_HEADER = "@ INSI      LAT     LONG  ELEV   TAV   AMP REFHT WNDHT"

# Columns DSSAT recognises in the daily records (IPWTH_alt.for:1025-1069),
# with the print format and the width of the header name (span = width + 1).
DAILY_FORMATS = {
    "SRAD": ".1f",   # MJ m-2 d-1
    "TMAX": ".1f",   # degC
    "TMIN": ".1f",   # degC
    "RAIN": ".1f",   # mm d-1
    "DEWP": ".1f",   # degC
    "WIND": ".0f",   # km d-1
    "PAR": ".1f",    # mol m-2 d-1
    "RHUM": ".1f",   # %
    "VAPR": ".2f",   # kPa
}
REQUIRED = ("SRAD", "TMAX", "TMIN", "RAIN")


def tav_amp(tmax, tmin, dates) -> tuple[float, float]:
    """``TAV`` and ``AMP`` from daily data, as the engine expects them."""
    t = (np.asarray(tmax, float) + np.asarray(tmin, float)) / 2.0
    s = pd.Series(t, index=pd.DatetimeIndex(dates))
    s = s[np.isfinite(s.values)]
    monthly = s.groupby(s.index.month).mean()
    return float(s.mean()), float(monthly.max() - monthly.min())


def write_wth(path, data: pd.DataFrame, insi: str, lat: float, lon: float,
              elev: float | None = None, tav: float | None = None,
              amp: float | None = None, refht: float | None = None,
              wndht: float | None = None, title: str = "",
              seven_digit: bool = True) -> Path:
    """Write one weather file.

    Parameters
    ----------
    data
        Daily records indexed by date (``DatetimeIndex``), with at least the
        columns SRAD, TMAX, TMIN, RAIN in DSSAT units. Other columns from
        ``DAILY_FORMATS`` are written when present. NaN becomes -99.
    insi
        Station code (up to 4 characters) for the header line. Not used to
        find the file: the file name comes from the FileX ``WSTA`` field.
    tav, amp
        Computed from ``data`` when not given.
    seven_digit
        Write ``YYYYDDD`` dates with the ``$WEATHER`` keyword (default), or
        legacy ``YYDDD`` dates.
    """
    path = Path(path)
    missing = [c for c in REQUIRED if c not in data.columns]
    if missing:
        raise ValueError(f"weather data lacks required columns {missing}")
    idx = pd.DatetimeIndex(data.index)
    if not idx.is_monotonic_increasing or idx.has_duplicates:
        raise ValueError("weather dates must be strictly increasing")
    if len(idx) > 1 and (np.diff(idx.values).astype("timedelta64[D]") != np.timedelta64(1, "D")).any():
        raise ValueError("weather dates must be consecutive days (no gaps)")
    if len(insi) > 4:
        raise ValueError("INSI is at most 4 characters")

    if tav is None or amp is None:
        t_av, t_amp = tav_amp(data["TMAX"], data["TMIN"], idx)
        tav = t_av if tav is None else tav
        amp = t_amp if amp is None else amp

    cols = [c for c in DAILY_FORMATS if c in data.columns]
    st_cols = parse_header(STATION_HEADER)
    station = format_row(
        st_cols,
        {"INSI": insi, "LAT": lat, "LONG": lon, "ELEV": -99 if elev is None else elev,
         "TAV": tav, "AMP": amp, "REFHT": -99 if refht is None else refht,
         "WNDHT": -99 if wndht is None else wndht},
        fmts={"LAT": ".3f", "LONG": ".3f", "ELEV": ".0f", "TAV": ".1f", "AMP": ".1f",
              "REFHT": ".1f", "WNDHT": ".1f"},
    )

    date_w = 7 if seven_digit else 5
    date_head = "@  DATE" if seven_digit else "@DATE"
    header = date_head + "".join(f" {c:>5}" for c in cols)

    if seven_digit:
        dates = (idx.year * 1000 + idx.dayofyear).astype(int)
    else:
        dates = ((idx.year % 100) * 1000 + idx.dayofyear).astype(int)

    block = [f"{d:0{date_w}d}" for d in dates]
    for c in cols:
        v = np.asarray(data[c].values, dtype=float)
        fmt = DAILY_FORMATS[c]
        txt = np.where(np.isfinite(v), np.char.mod(f"%6{fmt}", np.nan_to_num(v)), "   -99")
        too_long = np.char.str_len(txt) > 6
        if too_long.any():
            bad = v[too_long][0]
            raise ValueError(f"{c} value {bad} does not fit the 5-character column")
        block = [b + t for b, t in zip(block, txt)]

    lines = [
        f"$WEATHER : {title}".rstrip() if seven_digit else f"*WEATHER : {title}".rstrip(),
        "",
        STATION_HEADER,
        station,
        header,
        *block,
    ]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def read_wth(path) -> tuple[dict, pd.DataFrame]:
    """Read a weather file into (station dict, daily DataFrame indexed by date).

    Accepts both 5-digit and 7-digit dates. Used for tests and inspection.
    """
    lines = Path(path).read_text(encoding="latin-1").splitlines()
    station: dict = {}
    records = []
    header_cols = None
    is_daily = False
    for ln in lines:
        if not ln.strip() or ln.startswith(("*", "$", "!")):
            continue
        if ln.startswith("@"):
            header_cols = parse_header(ln)
            is_daily = any(c.name == "DATE" for c in header_cols)
            continue
        if header_cols is None:
            continue
        if not is_daily:
            for c in header_cols:
                txt = ln[c.start - 1 : c.end].strip()
                station[c.name] = txt
            continue
        records.append(ln)

    if not records:
        raise ValueError(f"no daily records in {path}")
    first = records[0].split()[0]
    n = len(first) if len(first) in (5, 7) else 5
    vals = {c.name: [] for c in header_cols if c.name != "DATE"}
    dates = []
    for ln in records:
        d = int(ln[:n])
        if n == 5:
            yy, doy = divmod(d, 1000)
            year = 1900 + yy if yy >= 30 else 2000 + yy  # test helper only
        else:
            year, doy = divmod(d, 1000)
        dates.append(pd.Timestamp(year=year, month=1, day=1) + pd.Timedelta(days=doy - 1))
        for c in header_cols:
            if c.name == "DATE":
                continue
            txt = ln[c.start - 1 : c.end].strip()
            vals[c.name].append(float(txt) if txt else np.nan)
    df = pd.DataFrame(vals, index=pd.DatetimeIndex(dates, name="date"))
    df = df.replace(-99.0, np.nan)
    for k in ("LAT", "LONG", "ELEV", "TAV", "AMP", "REFHT", "WNDHT"):
        if k in station:
            try:
                station[k] = float(station[k])
            except ValueError:
                pass
    return station, df

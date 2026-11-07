"""Build gridded FileX experiments from a template FileX.

Approach ("thin" FileX handling)
--------------------------------
The user supplies an ordinary DSSAT experiment file (the *template*), for
example a calibrated rice experiment with transplanting, puddling, bunds,
flood depths and split nitrogen. Every simulation unit (one grid cell, one
season) copies one template treatment and changes only:

* the field: weather station code ``WSTA`` (one weather file per cell), soil
  profile ``ID_SOIL``, coordinates and elevation;
* the dates: every date in the template treatment (planting, irrigation and
  flood operations, fertilizer, residue, initial conditions, simulation start,
  automatic-management windows, harvest) moves by the same number of days, so
  the template's timing relative to planting is kept;
* optional per-unit overrides of single columns (cultivar code, plant
  population, ...), given as ``"<factor>.<COLUMN>"`` columns of the units
  table, e.g. ``"MP.PPOP"`` or ``"CU.INGENO"``.

Identical factor levels are written once and shared between treatments.

Format facts used here (dssat-csm-os, verified 2026-11-05):

* Treatment rows: ``FORMAT (I3,I1,2(1X,I1),1X,A25,14I3)`` (``ipexp.for:865``),
  so at most 999 treatments and levels.
* Field rows: ``FORMAT (I3,A8,1X,2A4,1X,F5.0,1X,F5.0,1X,A5,2(1X,F5.0),
  2(1X,A5),1X,F5.0,1X,A10)`` and ``FORMAT (I3,2(A15,1X),A9,1X,F17.0,
  3(1X,F5.0),1X,A5,I6)`` (``ipexp.for:1300-1305``). All ``@L ID_FIELD`` rows
  must come before all ``@L ...XCRD`` rows (``MAKEFILEW.f90:93``).
* Other management rows are fixed formats whose fields line up with the header
  names: each field is one blank plus the characters up to the end of its
  header name (e.g. ``IPMAN.for:228,616``; ``IPSLIN.for:206-207``). The level
  number is ``I3`` in characters 1-3.
* Two-digit years: ``YY <= 40`` is 20YY, otherwise 19YY (``DATES.for:84-87``).
* Environment modifications: ``FORMAT (I3,I5,5(1X,A1,F4.0),1X,A1,F4.0,
  2(1X,A1,F4.0))`` (``IPENV.for:1000``), i.e. level, ODATE, then eight
  (code, amount) pairs EDAY ERAD EMAX EMIN ERAIN ECO2 EDEW EWIND. Codes A add,
  S subtract, M multiply, R replace (``Weather/WTHMOD.for`` ``WTHSET``); a row
  applies from ODATE on (``WTHMOD.for``, ``YYDDD .GE. WMDATE``).
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from fixedwidth import parse_header

# Factor columns of *TREATMENTS, in file order, and the section each refers to.
FACTORS = ["CU", "FL", "SA", "IC", "MP", "MI", "MF", "MR", "MC", "MT", "ME", "MH", "SM"]
FACTOR_SECTION = {
    "CU": "CULTIVARS",
    "FL": "FIELDS",
    "SA": "SOIL ANALYSIS",
    "IC": "INITIAL CONDITIONS",
    "MP": "PLANTING DETAILS",
    "MI": "IRRIGATION AND WATER MANAGEMENT",
    "MF": "FERTILIZERS",
    "MR": "RESIDUES AND ORGANIC FERTILIZER",
    "MC": "CHEMICAL APPLICATIONS",
    "MT": "TILLAGE",
    "ME": "ENVIRONMENT MODIFICATIONS",
    "MH": "HARVEST DETAILS",
    "SM": "SIMULATION CONTROLS",
}
# Factors whose dates we do not shift yet; templates must leave them at 0.
UNSUPPORTED = ("SA", "MC", "MT")

# Header names that hold dates (YYDDD or YYYYDDD).
DATE_COLUMNS = {"PDATE", "EDATE", "IDATE", "FDATE", "RDATE", "ICDAT", "SDATE",
                "PFRST", "PLAST", "HFRST", "HLAST", "HDATE", "TDATE", "CDATE", "ODATE"}

TREATMENT_HEADER = ("@N R O C TNAME.................... CU FL SA IC MP MI MF MR MC MT ME MH SM")

MAX_TREATMENTS = 999  # treatment number is I3 (ipexp.for:865)
MAX_LEVELS = 99       # some readers take factor levels as I2 (see _set_level)


# ----------------------------------------------------------------------------
# dates
# ----------------------------------------------------------------------------
def decode_date(txt: str) -> dt.date | None:
    """YYDDD or YYYYDDD text to a date; None for -99, 0 and blanks."""
    s = txt.strip()
    if not s.lstrip("-").isdigit():
        return None
    v = int(s)
    if v <= 0:
        return None
    if len(s) <= 5:
        yy, doy = divmod(v, 1000)
        year = 2000 + yy if yy <= 40 else 1900 + yy
    else:
        year, doy = divmod(v, 1000)
    if not 1 <= doy <= 366:
        return None
    return dt.date(year, 1, 1) + dt.timedelta(days=doy - 1)


def encode_date(d: dt.date, width: int = 5) -> str:
    doy = d.timetuple().tm_yday
    if width == 5:
        if not 1941 <= d.year <= 2040:
            raise ValueError(f"{d} cannot be written as YYDDD (1941-2040 only)")
        return f"{d.year % 100:02d}{doy:03d}"
    return f"{d.year:04d}{doy:03d}"


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------
@dataclass
class Section:
    title: str                      # the '*' line
    body: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.title[1:].split(":")[0].strip().upper()

    def matches(self, key: str) -> bool:
        return self.name.startswith(key)


def _level(row: str) -> int | None:
    s = row[:3].strip()
    return int(s) if s.isdigit() else None


def _is_header(line: str) -> bool:
    return line.startswith("@")


def _is_data(line: str) -> bool:
    return bool(line.strip()) and not line.startswith(("!", "@", "*"))


def level_blocks(body: list[str]) -> "OrderedDict[int, list[str]]":
    """Lines belonging to each level, with the headers they sit under.

    A header followed directly by another header (for example
    ``@  AUTOMATIC MANAGEMENT``) travels with the header after it.
    """
    blocks: OrderedDict[int, list[str]] = OrderedDict()
    last_headers: dict[int, int] = {}
    pending: list[str] = []
    current: list[str] = []
    current_id = -1
    for line in body:
        if _is_header(line):
            pending.append(line)
            continue
        if not _is_data(line):
            continue
        if pending:
            current, pending = pending, []
            current_id += 1
        lvl = _level(line)
        if lvl is None:
            continue
        b = blocks.setdefault(lvl, [])
        if last_headers.get(lvl) != current_id:
            b.extend(current)
            last_headers[lvl] = current_id
        b.append(line)
    return blocks


class FileX:
    """A parsed FileX: the title line plus its sections, in file order."""

    def __init__(self, title: str, sections: list[Section], path: Path | None = None):
        self.title = title
        self.sections = sections
        self.path = path

    @classmethod
    def read(cls, path) -> "FileX":
        path = Path(path)
        lines = path.read_text(encoding="latin-1").splitlines()
        title = ""
        sections: list[Section] = []
        for ln in lines:
            if ln.startswith("*"):
                if ln.upper().startswith("*EXP.DETAILS"):
                    title = ln
                    continue
                sections.append(Section(ln))
            elif sections:
                sections[-1].body.append(ln)
        return cls(title, sections, path)

    def section(self, key: str) -> Section | None:
        for s in self.sections:
            if s.matches(key):
                return s
        return None

    def treatments(self) -> pd.DataFrame:
        """Treatment table with factor levels as integers."""
        sec = self.section("TREATMENTS")
        rows = [ln for ln in sec.body if _is_data(ln)]
        recs = []
        for r in rows:
            r = r.ljust(34 + 3 * 14)
            rec = {"N": int(r[0:3]), "R": r[3], "O": r[5], "C": r[7], "TNAME": r[9:34].strip()}
            for i, f in enumerate(FACTORS):
                txt = r[34 + 3 * i: 37 + 3 * i].strip()
                rec[f] = int(txt) if txt else 0
            recs.append(rec)
        return pd.DataFrame(recs).set_index("N")

    def blocks(self, factor: str) -> "OrderedDict[int, list[str]]":
        sec = self.section(FACTOR_SECTION[factor])
        return level_blocks(sec.body) if sec else OrderedDict()

    def planting_date(self, level: int) -> dt.date | None:
        for hdr, row in _rows_with_header(self.blocks("MP").get(level, [])):
            cols = {c.name: c for c in parse_header(hdr)}
            if "PDATE" in cols:
                return decode_date(_span(row, cols["PDATE"]))
        return None


# ----------------------------------------------------------------------------
# row editing
# ----------------------------------------------------------------------------
def _rows_with_header(block: list[str]):
    hdr = None
    for ln in block:
        if _is_header(ln):
            hdr = ln
        else:
            yield hdr, ln


def _span(row: str, col) -> str:
    return row.ljust(col.end)[col.start - 1: col.end]


def _put(row: str, start: int, end: int, text: str, left: bool = False) -> str:
    """Write ``text`` into characters start..end (1-based, inclusive)."""
    width = end - start + 1
    if len(text) > width:
        raise ValueError(f"{text!r} does not fit in columns {start}-{end}")
    text = text.ljust(width) if left else text.rjust(width)
    row = row.ljust(end)
    return row[: start - 1] + text + row[end:]


def _set_level(row: str, level: int) -> str:
    """Level number right-aligned in characters 1-2, character 3 blank.

    Most readers take the level as ``I3`` (characters 1-3), but
    ``MAKEFILEW.f90:145,200`` and ``IPSIM.for:1331-1524`` read it as ``I2``, so
    factor levels are capped at 99 per FileX (``MAX_LEVELS``).
    """
    if not 1 <= level <= MAX_LEVELS:
        raise ValueError(f"level {level} outside 1-{MAX_LEVELS}")
    return f"{level:>2} " + row.ljust(3)[3:]


def _shift_row(hdr: str, row: str, days: int) -> str:
    if days == 0 or hdr is None:
        return row
    for c in parse_header(hdr):
        if c.name not in DATE_COLUMNS:
            continue
        txt = _span(row, c).strip()
        d = decode_date(txt)
        if d is None:
            continue
        new = encode_date(d + dt.timedelta(days=days), width=7 if len(txt) == 7 else 5)
        row = _put(row, c.end - len(new) + 1, c.end, new)
    return row


def _override_row(hdr: str, row: str, overrides: dict) -> str:
    if not overrides or hdr is None:
        return row
    cols = parse_header(hdr)
    for i, c in enumerate(cols):
        if c.name not in overrides:
            continue
        v = overrides[c.name]
        is_last = i == len(cols) - 1
        if isinstance(v, str):
            # Text: left-aligned from the start of the span (CNAME, INGENO ...)
            start = c.start
            end = max(c.end, start + len(v) - 1) if is_last else c.end
            row = _put(row, start, end, v, left=True)
        else:
            txt = _fmt_num(v, c.width)
            row = _put(row, c.start, c.end, txt)
    return row


def _fmt_num(v, width: int) -> str:
    if float(v) == int(v):
        s = str(int(v))
    else:
        s = f"{v:.{max(width - 2, 0)}g}"
        while len(s) > width and "." in s:
            s = s[:-1]
    if len(s) > width:
        raise ValueError(f"{v} does not fit in {width} characters")
    return s


def remap_block(block: list[str], new_level: int, days: int = 0,
                overrides: dict | None = None) -> list[str]:
    """Copy one level's lines with a new level number, shifted dates and overrides."""
    out = []
    hdr = None
    for ln in block:
        if _is_header(ln):
            hdr = ln
            out.append(ln)
            continue
        row = _set_level(ln, new_level)
        row = _shift_row(hdr, row, days)
        row = _override_row(hdr, row, overrides or {})
        out.append(row)
    return out


# ----------------------------------------------------------------------------
# fields
# ----------------------------------------------------------------------------
def field_rows(template_block: list[str], level: int, field_id: str, wsta: str,
               soil_id: str, lat: float | None, lon: float | None,
               elev: float | None) -> tuple[str, str]:
    """The two *FIELDS rows of one grid cell, built from a template level.

    Columns follow ``ipexp.for:1300-1305``: ID_FIELD 4-11, WSTA 13-20,
    ID_SOIL 70-79; XCRD 4-18, YCRD 20-34, ELEV 36-44.
    """
    rows = [ln for ln in template_block if not _is_header(ln)]
    if len(rows) < 2:
        raise ValueError("template field level needs both *FIELDS rows")
    r1, r2 = rows[0], rows[1]
    if len(wsta) not in (4, 8):
        raise ValueError(f"WSTA must be 4 or 8 characters, got {wsta!r}")
    if len(soil_id) != 10:
        raise ValueError(f"ID_SOIL must be 10 characters, got {soil_id!r}")
    r1 = _set_level(r1, level)
    r1 = _put(r1, 4, 11, field_id[:8], left=True)
    r1 = _put(r1, 13, 20, wsta, left=True)
    r1 = _put(r1, 70, 79, soil_id, left=True)
    r2 = _set_level(r2, level)
    if lon is not None:
        r2 = _put(r2, 4, 18, f"{lon:.5f}")
    if lat is not None:
        r2 = _put(r2, 20, 34, f"{lat:.5f}")
    if elev is not None and elev == elev:
        r2 = _put(r2, 36, 44, f"{elev:.0f}")
    return r1, r2


# ----------------------------------------------------------------------------
# environment modifications (scenarios)
# ----------------------------------------------------------------------------
ENV_COLUMNS = ("EDAY", "ERAD", "EMAX", "EMIN", "ERAIN", "ECO2", "EDEW", "EWIND")
ENV_HEADER = "@E ODATE EDAY  ERAD  EMAX  EMIN  ERAIN ECO2  EDEW  EWIND ENVNAME"
_SECTION_ORDER = ["TREATMENTS", "CULTIVARS", "FIELDS", "SOIL ANALYSIS", "INITIAL CONDITIONS",
                  "PLANTING DETAILS", "IRRIGATION", "FERTILIZERS", "RESIDUES", "CHEMICAL",
                  "TILLAGE", "ENVIRONMENT", "HARVEST", "SIMULATION"]


def with_environment(template: FileX, changes: dict, name: str = "") -> FileX:
    """A copy of ``template`` whose every treatment uses one environment modification level.

    ``changes`` maps a column of ``ENV_COLUMNS`` to ``(code, amount)``, e.g.
    ``{"ECO2": ("R", 550), "EMAX": ("A", 2)}``; columns left out are ``A 0``
    (no change). The modification starts on the earliest simulation start
    date (SDATE) of the template, and moves with each treatment's dates like
    every other date, so it covers the whole season. Refuses a template whose
    treatments already use environment modifications.
    """
    import copy

    trt = template.treatments()
    if (trt["ME"] != 0).any():
        raise ValueError("template treatments already use ENVIRONMENT MODIFICATIONS")
    bad = set(changes) - set(ENV_COLUMNS)
    if bad:
        raise ValueError(f"unknown environment columns {sorted(bad)}; use {ENV_COLUMNS}")
    starts = []
    for block in template.blocks("SM").values():
        for hdr, row in _rows_with_header(block):
            cols = {c.name: c for c in parse_header(hdr)} if hdr else {}
            if "SDATE" in cols:
                d = decode_date(_span(row, cols["SDATE"]).strip())
                if d:
                    starts.append(d)
    if not starts:
        raise ValueError("template has no simulation start date (SDATE)")
    row = f"{1:>2} {encode_date(min(starts))}"
    for col in ENV_COLUMNS:
        code, amount = changes.get(col, ("A", 0))
        if code not in "ASMR" or len(code) != 1:
            raise ValueError(f"{col}: code must be A, S, M or R, got {code!r}")
        row += f" {code}{_fmt_num(amount, 4):>4}"
    row += f" {name[:20]}".rstrip()
    out = copy.deepcopy(template)
    sec = out.section("TREATMENTS")
    sec.body = [(_put(ln.ljust(34 + 3 * 13), 65, 67, f"{1:>3}") if _is_data(ln) else ln) for ln in sec.body]
    new = Section("*ENVIRONMENT MODIFICATIONS", [ENV_HEADER, row, ""])
    old = next((i for i, s in enumerate(out.sections) if s.matches("ENVIRONMENT")), None)
    if old is not None:
        out.sections[old] = new
    else:
        rank = {k: i for i, k in enumerate(_SECTION_ORDER)}
        me = rank["ENVIRONMENT"]
        pos = next((i for i, s in enumerate(out.sections)
                    if next((r for k, r in rank.items() if s.matches(k)), -1) > me), len(out.sections))
        out.sections.insert(pos, new)
    return out


# ----------------------------------------------------------------------------
# builder
# ----------------------------------------------------------------------------
class TooManyLevels(ValueError):
    pass


@dataclass
class BuiltFileX:
    text: str
    units: pd.DataFrame  # input units with the treatment number added as TRNO


def build_filex(template: FileX, units: pd.DataFrame, exp_code: str = "XDSS0001",
                crop_code: str | None = None, title: str = "xdssat gridded run",
                soils: dict | None = None, ic_water: float | None = None,
                simulation_overrides: dict | None = None) -> BuiltFileX:
    """Write one FileX with one treatment per row of ``units``.

    ``units`` columns
        cell      identifier of the grid cell (any hashable; used in TNAME)
        wsta      weather station code, 4 or 8 characters (file ``<wsta>.WTH``)
        soil_id   10-character soil profile ID (in ``SOIL.SOL`` of the run dir)
        pdate     planting (or transplanting) date of this season
        lat, lon, elev   optional, written to the field coordinates
        trno_template    optional, template treatment to copy (default 1)
        "<F>.<COL>"      optional overrides, e.g. "MP.PPOP", "CU.INGENO"

    ``soils`` and ``ic_water``
        When both are given, initial soil water is rebuilt per soil profile as
        ``LL + ic_water * (DUL - LL)`` on the profile's own layers, keeping the
        template's NH4 and NO3 of the top layer. Otherwise the template's
        initial-condition layers are used as they are.
    ``simulation_overrides``
        Columns of the simulation-controls rows to force, e.g.
        ``{"NYERS": 1}`` (always applied) or output switches.
    """
    if len(units) == 0:
        raise ValueError("no units")
    if len(units) > MAX_TREATMENTS:
        raise ValueError("a FileX holds at most 999 treatments (ipexp.for:865)")
    need = {"cell", "wsta", "soil_id", "pdate"}
    missing = need - set(units.columns)
    if missing:
        raise ValueError(f"units lack columns {sorted(missing)}")

    trt = template.treatments()
    units = units.reset_index(drop=True).copy()
    if "trno_template" not in units:
        units["trno_template"] = int(trt.index[0])

    sim_over = {"NYERS": 1, **(simulation_overrides or {})}
    level_keys: dict[str, OrderedDict] = {f: OrderedDict() for f in FACTORS}
    new_blocks: dict[str, list[list[str]]] = {f: [] for f in FACTORS}
    treatment_rows = []

    for i, u in units.iterrows():
        t = int(u["trno_template"])
        if t not in trt.index:
            raise ValueError(f"template has no treatment {t}")
        trow = trt.loc[t]
        for f in UNSUPPORTED:
            if trow[f] != 0:
                raise NotImplementedError(
                    f"template treatment {t} uses factor {f}; only 0 is supported so far")
        anchor = template.planting_date(int(trow["MP"])) if trow["MP"] else None
        if anchor is None:
            raise ValueError(f"template treatment {t} has no planting date to anchor on")
        pdate = pd.Timestamp(u["pdate"]).date()
        days = (pdate - anchor).days

        levels = {}
        for f in FACTORS:
            tl = int(trow[f])
            if tl == 0 or f in UNSUPPORTED:
                levels[f] = 0
                continue
            ov = {k.split(".", 1)[1]: v for k, v in u.items()
                  if isinstance(k, str) and k.startswith(f + ".") and v == v}
            if f == "FL":
                key = (tl, u["wsta"], u["soil_id"], u.get("lat"), u.get("lon"), u.get("elev"))
            elif f == "CU":
                key = (tl, tuple(sorted(ov.items())))
            elif f == "IC" and soils is not None and ic_water is not None and u["soil_id"] in soils:
                key = (tl, days, u["soil_id"])
            elif f == "SM":
                key = (tl, days)
            else:
                key = (tl, days, tuple(sorted(ov.items())))
            if key not in level_keys[f]:
                n = len(level_keys[f]) + 1
                if n > MAX_LEVELS:
                    raise TooManyLevels(
                        f"factor {f} needs more than {MAX_LEVELS} levels in one FileX; "
                        "use smaller chunks (runner.split_chunks does this)")
                level_keys[f][key] = n
                block = template.blocks(f).get(tl)
                if block is None:
                    raise ValueError(f"template has no {f} level {tl}")
                if f == "FL":
                    fid = f"XD{(len(level_keys[f])):06d}"
                    new_blocks[f].append(list(field_rows(
                        block, n, fid, u["wsta"], u["soil_id"],
                        _opt(u, "lat"), _opt(u, "lon"), _opt(u, "elev"))))
                elif f == "CU":
                    new_blocks[f].append(remap_block(block, n, 0, ov))
                elif len(key) == 3 and f == "IC" and key[2] == u["soil_id"]:
                    new_blocks[f].append(_ic_from_soil(block, n, days, soils[u["soil_id"]], ic_water))
                elif f == "SM":
                    new_blocks[f].append(remap_block(block, n, days, sim_over))
                else:
                    new_blocks[f].append(remap_block(block, n, days, ov))
            levels[f] = level_keys[f][key]

        name = str(u["name"]) if "name" in u and u["name"] == u["name"] else f"{u['cell']} {pdate:%Y%m%d}"
        n_txt = f"{i + 1:>2} " if i + 1 <= 99 else f"{i + 1:>3}"
        row = f"{n_txt}{trow['R']:>1} {trow['O']:>1} {trow['C']:>1} {name[:25]:<25}"
        row += "".join(f"{levels[f]:>3}" for f in FACTORS)
        treatment_rows.append(row)

    units["TRNO"] = range(1, len(units) + 1)
    text = _assemble(template, exp_code, crop_code, title, treatment_rows, new_blocks)
    return BuiltFileX(text=text, units=units)


def _opt(u, k):
    v = u.get(k)
    return None if v is None or v != v else float(v)


def _ic_from_soil(block: list[str], level: int, days: int, soil, frac: float) -> list[str]:
    """Initial conditions on the soil's own layers.

    The surface line (crop, residue, ICDAT ...) comes from the template; the
    layer rows are rebuilt with SH2O = LL + frac * (DUL - LL), and NH4 and NO3
    taken from the template layer that contains the soil layer's mid-depth
    (below the template's deepest layer, its deepest values). So the template's
    mineral N profile is kept by depth: copying the top layer into every layer
    of a 200 cm profile would put about ten times the template's N in the soil.
    """
    split = next((i for i, ln in enumerate(block) if _is_header(ln) and "ICBL" in ln), None)
    if split is None:
        return remap_block(block, level, days)
    layer_hdr = block[split]
    cols = {c.name: c for c in parse_header(layer_hdr)}
    tmpl = []                                   # (bottom cm, NH4 text, NO3 text) per template layer
    for ln in block[split + 1:]:
        if _is_header(ln) or not ln.strip():
            continue
        tmpl.append((float(_span(ln, cols["ICBL"])),
                     _span(ln, cols["SNH4"]).strip() if "SNH4" in cols else "",
                     _span(ln, cols["SNO3"]).strip() if "SNO3" in cols else ""))
    out = remap_block(block[:split], level, days)
    out.append(layer_hdr)
    top = 0.0
    for _, r in soil.layers.iterrows():
        ll, dul = float(r["SLLL"]), float(r["SDUL"])
        sh2o = ll + frac * (dul - ll)
        bottom = float(r["SLB"])
        mid = (top + bottom) / 2
        top = bottom
        _, nh4, no3 = next((t for t in tmpl if t[0] >= mid), tmpl[-1]) if tmpl else (0, "", "")
        row = f"{level:>2} "
        row = _put(row, cols["ICBL"].start, cols["ICBL"].end, f"{bottom:.0f}")
        row = _put(row, cols["SH2O"].start, cols["SH2O"].end, f"{sh2o:.3f}")
        if nh4:
            row = _put(row, cols["SNH4"].start, cols["SNH4"].end, nh4)
        if no3:
            row = _put(row, cols["SNO3"].start, cols["SNO3"].end, no3)
        out.append(row)
    return out


def _assemble(template: FileX, exp_code: str, crop_code: str | None, title: str,
              treatment_rows: list[str], new_blocks: dict) -> str:
    if len(exp_code) != 8:
        raise ValueError("exp_code must be 8 characters, e.g. XDSS0001")
    crop = crop_code or (template.title.split()[1][8:10] if template.title else "")
    lines = [f"*EXP.DETAILS: {exp_code}{crop} {title}".rstrip(), ""]
    section_factor = {v: k for k, v in FACTOR_SECTION.items()}
    for sec in template.sections:
        if sec.matches("TREATMENTS"):
            lines.append(sec.title)
            lines.append(TREATMENT_HEADER)
            lines.extend(treatment_rows)
            lines.append("")
            continue
        factor = next((f for key, f in section_factor.items() if sec.matches(key)), None)
        if factor is None:
            lines.append(sec.title)
            lines.extend(sec.body)
            continue
        blocks = new_blocks.get(factor) or []
        if not blocks:
            continue  # unused section (e.g. SA, MC with all-zero levels)
        lines.append(sec.title)
        if factor == "FL":
            hdrs = [ln for ln in template.section("FIELDS").body if _is_header(ln)]
            lines.append(hdrs[0])
            lines.extend(b[0] for b in blocks)
            lines.append(hdrs[1])
            lines.extend(b[1] for b in blocks)
        elif len({ln for b in blocks for ln in b if _is_header(ln)}) == 1:
            hdr = next(ln for ln in blocks[0] if _is_header(ln))
            lines.append(hdr)
            lines.extend(ln for b in blocks for ln in b if not _is_header(ln))
        else:
            for b in blocks:
                lines.extend(b)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

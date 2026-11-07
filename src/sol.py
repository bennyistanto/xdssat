"""Read and write DSSAT soil profile files (``.SOL``).

The engine looks for profiles in ``SOIL.SOL`` in the run directory first
(``InputModule/ipexp.for:587-601``), so the runner writes one ``SOIL.SOL``
per chunk holding every profile that chunk uses.

Layout of one profile (``InputModule/IPSOIL_Inp.for``)::

    *IDSOIL0001  SOURCE      SL      150 Description
    @SITE        COUNTRY          LAT     LONG SCS FAMILY
     Site        Indonesia     -6.200  106.800 Family
    @ SCOM  SALB  SLU1  SLDR  SLRO  SLNF  SLPF  SMHB  SMPX  SMKE
       -99  0.13   7.5  0.30  80.0  1.00  1.00 IB001 IB001 IB001
    @  SLB  SLMH  SLLL  SDUL  SSAT  SRGF  SSKS  SBDM  SLOC  SLCL  SLSI  SLCF  SLNI  SLHW  SLHB  SCEC  SADC
        10   -99 0.258 0.389 0.650 1.000   -99  0.85  1.30  ...

* Profile line: ``FORMAT (1X,A10,2X,A11,1X,A5,1X,F5.0,1X,A50)`` (5030).
* Site line: ``FORMAT (2(1X,A11),2(1X,F8.3),1X,A50)`` (5035).
* Surface and layer tables are header-driven (``PARSE_HEADERS``).
* At most 20 layers (``NL = 20``, ``Utilities/ModuleDefs.for:49``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from fixedwidth import format_row, parse_header

MAX_LAYERS = 20

SURFACE_HEADER = "@ SCOM  SALB  SLU1  SLDR  SLRO  SLNF  SLPF  SMHB  SMPX  SMKE"
LAYER_HEADER = ("@  SLB  SLMH  SLLL  SDUL  SSAT  SRGF  SSKS  SBDM  SLOC  SLCL  SLSI"
                "  SLCF  SLNI  SLHW  SLHB  SCEC  SADC")
SURFACE_FMT = {"SALB": ".2f", "SLU1": ".1f", "SLDR": ".2f", "SLRO": ".1f",
               "SLNF": ".2f", "SLPF": ".2f"}
LAYER_FMT = {"SLB": ".0f", "SLLL": ".3f", "SDUL": ".3f", "SSAT": ".3f",
             "SRGF": ".3f", "SSKS": ".2f", "SBDM": ".2f", "SLOC": ".2f",
             "SLCL": ".1f", "SLSI": ".1f", "SLCF": ".1f", "SLNI": ".3f",
             "SLHW": ".1f", "SLHB": ".1f", "SCEC": ".1f", "SADC": ".1f"}
TEXT_COLS = {"SCOM", "SMHB", "SMPX", "SMKE", "SLMH"}


@dataclass
class SoilProfile:
    """One soil profile. ``layers`` has one row per layer, columns as in
    ``LAYER_HEADER`` (SLB = depth to the bottom of the layer, cm)."""

    id: str
    layers: pd.DataFrame
    source: str = "-99"
    texture: str = "-99"
    depth: float | None = None
    description: str = "-99"
    site: str = "-99"
    country: str = "-99"
    lat: float | None = None
    lon: float | None = None
    family: str = "-99"
    surface: dict = field(default_factory=dict)
    extra_tables: list[list[str]] = field(default_factory=list)  # raw extra @-tables

    def __post_init__(self):
        if len(self.id) != 10:
            raise ValueError(f"soil ID must be exactly 10 characters, got {self.id!r}")
        if len(self.layers) > MAX_LAYERS:
            raise ValueError(f"{self.id}: {len(self.layers)} layers, engine limit is {MAX_LAYERS}")
        if len(self.layers) == 0:
            raise ValueError(f"{self.id}: no layers")
        if self.depth is None:
            self.depth = float(self.layers["SLB"].max())

    def to_lines(self) -> list[str]:
        f = _num
        head = (f"*{self.id:<10}  {self.source[:11]:<11} {self.texture[:5]:<5} "
                f"{f(self.depth, 5, '.0f')} {self.description[:50]}").rstrip()
        site = (f" {self.site[:11]:<11} {self.country[:11]:<11} "
                f"{f(self.lat, 8, '.3f')} {f(self.lon, 8, '.3f')} {self.family[:50]}").rstrip()
        s_cols = parse_header(SURFACE_HEADER)
        surface = {"SMHB": "IB001", "SMPX": "IB001", "SMKE": "IB001", **self.surface}
        lines = [head, "@SITE        COUNTRY          LAT     LONG SCS FAMILY", site,
                 SURFACE_HEADER, format_row(s_cols, surface, SURFACE_FMT)]
        l_cols = parse_header(LAYER_HEADER)
        lines.append(LAYER_HEADER)
        for rec in self.layers.to_dict("records"):
            lines.append(format_row(l_cols, _clean(rec), LAYER_FMT))
        for tbl in self.extra_tables:
            lines.extend(tbl)
        return lines


def _num(v, width: int, fmt: str) -> str:
    if v is None or v != v:
        return "-99".rjust(width)
    s = format(v, fmt)
    if len(s) > width:
        raise ValueError(f"{v} does not fit {width} characters")
    return s.rjust(width)


def _clean(rec: dict) -> dict:
    return {k: (-99 if (v is None or (isinstance(v, float) and v != v)) else v)
            for k, v in rec.items()}


def write_sol(path, profiles, title: str = "xdssat soil profiles") -> Path:
    """Write profiles (an iterable of ``SoilProfile``) to one ``.SOL`` file."""
    lines = [f"*SOILS: {title}", ""]
    seen = set()
    for p in profiles:
        if p.id in seen:
            raise ValueError(f"duplicate soil ID {p.id}")
        seen.add(p.id)
        lines.extend(p.to_lines())
        lines.append("")
    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def read_sol(path, ids=None) -> dict[str, SoilProfile]:
    """Read profiles from a ``.SOL`` file. ``ids`` limits which are parsed."""
    want = set(ids) if ids is not None else None
    out: dict[str, SoilProfile] = {}
    lines = Path(path).read_text(encoding="latin-1").splitlines()
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if not (ln.startswith("*") and len(ln) > 11 and not ln.upper().startswith("*SOILS")):
            i += 1
            continue
        pid = ln[1:11]
        j = i + 1
        block = []
        while j < n and not lines[j].startswith("*"):
            block.append(lines[j])
            j += 1
        if want is None or pid in want:
            out[pid] = _parse_profile(ln, block)
        i = j
    return out


def _parse_profile(head: str, block: list[str]) -> SoilProfile:
    pad = head.ljust(90)
    depth_txt = pad[31:36].strip()
    prof = dict(id=head[1:11], source=pad[13:24].strip() or "-99",
                texture=pad[25:30].strip() or "-99",
                depth=float(depth_txt) if depth_txt not in ("", "-99") else None,
                description=pad[37:87].strip() or "-99")
    tables: list[tuple[str, list[str]]] = []
    for ln in block:
        if not ln.strip() or ln.startswith("!"):
            continue
        if ln.startswith("@"):
            tables.append((ln, []))
        elif tables:
            tables[-1][1].append(ln)
    surface: dict = {}
    layers = None
    extra = []
    for hdr, rows in tables:
        if hdr.startswith("@SITE"):
            if rows:
                r = rows[0].ljust(95)
                prof.update(site=r[1:12].strip() or "-99", country=r[13:24].strip() or "-99",
                            lat=_f(r[25:33]), lon=_f(r[34:42]), family=r[43:93].strip() or "-99")
            continue
        cols = parse_header(hdr)
        names = [c.name for c in cols]
        recs = []
        for r in rows:
            rec = {}
            for c in cols:
                txt = r[c.start - 1 : c.end].strip()
                rec[c.name] = txt if c.name in TEXT_COLS else _f(txt)
            recs.append(rec)
        if "SCOM" in names and not surface:
            surface = recs[0] if recs else {}
        elif "SLB" in names and "SLLL" in names and layers is None:
            layers = pd.DataFrame(recs, columns=names)
        else:
            extra.append([hdr, *rows])
    if layers is None:
        raise ValueError(f"profile {prof['id']} has no layer table")
    return SoilProfile(layers=layers, surface=surface, extra_tables=extra, **prof)


def _f(txt: str):
    txt = txt.strip()
    if not txt:
        return None
    try:
        v = float(txt)
    except ValueError:
        return None
    return None if v == -99 else v

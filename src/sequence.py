"""Crop sequences (rotations) on the grid: one DSSAT sequence per stratum, run in mode Q.

A sequence template is an ordinary DSSAT sequence experiment (``.SQX``, e.g. made in XBuild or
from dssat-csm-data/Sequence): the crops of one cycle share a treatment number, numbered in the
``R`` column of *TREATMENTS, each with its own cultivar, planting, management and simulation
levels; usually only the first crop has initial conditions. The engine runs a sequence in mode
Q from a batch file with one line per crop, starts again from the first line for the next
cycle, and carries soil water, N and organic matter from one crop to the next
(CSM.for:368-395, 568-575; SoilNi_init.for:77). It moves the reported dates of each cycle by
whole years itself (IRRIG.for:560-575, Fert_Place.for:253-261, AUTPLT.for:145-175), so the
template holds the dates of one cycle only.

xdssat keeps that design and changes only the field and the start:

* every crop of the sequence gets the cell's field (weather station, soil, coordinates);
* every date of every crop moves by the same number of days, so the first crop is planted on
  the first season's date of the experiment's calendar (``FileX`` ``shift_days``); crops
  without a planting date (fallow) move with the others;
* the first crop's initial soil water is rebuilt on the cell's soil profile when ``soils`` and
  ``ic_water`` are given (as for single seasons);
* ``NYERS`` (cycles to run) is set on every simulation-control level; ``NREPS`` is 1.

One engine call runs one stratum's whole sequence for all years (``run_sequence``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import config
import filex
from filex import FileX

BATCH = "DSSBatch.v48"


def is_sequence(path) -> bool:
    """A sequence template: extension .SQX (DSSAT's sequence experiments)."""
    return Path(path).suffix.upper() == ".SQX"


SECTION_TITLES = {
    "CU": "*CULTIVARS", "FL": "*FIELDS", "IC": "*INITIAL CONDITIONS", "MP": "*PLANTING DETAILS",
    "MI": "*IRRIGATION AND WATER MANAGEMENT", "MF": "*FERTILIZERS (INORGANIC)",
    "MR": "*RESIDUES AND ORGANIC FERTILIZER", "ME": "*ENVIRONMENT MODIFICATIONS",
    "MH": "*HARVEST DETAILS", "SM": "*SIMULATION CONTROLS",
}


def compose(sequences: dict[int, list[tuple[FileX, int, str]]], exp_code: str = "XDSQ0001",
            title: str = "xdssat sequence") -> str:
    """A sequence template (.SQX text) from treatments of ordinary templates.

    ``sequences``: sequence treatment number -> its crops in cycle order, each (template,
    treatment number, planting date "YYYY-MM-DD"). Every date of a crop moves so that it is
    planted on its date; dates of D sections (days after planting) stay. The first crop's field
    and initial conditions are the sequence's; the others start from the soil the crop before
    left. Levels are renumbered per section.
    """
    import datetime as dt
    blocks = {f: [] for f in filex.FACTORS}
    rows = []
    for seq_no, parts in sequences.items():
        seq_fl = 0
        for k, (fx, trno, when) in enumerate(parts, start=1):
            trow = fx.treatments().loc[trno]
            anchor = fx.planting_date(int(trow["MP"]))
            days = (dt.date.fromisoformat(when) - anchor).days
            codes = filex.management_codes(fx, int(trow["SM"])) if trow["SM"] else {}
            levels = {}
            for f in filex.FACTORS:
                tl = int(trow[f])
                if f == "FL" and k > 1:
                    levels[f] = seq_fl                      # one field per sequence
                    continue
                if tl == 0 or f in ("SA", "MC", "MT") or (k > 1 and f == "IC"):
                    levels[f] = 0
                    continue
                n = len(blocks[f]) + 1
                fdays = 0 if (codes.get(f) == "D" or f == "CU") else days
                blocks[f].append(filex.remap_block(fx.blocks(f)[tl], n, fdays))
                levels[f] = n
                if f == "FL":
                    seq_fl = n
            tname = (trow["TNAME"] or f"crop {k}")[:25]
            rows.append(f"{seq_no:>2} {k:>1} 1 0 {tname:<25}" + "".join(f"{levels[f]:>3}" for f in filex.FACTORS))
    out = [f"*EXP.DETAILS: {exp_code}SQ {title}", "",
           "*TREATMENTS                        -------------FACTOR LEVELS------------",
           filex.TREATMENT_HEADER, *rows, ""]
    for f in ("CU", "FL", "IC", "MP", "MI", "MF", "MR", "ME", "MH", "SM"):
        if not blocks[f]:
            continue
        out.append(SECTION_TITLES[f])
        if f == "FL":                                     # all ID_FIELD rows, then all XCRD rows
            heads = [ln for ln in blocks[f][0] if ln.startswith("@")]
            data = [[ln for ln in b if not ln.startswith("@") and ln.strip()] for b in blocks[f]]
            out += [heads[0]] + [d[0] for d in data] + [heads[1]] + [d[1] for d in data]
        else:
            for b in blocks[f]:
                out.extend(ln for ln in b if ln.strip())
        out.append("")
    return "\n".join(out) + "\n"


def sequence_table(fx: FileX) -> pd.DataFrame:
    """*TREATMENTS of a sequence template: one row per crop (N, R, O, C, TNAME, factors);
    unlike ``FileX.treatments`` the treatment number repeats."""
    sec = fx.section("TREATMENTS")
    recs = []
    for r in (ln for ln in sec.body if filex._is_data(ln)):
        r = r.ljust(34 + 3 * 14)
        rec = {"N": int(r[0:3]), "R": int(r[3:4]) if r[3:4].strip() else 0,
               "O": r[5], "C": r[7], "TNAME": r[9:34].strip()}
        for i, f in enumerate(filex.FACTORS):
            txt = r[34 + 3 * i: 37 + 3 * i].strip()
            rec[f] = int(txt) if txt else 0
        recs.append(rec)
    return pd.DataFrame(recs)


def components(fx: FileX, trtno: int) -> pd.DataFrame:
    t = sequence_table(fx)
    c = t[t.N == trtno].sort_values("R").reset_index(drop=True)
    if c.empty:
        raise ValueError(f"sequence template has no treatment {trtno}")
    return c


def _flat_template(fx: FileX, comp: pd.DataFrame) -> FileX:
    """A copy of the template whose TREATMENTS holds the crops of one sequence numbered 1..k,
    so ``filex.build_filex`` can copy them like ordinary treatments."""
    import copy
    out = copy.deepcopy(fx)
    sec = out.section("TREATMENTS")
    hdr = [ln for ln in sec.body if ln.startswith("@")]
    rows = []
    for k, r in enumerate(comp.itertuples(index=False), start=1):
        # FORMAT (I3,I1,2(1X,I1),1X,A25,14I3) (IPEXP.for:865): " 1 1 0 0 name ..."
        levels = "".join(f"{getattr(r, f):>3}" for f in filex.FACTORS)
        rows.append(f"{k:>2} {r.R:>1} {r.O:>1} {r.C:>1} {r.TNAME[:25]:<25}" + levels)
    sec.body = hdr + rows + [""]
    return out


@dataclass
class BuiltSequence:
    text: str
    components: pd.DataFrame          # one row per crop: R, TNAME, ...
    units: pd.DataFrame = field(default_factory=pd.DataFrame)


def build_sequence(template: FileX, trtno: int, wsta: str, soil_id: str, lat=None, lon=None, elev=None,
                   pdate=None, nyears: int = 1, soils: dict | None = None, ic_water: float | None = None,
                   simulation_overrides: dict | None = None, overrides: dict | None = None,
                   exp_code: str = "XDSQ0001", title: str = "xdssat sequence") -> BuiltSequence:
    """The FileX text of one sequence on one cell.

    ``pdate``: the first crop's planting date (None: the template's own dates); every date of
    every crop moves by the same number of days. ``overrides``: "<factor>.<column>" values for
    every crop (e.g. management fields of a scenario).
    """
    comp = components(template, trtno)
    flat = _flat_template(template, comp)
    first = comp.iloc[0]
    anchor = template.planting_date(int(first["MP"])) if first["MP"] else None
    if pdate is not None and anchor is None:
        raise ValueError(f"the first crop of sequence {trtno} has no planting date to anchor on")
    days = (pd.Timestamp(pdate).date() - anchor).days if pdate is not None else 0
    rows = []
    for k in range(1, len(comp) + 1):
        rows.append({"cell": 1, "wsta": wsta, "soil_id": soil_id, "lat": lat, "lon": lon, "elev": elev,
                     "pdate": pd.Timestamp(pdate) if pdate is not None else pd.Timestamp(anchor or "2000-01-01"),
                     "shift_days": days, "trno_template": k, "name": comp.TNAME[k - 1],
                     **(overrides or {})})
    units = pd.DataFrame(rows)
    sim = {"NYERS": int(nyears), "NREPS": 1, **(simulation_overrides or {})}
    built = filex.build_filex(flat, units, exp_code=exp_code, title=title, soils=soils, ic_water=ic_water,
                              simulation_overrides=sim)
    # every crop row of the built file belongs to sequence treatment 1 (R keeps the crop order)
    lines = built.text.splitlines()
    out, inside = [], False
    for ln in lines:
        if ln.startswith("*TREATMENTS"):
            inside = True
        elif ln.startswith("*"):
            inside = False
        if inside and filex._is_data(ln) and not ln.startswith("@"):
            ln = f"{1:>2} " + ln[3:]
        out.append(ln)
    return BuiltSequence("\n".join(out) + "\n", comp, built.units)


def write_batch(run_dir: Path, fx_name: str, n_components: int, trtno: int = 1) -> Path:
    """The mode Q batch file: one line per crop, FileX in columns 1-92, then TRTNO RP SQ as
    3(1X,I6) (CSM.for:266-292)."""
    head = "$BATCH(SEQUENCE)\n!\n@FILEX" + " " * 86 + "TRTNO     RP     SQ     OP     CO\n"
    body = "".join(f"{fx_name:<92}{trtno:>7}{1:>7}{k:>7}{0:>7}{0:>7}\n" for k in range(1, n_components + 1))
    p = Path(run_dir) / BATCH
    p.write_text(head + body)
    return p


def run_sequence(run_dir: Path, fx_name: str, n_components: int, outputs: str = "summary",
                 engine_dir: Path | None = None):
    """Run a sequence FileX in ``run_dir`` in mode Q; returns ``runner.RunResult``."""
    import runner
    write_batch(run_dir, fx_name, n_components)
    return runner.run_engine(Path(run_dir), BATCH, outputs, engine_dir, mode="Q")


def label_seasons(summary: pd.DataFrame, comp: pd.DataFrame) -> pd.DataFrame:
    """Add ``crop_order`` (R of the crop) and ``cycle`` (1, 2 ...) to a sequence summary, whose
    rows come in run order: crop 1, crop 2 ... of cycle 1, then of cycle 2 (RUNNO)."""
    s = summary.sort_values("RUNNO").reset_index(drop=True)
    k = len(comp)
    s["crop_order"] = [int(comp.R[i % k]) for i in range(len(s))]
    s["cycle"] = [i // k + 1 for i in range(len(s))]
    s["crop_name"] = [comp.TNAME[i % k] for i in range(len(s))]
    return s

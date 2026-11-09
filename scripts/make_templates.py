"""Write xdssat's rice template experiment, derived from DSSAT's IRMZ8601.RIX.

    python scripts/make_templates.py

IRMZ8601.RIX (IRRI, Munoz, 1986; dssat-csm-data, BSD-3) is a transplanted IR 58
experiment. Treatment 5 (140 kg N as urea, split) with water level 1 is fully
irrigated: after puddling (IR010) it keeps a constant 50 mm flood (IR011).

The template adds one water level and one treatment:

- water level 3, "rainfed bunded": the same puddling (IR010), percolation
  (IR008, 2 mm/d) and 150 mm bunds (IR009) on the same day, but no flood or
  irrigation operations, so the field holds only rain;
- treatment 11: treatment 5 with water level 3;
- cultivar level 2, IR 64 (IB0015 in RICER048.CUL);
- treatments 12 onwards: every cultivar x water level (1 flooded, 3 rainfed) x
  N level (0, 35, 70, 105, 140 kg N), numbered from 12 in that order (``TRNO``).

The constants below describe what the template file contains, not what a run
uses: a run picks its cultivars, N rates and water regimes in the experiment
file (configs/rice-java.yml), whose ``treatments > nitrogen_levels`` and
``water_regimes > template_level`` point at the levels written here.

Operation codes from dssat-csm-os/Data/DETAIL.CDE: IR006 flood depth, IR008
percolation rate (mm/d), IR009 bund height (mm), IR010 puddling, IR011 constant
flood depth (mm).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCE = ROOT.parent / "dssat-csm-data" / "Rice" / "IRMZ8601.RIX"
OUT = ROOT / "templates" / "XDRI8601.RIX"

# Template contents: cultivar name -> (CU level, INGENO in RICER048.CUL); kg N -> MF level
# (IRMZ8601: 0-140 kg N as urea, 2/3 at 18 and 1/3 at 41 days after transplanting);
# water regime -> MI level.
CULTIVARS = {"IR 58": (1, "IB0012"), "IR 64": (2, "IB0015")}
N_LEVELS = {0: 0, 35: 1, 70: 2, 105: 3, 140: 4}
WATER_LEVEL = {"irrigated": 1, "rainfed": 3}
# Treatment number of every (cultivar, water regime, kg N), from 12 up; 1-11 keep IRMZ8601's.
TRNO = {key: 12 + i for i, key in enumerate(
    (cv, w, n) for cv in CULTIVARS for w in WATER_LEVEL for n in N_LEVELS)}


# FORMAT (I3,I1,2(1X,I1),1X,A25,14I3) (IPEXP.for:865); factor levels CU..SM
def trt_line(n: int, name: str, cu: int, mi: int, mf: int) -> str:
    return (f"{n:2d} 1 0 0 " + f"{name:<25.25}"
            + "".join(f"{v:3d}" for v in (cu, 1, 0, 1, 1, mi, mf, 1, 0, 0, 0, 0, 1)))


TRT_11 = trt_line(11, "140 kg N, rainfed bunded", 1, 3, 4)
GRID = [trt_line(k, f"{cv} {'flood' if w == 'irrigated' else 'rainfed'} {n} kg N",
                 CULTIVARS[cv][0], WATER_LEVEL[w], N_LEVELS[n])
        for (cv, w, n), k in TRNO.items()]
CULTIVAR_LINES = [f"{lv:2d} RI {code} {cv}" for cv, (lv, code) in CULTIVARS.items() if lv > 1]
WATER_3 = [
    "@I  EFIR  IDEP  ITHR  IEPT  IOFF  IAME  IAMT IRNAME",
    " 3   -99   -99   -99   -99   -99   -99     1 rainfed bunded",
    "@I IDATE  IROP IRVAL",
    " 3 86033 IR010     0",
    " 3 86033 IR008     2",
    " 3 86033 IR009   150",
]


def write_rice():
    lines = SOURCE.read_text().splitlines()
    out, section = [], None
    for i, ln in enumerate(lines):
        if ln.startswith("*"):
            # close the irrigation section with the new level before the next section
            if section == "IRRIGATION":
                while out and not out[-1].strip():
                    out.pop()
                out += WATER_3 + [""]
            section = ln[1:].split()[0]
        if i == 0:
            ln = ln.rstrip() + "  (+ xdssat rainfed level)"
        out.append(ln)
        if section == "TREATMENTS" and ln.startswith("10 1 0 0"):
            out += [TRT_11] + GRID
        if section == "CULTIVARS" and ln.startswith(" 1 RI"):
            out += CULTIVAR_LINES
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(out) + "\n")
    print(f"wrote {OUT}")


# ------------------------------------------------------------------ maize
MZ_SOURCE = ROOT.parent / "dssat-csm-data" / "Maize" / "UFGA8201.MZX"
MZ_OUT = ROOT / "templates" / "XDMZ8201.MZX"
MZ_PLANTING = "82057"                     # PDATE of UFGA8201 (26 Feb 1982)
MZ_TOPDRESS = "82092"                     # 35 days after planting
# kg N -> MF level. 116 and 401 are UFGA8201's own levels; 0 is no fertilizer; 60, 120 and 180
# are added: urea (FE005), broadcast and incorporated (AP002) at 5 cm, 1/3 at planting and 2/3
# at 35 days after planting. The split is an xdssat assumption, not part of the experiment.
MZ_N_LEVELS = {0: 0, 60: 3, 116: 1, 120: 4, 180: 5, 401: 2}
MZ_NEW_N = {60: 3, 120: 4, 180: 5}
# water regime -> (MI level, SM level). Irrigated: automatic irrigation (SM 2 has IRRIG A, the
# AUTOMATIC MANAGEMENT row of UFGA8201: top 30 cm refilled to 100% of available water when it
# falls to 50%, IRRIG.for:820-840); MI 4 holds no dated irrigation and only marks the treatment.
# Rainfed: no irrigation level (MI 0) and SM 1 (IRRIG R): no water applied (IRRIG.for:702-714).
MZ_WATER = {"irrigated": (4, 2), "rainfed": (0, 1)}
# Treatment number of every (water regime, kg N), from 7 up; 1-6 keep UFGA8201's.
MZ_TRNO = {key: 7 + i for i, key in enumerate((w, n) for w in MZ_WATER for n in MZ_N_LEVELS)}


def mz_trt_line(n: int, name: str, mi: int, mf: int, sm: int) -> str:
    return (f"{n:2d} 1 0 0 " + f"{name:<25.25}"
            + "".join(f"{v:3d}" for v in (1, 1, 0, 1, 1, mi, mf, 0, 0, 0, 0, 0, sm)))


MZ_GRID = [mz_trt_line(k, f"{'auto irrigated' if w == 'irrigated' else 'rainfed'} {n} kg N",
                       MZ_WATER[w][0], MZ_N_LEVELS[n], MZ_WATER[w][1])
           for (w, n), k in MZ_TRNO.items()]
MZ_WATER_4 = [
    "@I  EFIR  IDEP  ITHR  IEPT  IOFF  IAME  IAMT IRNAME",
    " 4     1    30    50   100 GS000 IR001   -99 automatic (SM level 2)",
]


def mz_fert_rows() -> list[str]:
    rows = []
    for kg, lv in MZ_NEW_N.items():
        for date, amount in ((MZ_PLANTING, kg // 3), (MZ_TOPDRESS, kg - kg // 3)):
            rows.append(f"{lv:2d} {date} FE005 AP002     5 {amount:5d}     0     0     0     0   -99 "
                        f"urea {kg} kg N")
    return rows


def mz_auto_sm(body: list[str]) -> list[str]:
    """Simulation-control level 2: level 1 with automatic irrigation (IRRIG A)."""
    out, hdr = [], None
    for ln in body:
        if not ln.strip():
            continue
        if ln.startswith("@"):
            hdr = ln
            out.append(ln)
            continue
        row = " 2" + ln[2:]
        if hdr and hdr.startswith("@N MANAGEMENT"):
            end = hdr.index("IRRIG") + len("IRRIG")
            row = row[:end - 1] + "A" + row[end:]
        if hdr and hdr.startswith("@N GENERAL"):
            start = hdr.index("SNAME")
            row = row[:start] + f"{'AUTOMATIC IRRIGATION':<25.25}" + row[start + 25:]
        out.append(row.rstrip())
    return out


def write_maize():
    """templates/XDMZ8201.MZX from UFGA8201.MZX (Gainesville 1982 maize, N x irrigation;
    Bennett et al., 1986; the CERES-Maize example of Hoogenboom et al., 2019, section 12.1).

    Adds MI level 4, fertilizer levels 3-5, simulation-control level 2 (automatic
    irrigation) and treatments 7-18: every water regime x 0, 60, 116, 120, 180, 401 kg N.
    """
    # UFGA8201.MZX ends with a DOS end-of-file mark (Ctrl-Z, \x1a) after its last line
    lines = MZ_SOURCE.read_text().replace("\x1a", "").splitlines()
    out, section, sm_body = [], None, []

    def close(sec):
        while out and not out[-1].strip():
            out.pop()
        if sec == "IRRIGATION":
            out.extend(MZ_WATER_4)
        elif sec == "FERTILIZERS":
            out.extend(mz_fert_rows())
        elif sec == "SIMULATION":
            out.extend(mz_auto_sm(sm_body))
        out.append("")

    for i, ln in enumerate(lines):
        if ln.startswith("*"):
            if section in ("IRRIGATION", "FERTILIZERS", "SIMULATION"):
                close(section)
            section = ln[1:].split()[0]
        if i == 0:
            ln = ln.rstrip() + "  (+ xdssat levels)"
        if section == "SIMULATION" and not ln.startswith("*"):
            sm_body.append(ln)
        out.append(ln)
        if section == "TREATMENTS" and ln.startswith(" 6 1 0 0"):
            out += MZ_GRID
    if section == "SIMULATION":
        close(section)
    MZ_OUT.parent.mkdir(exist_ok=True)
    MZ_OUT.write_text("\n".join(out).rstrip() + "\n")
    print(f"wrote {MZ_OUT}")


# ------------------------------------------------------------------ pearl millet
ML_SOURCE = ROOT.parent / "dssat-csm-data" / "PearlMillet" / "ITSC8601.MLX"
ML_OUT = ROOT / "templates" / "XDML8601.MLX"
# kg N -> MF level and water regime -> MI level, as ITSC8601 has them: rainfed only (no
# irrigation section, MI 0); 0 kg N (MF 0) or 45 kg N as ammonium nitrate, 15 kg 25 days and
# 30 kg 50 days after sowing (MF 1).
ML_N_LEVELS = {0: 0, 45: 1}
ML_WATER = {"rainfed": 0}


def write_millet():
    """templates/XDML8601.MLX: ICRISAT Sahelian Centre, Sadore, Niger, 1986 (dssat-csm-data
    ITSC8601.MLX, BSD-3): pearl millet CIVT sown on 17 Jun 1986, 3 plants/m2 in 100 cm rows,
    5 cm deep, rainfed, with and without fertilizer. Copied as it is (its two treatments are
    the rainfed regime at 0 and 45 kg N), so a run does not need dssat-csm-data."""
    lines = ML_SOURCE.read_text().replace("\x1a", "").splitlines()
    lines[0] = lines[0].rstrip() + "  (xdssat copy)"
    ML_OUT.parent.mkdir(exist_ok=True)
    ML_OUT.write_text("\n".join(lines).rstrip() + "\n")
    print(f"wrote {ML_OUT}")


# ------------------------------------------------------------------ rice then maize (sequence)
RM_OUT = ROOT / "templates" / "XDRM8601.SQX"
# sequence treatment -> water regime of the rice; maize after it is rainfed in both
RM_SEQUENCES = {1: "irrigated", 2: "rainfed"}
RM_RICE_DATE, RM_MAIZE_DATE = "1986-11-15", "1987-03-15"   # 120 days apart, one cycle per year


def write_rice_maize():
    """templates/XDRM8601.SQX: wet-season rice, then dry-season maize on the same field, the
    soil's water, N and organic matter carried from the rice to the maize (padi then palawija).

    Sequence 1: IR 58 flooded (XDRI8601, 140 kg N) transplanted on 15 Nov, then McCurdy 84aa
    rainfed (XDMZ8201, 120 kg N) sown on 15 Mar. Sequence 2: the same with rainfed bunded rice.
    Written from the two templates above (src/sequence.py compose), so run them first.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from filex import FileX
    import sequence
    rice, maize = FileX.read(OUT), FileX.read(MZ_OUT)
    seqs = {s: [(rice, TRNO[("IR 58", w, 140)], RM_RICE_DATE),
                (maize, MZ_TRNO[("rainfed", 120)], RM_MAIZE_DATE)] for s, w in RM_SEQUENCES.items()}
    text = sequence.compose(seqs, exp_code="XDRM8601", title="RICE THEN MAIZE, JAVA PADI-PALAWIJA")
    RM_OUT.write_text(text)
    print(f"wrote {RM_OUT}")


# ------------------------------------------------------------------ rice, percolation levels
RW_OUT = ROOT / "templates" / "XDRW8601.RIX"
# MI level -> percolation rate (mm/d) of the flooded regime; level 1 keeps IRMZ8601's 2 mm/d
RW_PERCOLATION = {4: 1, 5: 4}


def write_rice_water():
    """templates/XDRW8601.RIX: XDRI8601.RIX plus two flooded water levels that differ from level 1
    only in the percolation rate (IR008): level 4 half of it (1 mm/d), level 5 double (4 mm/d).
    Percolation is a property of the puddled field the template cannot know; the Mekong case
    (configs/cases/mekong-rice.yml) runs all three to show how much of the water bill it sets.
    Written from XDRI8601.RIX, so run write_rice first."""
    lines = OUT.read_text().splitlines()
    lines[0] = lines[0].replace("(+ xdssat rainfed level)", "(+ xdssat rainfed and percolation levels)")
    start = lines.index("*IRRIGATION AND WATER MANAGEMENT")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("*"))
    sec = lines[start:end]
    # level 1 block: its "@I  EFIR" header, level row, "@I IDATE" header and its operation rows
    h = next(i for i, ln in enumerate(sec) if ln.startswith("@I  EFIR"))
    block = [sec[h], sec[h + 1], sec[h + 2]]
    block += [ln for ln in sec[h + 3:] if ln.startswith(" 1 ") and ln[3:8].isdigit()]
    extra = []
    for lv, perc in RW_PERCOLATION.items():
        for ln in block:
            if ln.startswith("@"):
                extra.append(ln)
            elif "IR008" in ln:
                extra.append(f"{lv:2d}" + ln[2:14] + f"{perc:6d}")
            elif ln[3:8].isdigit():                     # an operation row: IDATE IROP IRVAL
                extra.append(f"{lv:2d}" + ln[2:])
            else:                                       # the level row: name the level
                extra.append(f"{lv:2d}" + ln[2:44] + f" flood, percolation {perc} mm/d")
    body = sec[:]
    while body and not body[-1].strip():
        body.pop()
    lines[start:end] = body + extra + [""]
    # treatments: every cultivar x new water level x N level, numbered after XDRI8601's last (31)
    keys = [(cv, lv, n) for cv in CULTIVARS for lv in RW_PERCOLATION for n in N_LEVELS]
    rows = [trt_line(len(TRNO) + 12 + i, f"{cv} perc {RW_PERCOLATION[lv]} {n} kg N", CULTIVARS[cv][0], lv, N_LEVELS[n])
            for i, (cv, lv, n) in enumerate(keys)]
    last = max(i for i, ln in enumerate(lines) if ln[:3].strip().isdigit() and i < lines.index("*CULTIVARS"))
    lines[last + 1:last + 1] = rows
    RW_OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {RW_OUT}")


def main():
    write_rice()
    write_rice_water()
    write_maize()
    write_millet()
    write_rice_maize()


if __name__ == "__main__":
    sys.exit(main())

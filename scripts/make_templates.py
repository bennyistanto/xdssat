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


def main():
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


if __name__ == "__main__":
    sys.exit(main())

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
  N level (0, 35, 70, 105, 140 kg N), numbered by management.TREATMENT_GRID.

Operation codes from dssat-csm-os/Data/DETAIL.CDE: IR006 flood depth, IR008
percolation rate (mm/d), IR009 bund height (mm), IR010 puddling, IR011 constant
flood depth (mm).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import management as mg  # noqa: E402

SOURCE = ROOT.parent / "dssat-csm-data" / "Rice" / "IRMZ8601.RIX"
OUT = ROOT / "templates" / "XDRI8601.RIX"

# FORMAT (I3,I1,2(1X,I1),1X,A25,14I3) (IPEXP.for:865); factor levels CU..SM
def trt_line(n: int, name: str, cu: int, mi: int, mf: int) -> str:
    return (f"{n:2d} 1 0 0 " + f"{name:<25.25}"
            + "".join(f"{v:3d}" for v in (cu, 1, 0, 1, 1, mi, mf, 1, 0, 0, 0, 0, 1)))


TRT_11 = trt_line(11, "140 kg N, rainfed bunded", 1, 3, 4)
GRID = [trt_line(k, f"{cv} {'flood' if w == 'irrigated' else 'rainfed'} {n} kg N",
                 mg.CULTIVARS[cv][0], mg.WATER_LEVEL[w], mg.N_LEVELS[n])
        for (cv, w, n), k in mg.TREATMENT_GRID.items()]
CULTIVAR_LINES = [f"{lv:2d} RI {code} {cv}" for cv, (lv, code) in mg.CULTIVARS.items() if lv > 1]
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

"""Prototype: N grid cells as N treatments in one FileX, one weather file per cell.

Spike only (2026-11-05 feasibility review). Takes treatment 1 and field 1 of an
existing FileX and clones them n times; each clone points at its own copy of the
weather file, named GRnnnnnn.WTH. Not production code.

Usage: python chunk_filex.py <src_filex> <src_wth> <run_dir> <n_cells>
"""
import shutil
import sys
from pathlib import Path

src_x, src_w, run_dir, n = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
run_dir.mkdir(parents=True, exist_ok=True)
lines = src_x.read_text(encoding="latin-1").splitlines()

out = []
i = 0
while i < len(lines):
    ln = lines[i]
    if ln.startswith("*TREATMENTS"):
        out += [ln, lines[i + 1]]
        tmpl = lines[i + 2]  # treatment 1 row
        for k in range(1, n + 1):
            row = f"{k:>3}" + tmpl[3:]  # TRNO is I3 (IPEXP.for format 55)
            # FL column: replace factor level for field (2nd factor) - columns per header
            hdr = lines[i + 1]
            fl_end = hdr.index(" FL") + 3
            row = row[: fl_end - 3] + f"{k:>3}" + row[fl_end:]
            out.append(row)
        i += 2
        while i < len(lines) and lines[i].strip() and not lines[i].startswith("*"):
            i += 1
        continue
    if ln.startswith("*FIELDS"):
        h1, r1, h2, r2 = lines[i + 1 : i + 5]
        # MAKEFILEW.f90 reads every row under the first "@L ID_" header as (I3,9X,A8),
        # so all ID rows must come first, then all coordinate rows under one header.
        out += [ln, h1]
        for k in range(1, n + 1):
            wsta = f"GR{k:06d}"  # 8 chars -> GRnnnnnn.WTH
            out.append(f"{k:>3}" + r1[3:12] + f"{wsta:<8}" + r1[20:])
            shutil.copy(src_w, run_dir / f"{wsta}.WTH")
        out.append(h2)
        out += [f"{k:>3}" + r2[3:] for k in range(1, n + 1)]
        i += 5
        continue
    out.append(ln)
    i += 1

(run_dir / src_x.name).write_text("\n".join(out) + "\n", encoding="latin-1")
print("wrote", run_dir / src_x.name)

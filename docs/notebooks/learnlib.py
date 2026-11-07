"""Small helpers for the Learn DSSAT notebooks.

The notebooks run the stock DSSAT engine (``config.ENGINE_DIR``) on example
experiments from DSSAT/dssat-csm-data, using xdssat's own modules read-only:

    filex.FileX / build_filex   build experiments in code (replaces XBuild)
    runner.run_engine           run dscsm048 in a short work folder
    runner.read_summary / read_daily_csv   read the CSV outputs
    genotype.write_cultivar_files          per-run cultivar coefficient changes

Work folders go under ``config.WORK_ROOT/learn/`` (a short path outside
OneDrive, which the engine needs). Nothing here writes into the repository
except ``export()``, which writes small CSV files for the website.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent            # docs/notebooks
ROOT = HERE.parents[1]                             # xdssat
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import filex  # noqa: E402
import genotype  # noqa: E402
import runner  # noqa: E402

CSM_DATA = Path(ROOT.parent / "dssat-csm-data")
CSM_DATA_COMMIT = "99c95b340acb9515c2d0a8ab582f5fbbf076f345"
CSM_OS_COMMIT = "25c93b9a2f73f8ec45bf441b198dac5fb7b22849"
DATA_OUT = HERE.parent / "learn" / "data"      # read by the module pages in docs/learn/
COURSE = HERE / "course-data"        # data files of the 2018 course exercises (see its README.md)


def csm_data_url(rel: str) -> str:
    """Link to a file of DSSAT/dssat-csm-data at the pinned commit."""
    return f"https://github.com/DSSAT/dssat-csm-data/blob/{CSM_DATA_COMMIT}/{rel}"


def csm_os_url(rel: str, line: int | None = None) -> str:
    """Link to a file (and line) of DSSAT/dssat-csm-os at the pinned commit."""
    return f"https://github.com/DSSAT/dssat-csm-os/blob/{CSM_OS_COMMIT}/{rel}" + (f"#L{line}" if line else "")


def stage_course(run_dir: Path, *rel_paths: str) -> None:
    """Copy course data files (paths relative to ``notebooks/course-data``) into ``run_dir``."""
    for rel in rel_paths:
        src = COURSE / rel
        if not src.is_file():
            raise FileNotFoundError(src)
        shutil.copy2(src, Path(run_dir) / src.name)


def replace_section(text: str, name: str, block: str) -> str:
    """Replace the FileX section that starts with ``*<name>`` by ``block`` (what XBuild does on save)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.upper().startswith("*" + name.upper()))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("*")), len(lines))
    return "\n".join(lines[:start] + block.strip("\n").splitlines() + [""] + lines[end:]) + "\n"


def read_flood(run_dir: Path) -> pd.DataFrame:
    """Daily rice flood water of every treatment, from ``FloodW.OUT``.

    The engine writes it only as text (``Management/OpFlood.for``), one block per treatment with the
    columns ``YEAR DOY DAS ABUND FLOD PRED IRRD ROFD EFAD INFD FROD``: bund height, flood depth, rain,
    irrigation, runoff, evaporation from the flood, infiltration and flood runoff over the bund, mm.
    """
    rows, trno, hdr = [], None, None
    for ln in (Path(run_dir) / "FloodW.OUT").read_text(encoding="latin-1").splitlines():
        m = re.match(r"\s*TREATMENT\s+(\d+)", ln)
        if m:
            trno = int(m.group(1))
        elif ln.startswith("@YEAR"):
            hdr = ln[1:].split()
        elif hdr and trno is not None and ln[:1] == " " and ln.strip():
            v = ln.split()
            if len(v) == len(hdr) and v[0].isdigit():
                rows.append([trno] + v)
    df = pd.DataFrame(rows, columns=["TRNO"] + (hdr or []))
    for c in hdr or []:
        df[c] = pd.to_numeric(df[c])
    df.insert(1, "date", pd.to_datetime(df.YEAR * 1000 + df.DOY, format="%Y%j"))
    return df


def work_dir(name: str) -> Path:
    """A fresh, short run folder ``WORK_ROOT/learn/<name>``."""
    d = Path(config.WORK_ROOT) / "learn" / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return config.check_short_path(d)


def stage(run_dir: Path, *rel_paths: str) -> None:
    """Copy files from dssat-csm-data (paths relative to its root) into ``run_dir``."""
    for rel in rel_paths:
        src = CSM_DATA / rel
        if not src.is_file():
            raise FileNotFoundError(src)
        shutil.copy2(src, Path(run_dir) / src.name)


def run(run_dir: Path, filex_name: str, outputs: str = "daily", cultivar_overrides: dict | None = None,
        crop: str | None = None):
    """Run one FileX; returns (summary, daily dict, RunResult).

    ``cultivar_overrides`` like {"IB0035": {"P1": 200}} writes a local copy of
    the cultivar file ``crop`` (e.g. "MZCER048") with those values first.
    """
    if cultivar_overrides:
        write_cultivar_file(Path(run_dir), cultivar_overrides, crop=crop)
    res = runner.run_engine(Path(run_dir), filex_name, outputs=outputs)
    if res.returncode != 0 or res.summary.empty:
        raise RuntimeError(f"engine failed ({res.returncode}):\n{res.error_out[-2000:]}\n{res.stdout[-2000:]}")
    daily = runner.read_daily_csv(run_dir) if outputs == "daily" else {}
    return res.summary, daily, res


def write_cultivar_file(run_dir: Path, overrides: dict, crop: str) -> None:
    """Copy ``<crop>.CUL/.ECO/.SPE`` into ``run_dir`` with coefficient overrides.

    Same idea as ``genotype.write_cultivar_files``, but the field of a
    coefficient runs from the end of the previous header name to the end of its
    own name, so short names (maize ``P1``, ``P2``) keep their full width. The
    engine reads the local cultivar file first (``ipexp.for:507-517``, see
    ``src/genotype.py``).
    """
    g = Path(config.ENGINE_DIR) / "Genotype"
    lines = (g / f"{crop}.CUL").read_text(encoding="latin-1").splitlines()
    header, done = None, set()
    for i, ln in enumerate(lines):
        if ln.startswith("@VAR#"):
            header = ln
            continue
        if header is None or ln[:6] not in overrides:
            continue
        for name, value in overrides[ln[:6]].items():
            ln = _set_coef(ln, header, name, value, crop)
        lines[i] = ln
        done.add(ln[:6])
    missing = set(overrides) - done
    if missing:
        raise KeyError(f"cultivars not in {crop}.CUL: {sorted(missing)}")
    _write_genotype(run_dir, lines, crop)


def _set_coef(ln: str, header: str, name: str, value, crop: str) -> str:
    """Write one coefficient into a cultivar row (field = end of previous header name + 1 .. end of name)."""
    ends = {m.group(0): m.end() for m in re.finditer(r"\S+", header)}
    order = [m.group(0) for m in re.finditer(r"\S+", header)]
    if name not in ends:
        raise KeyError(f"{crop}.CUL has no coefficient {name}")
    end = ends[name]
    start = ends[order[order.index(name) - 1]] + 1
    ln = ln.ljust(end)
    old = ln[start:end].strip()
    dec = len(old.split(".")[1]) if "." in old else 0
    text = f"{float(value):.{dec}f}"
    while len(text) > end - start and dec > 0:     # e.g. 1100.0 in a 5-character field
        dec -= 1
        text = f"{float(value):.{dec}f}"
    if len(text) > end - start:
        raise ValueError(f"{value} does not fit the {end - start}-character {name} field")
    return ln[:start] + text.rjust(end - start) + ln[end:]


def _write_genotype(run_dir: Path, cul_lines: list[str], crop: str) -> None:
    g = Path(config.ENGINE_DIR) / "Genotype"
    (Path(run_dir) / f"{crop}.CUL").write_text("\n".join(cul_lines) + "\n", encoding="latin-1")
    for ext in ("ECO", "SPE"):
        if (g / f"{crop}.{ext}").exists():
            shutil.copy2(g / f"{crop}.{ext}", Path(run_dir) / f"{crop}.{ext}")


def write_cultivar_variants(run_dir: Path, base: str, variants: dict, crop: str,
                            names: dict | None = None) -> list[str]:
    """Copy ``<crop>.CUL`` into ``run_dir`` with extra cultivars derived from ``base``.

    ``variants`` maps a new 6-character code (e.g. "XV0001") to coefficient
    overrides, e.g. {"XV0001": {"CSDL": 12.1}}. Each variant is a copy of the
    ``base`` row with its own code and name, inserted after it, so one FileX can
    run many coefficient sets as separate treatments (``CU.INGENO``) in a single
    engine call. Returns the codes written.
    """
    g = Path(config.ENGINE_DIR) / "Genotype"
    lines = (g / f"{crop}.CUL").read_text(encoding="latin-1").splitlines()
    header, at = None, None
    for i, ln in enumerate(lines):
        if ln.startswith("@VAR#"):
            header = ln
        elif header and ln[:6] == base:
            at, hdr = i, header
            break
    if at is None:
        raise KeyError(f"cultivar {base} not in {crop}.CUL")
    new = []
    for code, ov in variants.items():
        if len(code) != 6:
            raise ValueError(f"cultivar code {code!r} must have 6 characters")
        label = (names or {}).get(code, "var " + code)
        ln = code + " " + f"{label[:16]:<16}" + lines[at][23:]
        for name, value in ov.items():
            ln = _set_coef(ln, hdr, name, value, crop)
        new.append(ln)
    lines[at + 1:at + 1] = new
    _write_genotype(run_dir, lines, crop)
    return list(variants)


def run_sequence(run_dir: Path, sqx: str, trtno: int, components: int) -> pd.DataFrame:
    """Run one sequence (crop rotation) treatment in the engine's mode ``Q``; returns the summary.

    Mode Q reads a batch file, one line per rotation component: the FileX name in the first 92
    columns, then treatment, replicate and sequence (component) numbers as ``1X,I6`` fields
    (``CSM_Main/CSM.for``, the batch reader). The engine runs the lines in order and starts again
    from the first one for the next year, carrying the soil from one crop to the next, so each
    sequence treatment gets its own engine call.
    """
    import subprocess
    run_dir = Path(run_dir)
    batch = "$BATCH(SEQUENCE)\n!\n@FILEX" + " " * 86 + "TRTNO     RP     SQ     OP     CO\n"
    batch += "".join(f"{sqx:<92}{trtno:>7}{1:>7}{k:>7}{0:>7}{0:>7}\n" for k in range(1, components + 1))
    (run_dir / "DSSBatch.v48").write_text(batch)
    for f in run_dir.glob("*.csv"):
        f.unlink()
    runner.write_control(run_dir, "summary")
    p = subprocess.run([str(config.engine_exe()), "Q", "DSSBatch.v48", "DSCSM048.CTR"], cwd=run_dir,
                       capture_output=True, text=True, encoding="latin-1")
    s = runner.read_summary(run_dir)
    if p.returncode != 0 or s.empty:
        raise RuntimeError(f"sequence run failed ({p.returncode}): {p.stdout[-800:]}")
    return s


def write_built(run_dir: Path, template: "filex.FileX", units: pd.DataFrame, exp_code: str, ext: str,
                **kw) -> tuple[str, pd.DataFrame]:
    """``filex.build_filex`` written into ``run_dir``; returns (file name, units with TRNO)."""
    switches = kw.pop("switches", None)
    built = filex.build_filex(template, units, exp_code=exp_code, **kw)
    text = set_switches(built.text, switches) if switches else built.text
    name = exp_code + ext
    (Path(run_dir) / name).write_text(text, encoding="latin-1")
    return name, built.units


def set_switches(text: str, switches: dict) -> str:
    """Set one-letter options in *SIMULATION CONTROLS, e.g. {"WATER": "N", "NITRO": "N", "CO2": "D"}.

    The engine reads these rows as ``FORMAT (I3,11X,9(5X,A1))`` (``InputModule/IPSIM.for:217,1001``):
    each option is the last character of a 6-character field, right under the end of its header
    name. (``filex.build_filex``'s ``simulation_overrides`` left-aligns text, which suits names
    such as CNAME but not these switches.)
    """
    out, header, in_sim, seen = [], None, False, set()
    for ln in text.splitlines():
        if ln.startswith("*"):
            in_sim = ln.upper().startswith("*SIMULATION CONTROLS")
            header = None
        elif in_sim and ln.startswith("@"):
            header = ln
        elif in_sim and header and ln.strip() and not ln.startswith("!"):
            for name, val in switches.items():
                m = re.search(rf"(?<=\s){re.escape(name)}(?=\s|$)", header)
                if m:
                    col = m.end() - 1
                    ln = ln.ljust(col + 1)
                    ln = ln[:col - 5] + " " * 5 + str(val)[:1] + ln[col + 1:]
                    seen.add(name)
        out.append(ln)
    missing = set(switches) - seen
    if missing:
        raise KeyError(f"no simulation-control column {sorted(missing)}")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------- observed data
def _read_table(path: Path) -> pd.DataFrame:
    """Read the first @-headed table of a DSSAT FileA/FileT (whitespace separated)."""
    rows, header = [], None
    for ln in Path(path).read_text(encoding="latin-1").splitlines():
        if ln.startswith("@"):
            if header is None:
                header = ln[1:].split()
            continue
        if header is None or not ln.strip() or ln.lstrip().startswith(("!", "*")):
            continue
        rows.append(ln.split())
    df = pd.DataFrame([r[: len(header)] for r in rows], columns=header)
    return df.apply(pd.to_numeric, errors="coerce").replace(-99, np.nan)


def _date(v) -> pd.Timestamp:
    v = int(v)
    if v > 1_000_000:                    # YYYYDDD
        return pd.to_datetime(str(v), format="%Y%j")
    yy, ddd = divmod(v, 1000)            # YYDDD, DSSAT rule: YY <= 40 is 20YY (DATES.for:84-87)
    yyyy = 2000 + yy if yy <= 40 else 1900 + yy
    return pd.to_datetime(f"{yyyy}{ddd:03d}", format="%Y%j")


def read_filea(path) -> pd.DataFrame:
    """End-of-season observations (FileA, e.g. UFGA8201.MZA); date columns parsed."""
    df = _read_table(path)
    for c in ("ADAT", "MDAT", "HDAT", "R1AT"):
        if c in df:
            df[c] = df[c].map(lambda v: _date(v) if v == v else pd.NaT)
    return df


def read_filet(path) -> pd.DataFrame:
    """Time-series observations (FileT, e.g. UFGA8201.MZT), long by date."""
    df = _read_table(path)
    df["date"] = df["DATE"].map(_date)
    return df


def engine_layers(bottoms) -> list[float]:
    """Layer bottoms (cm) the engine simulates for a soil profile, soil layer option ``MESOL`` = 2.

    A port of ``LYRSET2`` (``Soil/SoilUtilities/LMATCH.for:263``): the top layers are fixed at 5 and
    15 cm, thin layers are merged into the next one, and thick layers are split into 2, 3 or 4 equal
    layers depending on depth. Daily soil outputs (``SW1D`` ...) refer to these layers.
    """
    def nint(x):                         # Fortran NINT: halves round away from zero
        return float(np.floor(x + 0.5))

    ds = [5.0, 15.0]
    for cum in [float(b) for b in bottoms]:
        th = cum - ds[-1]
        if cum <= 15.0 or th < 2.0:
            continue
        if (cum < 90 and th <= 15) or (cum >= 90 and th <= 30) or (cum >= 200 and th <= 90):
            ds.append(cum)
        elif (cum < 90 and th <= 30) or (cum >= 90 and th <= 60) or (cum >= 200 and th <= 180):
            ds += [cum - nint(th * 0.5), cum]
        elif (cum < 90 and th <= 45) or (cum >= 90 and th <= 90) or (cum >= 200 and th <= 270):
            ds += [cum - nint(th * 0.66667), cum - nint(th * 0.33333), cum]
        else:
            ds += [cum - nint(th * 0.75), cum - nint(th * 0.5), cum - nint(th * 0.25), cum]
    if float(bottoms[-1]) > ds[-1]:
        ds[-1] = float(bottoms[-1])
    return ds


def _fit(v, width: int = 5) -> str:
    """A number in at most ``width`` characters (the engine reads these files by whitespace)."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
        return "-99"
    v = float(v)
    if v == int(v) and abs(v) < 10 ** width:
        return str(int(v))
    for prec in range(width, 0, -1):     # shortest form with the most significant digits that fits
        t = f"{v:.{prec}g}"
        if "e" in t:
            continue
        if t.startswith("0."):
            t = t[1:]                    # .5186 instead of 0.5186, as DSSAT files do
        elif t.startswith("-0."):
            t = "-" + t[2:]
        if len(t) <= width:
            return t
    return str(int(round(v)))


def write_observed(path, df: pd.DataFrame, kind: str, exp_code: str, title: str = "") -> Path:
    """Write a FileA (``kind="A"``, one row per treatment) or FileT (``kind="T"``, one row per
    treatment and date) from a table: the job of the shell's ATCreate tool.

    ``df`` has ``TRNO`` (and ``DATE`` as a Timestamp for a FileT) plus one column per DSSAT variable
    code (``Data/DATA.CDE``). Columns are 6 characters wide and right-aligned under their header,
    missing values are -99, dates are written as YYDDD.
    """
    d = df.copy()
    cols = [c for c in d.columns if c not in ("TRNO", "DATE")]
    keys = ["TRNO"] + (["DATE"] if kind == "T" else [])
    head = f"*EXP.DATA ({kind}): {exp_code} {title}".rstrip()
    # "@TRNO" takes 5 of the first 6 columns; every later name ends where its 6-character values end
    lines = [head, "", "@TRNO " + "".join(f"{c:>6}" for c in keys[1:] + cols)]
    for _, r in d.iterrows():
        row = f"{int(r.TRNO):>6}"
        if kind == "T":
            t = pd.Timestamp(r.DATE)
            row += f"{t.year % 100:02d}{t.dayofyear:03d}".rjust(6)
        row += "".join(f"{_fit(r[c]):>6}" for c in cols)
        lines.append(row)
    p = Path(path)
    p.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return p


def data_codes(codes) -> pd.DataFrame:
    """Descriptions of DSSAT variable codes from the engine's ``DATA.CDE``."""
    rows = {}
    for ln in (Path(config.ENGINE_DIR) / "DATA.CDE").read_text(encoding="latin-1").splitlines():
        if not ln or ln[0] in "*@!$ ":
            continue
        code = ln[:6].strip()
        if code in codes and code not in rows:
            rows[code] = {"code": code, "label": ln[7:23].strip(), "description": ln[23:79].strip()}
    return pd.DataFrame([rows.get(c, {"code": c, "label": "", "description": "(not in DATA.CDE)"}) for c in codes])


# ----------------------------------------------------------------------------- statistics
def rmse(obs, sim) -> float:
    o, s = np.asarray(obs, float), np.asarray(sim, float)
    ok = np.isfinite(o) & np.isfinite(s)
    return float(np.sqrt(np.mean((s[ok] - o[ok]) ** 2))) if ok.any() else np.nan


def willmott_d(obs, sim) -> float:
    """Index of agreement d (Willmott 1981): 1 is perfect agreement."""
    o, s = np.asarray(obs, float), np.asarray(sim, float)
    ok = np.isfinite(o) & np.isfinite(s)
    o, s = o[ok], s[ok]
    if len(o) < 2:
        return np.nan
    om = o.mean()
    den = np.sum((np.abs(s - om) + np.abs(o - om)) ** 2)
    return float(1 - np.sum((s - o) ** 2) / den) if den > 0 else np.nan


# ----------------------------------------------------------------------------- export
def export(df: pd.DataFrame, module: str, name: str, digits: int = 3) -> Path:
    """Write a small CSV for the website under ``docs/learn/data/<module>/``."""
    out = DATA_OUT / module
    out.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    for c in df.select_dtypes("float").columns:
        df[c] = df[c].round(digits)
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.strftime("%Y-%m-%d")
        elif pd.api.types.is_bool_dtype(df[c]):
            df[c] = df[c].map({True: "true", False: "false"})    # d3.autoType reads only lowercase
    p = out / f"{name}.csv"
    df.to_csv(p, index=False)
    return p


def cultivar_table(crop: str = "MZCER048") -> pd.DataFrame:
    """Cultivar coefficients of the engine's cultivar file as a table (first entry of each code)."""
    path = Path(config.ENGINE_DIR) / "Genotype" / f"{crop}.CUL"
    lines = path.read_text(encoding="latin-1").splitlines()
    head = next(ln for ln in lines if ln.startswith("@VAR#"))
    names = head[23:].split()
    rows = []
    for ln in lines:
        if not ln or ln[0] in "!@*$ " or len(ln) < 30:
            continue
        vals = ln[23:].split()
        if len(vals) < len(names):
            continue
        rows.append({"var": ln[:6], "name": ln[7:23].strip(), **dict(zip(names, vals))})
    t = pd.DataFrame(rows).drop_duplicates("var", keep="first")
    for c in names[2:]:
        t[c] = pd.to_numeric(t[c], errors="coerce")
    return t

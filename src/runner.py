"""Run the DSSAT engine on chunks of simulation units.

One chunk = one run directory = one FileX with up to a few hundred
treatments (one per grid-cell season), the weather files of its cells and a
``SOIL.SOL`` with its soil profiles. Chunks run as separate ``dscsm048``
processes, so they can run in parallel.

Engine behaviour this module relies on (see the dssat-engine skill):

* output file names are fixed and cleared at start-up, so every process needs
  its own directory;
* a fatal input error ends the process with ``STOP 99`` and skips every
  remaining treatment of the FileX. With CSV output, ``summary.csv`` is then
  empty: the rows of treatments that had already finished are lost too
  (measured 2026-11-05). ``ERROR.OUT`` names the failing treatment, so we
  record that one, drop it, and run all the others again.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

import config
from filex import MAX_LEVELS, MAX_TREATMENTS, FileX, build_filex

# Control-file presets (rows of the engine's DSCSM048.CTR):
#   8 = CSV output, Summary only (VBOSE=0); 7 = CSV output for all files.
CTRNO = {"summary": 8, "daily": 7}


@dataclass
class RunResult:
    run_dir: Path
    returncode: int
    seconds: float
    stdout: str
    summary: pd.DataFrame
    error_out: str = ""


@dataclass
class ChunkResult:
    chunk_id: int
    summary: pd.DataFrame                    # one row per finished unit
    failed: pd.DataFrame                     # units that stopped the engine
    runs: list[RunResult] = field(default_factory=list)
    daily: dict[str, pd.DataFrame] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# single engine call
# ----------------------------------------------------------------------------
def write_control(run_dir: Path, outputs: str = "summary", engine_dir: Path | None = None) -> Path:
    """Copy the engine's DSCSM048.CTR with ``@CTRNO`` set for lean output."""
    src = Path(engine_dir or config.ENGINE_DIR) / "DSCSM048.CTR"
    lines = src.read_text(encoding="latin-1").splitlines()
    out = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if lines[i].startswith("@CTRNO"):
            out.append(f"    {CTRNO[outputs]:02d} xdssat {outputs}")
            i += 2  # replace the line after the header
            continue
        i += 1
    dst = run_dir / "DSCSM048.CTR"
    dst.write_text("\n".join(out) + "\n", encoding="latin-1")
    return dst


def run_engine(run_dir: Path, filex_name: str, outputs: str = "summary",
               engine_dir: Path | None = None, timeout: float | None = 3600, mode: str = "A") -> RunResult:
    """Run ``dscsm048 <mode> <file> DSCSM048.CTR`` in ``run_dir``: mode A runs every treatment of
    a FileX, mode Q a sequence batch file (src/sequence.py)."""
    run_dir = config.check_short_path(Path(run_dir))
    exe = config.engine_exe(engine_dir)
    write_control(run_dir, outputs, engine_dir)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(exe), mode, filex_name, "DSCSM048.CTR"],
        cwd=run_dir, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="latin-1", timeout=timeout,
    )
    secs = time.perf_counter() - t0
    err = run_dir / "ERROR.OUT"
    return RunResult(
        run_dir=run_dir, returncode=proc.returncode, seconds=secs,
        stdout=proc.stdout + proc.stderr, summary=read_summary(run_dir),
        error_out=err.read_text(encoding="latin-1") if err.exists() else "",
    )


def read_summary(run_dir: Path) -> pd.DataFrame:
    """Read ``summary.csv`` (CSV summary output); empty frame if absent."""
    p = Path(run_dir) / "summary.csv"
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    df = pd.read_csv(p, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    return df.replace(-99, pd.NA)


def read_daily_csv(run_dir: Path) -> dict[str, pd.DataFrame]:
    """Daily CSV outputs in ``run_dir`` (plantgro, soilwat, weather, soilni ...).

    Measured 2026-11-05 (control preset 7): one CSV per output group holding
    every treatment of the FileX, keyed by ``TRTNUM`` with ``YEAR`` and
    ``DOY``. We rename ``TRTNUM`` to ``TRNO`` and add a ``date`` column.
    ``summary.csv`` and ``EnvSum.csv`` (seasonal) are skipped. Rice flood
    water (``FloodW.OUT``) has no CSV form and is not read here.
    """
    out = {}
    for p in sorted(Path(run_dir).glob("*.csv")):
        if p.stem.lower() in ("summary", "envsum"):
            continue
        try:
            df = pd.read_csv(p, skipinitialspace=True)
        except (pd.errors.ParserError, pd.errors.EmptyDataError):
            continue
        df.columns = [c.strip() for c in df.columns]
        if "TRTNUM" in df:
            df = df.rename(columns={"TRTNUM": "TRNO"})
        if {"YEAR", "DOY"} <= set(df.columns):
            df.insert(0, "date", pd.to_datetime(
                df["YEAR"].astype(int) * 1000 + df["DOY"].astype(int), format="%Y%j"))
        out[p.stem.lower()] = df.replace(-99, pd.NA)
    return out


# ----------------------------------------------------------------------------
# chunks
# ----------------------------------------------------------------------------
def run_chunk(chunk_id: int, units: pd.DataFrame, template: FileX,
              write_inputs: Callable[[Path, pd.DataFrame], None],
              work_root: Path | None = None, outputs: str = "summary",
              engine_dir: Path | None = None, keep: bool = False,
              max_retries: int | None = None, daily_groups=None, **build_kw) -> ChunkResult:
    """Run one chunk of units, skipping and recording units that crash.

    ``write_inputs(run_dir, units)`` must write the ``<wsta>.WTH`` files and
    ``SOIL.SOL`` the units need into ``run_dir``.
    """
    root = Path(work_root or config.WORK_ROOT)
    run_dir = root / f"c{chunk_id:05d}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    config.check_short_path(run_dir)
    write_inputs(run_dir, units)

    ext = template.path.suffix if template.path else ".RIX"
    exp_code = f"XD{chunk_id % 1_000_000:06d}"
    fx_name = exp_code + ext.upper()

    pending = units.reset_index(drop=True)
    done, failed, runs = [], [], []
    daily: dict[str, list] = {}
    retries = 0
    while len(pending):
        built = build_filex(template, pending, exp_code=exp_code, **build_kw)
        (run_dir / fx_name).write_text(built.text, encoding="latin-1")
        res = run_engine(run_dir, fx_name, outputs, engine_dir)
        runs.append(res)
        summ = res.summary
        got = set(summ["TRNO"].astype(int)) if "TRNO" in summ else set()
        if len(summ):
            merged = summ.merge(built.units, on="TRNO", how="inner",
                                suffixes=("", "_unit"))
            done.append(merged)
        if outputs == "daily":
            keys = ["TRNO"] + [c for c in ("cell", "stratum", "water", "season", "season_year", "pdate")
                               if c in built.units]
            for name, df in read_daily_csv(run_dir).items():
                if daily_groups is not None and name not in daily_groups:
                    continue
                if "TRNO" in df:
                    df = df.merge(built.units[keys], on="TRNO", how="inner")
                daily.setdefault(name, []).append(df)
        missing = [t for t in built.units["TRNO"] if t not in got]
        if not missing:
            break
        culprit = failed_treatment(res)
        if culprit is None or culprit not in missing:
            culprit = missing[0]
        bad = built.units[built.units["TRNO"] == culprit].copy()
        bad["error"] = _first_error(res)
        failed.append(bad)
        rerun = [t for t in missing if t != culprit]
        pending = built.units[built.units["TRNO"].isin(rerun)].drop(columns="TRNO")
        retries += 1
        if max_retries is not None and retries > max_retries:
            rest = built.units[built.units["TRNO"].isin(rerun)].copy()
            rest["error"] = "not run: retry limit reached"
            failed.append(rest)
            break

    if not keep:
        shutil.rmtree(run_dir, ignore_errors=True)
    return ChunkResult(
        chunk_id=chunk_id,
        summary=pd.concat(done, ignore_index=True) if done else pd.DataFrame(),
        failed=pd.concat(failed, ignore_index=True) if failed else pd.DataFrame(),
        runs=runs,
        daily={k: pd.concat(v, ignore_index=True) for k, v in daily.items()},
    )


_TRT_RE = re.compile(r"^\s*TREATMENT\s+(\d+)\s*:", re.MULTILINE)


def failed_treatment(res: RunResult) -> int | None:
    """Treatment number that stopped the engine, from ERROR.OUT.

    ERROR.OUT repeats the run header of the failing run, including a line
    ``TREATMENT  6   : <name>``. Measured 2026-11-05 with an unknown soil ID.
    """
    if res.returncode == 0:
        return None
    hits = _TRT_RE.findall(res.error_out) or _TRT_RE.findall(res.stdout)
    return int(hits[-1]) if hits else None


def _first_error(res: RunResult) -> str:
    """The engine's error message: the ERROR.OUT lines after ``<FileX>, Trt n``."""
    txt = res.error_out or res.stdout
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    start = next((i + 1 for i, ln in enumerate(lines) if ", Trt" in ln), 0)
    keep = [ln for ln in lines[start:]
            if not ln.startswith(("Additional information", "Please press"))]
    return " | ".join(keep[:4])[:500] or f"engine exit code {res.returncode}"


def split_chunks(units: pd.DataFrame, chunk_size: int = 200,
                 per_soil_ic: bool = False) -> list[pd.DataFrame]:
    """Split units into chunks that fit one FileX.

    A chunk holds at most ``chunk_size`` treatments (<= 999), at most 99
    distinct cells (field levels) and at most 99 distinct management
    combinations (template treatment, planting date and overrides), because
    some engine readers take factor levels as two digits (see
    ``filex._set_level``). With ``per_soil_ic`` (initial conditions rebuilt on
    each soil, ``build_filex(soils=..., ic_water=...)``) every (soil, planting
    date, template treatment) is its own IC level, so those are capped at 99
    too. Each cell's seasons stay together where possible.
    """
    if chunk_size > MAX_TREATMENTS:
        raise ValueError("chunk_size above 999 exceeds the FileX treatment limit")
    units = units.sort_values(["cell", "pdate"], kind="stable").reset_index(drop=True)
    mgmt_cols = ["pdate"] + [c for c in units.columns
                             if c == "trno_template" or (isinstance(c, str) and "." in c)]
    mgmt = list(units[mgmt_cols].astype(str).itertuples(index=False, name=None))
    if per_soil_ic:
        ic = list(zip(units["soil_id"].astype(str), mgmt))
    else:
        ic = [None] * len(units)
    chunks, start = [], 0
    cells: set = set()
    keys: set = set()
    ics: set = set()
    for i, (cell, key, ick) in enumerate(zip(units["cell"], mgmt, ic)):
        new_cells = len(cells | {cell})
        new_keys = len(keys | {key})
        new_ics = len(ics | {ick})
        if i > start and (i - start >= chunk_size or new_cells > MAX_LEVELS
                          or new_keys > MAX_LEVELS or new_ics > MAX_LEVELS):
            chunks.append(units.iloc[start:i])
            start, cells, keys, ics = i, set(), set(), set()
        cells.add(cell)
        keys.add(key)
        ics.add(ick)
    chunks.append(units.iloc[start:])
    return chunks


def run_units(units: pd.DataFrame, template: FileX,
              write_inputs: Callable[[Path, pd.DataFrame], None],
              chunk_size: int = 200, workers: int = 4, **kw) -> list[ChunkResult]:
    """Run all units in parallel chunks. Threads are enough: the work happens
    in the engine subprocesses."""
    per_soil_ic = kw.get("soils") is not None and kw.get("ic_water") is not None
    chunks = split_chunks(units, chunk_size, per_soil_ic=per_soil_ic)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_chunk, i + 1, c, template, write_inputs, **kw)
                for i, c in enumerate(chunks)]
        return [f.result() for f in futs]

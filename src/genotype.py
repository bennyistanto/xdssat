"""Cultivar files written into each run directory, with optional coefficient overrides.

The engine looks for the cultivar file (e.g. RICER048.CUL) in the working
directory first and only then in the Genotype folder of DSSATPRO
(dssat-csm-os InputModule/ipexp.for:507-517). When it is found locally the
genotype path is blank, so the ecotype and species files are copied too.

Overrides change one coefficient of one cultivar in place, right-aligned under
its header name, e.g. {"IB0012": {"TCLDF": 18.0}}. The engine's own files are
never modified.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import config

CROP = "RICER048"


@lru_cache(maxsize=8)
def _read(path: str) -> str:
    return Path(path).read_text(encoding="latin-1")


def _set(line: str, header: str, name: str, value: float) -> str:
    """Write one coefficient into a cultivar row. The field runs from two characters after the
    end of the previous header name to the end of its own name (the header rule of the engine,
    READS.for:997), so short names such as P1 get the width of their values. The file's own
    precision is kept, with fewer decimals when the value would not fit."""
    toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"\S+", header)]
    k = [t[0] for t in toks].index(name)
    end = toks[k][2]
    start = toks[k - 1][2] + 1 if k else 0
    width = end - start
    line = line.ljust(end)
    old = line[start:end].strip()
    decimals = len(old.split(".")[1]) if "." in old else 1    # keep the file's own precision
    text = f"{value:.{decimals}f}"
    while len(text) > width and decimals > 0:
        decimals -= 1
        text = f"{value:.{decimals}f}"
    if len(text) > width:
        raise ValueError(f"{value} does not fit the {width}-character {name} field")
    return line[:start] + text.rjust(width) + line[end:]


def cultivar_text(overrides: dict | None = None, crop: str = CROP, engine_dir: Path | None = None) -> str:
    """The cultivar file with ``overrides`` ({cultivar code: {coefficient: value}}) applied."""
    text = _read(str(Path(engine_dir or config.ENGINE_DIR) / "Genotype" / f"{crop}.CUL"))
    if not overrides:
        return text
    lines, header, done = text.splitlines(), None, set()
    for i, ln in enumerate(lines):
        if ln.startswith("@VAR#"):
            header = ln
        elif header and ln[:6] in overrides:
            for name, value in overrides[ln[:6]].items():
                if name not in header.split():
                    raise KeyError(f"{crop}.CUL has no coefficient {name}")
                ln = _set(ln, header, name, float(value))
            lines[i] = ln
            done.add(ln[:6])
    missing = set(overrides) - done
    if missing:
        raise KeyError(f"cultivars not in {crop}.CUL: {sorted(missing)}")
    return "\n".join(lines) + "\n"


def variants_text(base: str, variants: dict, crop: str = CROP, engine_dir: Path | None = None,
                  names: dict | None = None) -> str:
    """The cultivar file with extra cultivars copied from ``base``: ``variants`` maps a new
    6-character code to coefficient values, e.g. {"GL0001": {"P1": 210.0}}. Each is inserted
    after ``base`` with its own code and name, so one FileX runs many coefficient sets as
    treatments (``CU.INGENO``) in one engine call (GLUE, sensitivity sweeps)."""
    lines = cultivar_text(None, crop, engine_dir).splitlines()
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
            if name not in hdr.split():
                raise KeyError(f"{crop}.CUL has no coefficient {name}")
            ln = _set(ln, hdr, name, float(value))
        new.append(ln)
    lines[at + 1:at + 1] = new
    return "\n".join(lines) + "\n"


def write_variants(run_dir: Path, base: str, variants: dict, crop: str = CROP,
                   engine_dir: Path | None = None) -> None:
    """``variants_text`` into ``run_dir``, with the engine's ecotype and species files."""
    g = Path(engine_dir or config.ENGINE_DIR) / "Genotype"
    (Path(run_dir) / f"{crop}.CUL").write_text(variants_text(base, variants, crop, engine_dir), encoding="latin-1")
    for ext in ("ECO", "SPE"):
        src = g / f"{crop}.{ext}"
        if src.exists():
            (Path(run_dir) / src.name).write_text(_read(str(src)), encoding="latin-1")


RICE = {"code": "RI", "name": "rice", "model": "RICER048", "model_name": "CERES-Rice"}


def _code_list(path: Path, section: str | None, code: str) -> str | None:
    """Description of ``code`` in a DSSAT code list (``*.CDE``): rows ``CODE  description``."""
    if not path.is_file():
        return None
    inside = section is None
    for ln in _read(str(path)).splitlines():
        if ln.startswith("*"):
            inside = section is None or ln[1:].strip().lower().startswith(section.lower())
        elif inside and ln[:8].split() == [code]:
            return ln[8:].strip() or None
    return None


def crop_info(crop_model: str = CROP, crop_code: str | None = None, engine_dir: Path | None = None) -> dict:
    """The crop of a run, named from the engine's own code lists.

    ``{"code": "MZ", "name": "maize", "model": "MZCER048", "model_name": "CERES-Maize"}``:
    the crop name from DETAIL.CDE (``*Crop and Weed Species``), the model from
    SIMULATION.CDE (``MZCER    MZ   CERES-Maize``). The crop code defaults to the
    first two letters of the model; a name not found falls back to the code.
    """
    d = Path(engine_dir or config.ENGINE_DIR)
    code = (crop_code or crop_model[:2]).upper()
    name = _code_list(d / "DETAIL.CDE", "Crop and Weed Species", code)
    name = name.split("  ")[0].strip() if name else None
    mline = _code_list(d / "SIMULATION.CDE", None, crop_model[:5])
    model_name = mline.split(None, 1)[1].strip() if mline and len(mline.split(None, 1)) == 2 else crop_model
    return {"code": code, "name": (name or code).lower(), "model": crop_model, "model_name": model_name}


def write_cultivar_files(run_dir: Path, overrides: dict | None = None, crop: str = CROP,
                         engine_dir: Path | None = None) -> None:
    g = Path(engine_dir or config.ENGINE_DIR) / "Genotype"
    (Path(run_dir) / f"{crop}.CUL").write_text(cultivar_text(overrides, crop, engine_dir), encoding="latin-1")
    for ext in ("ECO", "SPE"):
        src = g / f"{crop}.{ext}"
        if src.exists():
            (Path(run_dir) / src.name).write_text(_read(str(src)), encoding="latin-1")

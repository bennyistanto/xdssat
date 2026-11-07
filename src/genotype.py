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

from functools import lru_cache
from pathlib import Path

import config

CROP = "RICER048"


@lru_cache(maxsize=8)
def _read(path: str) -> str:
    return Path(path).read_text(encoding="latin-1")


def _set(line: str, header: str, name: str, value: float) -> str:
    end = header.index(name) + len(name)          # values end under the end of the name
    start = header.rfind(" ", 0, header.index(name)) + 1
    width = end - start
    line = line.ljust(end)
    old = line[start:end].strip()
    decimals = len(old.split(".")[1]) if "." in old else 1    # keep the file's own precision
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


def write_cultivar_files(run_dir: Path, overrides: dict | None = None, crop: str = CROP,
                         engine_dir: Path | None = None) -> None:
    g = Path(engine_dir or config.ENGINE_DIR) / "Genotype"
    (Path(run_dir) / f"{crop}.CUL").write_text(cultivar_text(overrides, crop, engine_dir), encoding="latin-1")
    for ext in ("ECO", "SPE"):
        src = g / f"{crop}.{ext}"
        if src.exists():
            (Path(run_dir) / src.name).write_text(_read(str(src)), encoding="latin-1")

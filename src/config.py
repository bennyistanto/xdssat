"""Where the DSSAT engine lives and where runs happen.

Two locations, both outside the project folder:

ENGINE_DIR
    Holds ``dscsm048`` (``dscsm048.exe`` on Windows), the engine ``Data`` files
    (``*.CDE``, ``Genotype/``, ``StandardData/`` ...) and ``DSSATPRO.V48`` /
    ``DSSATPRO.L48``. Built by ``scripts/build_engine.py``.
WORK_ROOT
    Parent of the per-process run directories. Must be a short path: the
    engine keeps file paths in fixed ``CHARACTER*80`` to ``*102`` buffers
    (see docs/feasibility-review.md, section 4), so OneDrive or a deep temp
    folder will silently break runs.

Override either with the environment variables ``XDSSAT_ENGINE`` and
``XDSSAT_WORK``, or pass paths explicitly to the runner functions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    _DEFAULT_ENGINE = Path("C:/TEMP/dsn/dssat")
    _DEFAULT_WORK = Path("C:/TEMP/dsn/work")
else:
    _DEFAULT_ENGINE = Path.home() / ".xdssat" / "dssat"
    _DEFAULT_WORK = Path("/tmp/xdssat")

ENGINE_DIR = Path(os.environ.get("XDSSAT_ENGINE", _DEFAULT_ENGINE))
WORK_ROOT = Path(os.environ.get("XDSSAT_WORK", _DEFAULT_WORK))

# Input data (large; keep outside OneDrive). Override with XDSSAT_AGERA5,
# XDSSAT_SOILGRIDS, XDSSAT_INPUTS and XDSSAT_CACHE. The defaults are the
# author's machine; see docs/getting-started.md.
AGERA5_DIR = Path(os.environ.get("XDSSAT_AGERA5", "E:/temp/agera5"))
SOILGRIDS_DIR = Path(os.environ.get("XDSSAT_SOILGRIDS", "E:/temp/soilgrids"))
INPUTS_DIR = Path(os.environ.get("XDSSAT_INPUTS", "E:/temp"))
CACHE_DIR = Path(os.environ.get("XDSSAT_CACHE", "E:/temp/xdssat"))

# Every setting, for scripts/check_setup.py: name -> (env variable, value).
SETTINGS = {
    "ENGINE_DIR": ("XDSSAT_ENGINE", ENGINE_DIR),
    "WORK_ROOT": ("XDSSAT_WORK", WORK_ROOT),
    "AGERA5_DIR": ("XDSSAT_AGERA5", AGERA5_DIR),
    "SOILGRIDS_DIR": ("XDSSAT_SOILGRIDS", SOILGRIDS_DIR),
    "INPUTS_DIR": ("XDSSAT_INPUTS", INPUTS_DIR),
    "CACHE_DIR": ("XDSSAT_CACHE", CACHE_DIR),
}

EXE_NAME = "dscsm048.exe" if IS_WINDOWS else "dscsm048"
PROFILE_NAME = "DSSATPRO.V48" if IS_WINDOWS else "DSSATPRO.L48"

# Longest run-directory path we accept. The engine prepends directories to
# 12-character file names (e.g. GR000123.WTH) inside 80-character buffers;
# 60 leaves room for the name and a separator.
MAX_RUN_DIR_LEN = 60


def engine_exe(engine_dir: Path | None = None) -> Path:
    """Path of the engine executable; raises if it is missing."""
    exe = Path(engine_dir or ENGINE_DIR) / EXE_NAME
    if not exe.is_file():
        raise FileNotFoundError(
            f"DSSAT engine not found at {exe}. Build it with "
            "`python scripts/build_engine.py`, or set XDSSAT_ENGINE."
        )
    return exe


def check_short_path(path: Path, limit: int = MAX_RUN_DIR_LEN) -> Path:
    """Raise if ``path`` is too long for the engine's fixed path buffers."""
    p = str(Path(path).resolve())
    if len(p) > limit:
        raise ValueError(
            f"Path {p!r} is {len(p)} characters; the DSSAT engine needs <= {limit}. "
            "Use a short work root such as C:/TEMP/dsn/work (set XDSSAT_WORK)."
        )
    return Path(p)

"""Shared test setup.

Tests import the flat modules in ``src/`` (living-folder layout, no package).
Fixture data comes from the sibling ``dssat-csm-data`` repository; tests that
need it are skipped when it is absent. Tests marked ``engine`` run the real
``dscsm048`` and are skipped when it is not installed (see
``scripts/build_engine.py``).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

DATA = Path(ROOT.parent / "dssat-csm-data")

has_data = DATA.is_dir()
has_engine = (config.ENGINE_DIR / config.EXE_NAME).is_file()

needs_data = pytest.mark.skipif(not has_data, reason=f"fixture data not found at {DATA}")
needs_engine = pytest.mark.skipif(not (has_data and has_engine),
                                  reason="DSSAT engine or fixture data not installed")


def pytest_configure(config):
    config.addinivalue_line("markers", "engine: runs the real DSSAT engine")


@pytest.fixture
def short_tmp(tmp_path_factory):
    """A run directory under the short work root (the engine needs short paths)."""
    import uuid
    d = config.WORK_ROOT / f"t{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)

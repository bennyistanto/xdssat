"""Check an xdssat setup and say what to do next.

    python scripts/check_setup.py              # everything
    python scripts/check_setup.py --no-run     # skip the 2-second engine test run

Read-only, except for one short engine run in a temporary folder under
WORK_ROOT (deleted afterwards). Prints one line per item: OK, WARN or MISSING,
with the command that fixes it. Exit code 1 if anything required for running
the engine is missing. See docs/getting-started.md.
"""
import argparse
import importlib
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

REQUIRED = ["numpy", "pandas", "xarray", "netCDF4", "cftime", "geopandas", "pyogrio",
            "shapely", "pyproj", "rasterio", "scipy", "openpyxl"]
OPTIONAL = ["pytest", "matplotlib", "jupyterlab", "dask"]
FIXTURES = ROOT.parent / "dssat-csm-data"
INPUTS = {   # relative to INPUTS_DIR
    "rice polygons (ATR/BPN 2024)": "idn-lbs/idn_phy_luasbakusawah_atrbpn_2024_a.gpkg",
    "Java irrigated rice (MoA)": "idn-lbs/idn_phy_ricefield_java_irrigated_moa_a.shp",
    "Java rainfed rice (MoA)": "idn-lbs/idn_phy_ricefield_java_rainfed_moa_a.shp",
    "agro-ecological zones / islands": "idn-soil/idn_phy_agroecological_zone_bbsdlp_a.shp",
    "soil map 1:50k (BBSDLP)": "idn-soil/idn_phy_tanahsemidetil_50k_bbsdlp_a.shp",
}

failures = 0


def line(status, what, detail="", fix=""):
    global failures
    if status == "MISSING" and what.startswith("*"):
        failures += 1
    print(f"  {status:8s}{what.lstrip('*')}" + (f": {detail}" if detail else ""))
    if fix and status != "OK":
        print(f"{'':10s}-> {fix}")


def section(t):
    print(f"\n{t}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-run", action="store_true", help="do not run the engine")
    a = ap.parse_args()

    section("Python")
    print(f"  {sys.executable} ({sys.version.split()[0]})")
    for m in REQUIRED + OPTIONAL:
        try:
            mod = importlib.import_module(m)
            line("OK", m, getattr(mod, "__version__", ""))
        except Exception as e:  # noqa: BLE001
            req = m in REQUIRED
            line("MISSING" if req else "WARN", ("*" if req else "") + m, type(e).__name__,
                 "pip install -r requirements.txt (or conda install -c conda-forge " + m + ")")

    section("Settings (environment variable, value, where it came from)")
    import os
    for name, (env, value) in config.SETTINGS.items():
        src = "env" if env in os.environ else "default"
        print(f"  {name:14s}{env:18s}{value}  [{src}]")

    section("DSSAT engine")
    eng = config.ENGINE_DIR
    exe = eng / config.EXE_NAME
    line("OK" if exe.is_file() else "MISSING", "*engine executable", str(exe),
         "python scripts/build_engine.py  (or set XDSSAT_ENGINE to an installed DSSAT 4.8)")
    ctr = eng / "DSCSM048.CTR"
    line("OK" if ctr.is_file() else "MISSING", "*DSCSM048.CTR (control file)", str(ctr),
         "copy it from dssat-csm-os/Data/DSCSM048.CTR into the engine folder")
    prof = eng / config.PROFILE_NAME
    if prof.is_file():
        line("OK", f"{config.PROFILE_NAME} (path profile)", str(prof))
    else:
        line("MISSING", f"*{config.PROFILE_NAME} (path profile)", str(prof),
             "python scripts/build_engine.py writes it; for an installed DSSAT it is in the install folder")
    for sub in ("Genotype", "StandardData"):
        d = eng / sub
        line("OK" if d.is_dir() else "MISSING", f"*{sub}/", str(d),
             "copy dssat-csm-os/Data/* into the engine folder (build_engine.py does this)")
    for name, path, limit in (("engine folder", eng, 40), ("work root", config.WORK_ROOT, 40)):
        p = str(Path(path).resolve())
        bad_len = len(p) > limit
        in_onedrive = "onedrive" in p.lower()
        status = "WARN" if (bad_len or in_onedrive) else "OK"
        why = "; ".join(x for x in (f"{len(p)} characters (keep under {limit})" if bad_len else "",
                                     "inside OneDrive" if in_onedrive else "") if x)
        line(status, f"{name} path", why or p,
             "use a short local folder such as C:/TEMP/dsn/... (set XDSSAT_ENGINE / XDSSAT_WORK)")
    info = eng / "BUILD_INFO.txt"
    if info.is_file():
        line("OK", "build info", info.read_text().splitlines()[0])

    if not a.no_run and exe.is_file() and ctr.is_file():
        rix = FIXTURES / "Rice" / "IRMZ8601.RIX"
        if not rix.is_file():
            line("WARN", "engine test run", f"skipped: {rix} not found",
                 "git clone https://github.com/DSSAT/dssat-csm-data next to this folder")
        else:
            from runner import run_engine
            run = config.WORK_ROOT / f"check_{uuid.uuid4().hex[:6]}"
            try:
                run.mkdir(parents=True)
                shutil.copy(rix, run)
                for w in (FIXTURES / "Weather").glob("IRMZ*.WTH"):
                    shutil.copy(w, run)
                shutil.copy(FIXTURES / "Soil" / "SOIL.SOL", run)
                res = run_engine(run, rix.name, timeout=300)
                n = 0 if res.summary is None else len(res.summary)
                ok = res.returncode == 0 and n > 0
                line("OK" if ok else "MISSING", "*engine test run (IRMZ8601.RIX)",
                     f"{n} treatments simulated" if ok else (res.error_out or "")[-300:],
                     "check ERROR.OUT; usually a wrong DSSATPRO path or a missing Data file")
            finally:
                shutil.rmtree(run, ignore_errors=True)

    section("Test fixtures (for python -m pytest tests)")
    line("OK" if FIXTURES.is_dir() else "WARN", "dssat-csm-data", str(FIXTURES),
         "git clone https://github.com/DSSAT/dssat-csm-data next to this folder")

    section("Source data")
    ag = config.AGERA5_DIR
    import agera5
    for var, v in agera5.VARIABLES.items():
        years = agera5.available_years(var) if (ag / v.folder).is_dir() else []
        line("OK" if years else "MISSING", f"AgERA5 {v.folder} ({var})",
             f"{years[0]}-{years[-1]}, {len(years)} files" if years else str(ag / v.folder),
             "set XDSSAT_AGERA5 to the folder with srad/, t2mmax/, t2mmin/, precip/, t2mdp/, ws10m/")
    for what, rel in INPUTS.items():
        p = config.INPUTS_DIR / rel
        line("OK" if p.exists() else "MISSING", what, str(p), "set XDSSAT_INPUTS or copy the file there")
    vrt = config.SOILGRIDS_DIR / "clay" / "clay_0-5cm_mean.vrt"
    line("OK" if vrt.is_file() else "MISSING", "SoilGrids mirror", str(config.SOILGRIDS_DIR),
         "set XDSSAT_SOILGRIDS; then python scripts/fetch_soilgrids.py fills missing tiles")

    section("Prepared caches (in CACHE_DIR, built by the scripts)")
    c = config.CACHE_DIR
    steps = [
        ("AgERA5 Indonesia cache", c / "agera5" / "idn" / "TMAX", "python scripts/build_agera5_cache.py"),
        ("100 m rasters", c / "rasters" / "idn_rice.tif", "python scripts/prepare_rasters.py"),
        ("Java irrigation (filled)", c / "rasters" / "idn_irrigation_filled.tif",
         "python scripts/prepare_rasters.py --layers irrigation irrigation_filled"),
        ("SoilGrids Indonesia cache", c / "soilgrids" / "idn" / "soc_0-5cm_mean.tif",
         "python scripts/fetch_soilgrids.py, then python scripts/build_soilgrids_cache.py"),
        ("Java soil profiles", c / "soils" / "java.SOL", "python scripts/prepare_soils.py --aoi java"),
    ]
    for what, p, fix in steps:
        line("OK" if p.exists() else "MISSING", what, str(p), fix)

    print(f"\n{'Engine ready.' if failures == 0 else f'{failures} required item(s) missing.'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

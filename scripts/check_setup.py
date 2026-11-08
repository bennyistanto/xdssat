"""Check an xdssat setup and say what to do next.

    python scripts/check_setup.py                                    # for configs/xdssat.yml
    python scripts/check_setup.py --config configs/<study>.yml       # for another experiment file
    python scripts/check_setup.py --no-run                           # skip the 2-second engine test run

Read-only, except for one short engine run in a temporary folder under the work
root (deleted afterwards). First checks the experiment file (configs/xdssat.yml;
configs/rice-java.yml explains every setting) and lists every folder with where
its value came from and the line of the file that sets it. Then prints one line
per item: OK, WARN or MISSING, with the command, or the line of the experiment
file, that fixes it. Exit code 1 if anything required for running the engine is
missing or the experiment file has problems. See docs/getting-started.md.
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

REQUIRED = ["yaml", "numpy", "pandas", "xarray", "netCDF4", "cftime", "geopandas", "pyogrio",
            "shapely", "pyproj", "rasterio", "scipy", "openpyxl"]
PIP_NAME = {"yaml": "pyyaml"}
OPTIONAL = ["pytest", "matplotlib", "jupyterlab", "dask"]
FIXTURES = ROOT.parent / "dssat-csm-data"
INPUTS = {   # relative to paths > inputs
    "rice polygons (ATR/BPN 2024)": "idn-lbs/idn_phy_luasbakusawah_atrbpn_2024_a.gpkg",
    "Java irrigated rice (MoA)": "idn-lbs/idn_phy_ricefield_java_irrigated_moa_a.shp",
    "Java rainfed rice (MoA)": "idn-lbs/idn_phy_ricefield_java_rainfed_moa_a.shp",
    "agro-ecological zones / islands": "idn-soil/idn_phy_agroecological_zone_bbsdlp_a.shp",
    "soil map 1:50k (BBSDLP)": "idn-soil/idn_phy_tanahsemidetil_50k_bbsdlp_a.shp",
}
BOUNDARIES = ("idn_bnd_adm1.shp", "idn_bnd_adm2.shp")   # in paths > boundaries

failures = 0
EXP = config.CURRENT      # the experiment checked; set in main()


def line(status, what, detail="", fix=""):
    global failures
    if status == "MISSING" and what.startswith("*"):
        failures += 1
    print(f"  {status:8s}{what.lstrip('*')}" + (f": {detail}" if detail else ""))
    if fix and status != "OK":
        print(f"{'':10s}-> {fix}")


def section(t):
    print(f"\n{t}")


def edit(key: str) -> str:
    """Where to change setting ``key`` (e.g. "paths.engine") in the experiment file."""
    sec, name = key.split(".", 1)
    value = EXP.data.get(sec, {}).get(name) if isinstance(EXP.data.get(sec), dict) else None
    shown = value if value is not None else "<folder>"
    src = EXP.where(key)
    if src.startswith("environment variable"):
        var = src.split()[2]
        return (f"this value comes from the environment variable {var}: change or unset {var} "
                f"(without it, '{name}:' under '{sec}:' in the experiment file is used)")
    var = config.PATH_KEYS[name][0] if sec == "paths" else None
    env = f", or set the environment variable {var}" if var else ""
    if EXP.file is None:
        ex = config.read_yaml(config.EXAMPLE_FILE)[1].get(key) if config.EXAMPLE_FILE.is_file() else None
        return (f"copy configs/{config.EXAMPLE_FILE.name} to configs/{config.DEFAULT_FILE.name}, then edit "
                f"its line {ex}:  {name}: <folder>{env}")
    ln = EXP.lines.get(key)
    if ln:
        return f"edit {EXP.file} line {ln}:  {name}: {shown}{env}"
    return f"add to {EXP.file}, under '{sec}:', the line   {name}: <folder>{env}"


def folder(key: str, what: str, required: bool):
    """The folder ``paths > key``, or None after reporting that it is not set."""
    if EXP.has_path(key):
        return EXP.path(key)
    line("MISSING", ("*" if required else "") + what, "paths > " + key + " is not set", edit(f"paths.{key}"))
    return None


def check_experiment(path):
    """Load and check the experiment file; print its problems. Returns the number of problems."""
    global EXP
    section("Experiment file")
    try:
        EXP = config.use(config.load(path))
        line("OK", "settings", str(EXP.file) if EXP.file else
             f"no configs/{config.DEFAULT_FILE.name}: built-in defaults "
             f"(= configs/{config.EXAMPLE_FILE.name}, with no folders set)")
        problems = config.check_files(EXP)
    except config.ConfigError as e:
        problems = e.problems
        try:
            EXP = config.use(config.load(path, only=("paths",)))
            print("  (the checks below use the paths of the file and the built-in defaults for the rest)")
        except config.ConfigError:
            EXP = config.use(config.load_defaults())
            print("  (the checks below use the built-in defaults)")
    except ImportError:
        EXP = config.use(config.load_defaults())
        problems = ["PyYAML is missing, so no experiment file can be read: "
                    "pip install pyyaml (or conda install -c conda-forge pyyaml)"]
    for p in problems:
        print("  PROBLEM " + p.replace("\n", "\n" + " " * 10))
    if not problems:
        runs = EXP.combinations()
        print(f"  {len(runs)} run(s): " + "; ".join(f"{cv}, {n} kg N, scenario {sc['name']}"
                                                 for cv, n, sc in runs))
    return len(problems)


def main():
    global failures
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path,
                    help="experiment file (default: XDSSAT_CONFIG, else configs/xdssat.yml, "
                         "else the built-in defaults)")
    ap.add_argument("--no-run", action="store_true", help="do not run the engine")
    a = ap.parse_args()

    failures += check_experiment(a.config)

    section("Folders (where each value came from; an environment variable wins over the file)")
    for key in config.PATH_KEYS:
        k = f"paths.{key}"
        need = "needed to run" if key in config.RUN_PATHS else "needed to prepare inputs"
        print(f"  {key:11s}{EXP.path(key) if EXP.has_path(key) else '(not set)'}   [{need}]")
        print(f"{'':13s}from: {EXP.where(k)}")
        print(f"{'':13s}to change it: {edit(k)}")

    section("Python")
    print(f"  {sys.executable} ({sys.version.split()[0]})")
    for m in REQUIRED + OPTIONAL:
        try:
            mod = importlib.import_module(m)
            line("OK", m, getattr(mod, "__version__", ""))
        except Exception as e:  # noqa: BLE001
            req = m in REQUIRED
            pkg = PIP_NAME.get(m, m)
            line("MISSING" if req else "WARN", ("*" if req else "") + m, type(e).__name__,
                 "pip install -r requirements.txt (or conda install -c conda-forge " + pkg + ")")

    section("DSSAT engine")
    eng = folder("engine", "engine folder", required=True)
    work = folder("work", "work root", required=True)
    exe = ctr = None
    if eng is not None:
        exe = eng / config.EXE_NAME
        line("OK" if exe.is_file() else "MISSING", "*engine executable", str(exe),
             "python scripts/build_engine.py, or point paths > engine at an installed DSSAT 4.8: "
             + edit("paths.engine"))
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
    for name, key, path, limit in (("engine folder", "paths.engine", eng, 40),
                                   ("work root", "paths.work", work, 40)):
        if path is None:
            continue
        p = str(Path(path).resolve())
        bad_len = len(p) > limit
        in_onedrive = "onedrive" in p.lower()
        status = "WARN" if (bad_len or in_onedrive) else "OK"
        why = "; ".join(x for x in (f"{len(p)} characters (keep under {limit})" if bad_len else "",
                                     "inside OneDrive" if in_onedrive else "") if x)
        line(status, f"{name} path", why or p, "use a short local folder such as C:/TEMP/dsn/...: " + edit(key))
    if eng is not None and (eng / "BUILD_INFO.txt").is_file():
        line("OK", "build info", (eng / "BUILD_INFO.txt").read_text().splitlines()[0])

    if not a.no_run and exe is not None and exe.is_file() and ctr.is_file() and work is not None:
        rix = FIXTURES / "Rice" / "IRMZ8601.RIX"
        if not rix.is_file():
            line("WARN", "engine test run", f"skipped: {rix} not found",
                 "git clone https://github.com/DSSAT/dssat-csm-data next to this folder")
        else:
            from runner import run_engine
            run = work / f"check_{uuid.uuid4().hex[:6]}"
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

    section("Source data (needed to prepare inputs, not to run)")
    ag = folder("agera5", "AgERA5 archive", required=False)
    if ag is not None:
        import agera5
        for var, v in agera5.VARIABLES.items():
            years = agera5.available_years(var, ag) if (ag / v.folder).is_dir() else []
            line("OK" if years else "MISSING", f"AgERA5 {v.folder} ({var})",
                 f"{years[0]}-{years[-1]}, {len(years)} files" if years else str(ag / v.folder),
                 "point paths > agera5 at the folder with srad/, t2mmax/, t2mmin/, precip/, t2mdp/, ws10m/: "
                 + edit("paths.agera5"))
    inp = folder("inputs", "source maps", required=False)
    if inp is not None:
        for what, rel in INPUTS.items():
            p = inp / rel
            line("OK" if p.exists() else "MISSING", what, str(p), "copy the file there, or " + edit("paths.inputs"))
    sg = folder("soilgrids", "SoilGrids mirror", required=False)
    if sg is not None:
        vrt = sg / "clay" / "clay_0-5cm_mean.vrt"
        line("OK" if vrt.is_file() else "MISSING", "SoilGrids mirror", str(sg),
             edit("paths.soilgrids") + "; then python scripts/fetch_soilgrids.py fills missing tiles")
    bnd = folder("boundaries", "admin boundaries", required=False)
    if bnd is not None:
        for f in BOUNDARIES:
            p = bnd / f
            line("OK" if p.is_file() else "MISSING", f"admin boundaries {f}", str(p),
                 "copy the file there, or " + edit("paths.boundaries"))

    section("Prepared caches (in paths > cache, built by the scripts)")
    c = folder("cache", "cache folder", required=True)
    if c is not None:
        line("OK" if c.is_dir() else "MISSING", "cache folder", str(c), edit("paths.cache"))
        steps = [
            ("AgERA5 Indonesia cache", c / "agera5" / "idn" / "TMAX", "python scripts/build_agera5_cache.py"),
            ("100 m rasters", c / "rasters" / "idn_rice.tif", "python scripts/prepare_rasters.py"),
            ("Java irrigation (filled)", c / "rasters" / "idn_irrigation_filled.tif",
             "python scripts/prepare_rasters.py --layers irrigation irrigation_filled"),
            ("SoilGrids Indonesia cache", c / "soilgrids" / "idn" / "soc_0-5cm_mean.tif",
             "python scripts/fetch_soilgrids.py, then python scripts/build_soilgrids_cache.py"),
            (f"{EXP.aoi} soil profiles", c / "soils" / f"{EXP.aoi}.SOL",
             f"python scripts/prepare_soils.py --aoi {EXP.aoi}"),
        ]
        for what, p, fix in steps:
            line("OK" if p.exists() else "MISSING", what, str(p), fix)

    section("Prepared inputs (the inputs section of the experiment file; src/inputs.py)")
    if c is not None:
        import inputs
        for kind, name in EXP.data["inputs"].items():
            if not name:
                line("OK", kind, "not used (null)")
                continue
            ok = inputs.manifest_path(kind, name, c).is_file()
            line("OK" if ok else "MISSING", ("" if kind in inputs.OPTIONAL else "*") + kind, name,
                 "python scripts/register_inputs.py registers files made by the original scripts; "
                 f"datasets of this kind: {', '.join(inputs.available(kind, c)) or 'none'}")
        print("  (whether they meet the requirements is checked with the experiment file above)")

    print(f"\n{'Engine ready.' if failures == 0 else f'{failures} required item(s) missing or wrong.'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

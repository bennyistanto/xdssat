# Getting started

From an empty machine to a gridded rice run for Java. Each step says what it
needs, what to run, and how long it took on the reference machine (Windows 11,
2 x Intel Xeon Gold 6138: 40 cores, 80 logical processors; data on a local hard disk; measured November 2026).

If you only want to know what is missing on your machine, run the checker; it
prints the fix for every missing item:

```bash
python scripts/check_setup.py
```

## 0. What the pieces are

| Piece | What it is | Where it lives |
|---|---|---|
| xdssat (this folder) | Python code, scripts, tests | anywhere, OneDrive is fine |
| DSSAT engine | `dscsm048` executable plus its `Data` files | short local path, **not** OneDrive |
| Work root | one temporary folder per engine process | short local path, **not** OneDrive |
| Source data | AgERA5, SoilGrids, rice and soil maps | any local disk |
| Cache | everything the scripts derive from the source data | any local disk, ~5 GB for Indonesia |
| `dssat-csm-data` | DSSAT example experiments, used by the tests | next to this folder |
| `dssat-csm-os` | DSSAT source, only needed to build the engine | next to this folder |

Why the short paths: the engine keeps file paths in fixed-length Fortran text
buffers (80-102 characters), so a long or synced folder breaks runs without a
clear error. Keep the engine and work folders under about 40 characters.

Suggested layout:

```
Documents/Github/            <- or any parent folder
    xdssat/                  this repository
    dssat-csm-data/          git clone https://github.com/DSSAT/dssat-csm-data
    dssat-csm-os/            git clone https://github.com/DSSAT/dssat-csm-os   (only to build)
C:/TEMP/dsn/dssat/           engine (XDSSAT_ENGINE)
C:/TEMP/dsn/work/            run folders (XDSSAT_WORK)
```

## 1. Python environment

Any environment with the packages in [`requirements.txt`](../requirements.txt)
works. Tested: Python 3.11 with pandas 3.0 (conda env `climate`) and Python
3.12 with pandas 2.3 (miniforge base).

**Use an existing conda environment.** On Windows, run it through
`conda run` (or activate it first). Calling `envs/<name>/python.exe` directly
can exit silently with code 127 on `import numpy`, because without activation
its DLL folders are not on `PATH` and `GDAL_DATA` is not set.

```bash
conda run --no-capture-output -n climate python scripts/check_setup.py
```

If `conda` is not on `PATH`, call it by full path, for example
`C:/Users/<you>/miniforge3/Scripts/conda.exe run ...`.

**Or create a new one** (named `xdssat`):

```bash
conda env create -f environment.yml
```

Add missing packages from conda-forge rather than pip where possible; GDAL,
rasterio and netCDF4 bring compiled libraries.

In the rest of this guide, `python` means "the environment you chose", for
example `conda run --no-capture-output -n climate python`.

## 2. DSSAT engine

xdssat needs these in the engine folder (`XDSSAT_ENGINE`):

- `dscsm048.exe` (Windows) or `dscsm048` (Linux, macOS)
- `DSCSM048.CTR`, the control file xdssat copies and edits for every run
- `DSSATPRO.V48` (Windows) or `DSSATPRO.L48`, the path profile
- the engine `Data` files: `*.CDE`, `Genotype/`, `StandardData/`, ...

### Case A: DSSAT is not installed (build from source)

This is the tested route. `scripts/build_engine.py` exports the source, builds
it, copies the executable and `Data/` into the engine folder and writes the
path profile. It records the source commit in `BUILD_INFO.txt`.

Tools needed on `PATH`: `git`, `cmake`, a Fortran compiler and `make`.

- **Windows (tested):** GNU Fortran 13.2.0 from the MinGW-w64 toolchain that
  ships with Strawberry Perl (https://strawberryperl.com), CMake 3.29
  (https://cmake.org), and its `mingw32-make`. Open a new terminal after
  installing so they are on `PATH`; check with `gfortran --version`,
  `cmake --version`, `mingw32-make --version`.
- **Linux / macOS (not tested here):** `gfortran`, `cmake` and `make` from the
  system package manager, or the build tools listed in `environment.yml`.

Then:

```bash
git clone https://github.com/DSSAT/dssat-csm-os ../dssat-csm-os
python scripts/build_engine.py
```

Options: `--source <path>` for another checkout, `--clone` to fetch the source
itself, `--ref <tag>` with `--clone`, `--engine-dir <path>` to install
elsewhere (then set `XDSSAT_ENGINE` to the same path). On the reference machine
the build took about 67 s (commit `25c93b9a`, v4.8.6.4).

`cmake --install` is deliberately not used: it fails on Windows
(`CMakeLists.txt` installs `Utilities/run_dssat`, which is only generated on
other systems). The script copies the files itself.

### Case B: DSSAT 4.8 is already installed on Windows

**Not tested: there was no installed DSSAT on the reference machine.** The
steps below follow from what xdssat needs (list above) and from how the engine
finds its path profile: it looks in the current folder, then the executable's
folder, then `%DSSAT_HOME%`, then `C:\DSSAT48\` (`InputModule/PATH.for:144-166`
in the engine source).

1. Point xdssat at the install folder, for example `C:\DSSAT48` (short, outside
   OneDrive, so it qualifies):

   ```powershell
   [Environment]::SetEnvironmentVariable("XDSSAT_ENGINE", "C:\DSSAT48", "User")
   ```

   Open a new terminal afterwards. For one session only:
   `$env:XDSSAT_ENGINE = "C:\DSSAT48"`.
2. Run `python scripts/check_setup.py`. It lists any file xdssat needs that the
   install lacks (for example `DSCSM048.CTR`; copy it from
   `dssat-csm-os/Data/` if so) and runs one short rice experiment.
3. The tests compare xdssat against the same engine, so they should pass with
   any 4.8 build. Absolute results can differ slightly between engine
   versions; for work you want to reproduce, Case A pins the version.

Do not point `XDSSAT_ENGINE` at a folder inside OneDrive.

## 3. Configuration

All locations are read in [`src/config.py`](../src/config.py) from environment
variables. The defaults are the author's machine; on another machine either set
the variables or change the defaults in `config.py`.

| Variable | Default (Windows) | Holds |
|---|---|---|
| `XDSSAT_ENGINE` | `C:/TEMP/dsn/dssat` | the DSSAT engine (section 2) |
| `XDSSAT_WORK` | `C:/TEMP/dsn/work` | one run folder per engine process; temporary |
| `XDSSAT_AGERA5` | `E:/temp/agera5` | AgERA5: `<folder>/agera5_<folder>_<YYYY>.nc` |
| `XDSSAT_SOILGRIDS` | `E:/temp/soilgrids` | SoilGrids 2.0 mirror: `<prop>/<prop>_<depth>_mean.vrt` + tiles |
| `XDSSAT_INPUTS` | `E:/temp` | rice and soil maps (`idn-lbs/`, `idn-soil/`) |
| `XDSSAT_CACHE` | `E:/temp/xdssat` | everything the scripts build |

On Linux and macOS the engine and work defaults are `~/.xdssat/dssat` and
`/tmp/xdssat`.

Set one permanently (PowerShell, current user):

```powershell
[Environment]::SetEnvironmentVariable("XDSSAT_CACHE", "D:\xdssat-cache", "User")
```

`python scripts/check_setup.py` prints every setting and whether it came from
the environment or the default.

## 4. Check the setup

```bash
python scripts/check_setup.py
python -m pytest tests
```

The checker runs one rice experiment (`IRMZ8601.RIX`, 10 treatments) in the
work root. The test suite (57 tests, about 45 s) needs `dssat-csm-data`; tests
that need the engine or the prepared caches are skipped when those are absent.

## 5. Source data

| Data | Expected location | Notes |
|---|---|---|
| AgERA5 daily, one netCDF per variable-year | `XDSSAT_AGERA5/<folder>/agera5_<folder>_<YYYY>.nc` | folders `srad`, `t2mmax`, `t2mmin`, `precip` (required), `t2mdp`, `ws10m`. Global 0.1 degree. https://doi.org/10.24381/cds.6c68c9bb |
| SoilGrids 2.0 mean layers | `XDSSAT_SOILGRIDS/<prop>/` | same layout as https://files.isric.org/soilgrids/latest/data/. Only the `.vrt` files are needed to start; step 6 downloads the tiles over Indonesia |
| Rice fields 2024 (ATR/BPN) | `XDSSAT_INPUTS/idn-lbs/idn_phy_luasbakusawah_atrbpn_2024_a.gpkg` | all Indonesia; attributes only for Sumatra |
| Java rice, irrigated and rainfed (Ministry of Agriculture) | `XDSSAT_INPUTS/idn-lbs/idn_phy_ricefield_java_{irrigated,rainfed}_moa_a.shp` | irrigation type for Java |
| Agro-ecological zones (BBSDLP) | `XDSSAT_INPUTS/idn-soil/idn_phy_agroecological_zone_bbsdlp_a.shp` | island groups (used to select Java) and zones |
| Soil map 1:50,000 (BBSDLP) | `XDSSAT_INPUTS/idn-soil/idn_phy_tanahsemidetil_50k_bbsdlp_a.shp` | class names only; rasterised for reference |

## 6. Prepare the inputs

Run in this order. Every script is resumable or cheap to rerun, and writes into
`XDSSAT_CACHE`. Times are from the reference machine.

| Step | Command | Time | Output |
|---|---|---|---|
| 1. Weather cache, Indonesia | `python scripts/build_agera5_cache.py` | 13.8 min | `agera5/idn/<VAR>/`, 2.5 GB |
| 2. 100 m rasters (rice, soil classes, regions, zones, irrigation) | `python scripts/prepare_rasters.py` | about 8 min | `rasters/idn_*.tif` + class tables |
| 3. Missing SoilGrids tiles over Indonesia | `python scripts/fetch_soilgrids.py` | 31.8 min for 15,269 tiles (4.37 GB) | tiles in `XDSSAT_SOILGRIDS` |
| 4. SoilGrids Indonesia cache | `python scripts/build_soilgrids_cache.py` | 33.5 min | `soilgrids/idn/*.tif`, 1.7 GB |
| 5. Soil profiles for Java | `python scripts/prepare_soils.py --aoi java` | 0.5 min | `soils/java.SOL` (1,271 profiles) |

Notes:

- Step 2 does not depend on step 1. Steps 3-5 need step 1 (they use the land
  mask of the weather grid); step 5 also needs steps 2-4.
- Step 3 downloads from ISRIC (CC-BY 4.0). `--dry-run` only counts what is
  missing. Re-running skips tiles already present.
- Step 5 without `--aoi` builds profiles for all of Indonesia.
- What each step does and why is in the module docstrings:
  [`agera5.py`](../src/agera5.py), [`rasters.py`](../src/rasters.py),
  [`soils.py`](../src/soils.py).

## 7. Run the simulations

First a quick test on 30 cells and 2 years (about 20 s):

```bash
python scripts/run_aoi.py --aoi java --mode uniform --years 2015 2016 --sample 30 --out C:/TEMP/xd_test
```

Then a full run, one of three management modes
([`management.py`](../src/management.py)):

```bash
python scripts/run_aoi.py --aoi java --mode uniform  --years 1991 2020
python scripts/run_aoi.py --aoi java --mode by_zone  --years 1991 2020 [--zone-table zones.csv]
python scripts/run_aoi.py --aoi java --mode per_cell --years 1991 2020
```

- **Strata**: one per weather cell and water regime (irrigated or rainfed,
  from the Ministry of Agriculture classes), carrying its rice area in ha.
- **uniform**: wet season 15 Nov for all rice, dry season 1 Apr for irrigated
  rice. These dates are placeholders.
- **per_cell**: wet-season onset per cell from AgERA5 rain with the BMKG
  dasarian rule (a dasarian of at least 50 mm followed by two more), median
  over the years; transplanting 20 days after onset, dry season 120 days later.
- **by_zone**: one calendar per agro-ecological zone, from `--zone-table`
  (columns `zone,season,month_day`) or, without it, the median onset of each
  zone's cells.
- Template: [`templates/XDRI8601.RIX`](../templates/XDRI8601.RIX), IRRI's
  IRMZ8601 (IR 58, 140 kg N) with an added rainfed bunded water level, IR 64
  as a second cultivar, and one treatment per cultivar, water regime and N
  rate (`scripts/make_templates.py`, `management.TREATMENT_GRID`).
- `--cultivar "IR 64"` and `--n 0|35|70|105|140` pick the treatment (default
  IR 58, 140 kg N, as urea: 2/3 at 18 and 1/3 at 41 days after
  transplanting). Non-default runs get a suffix such as `_ir64_n105`.
- `--slpf 0.73` sets the soil fertility factor `SLPF` of every profile. CERES-Rice
  multiplies daily carbohydrate production by it (`RI_Grosub.for:648-649`), so
  it stands for yield losses the model does not simulate. It is the level
  calibration against district statistics (`scripts/compare_districts.py`).
  Yield falls faster than SLPF: on a 40-cell sample, 0.8 gave 71% and 0.7 gave
  55% of the SLPF 1 yield.
- `--tcldf 19` sets the cultivar's cold-sterility threshold (C). From late panicle
  development to heading the engine sums degree-days of canopy temperature
  (0.87 + 0.92 Tmin) below it and cuts spikelet fertility
  (`RI_Phenol.for:600-648`). The cultivar file is written into each run folder
  (`src/genotype.py`; the engine reads a local `RICER048.CUL` first), so the
  engine's own files stay untouched. 19 C lowers cool highland cells (Tmin < 20 C)
  by about 40% and leaves the lowlands unchanged.
- `--slpf-rainfed 0.58` gives rainfed strata their own SLPF (their cells get a
  copy of the soil profile with ID `XR<iy><ix>`). Current calibration:
  `--slpf 0.76 --slpf-rainfed 0.58 --tcldf 19`, fitted with
  `scripts/fit_slpf.py` on four runs (SLPF 0.53, 0.63, 0.73, 0.83, all TCLDF 19)
  against BDSP district means 1992-2020. The script interpolates each season's
  yield between the runs (cubic), searches the irrigated x rainfed grid and
  reports a leave-one-province-out check.
- `--slpf-table slpf_temp_v1.csv` sets SLPF per stratum from a table (columns
  `iy, ix, water, slpf`), replacing `--slpf` and `--slpf-rainfed`. `fit_slpf.py`
  writes it for its temperature model: SLPF = regime value + slope x (cell
  mean season Tmin - 23 C), clipped to the SLPF range of the runs. **Current
  calibration:** `--slpf-table E:/temp/xdssat/checks/districts/slpf_temp_v1.csv
  --tcldf 19` (irrigated 0.758, rainfed 0.632, slope +0.021 per C). This is a
  statistical correction of the model's long-season yield bonus in cool
  cells, not a mechanism.

Outputs go to `XDSSAT_CACHE/runs/<aoi>_<mode>_<years>/`: `strata.csv`,
`units.csv`, `summary.csv` (one row per season, DSSAT summary joined to the
stratum and its `ha`), `failed.csv`, `run_info.json`, and `onset.csv` /
`zone_table.csv` where used.

The run also writes `<aoi>_<mode>_<years>.nc`, a CF netCDF of the seasonal
results on the AgERA5 0.1 degree grid ([`outputs.py`](../src/outputs.py)). To
rebuild it from an existing run folder:

```bash
python scripts/make_netcdf.py E:/temp/xdssat/runs/java_per_cell_1991-2020
```

- Dimensions `(system, time, lat, lon)`. A `system` is one season and water
  regime that was simulated: `wet_irrigated`, `wet_rainfed`, `dry_irrigated`.
- `time` is 1 Aug of each agricultural year; `planting_date`,
  `anthesis_date` and `maturity_date` hold the actual dates.
- Per season: `yield` (HWAH), `biomass`, `lai_max`, `rain`, `irrigation`,
  `evapotranspiration`, `n_uptake`, `season_length`.
- Areas: `rice_area` (ha of the system's water regime in the cell),
  `harvested_area` (the same where a yield was simulated) and `production`
  (t). The area-weighted mean yield of any region is
  `production.sum() / harvested_area.sum() * 1000` in kg/ha. The irrigated
  area appears under both `wet_irrigated` and `dry_irrigated`, so do not sum
  `rice_area` over `system`; `rice_area_total` is the rice area per cell.
- Values are per stratum (one simulation per cell and regime), not per 100 m
  pixel. Java 1991-2020 is 2.4 MB and takes 6 s.

### Compare with BPS yields

`scripts/compare_bps.py` compares the runs with BPS provincial productivity
(quintal/ha of gabah kering giling, 1993-2015, CSV export of the BPS web
table). It needs the province layers (`prepare_rasters.py --layers province
province_filled`), groups seasons by calendar year of harvest, weights by rice
area and converts dry matter to GKG at 14% moisture (`--moisture`, an
assumption). Results go to `XDSSAT_CACHE/checks/bps/`.

```bash
python scripts/compare_bps.py --bps path/to/produktivitas_padi_kuintaperha.csv
```

### Compare with district statistics

District (admin2) harvested area, production and yield come from the Ministry
of Agriculture BDSP portal (source named there: BPS):

```bash
python scripts/fetch_bdsp.py          # 114 paced requests, raw pages cached in XDSSAT_CACHE/obs/bdsp/raw
python scripts/clean_bdsp.py          # cleaned CSVs + flags in XDSSAT_CACHE/obs
python scripts/prepare_rasters.py --layers adm2 adm2_filled
python scripts/compare_districts.py --run E:/temp/xdssat/runs/java_per_cell_1991-2020
```

`compare_districts.py` reports the level (simulated / observed), the spatial
agreement of district means, the detrended year-to-year correlation per
district and simulated vs reported harvested area. The admin2 boundaries
(`--adm2`) are the latest BPS-coded districts; `clean_bdsp.py` moves old Papua
and Papua Barat codes to the new provinces.

The older 40-cell check is `spikes/java_engine_check.py`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python.exe` of a conda env exits with code 127, no message | env not activated; use `conda run -n <env>` or activate first |
| `Warning: Cannot find ... GDAL_DATA` | same cause |
| Engine stops at once, `ERROR.OUT` mentions a path or file not found | engine or work folder too long, inside OneDrive, or `DSSATPRO.V48` pointing elsewhere. Rebuild with `build_engine.py` or fix the profile |
| `DSSATPRO.V48` edited by hand does not work | lines must read `KEY C: \path` with a space after the drive letter |
| A run is missing some treatments | the engine stopped on a bad input (`STOP 99`). The runner reruns the others and lists the culprit with its error in `failed` |
| Reading SoilGrids reports missing tiles | run `scripts/fetch_soilgrids.py`, then `build_soilgrids_cache.py` |
| Git: `.git` folder grows large in OneDrive | OneDrive locks interrupt git's repacking. Run `git config gc.auto 0` in the repository and pause OneDrive before a manual `git gc` |

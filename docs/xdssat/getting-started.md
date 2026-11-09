---
title: "Getting started"
aliases:
  - ../getting-started.html
---

From an empty machine to a gridded rice run for Java. Each step says what it
needs, what to run, and how long it took on the reference machine (Windows 11,
2 x Intel Xeon Gold 6138: 40 cores, 80 logical processors; data on a local hard disk; measured November 2026).

**Everything you set lives in one file.** Copy
[`configs/rice-java.yml`](https://github.com/bennyistanto/xdssat/blob/main/configs/rice-java.yml) to `configs/xdssat.yml` and edit that copy:
machine paths, area, cultivars, N rates, water regimes, planting calendar,
scenarios (CO2, warming, calibration factors) and years. Every setting is
explained in the file, with its range, its default and what happens if it is
set too low or too high. You never need to edit Python (section 3).

If you only want to know what is missing on your machine, run the checker; it
checks the experiment file too, and prints the fix for every missing item,
including the line of the file to change:

```bash
python scripts/check_setup.py
```

## Run an example first

`example-inputs/` holds five small areas with their inputs already prepared from
public data (AgERA5, SoilGrids, MapSPAM 2020), each about 1 x 1 degree for the
agricultural years 2018-2020: maize in western Kenya, rice in the Mekong Delta,
pearl millet in Niger, rice in West Java and maize in East Java ([Examples](examples.qmd)).
They need no source data: only the Python environment (section 1), the DSSAT
engine (section 2), and three folders given as environment variables
(PowerShell):

```powershell
$env:XDSSAT_ENGINE = "C:\TEMP\dsn\dssat"     # the engine folder (section 2)
$env:XDSSAT_WORK = "C:\TEMP\dsn\work"        # short, local, outside OneDrive
$env:XDSSAT_CACHE = "D:\xdssat-cache"        # where the run folders go
python scripts/run_aoi.py --config example-inputs/kenya_maize/experiment.yml
```

Each example's `experiment.yml` reads the datasets of its own folder
(`paths > datasets`) and writes its run folders to `<cache>/runs`.

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
C:/TEMP/dsn/dssat/           engine (paths > engine)
C:/TEMP/dsn/work/            run folders (paths > work)
```

## 1. Python environment

Any environment with the packages in [`requirements.txt`](https://github.com/bennyistanto/xdssat/blob/main/requirements.txt)
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

xdssat needs these in the engine folder (`paths > engine` in the experiment file):

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
elsewhere (then set `paths > engine` to the same path). On the reference machine
the build took about 67 s (commit `25c93b9a`, v4.8.6.4).

`cmake --install` is deliberately not used: it fails on Windows
(`CMakeLists.txt` installs `Utilities/run_dssat`, which is only generated on
other systems). The script copies the files itself.

### Case B: DSSAT 4.8 is already installed on Windows

**This route is untested** (no installed DSSAT on the reference machine). The
steps below follow from what xdssat needs (list above) and from how the engine
finds its path profile: it looks in the current folder, then the executable's
folder, then `%DSSAT_HOME%`, then `C:\DSSAT48\` (`InputModule/PATH.for:144-166`
in the engine source).

1. Point xdssat at the install folder, for example `C:\DSSAT48` (short, outside
   OneDrive, so it qualifies): in `configs/xdssat.yml`, under `paths:`, set
   `engine: C:/DSSAT48`. Or set the environment variable, which wins over the
   file (useful when one experiment file is shared by several machines):

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

Do not point `paths > engine` at a folder inside OneDrive.

## 3. Configuration

### One experiment file

An experiment is **one YAML file** that holds everything a user changes. All of
them live in [`configs/`](https://github.com/bennyistanto/xdssat/blob/main/configs/README.md). The reference is
[`configs/rice-java.yml`](https://github.com/bennyistanto/xdssat/blob/main/configs/rice-java.yml): each section starts
with what it is for, and every setting has a comment above it with what it
means, its range, its default, and what happens if it is set too low or too
high. As written it reproduces the standard Java run (IR 58, 140 kg N, uniform
calendar, 1991-2020).
[`configs/rice-java-all-options.yml`](https://github.com/bennyistanto/xdssat/blob/main/configs/rice-java-all-options.yml) is the same
case with every setting written out and every kind of option in use, as a menu to copy
from.

```bash
cp configs/rice-java.yml configs/xdssat.yml       # your computer's file (gitignored)
cp configs/rice-java.yml configs/co2-java.yml     # or one file per study, to keep and share
```

**Fill in the `paths` section first.** xdssat has no built-in folders, and the
committed files leave them empty (`null`), so nothing silently points at another
computer's disks. A step that needs a folder that is not set stops and names the
line to fill.

Edit the copy in any text editor. The sections:

| Section | What it sets |
|---|---|
| `paths` | engine, work root, AgERA5, SoilGrids, source maps, cache folder, admin boundaries |
| `inputs` | which prepared dataset of each kind the run reads: weather, crop mask, soil, water regime, region, zones (section 6) |
| `aoi` | name, box and region of the area of interest |
| `template` | the DSSAT experiment (FileX) every season copies, and the crop model |
| `cultivars` | the varieties to run, as name: cultivar code (INGENO) |
| `treatments` | N rates, water regimes (from the irrigation classes), initial soil water |
| `seasons` | mode (uniform, per_cell, by_zone), calendar, onset rule, zone table |
| `scenarios` | variations: weather changes (CO2, temperature, rain ...), cultivar coefficients, SLPF |
| `years` | the agricultural years to simulate |
| `run` | workers, chunk size, quick-test sample, output folder |

A setting left out takes its default. Relative paths are read from the xdssat
folder. Which file is used: `--config <file>` if given, else the file named by
the environment variable `XDSSAT_CONFIG`, else `configs/xdssat.yml`, else the
built-in defaults (identical to `configs/rice-java.yml`, so with no folders set).

Check a file before running it:

```bash
python scripts/check_setup.py --config configs/co2-java.yml
python scripts/run_aoi.py --config configs/co2-java.yml --check
```

The second lists the run folders the file would make, and runs nothing. A
mistake gives a message naming the file, the line, the setting, what is wrong
and an example of a valid value, for example:

```
xdssat.yml line 199, setting 'treatments > nitrogen_kg':
  150 kg N/ha is not one of the rates of the template (allowed: 0, 35, 70, 105, 140). ...
  Example of a valid value:  nitrogen_kg: [0, 35]
```

### Paths on several machines

The eight folders under `paths` can also come from environment variables, which
win over the file. One shared experiment file then runs on several machines,
each with its own folders. None has a default; the third column is the reference
machine's, as a pattern:

| Setting | Environment variable | Reference machine | Holds |
|---|---|---|---|
| `paths > engine` | `XDSSAT_ENGINE` | `C:/TEMP/dsn/dssat` | the DSSAT engine (section 2) |
| `paths > work` | `XDSSAT_WORK` | `C:/TEMP/dsn/work` | one run folder per engine process; temporary |
| `paths > agera5` | `XDSSAT_AGERA5` | `E:/temp/agera5` | AgERA5: `<folder>/agera5_<folder>_<YYYY>.nc` |
| `paths > soilgrids` | `XDSSAT_SOILGRIDS` | `E:/temp/soilgrids` | SoilGrids 2.0 mirror: `<prop>/<prop>_<depth>_mean.vrt` + tiles |
| `paths > inputs` | `XDSSAT_INPUTS` | `E:/temp` | rice and soil maps (`idn-lbs/`, `idn-soil/`) |
| `paths > cache` | `XDSSAT_CACHE` | `E:/temp/xdssat` | everything the scripts build |
| `paths > boundaries` | `XDSSAT_BOUNDARIES` | `F:/temp/hybrid-bias-correction/data/subset/bnd` | `idn_bnd_adm1.shp`, `idn_bnd_adm2.shp` (district and province layers, comparisons) |
| `paths > datasets` | `XDSSAT_DATASETS` | not set (= `cache`) | the prepared inputs, `inputs/<kind>/<name>/`; set to read shipped datasets such as `example-inputs/<area>/` |

A run needs `engine`, `work` and `cache`; the others are needed only to prepare
the inputs. Set a variable permanently (PowerShell, current user):

```powershell
[Environment]::SetEnvironmentVariable("XDSSAT_CACHE", "D:\xdssat-cache", "User")
```

`python scripts/check_setup.py` prints every path, where its value came from
(a line of the file, an environment variable, or the default) and the exact
line to edit. The loader is [`src/config.py`](https://github.com/bennyistanto/xdssat/blob/main/src/config.py).

## 4. Check the setup

```bash
python scripts/check_setup.py
python -m pytest tests
```

The checker first reads the experiment file and reports any problem in it,
then runs one rice experiment (`IRMZ8601.RIX`, 10 treatments) in the work
root. The test suite (188 tests, about 2.5 minutes on the reference machine
with the engine and the prepared caches present) needs `dssat-csm-data`; tests
that need the engine or the prepared caches are skipped when those are absent.

## 5. Source data of the Java case study

The Java case study is built from these files. For another area, use your own
data or the alternatives in [Input data](data.qmd).

| Data | Expected location | Notes |
|---|---|---|
| AgERA5 daily, one netCDF per variable-year | `<paths > agera5>/<folder>/agera5_<folder>_<YYYY>.nc` | folders `srad`, `t2mmax`, `t2mmin`, `precip` (required), `t2mdp`, `ws10m`. Global 0.1 degree. https://doi.org/10.24381/cds.6c68c9bb |
| SoilGrids 2.0 mean layers | `<paths > soilgrids>/<prop>/` | same layout as https://files.isric.org/soilgrids/latest/data/. Only the `.vrt` files are needed to start; step 6 downloads the tiles over Indonesia |
| Rice fields 2024 (ATR/BPN) | `<paths > inputs>/idn-lbs/idn_phy_luasbakusawah_atrbpn_2024_a.gpkg` | all Indonesia; attributes only for Sumatra |
| Java rice, irrigated and rainfed (Ministry of Agriculture) | `<paths > inputs>/idn-lbs/idn_phy_ricefield_java_{irrigated,rainfed}_moa_a.shp` | irrigation type for Java |
| Agro-ecological zones (BBSDLP) | `<paths > inputs>/idn-soil/idn_phy_agroecological_zone_bbsdlp_a.shp` | island groups (used to select Java) and zones |
| Soil map 1:50,000 (BBSDLP) | `<paths > inputs>/idn-soil/idn_phy_tanahsemidetil_50k_bbsdlp_a.shp` | class names only; rasterised for reference |

## 6. Prepare the inputs

A run reads six kinds of prepared input: weather, crop mask and soil
(required), water regime, region and zones (optional). Each is a dataset in a
standard form in `<paths > cache>/inputs/<kind>/<name>/`, and the `inputs`
section of the experiment file names which dataset of each kind a run uses.
[Input data](data.qmd) describes each kind: what it is used for, the form
it must have, how to prepare it from your own files, and the global datasets to
use when you have none.

```bash
python scripts/register_inputs.py --list   # the prepared datasets, and whether the experiment's choice passes
```

### The Java case study's inputs

The Java case study's inputs are made from the source data of section 5. Run
in this order. Every script is resumable or cheap to rerun, and writes into the
cache folder (`paths > cache`). Times are from the reference machine.

| Step | Command | Time | Output |
|---|---|---|---|
| 1. Weather cache, Indonesia | `python scripts/build_agera5_cache.py` | 13.8 min | `agera5/idn/<VAR>/`, 2.5 GB |
| 2. 100 m rasters (rice, soil classes, regions, zones, irrigation) | `python scripts/prepare_rasters.py` | about 8 min | `rasters/idn_*.tif` + class tables |
| 3. Missing SoilGrids tiles over Indonesia | `python scripts/fetch_soilgrids.py` | 31.8 min for 15,269 tiles (4.37 GB) | tiles in `paths > soilgrids` |
| 4. SoilGrids Indonesia cache | `python scripts/build_soilgrids_cache.py` | 33.5 min | `soilgrids/idn/*.tif`, 1.7 GB |
| 5. Soil profiles for Java | `python scripts/prepare_soils.py --aoi java` | 0.5 min | `soils/java.SOL` (1,271 profiles) |

Notes:

- Step 2 does not depend on step 1. Steps 3-5 need step 1 (they use the land
  mask of the weather grid); step 5 also needs steps 2-4.
- Step 3 downloads from ISRIC (CC-BY 4.0). `--dry-run` only counts what is
  missing. Re-running skips tiles already present.
- Step 5 without `--aoi` builds profiles for all of Indonesia. `--aoi` takes the
  `aoi > name` of the experiment file (add `--config <file>` for another file).
- Each script registers what it made as a prepared input at the end, under the
  names `configs/rice-java.yml` uses. For a cache made before that, run
  `python scripts/register_inputs.py` once.
- What each step does and why is in the module docstrings:
  [`agera5.py`](https://github.com/bennyistanto/xdssat/blob/main/src/agera5.py), [`rasters.py`](https://github.com/bennyistanto/xdssat/blob/main/src/rasters.py),
  [`soils.py`](https://github.com/bennyistanto/xdssat/blob/main/src/soils.py).

## 7. Run the simulations

### From the experiment file

```bash
python scripts/run_aoi.py                                   # configs/xdssat.yml
python scripts/run_aoi.py --config configs/co2-java.yml
```

The run covers **every cultivar x N rate x scenario** of the file, one run
folder each, and copies the resolved settings of each folder into it as
`experiment.yml`, so every result can be traced to, and rerun from, the exact
settings that made it.

First a quick test on 30 cells and 2 years (about 35 s on the reference
machine). Either set `years: [2015, 2016]` and, under `run`, `sample: 30` in
the file, or keep the file as it is and add flags, which win over the file:

```bash
python scripts/run_aoi.py --sample 30 --years 2015 2016 --out C:/TEMP/xd_test
```

### Several varieties, treatments and scenarios

For example, two cultivars, three N rates and three scenarios give 18 run
folders:

```yaml
cultivars:
  "IR 58": IB0012
  "IR 64": IB0015
treatments:
  nitrogen_kg: [0, 70, 140]
scenarios:
  - name: baseline
  - name: co2_550
    environment: {co2: {replace: 550}}
  - name: warmer2
    environment: {tmax: {add: 2}, tmin: {add: 2}}
```

- **Cultivars** are given by their code (INGENO) in the cultivar file. A
  cultivar the template lists (IR 58, IR 64) uses its own cultivar level; any
  other code is put into the template's cultivar level (`CU.INGENO`), so a new
  variety needs no FileX edit, only its line in `RICER048.CUL`.
- **N rates** must be fertilizer levels of the template
  (`treatments > nitrogen_levels`, default 0, 35, 70, 105, 140 kg N as urea,
  2/3 at 18 and 1/3 at 41 days after transplanting).
- **Water regimes** come from the water regime input
  (`treatments > water_regimes`). In the Java case study the Ministry of
  Agriculture classes 1-3 run the template's flooded level and class 4 its
  rainfed bunded level; crop without a class takes `treatments > default_regime`.
- **Scenarios** change the weather for the whole season through the FileX
  ENVIRONMENT MODIFICATIONS section (daylength, radiation, tmax, tmin, rain,
  co2, dewpoint, wind; add, subtract, multiply or replace), cultivar file
  coefficients (`cultivar_coefficients`, e.g. `{TCLDF: 19}`), and the soil
  fertility factor (`slpf`, `slpf_rainfed`, `slpf_table`). They also change the
  template's simulation controls (`simulation`, e.g. `{WATER: N, NITRO: N}` for
  potential production, `{ITHRL: 80}` for an automatic-irrigation threshold),
  single planting and initial-condition fields (`management`, e.g.
  `{MP.PPOP: 150}`), and move every planting date (`planting_offset_days`).
  `configs/rice-java.yml` lists the allowed fields and codes.
- **Folder names** are `<aoi>_<mode>_<first>-<last>` plus a tag for anything
  that is not the default: `_ir64_n105` for cultivar and N, `_slpf0.76_rf0.58`,
  `_tcldf19`, and the scenario name unless it is `baseline`.

### Several areas

Keep one file per area, each with its own `aoi` section (name, box, region),
and build its soil profiles once:

```bash
python scripts/prepare_soils.py --config configs/westjava.yml --aoi westjava
python scripts/run_aoi.py --config configs/westjava.yml
```

Each area needs its own inputs. The Ministry of Agriculture irrigation map
covers Java only: elsewhere, use another water regime map, or none with
`treatments > default_regime` ([Input data](data.qmd#water-regime)).

### Another crop

The crop comes from the template FileX. `configs/maize-example.yml` runs
CERES-Maize in East Java; [Another crop](another-crop.qmd) lists what
changes per crop and which settings act on maize.

```bash
python scripts/run_aoi.py --config configs/maize-example.yml
```

### Crop sequences

A template with the extension `.SQX` is a DSSAT sequence experiment: the crops of
one cycle share a treatment number, in order in the `R` column. Every stratum
then runs its whole sequence for all years in one engine call (mode Q), with the
soil's water, N and organic matter carried from crop to crop
([`src/sequence.py`](https://github.com/bennyistanto/xdssat/blob/main/src/sequence.py)).

- `treatments > water_regimes > template_level` names the sequence treatment of
  each water regime.
- The first date of `seasons > calendar` is the first crop's planting; every
  date of every crop moves with it, so the crops keep their place in the cycle.
- `years` are the cycles. The engine runs on until that many years after the
  first sowing; a first crop it starts after that is left out.
- Cultivars and N rates come from the template. Scenarios work as for single
  seasons, except `slpf_table`.

`templates/XDRM8601.SQX` (`python scripts/make_templates.py`) is wet-season rice
(IR 58, 140 kg N, transplanted on 15 November; sequence 1 flooded, 2 rainfed
bunded) followed by rainfed maize (McCurdy 84aa, 120 kg N, sown on 15 March),
composed from the two single-crop templates with `sequence.compose`.

**Tested**: DSSAT's `Sequence/ITHY7501.SQX` (soybean, chickpea in an automatic
planting window, fallow; 5 cycles, measured weather) rebuilt through xdssat gives
the same seasons as the original run (`tests/test_sequence.py`).

### Management modes

`seasons > mode` (or `--mode`) picks one of three ways to set planting dates
([`management.py`](https://github.com/bennyistanto/xdssat/blob/main/src/management.py)):

- **Strata**: one per weather cell and water regime (irrigated or rainfed, from
  the water regime input), carrying its crop area in ha.
- **uniform**: the dates of `seasons > calendar`; by default wet season 15 Nov
  for all rice, dry season 1 Apr for irrigated rice. These dates are
  placeholders.
- **per_cell**: wet-season onset per cell from the weather input's rain with the BMKG
  dasarian rule (`seasons > onset`; default a dasarian of at least 50 mm
  followed by two more), median over the years; transplanting 20 days after
  onset, the next season 120 days later.
- **by_zone**: one calendar per agro-ecological zone, from
  `seasons > zone_table` (a CSV with columns `zone,season,month_day`, or rows
  in the file) or, without it, the median onset of each zone's cells.
- Template: [`templates/XDRI8601.RIX`](https://github.com/bennyistanto/xdssat/blob/main/templates/XDRI8601.RIX), IRRI's
  IRMZ8601 (IR 58, 140 kg N) with an added rainfed bunded water level, IR 64
  as a second cultivar, and one treatment per cultivar, water regime and N
  rate (`scripts/make_templates.py`). The runner picks the treatment by its
  irrigation and fertilizer levels.

### Calibration settings

These are scenario settings in the file; the flags of the same name set them
for every scenario of the run.

- `slpf: 0.73` (`--slpf 0.73`) sets the soil fertility factor `SLPF` of every
  profile. CERES-Rice multiplies daily carbohydrate production by it
  (`RI_Grosub.for:648-649`), so it stands for yield losses the model does not
  simulate. It is the level calibration against district statistics
  (`scripts/compare_districts.py`). Yield falls faster than SLPF: on a 40-cell
  sample, 0.8 gave 71% and 0.7 gave 55% of the SLPF 1 yield.
- `cultivar_coefficients: {TCLDF: 19}` (`--tcldf 19`) sets the cultivar's
  cold-sterility threshold (C). From late panicle development to heading the
  engine sums degree-days of canopy temperature (0.87 + 0.92 Tmin) below it
  and cuts spikelet fertility (`RI_Phenol.for:600-648`). The cultivar file is
  written into each run folder (`src/genotype.py`; the engine reads a local
  `RICER048.CUL` first), so the engine's own files stay untouched. 19 C lowers
  cool highland cells (Tmin < 20 C) by about 40% and leaves the lowlands
  unchanged.
- `slpf_rainfed: 0.58` (`--slpf-rainfed 0.58`) gives rainfed strata their own
  SLPF (their cells get a copy of the soil profile with ID `XR<iy><ix>`). The
  pair `slpf: 0.76, slpf_rainfed: 0.58` with TCLDF 19 was fitted with
  `scripts/fit_slpf.py` on four runs (SLPF 0.53, 0.63, 0.73, 0.83, all TCLDF
  19) against BDSP district means 1992-2020. The script interpolates each
  season's yield between the runs (cubic), searches the irrigated x rainfed
  grid and reports a leave-one-province-out check.
- `slpf_table: slpf_temp_v1.csv` (`--slpf-table`) sets SLPF per stratum from a
  table (columns `iy, ix, water, slpf`), replacing `slpf` and `slpf_rainfed`.
  `fit_slpf.py` writes it for its temperature model: SLPF = regime value +
  slope x (cell mean season Tmin - 23 C), clipped to the SLPF range of the
  runs. **Current calibration:** a scenario with
  `slpf_table: E:/temp/xdssat/checks/districts/slpf_temp_v1.csv` and
  `cultivar_coefficients: {TCLDF: 19}` (irrigated 0.758, rainfed 0.632, slope
  +0.021 per C). This is a statistical correction of the model's long-season
  yield bonus in cool cells, not a mechanism.

### Flags

Every flag of `run_aoi.py` still works and wins over the file: `--mode`,
`--years`, `--zone-table`, `--onset-years`, `--sample`, `--seed`, `--block`,
`--chunk-size`, `--workers`, `--cultivar` (one cultivar, from the file or the
template), `--n` (one N rate), `--slpf`, `--slpf-rainfed`, `--slpf-table`,
`--tcldf` and `--out` (the exact folder, for an experiment with one
combination). `python scripts/run_aoi.py --help` lists the setting each one
replaces.

### Outputs

Each run folder (in `run > output_folder`, default `<cache>/runs/`) holds
`experiment.yml` (the resolved settings), `strata.csv`,
`units.csv`, `summary.csv` (one row per season, DSSAT summary joined to the
stratum and its `ha`), `failed.csv`, `run_info.json`, and `onset.csv` /
`zone_table.csv` where used.

The run also writes `<aoi>_<mode>_<years>.nc`, a CF netCDF of the seasonal
results on the weather grid ([`outputs.py`](https://github.com/bennyistanto/xdssat/blob/main/src/outputs.py)). To
rebuild it from an existing run folder:

```bash
python scripts/make_netcdf.py E:/temp/xdssat/runs/java_per_cell_1991-2020
```

- Dimensions `(system, time, lat, lon)`. A `system` is one season and water
  regime that was simulated: `wet_irrigated`, `wet_rainfed`, `dry_irrigated`.
- `time` is 1 Aug of each agricultural year; `planting_date`,
  `anthesis_date` and `maturity_date` hold the actual dates.
- Per season: `yield` (HWAH), `biomass`, `lai_max`, `rain`, `irrigation`,
  `evapotranspiration`, `n_uptake`, `season_length`, and the water and N
  balance: `n_fertilizer` (NICM), `n_leached` (NLCM), `n_mineralized` (NMINC),
  `n2o_emitted` (N2OEM), `n_fixed` (NFXM), `runoff` (ROCM), `drainage` (DRCM),
  `soil_evaporation` (ESCM), `transpiration` (EPCM), `water_at_maturity`
  (SWXM). Definitions: the engine's `Data/DATA.CDE`.
- Areas: `crop_area` (ha of the system's water regime in the cell),
  `harvested_area` (the same where a yield was simulated) and `production`
  (t). The area-weighted mean yield of any region is
  `production.sum() / harvested_area.sum() * 1000` in kg/ha. The irrigated
  area appears under both `wet_irrigated` and `dry_irrigated`, so do not sum
  `crop_area` over `system`; `crop_area_total` is the crop area per cell. Rice
  runs also carry them as `rice_area` and `rice_area_total`.
- Values are per stratum (one simulation per cell and regime), not per 100 m
  pixel. Java 1991-2020 is 2.4 MB and takes 6 s.

Daily outputs are kept when `run > daily` lists the engine's daily groups
(e.g. `[plantgro, soilwat]`): `daily_<group>.csv.gz` in the run folder, one
row per season and day, keyed by `cell`, `stratum`, `water`, `season`,
`season_year` and `pdate`.

### Seasonal analysis

How the results vary over the years, per cell and per region, as the DSSAT
seasonal analysis tool shows them for one field (box plots, cumulative
distributions, mean-variance):

```bash
python scripts/seasonal_summary.py <run folder> --below 5000
python scripts/seasonal_summary.py <run A> <run B> --out <folder>     # mean-variance of runs
python scripts/seasonal_summary.py <run> --price 0.25 --fixed-cost 400 --n-cost 0.9
```

It writes `seasonal_cells.csv` (per cell, regime and season: mean, sd, cv,
quantiles, share of years below `--below`), `seasonal_years.csv` (the
area-weighted mean of each year), `seasonal_region.csv` and `seasonal_cdf.csv`
into the run folder, and `mean_variance.csv` for several runs. `--var` picks
another summary column (e.g. `NLCM`, N leached). With `--price` (per kg of
the variable; HWAH is dry matter) and costs it adds the gross margin per ha:
price x yield - fixed costs - N price x NICM - irrigation price x IRCM. The
engine itself has no economics.

**Tested** on the 30-year uniform-calendar Java runs (IR 58, 140 kg N):
area-weighted yield 7,807 kg/ha uncalibrated (sd 193, cv 2.5%) and 4,635
kg/ha calibrated (`slpf_temp_v1`, TCLDF 19; sd 157, cv 3.4%). Per cell the
median cv over the years is 4.0-4.7% uncalibrated and 6.9-7.4% calibrated,
by season and regime.

### Compare with BPS yields

`scripts/compare_bps.py` compares the runs with BPS provincial productivity
(quintal/ha of gabah kering giling, 1993-2015, CSV export of the BPS web
table). It needs the province layers (`prepare_rasters.py --layers province
province_filled`), groups seasons by calendar year of harvest, weights by rice
area and converts dry matter to GKG at 14% moisture (`--moisture`, an
assumption). Results go to `<cache>/checks/bps/`.

```bash
python scripts/compare_bps.py --bps path/to/produktivitas_padi_kuintaperha.csv
```

### Compare with district statistics

District (admin2) harvested area, production and yield come from the Ministry
of Agriculture BDSP portal (source named there: BPS):

```bash
python scripts/fetch_bdsp.py          # 114 paced requests, raw pages cached in <cache>/obs/bdsp/raw
python scripts/clean_bdsp.py          # cleaned CSVs + flags in <cache>/obs
python scripts/prepare_rasters.py --layers adm2 adm2_filled
python scripts/compare_districts.py --run E:/temp/xdssat/runs/java_per_cell_1991-2020
```

`compare_districts.py` reports the level (simulated / observed), the spatial
agreement of district means, the detrended year-to-year correlation per
district and simulated vs reported harvested area. The admin2 boundaries
(`--adm2`) are the latest BPS-coded districts; `clean_bdsp.py` moves old Papua
and Papua Barat codes to the new provinces.

A smaller check on 40 cells: `spikes/java_engine_check.py`.

### Compare and calibrate in any area

The same comparison for any country and region map, from the run's own inputs
and a plain table of observations
([`src/evaluate.py`](https://github.com/bennyistanto/xdssat/blob/main/src/evaluate.py)):

```bash
python scripts/prepare_layers.py --config <file> --kind region       # e.g. districts, as a region input
python scripts/compare_regions.py <run folder> --obs yields.csv --region-input <districts> --units t/ha
python scripts/compare_regions.py <run folder> --obs national.csv     # region "all": a national series
```

The observations are a CSV with columns `region` (a class name of the region
input, or `all`), `year` and `yield`, optionally `area` (harvested ha) and
`group` (e.g. province). `--units` is kg/ha, t/ha or ku/ha; `--moisture` (0.14)
is the moisture of the reported yields, since HWAH is dry matter. Seasons go to
the calendar year of harvest (`--year-basis season`: the agricultural year).
`--config` gives the experiment file of a run folder made before runs kept
theirs. The SLPF fit takes the same tables:

```bash
python scripts/fit_slpf.py --runs <three runs> --config <experiment.yml> \
    --region-input <districts> --obs-table yields.csv --obs-units t/ha
```

**Tested**: the Java districts as a region input (`idn_adm2_filled.tif`, the BPS
codes as class names) and the BDSP yields as a table give, for
`java_per_cell_1991-2020_slpf_temp_v1_tcldf19`, the same 94 districts, crop area,
simulated means, ratios and detrended correlations as `compare_districts.py`
(differences below 1e-13); `fit_slpf.py` through the table gives byte-identical
`slpf_table.csv`, `fit_slpf_grid.csv` and `fit_slpf.json`.

### Calibrate cultivar coefficients (GLUE)

Cultivar coefficients against field experiments, with the method of DSSAT's GLUE
tool: round 1 draws the phenology coefficients (P in the cultivar file's
`!Calibration` row) between its `MINIMA` and `MAXIMA`, round 2 the growth
coefficients (G); each draw runs on the engine and is weighted by the normal
likelihood of the FileA measurements
([`src/glue.py`](https://github.com/bennyistanto/xdssat/blob/main/src/glue.py)):

```bash
python scripts/glue_calibrate.py --crop MZCER048 --cultivar IB0035 \
    --experiment ../dssat-csm-data/Maize/UFGA8201.MZX:4 --experiment ../dssat-csm-data/Maize/UFGA8201.MZX:3 --n 1000
```

It prints the calibrated values as a `cultivar_coefficients` line for a
scenario. **Tested** with 300 sets per round on the two irrigated treatments of
UFGA8201 (42 s): P1 255 (posterior mean 251, sd 11; the cultivar file has 259),
P2 0.91, P5 970 (947), G2 990 (924), G3 12.0 (8.2). G2 is at the top of its
range (990), so the measurements would like more than the range allows.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python.exe` of a conda env exits with code 127, no message | env not activated; use `conda run -n <env>` or activate first |
| `Warning: Cannot find ... GDAL_DATA` | same cause |
| Engine stops at once, `ERROR.OUT` mentions a path or file not found | engine or work folder too long, inside OneDrive, or `DSSATPRO.V48` pointing elsewhere. Rebuild with `build_engine.py` or fix the profile |
| `DSSATPRO.V48` edited by hand does not work | lines must read `KEY C: \path` with a space after the drive letter |
| `run_aoi.py` or `check_setup.py` prints `PROBLEM` with a line number | the experiment file has a mistake on that line; the message names the setting and shows a valid example. Compare the line with the same line of `configs/rice-java.yml` |
| `The folder 'paths > ...' is not set` | that folder has no value on this computer: fill the line it names in `configs/xdssat.yml`, or set the environment variable it names |
| A run is missing some treatments | the engine stopped on a bad input (`STOP 99`). The runner reruns the others and lists the culprit with its error in `failed` |
| Reading SoilGrids reports missing tiles | run `python scripts/fetch_soilgrids.py --aoi <aoi>`; `build_soilgrids_cache.py --aoi <aoi>` makes reading faster |
| Git: `.git` folder grows large in OneDrive | OneDrive locks interrupt git's repacking. Run `git config gc.auto 0` in the repository and pause OneDrive before a manual `git gc` |

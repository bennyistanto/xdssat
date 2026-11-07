# Feasibility review: gridded DSSAT in Jupyter with netCDF in and out

> Date: 2026-11-05 | Reviewer: Claude (Opus 5.5) with Benny Istanto
> Scope: the 8 local DSSAT forks, the existing Python interfaces, and a working build and run of the engine on this machine.

## 1. Goal and short answer

**Goal.** Run DSSAT from a Jupyter notebook. Inputs are gridded netCDF (weather and, ideally, soil and management). Simulations run per grid cell. Outputs are written as netCDF. Parameters must be easy to change, and results should feed charts, maps and tables.

**Short answer: feasible, with moderate difficulty. No blockers were found.**

The engine builds and runs on this Windows machine:

- It compiled from source in about 67 s.
- A 500-cell grid ran correctly inside one process.
- Parallel processes scale almost linearly.

The hard parts sit outside the Fortran:

1. writing fixed-width DSSAT input files correctly at scale;
2. getting a defensible soil profile for every grid cell;
3. isolating failures, so that one bad cell does not end a whole batch;
4. validating results against the official DSSAT.

None of these needs changes to the engine.

**Recommended architecture:** new Python code (a living folder, not an installable package; see section 12) that drives the stock `dscsm048` executable as a subprocess. It translates netCDF/xarray into DSSAT text files per chunk of cells, runs chunks in parallel in isolated directories, and assembles `summary.csv` and daily outputs back into CF-compliant netCDF. Section 7 compares this with the two alternatives.

## 2. How this was verified

Every claim below is either a file:line in the local forks, a command that was actually run, or a cited source. Anything that was not checked is marked **(unverified)**.

| Item | Value |
|---|---|
| Machine | Windows 11 Pro for Workstations, 2 x Intel Xeon Gold 6138: 40 cores, 80 logical processors (Windows `Win32_Processor`; `nproc` in Git Bash reports 64) |
| Compiler | GNU Fortran 13.2.0, MinGW-w64 UCRT (via Strawberry Perl, on PATH) |
| Build tool | CMake 3.29.2, `mingw32-make` |
| Python | 3.12.10 (miniforge): numpy 2.3.5, pandas 2.3.3, xarray 2025.12.0, netCDF4 1.7.4, scipy 1.17.0, matplotlib 3.10.8, cartopy 0.25.0, geopandas 1.1.2, rasterio 1.5.0 |
| Missing Python | dask, zarr, rioxarray, h5netcdf, jupyterlab, ipywidgets, hvplot |
| Engine source | `dssat-csm-os` @ `25c93b9a` (v4.8.6.4, 2026-10-02), branch `develop` |
| Reproduce | [spikes/README.md](../spikes/README.md) and [spikes/chunk_filex.py](../spikes/chunk_filex.py) |

## 3. Repository-by-repository review

### 3.1 dssat-csm-os: the engine (core dependency)

- **Size.** Fortran 77/90: 538 `.for`/`.f90` files, about 313k lines (`find ... | wc -l`). It covers 45+ crops through a handful of model families: CERES (`MZCER048`), CROPGRO (`CRGRO048`), CSCER, SUBSTOR, CASUPRO and others, mapped per crop in `Data/DSSATPRO.v48`.
- **Build output.** One CMake target, `ADD_EXECUTABLE(dscsm048 ...)` ([CMakeLists.txt:658](../../dssat-csm-os/CMakeLists.txt)). There is no library target, no C API and no Python binding.
- **Runtime model.** A single `PROGRAM CSM` driven by command-line run modes ([CSM_Main/CSM.for:150-210](../../dssat-csm-os/CSM_Main/CSM.for)):
  - `A`: all treatments in a FileX
  - `B`/`N`/`Q`/`S`/...: batch file
  - `C`: one treatment
  - `D`: debug, which reads a prepared `DSSAT48.INP`
- **netCDF support:** none in `develop`, `master`, `release`, `CI` or `FlexibleIO`, checked with `git grep -il "netcdf\|nf90_"` on each branch.
- **The `FlexibleIO` branch** (upstream, 2026-08-25) adds C++ readers for daily/hourly weather in CSV and TXT (`FlexibleIO/Input/IPWTHDCSV.cpp` etc.). It is a dissertation branch and not merged. It shows upstream moving towards more flexible weather input, but not towards netCDF.
- **Verdict:** use it as is, unmodified, as an external executable.

### 3.2 dssat-csm-data: example inputs (test fixtures)

- **Contents.**
  - Experiments per crop (`Maize/*.MZX`, `.MZA` observed summary, `.MZT` observed time series).
  - 1,052 weather files in `Weather/`.
  - Soil databases in `Soil/*.SOL`.
  - Seasonal (`*.SNX`) and spatial (`Spatial/*.GSX`) demos.
- **No netCDF files.** `find -iname "*.nc*"` returned nothing.
- **`Spatial/DTCM7001.GSX`.** DSSAT's own "spatial" mode is just a FileX where each treatment points at a different field (23 fields). That is the same pattern used by the chunk prototype in section 5.
- **Verdict:** the source of regression fixtures. Use `.MZX` plus `.MZA`/`.MZT` pairs to validate the Python writers and readers against the engine.

### 3.3 pythia: the existing gridded runner (design reference)

- **What it is.** Pythia v2.3.2, Python, BSD-3, actively maintained (last commit 2026-08-19), about 3,950 lines.
- **Pipeline.**
  1. Read a JSON config.
  2. Sample points from GeoTIFF or shapefile.
  3. Render a FileX per pixel from a Jinja2 template ([pythia/template.py](../../pythia/pythia/template.py)), with a hand-kept table of fixed-width formats per field.
  4. Symlink WTH and SOL files into a per-pixel directory ([pythia/peerless.py:51](../../pythia/pythia/peerless.py)).
  5. Run `cd dir && dscsm048 A file` through `subprocess` with `shell=True` in a `multiprocessing.Pool` ([pythia/dssat.py:12-25](../../pythia/pythia/dssat.py)).
  6. Merge every `summary.csv` into one CSV ([pythia/analytics.py:176](../../pythia/pythia/analytics.py)).
- **No netCDF or xarray anywhere** (`grep -rni "netcdf\|xarray"` returned nothing).
- **Weather must already exist as WTH files.** It is matched to the nearest point in a vector layer ([pythia/functions.py:133](../../pythia/pythia/functions.py)).
- **Soil comes from a raster of profile IDs** plus a `GHR.db` lookup and `.SOL` files (README, section "Soil raster compatibility").
- **Windows needs Developer Mode** for symlinks (README).
- **Verdict.** It confirms that the subprocess-per-cell architecture works at scale. Do not build on it:
  - its unit is one directory per pixel, which means many small processes;
  - it is CLI and JSON driven, not notebook-first;
  - it outputs CSV, not netCDF.
- **Borrow ideas only:**
  - the soil-ID raster approach;
  - its format table as a cross-check for our writers.

### 3.4 glue: GLUE calibration (later phase, optional)

- **What it is.** R scripts for Bayesian (GLUE) estimation of cultivar coefficients, parallelised with R `parallel`.
- **How it runs the engine.** Through `system('DSCSM048.EXE <model> B DSSBatch.v48 DSCSM048.CTR')` ([glue/ModelRun.r:145](../../glue/ModelRun.r)).
- **Requirements.** A `C:/DSSAT48` style layout, set in [glue/SimulationControl.csv](../../glue/SimulationControl.csv), and a "C file" that only the GLUESelect GUI creates (README).
- **Verdict.** Out of scope for the first version. It is evidence that calibration is also just "rewrite `.CUL`/`.ECO`, rerun the executable". That can be done in Python later, for example with scipy or a GLUE port.

### 3.5 TSE: Time-Series cultivar Estimator (not reusable)

- **The repository contains only `TSE.zip`**: a 53.6 MB Windows `TSE.exe` plus icons, a user-guide PDF, and a README. There is no source code (`unzip -l TSE/TSE.zip`).
- **Verdict:** nothing to reuse. Mention it to users as an external calibration tool.

### 3.6 XB2: XBuild 2 (format reference)

- **What it is.** The Java Swing FileX editor: 263 `.java` files, about 37k lines. It has one model class per FileX section (`src/FileXModel/*.java`) and one writer service per section (`src/FileXService/*.java`).
- **Windows-only paths.** For example, `rootPath + "\\DETAIL.CDE"` in [DSSATRepository/CropRepository.java:26](../../XB2/src/DSSATRepository/CropRepository.java).
- **Verdict.** The best second reference for FileX column widths and section order, after the Fortran readers themselves. Do not run or port the GUI.

### 3.7 NASAP_CHIRPS_WTH: weather builder (partly relevant)

- **What it is.** Python, last commit 2022-11-04. It:
  1. downloads NASA POWER daily point data through `power.larc.nasa.gov/api/temporal/daily/point` ([getnasap.py:38](../../NASAP_CHIRPS_WTH/getnasap.py));
  2. downloads CHIRPS 0.05° daily **netCDF** by month from `data.chc.ucsb.edu` ([CHIRPS.py:36](../../NASAP_CHIRPS_WTH/CHIRPS.py));
  3. replaces POWER rainfall with CHIRPS;
  4. gap-fills SRAD;
  5. writes one `<ID>.WTH` per point with 7-digit `YYYYDDD` dates ([getnasap.py:140-233](../../NASAP_CHIRPS_WTH/getnasap.py)).
- **Issues.**
  - It uses GDAL (`osgeo`) per band.
  - `__main__` imports `chirps` while the file is `CHIRPS.py`, which breaks on case-sensitive file systems.
  - The README states it is tested on Linux only, and "/" must be swapped for Windows.
- **Verdict.** Useful as a recipe: which variables, the SRAD QC rules, and the WTH header layout. Reimplement it with xarray. A whole CHIRPS grid can be converted to WTH files in a vectorised pass instead of point by point.

### 3.8 WGEN_sa: stand-alone weather generator (low relevance)

- **What it is.** 11 Fortran files with no build system. `OPSYS = 'WINDO'` is hard-coded in `Source/ModuleDefs_sa.for`. Sample `.CLI`/`.WTG` data. Last commit 2024-05-07.
- **Relevance.** The engine already contains WGEN and SIMMETEO (`Weather/WGEN.for`), selected through the FileX weather switch (`MEWTH`).
- **Verdict:** not needed. Use it only if stochastic weather generation outside the engine becomes a requirement.

## 4. Engine facts that shape the design

| # | Fact | Evidence | Consequence |
|---|---|---|---|
| F1 | Output files have fixed names (`Summary.OUT`, `PlantGro.OUT`, `summary.csv`...) and are written to the current directory. Previous outputs are deleted at start. | `CALL OPCLEAR` [CSM.for:132](../../dssat-csm-os/CSM_Main/CSM.for); `SUBROUTINE OPCLEAR` [UTILS.for:973](../../dssat-csm-os/Utilities/UTILS.for); observed in run1 | One working directory per concurrent process. Never two runs in one directory. |
| F2 | The temporary input file is always `DSSAT48.INP`. | [CSM.for:135](../../dssat-csm-os/CSM_Main/CSM.for) | Same as F1. |
| F3 | `DSSATPRO.V48` is looked up in this order: the current directory, the executable's directory, `%DSSAT_HOME%`, then `C:\DSSAT48\`. | [InputModule/PATH.for:144-166](../../dssat-csm-os/InputModule/PATH.for) | Ship our own `DSSATPRO.V48` next to the bundled executable. No system install is needed. |
| F4 | Paths are fixed-length strings: `PATHEX*80`, `DSSATP*102`, weather `PATHWTW*80`, `FILEWW*92`. | [CSM.for:96-97](../../dssat-csm-os/CSM_Main/CSM.for); [Weather/IPWTH_alt.for:60-61](../../dssat-csm-os/Weather/IPWTH_alt.for) | Run directories and the install must sit on **short paths**, never under OneDrive. This scratchpad path (118 characters) is already too long. |
| F5 | The weather file name comes from `WSTA` in the FileX: `WSTA(4)+YY+"01.WTH"`, or `WSTA(4)+WSTA1(4)+".WTH"` (8 characters), or `WSTA.WTH`. | [InputModule/IPEXP.for:698-706,868-869](../../dssat-csm-os/InputModule/IPEXP.for) | Each cell needs an 8-character station code, for example `GR000123`. Multi-year single files work. |
| F6 | One FileX holds up to 999 treatments and 999 levels per factor: `FORMAT (I3,I1,2(1X,I1),1X,A25,14I3)`. | [IPEXP.for:865](../../dssat-csm-os/InputModule/IPEXP.for) | Many cells per process are possible. See the measurements in section 5. |
| F7 | `*FIELDS` layout is strict. All `@L ID_FIELD` rows must come first, then all coordinate rows. The weather station is read as `(I3,9X,A8)` from every row under the first header. | [InputModule/MAKEFILEW.f90:93](../../dssat-csm-os/InputModule/MAKEFILEW.f90); reproduced: interleaved layout gave "Weather station input is missing" | Writers must follow the reader's exact layout. Fixture tests are mandatory. |
| F8 | A fatal input error ends the process with `STOP 99`. The cells after it in the same FileX are never run. (The "press ENTER" prompt does not block; its `READ` is commented out.) | [Utilities/ERROR.for:186-187,226](../../dssat-csm-os/Utilities/ERROR.for); reproduced in the chunk test | The runner must detect partial results and retry the remaining cells, splitting the failing chunk until it isolates the bad cell. |
| F9 | CSV output exists. `FMOPT = C` in the control file, and `DSCSM048.CTR` option 8 gives "CSV, only Summary". | [Data/DSCSM048.CTR](../../dssat-csm-os/Data/DSCSM048.CTR); 40 `CsvOut*` writers in [Utilities/CsvOuts/csvoutput.f90](../../dssat-csm-os/Utilities/CsvOuts/csvoutput.f90) | Summary parsing is trivial (`pd.read_csv`). Daily CSV exists only for modules that have a `CsvOut*` writer. Others need fixed-width `.OUT` parsing. |
| F10 | Soil profiles are limited to 20 layers. | `NL = 20` [Utilities/ModuleDefs.for:49](../../dssat-csm-os/Utilities/ModuleDefs.for) | The soil writer must aggregate deeper or finer source profiles. |
| F11 | Embedding the engine in-process is impractical: 86 `STOP` statements, 585 `SAVE` statements (state persists between calls), 864 `OPEN` statements on fixed unit numbers and names. | `grep` counts over the source | A `STOP` would kill the Jupyter kernel, and state would leak between runs. Use a subprocess. |
| F12 | On Windows, `cmake --install` fails: `install(FILES Utilities/run_dssat ...)` is unconditional, but the file is only generated when not on WIN32. | [CMakeLists.txt:670](../../dssat-csm-os/CMakeLists.txt) vs `IF(WIN32)` block; reproduced | Our build script copies `bin/` and `Data/` itself. A one-line upstream PR would be possible. |

## 5. Measurements

All runs use the locally built `dscsm048.exe` (v4.8.6.4, RELEASE). The commands are in [spikes/README.md](../spikes/README.md).

| Test | Result |
|---|---|
| Build from source (`mingw32-make -j8`) | 67 s, exit 0, 196 compiler warnings, 14.8 MB exe |
| `UFGA8201.MZX`, 6 maize treatments, full output | 1.6 s wall-clock, 27 output files. Yields respond to N and irrigation as expected (HWAM 2,293 to 11,854 kg/ha). |
| Same, CSV Summary only (CTR option 8), 20 repeats | 19.1 s, so about 0.95 s per process and about 0.16 s per season |
| One FileX with N cells (one weather file per cell), single process | N=50: 9.0 s; N=200: 41.1 s; N=500: 122.1 s (0.18, 0.21, 0.24 s per cell; mildly super-linear) |
| 8 parallel processes × 50 cells, separate directories | 7.5 s wall-clock for 400 cells, against 9.0 s for 50 cells alone: near-linear scaling |

**Throughput estimate (extrapolated, not measured at this scale).** At about 0.18 s per cell-season and near-linear scaling, 10,000 cells × 30 seasons is about 54,000 core-seconds. That is roughly 28 minutes on 32 processes.

**Open questions:**

- **Per-season cost.** About 0.16 s per season seems high for this model. Antivirus scanning of the many small writes is a suspect **(unverified)**. Worth profiling on Linux or WSL and with a Defender exclusion.
- **Multi-year runs.** Running several seasons per treatment (`NYERS > 1`) should amortise input parsing. Not yet measured.

## 6. Existing Python interfaces

| Project | Status (GitHub API, 2026-11-05) | Approach | netCDF | Reuse verdict |
|---|---|---|---|---|
| [XiaogangHe/pyDSSAT](https://github.com/XiaogangHe/pyDSSAT) | last push 2015-01-19; DSSAT 4.5 era (`DSCSM045.CTR`, `DSSATPRO.L45`); license NOASSERTION | Bundled data plus scripts | No | Dead. The license is unclear, so do not copy from it. |
| [AgroClimaticTools/pyDSSAT](https://github.com/AgroClimaticTools/pyDSSAT) | last push 2025-09-29; MIT; 3 stars | Utilities: read outputs, edit SOL, make batch files, change maize and soybean cultivars (6 modules) | No | Narrow. At most an idea source. |
| [daquinterop/Py_DSSATTools](https://github.com/daquinterop/Py_DSSATTools) | last push 2026-09-09; v3.0.2; **GPL-3.0**; 94 stars | Object model per FileX section, `WeatherStation`, `SoilProfile`; bundles `dscsm048`/`.exe`; one treatment per run | No | The most complete point-based option. The README says "only been tested for a Linux environment". Because of the GPL, copying its code would force GPL on this MPL-2.0 repo, so treat it as a behavioural reference only. |
| [DSSAT/pythia](https://github.com/DSSAT/pythia) | active, BSD-3 | Section 3.3 | No | Architecture reference |
| [palderman/dssat-csm-os `gridded`](https://github.com/palderman/dssat-csm-os/tree/gridded) | last push 2026-08-31; 50 commits ahead of and 328 behind `DSSAT:develop`; based on v4.8.5.0 | **Native netCDF readers inside the engine** (weather, soil, every FileX section: `FlexibleIO/NetCDF/read_nc_*.for`), netCDF output (`dssat_output.f90`), MPI driver (`FlexibleIO/MPI/run_mpi_dssat.f90`) | **Yes** | See Option B. |

The `gridded` branch is documented in Alderman (2021), ["Parallel gridded simulation framework for DSSAT-CSM (version 4.7.5.21) using MPI and NetCDF"](https://gmd.copernicus.org/articles/14/6541/2021/), *Geosci. Model Dev.* 14, 6541-6569. The paper reports:

- a 3.7 to 4 times shorter execution time from netCDF I/O compared with text I/O;
- 12.2 to 13.4 times speed-up from 1 to 16 cores.

Code is archived at [doi:10.5281/zenodo.4893438](https://zenodo.org/record/4893438). Its CMake is Linux-only: hard-coded `/usr/lib/x86_64-linux-gnu` and `/carl-data/...`, `find_package(MPI REQUIRED)`, and links `netcdff` (lines 689-701 of its `CMakeLists.txt`).

**Agreement with your call:** none of these covers "notebook, netCDF in and out, gridded". Building new is justified. The new package should not copy GPL code.

## 7. Architecture options

### Option A: Python orchestrator around the stock executable (recommended)

```
netCDF / xarray inputs          per-chunk work dir (short path, e.g. C:\dsn\run\c0001)
  weather (time,lat,lon)  --->   GR000001.WTH ... GRnnnnnn.WTH
  soil ids or properties  --->   XX.SOL (one profile per cell or per class)
  mgmt params / rasters   --->   CHUNK.MZX (cells = treatments, max 999)
                                 DSSATPRO.V48, DSCSM048.CTR (CSV)
                                        |
                       subprocess pool: dscsm048 A CHUNK.MZX DSCSM048.CTR
                                        |
  xarray.Dataset  <---  summary.csv (+ PlantGro/SoilWat daily)  <---  per chunk
  to_netcdf (CF, cell->lat/lon, season dim, compression)
```

- **Pros:**
  - The engine stays untouched, so official versions can be followed.
  - It works on Windows, Linux and WSL.
  - Failures stay inside each process.
  - All parts were proven on this machine (section 5).
- **Cons:**
  - Text I/O overhead: Alderman measured 3.7 to 4 times compared with native netCDF.
  - Writer fidelity is our responsibility.

### Option B: adopt or rebase Alderman's `gridded` engine

- **Pros:** genuine netCDF in and out inside the engine, plus MPI.
- **Cons:**
  - Linux-only build.
  - Needs netcdf-fortran and MPI. Neither is available in this MinGW toolchain **(unverified for MSYS2)**.
  - 328 commits behind upstream.
  - One maintainer.
  - Rebasing means owning a Fortran fork.
- **Recommendation:** revisit as an optional fast backend after Option A works. Its netCDF input schema is worth reading when we design ours, so that the two stay compatible **(not yet read in detail)**.

### Option C: compile the engine as a library (f2py or ctypes)

Rejected on F11: `STOP`, `SAVE` state and fixed file units. A single bad input would kill the kernel.

## 8. Difficulty by component (Option A)

The effort figures are my estimates for one developer with Claude, not measurements.

| # | Component | Difficulty | Why | Estimate |
|---|---|---|---|---|
| 1 | Engine provisioning (build script, bundled exe, generated `DSSATPRO.V48`, short-path work root) | Low | Proven in this review | 1-2 days |
| 2 | WTH writer from xarray (unit conversion K to °C, J/m² to MJ/m², TAV/AMP, gaps, 7-digit dates) | Low-Medium | Simple format. QC and gap-filling need care. | 2-4 days |
| 3 | SOL writer and reader (max 20 layers, codes from `SOIL.CDE`) | Medium | Format is fine. Data sourcing is the hard part (row 9). | 3-5 days |
| 4 | FileX model and writer (sections, column widths, the reader quirks in F7) | **Medium-High** | 15+ sections, fixed-width, quirks only visible in Fortran readers. Start with a subset: treatments, cultivars, fields, initial conditions, planting, irrigation, fertilizer, harvest, simulation controls. | 1.5-3 weeks |
| 5 | Chunked parallel runner (isolation, retry by halving a failing chunk, progress in the notebook, resumable) | Medium | Logic is clear (F1, F8). Robustness work. | 1 week |
| 6 | Output readers (`summary.csv`, fixed-width `.OUT` daily files, multi-run blocks) | Medium | CSV is easy. `.OUT` parsing needs header-driven column specs. | 3-5 days |
| 7 | netCDF assembly (cell to lat/lon, season and time dimensions, CF attributes, compression; dask for large grids) | Medium | Standard xarray work. Daily outputs can be large. | 3-5 days |
| 8 | Notebook API and visualisation (parameter overrides, sweeps, maps, charts, tables) | Low-Medium | The libraries are installed. Needs API design. | 1 week |
| 9 | Gridded soil data (choose a source, map cells to profiles) | **Medium-High (data, not code)** | Options: Han, Ines & Koo (2019) [10-km global DSSAT-format soils](https://doi.org/10.1016/j.envsoft.2019.05.012), dataset [doi:10.7910/DVN/1PEEY0](https://doi.org/10.7910/DVN/1PEEY0); pythia's GHR/HC27 raster; or SoilGrids plus pedotransfer functions. Not evaluated yet. | 1 week (evaluation) |
| 10 | Validation (writers round-trip `dssat-csm-data` fixtures; results match the reference engine) | Medium, essential | Without this, results cannot be trusted. | Ongoing, about 1 week up front |

**Overall:**

- A credible first version (one crop family, maize CERES, weather from netCDF, soil from one source, summary to netCDF, maps in a notebook): roughly 4 to 6 weeks.
- Broad crop and section coverage: further months.

These are estimates.

## 9. Risks

1. **Silent format errors.** A misplaced column can be read as a different value without any error. Mitigate with round-trip tests against the Fortran readers, using the engine's own echo in `OVERVIEW.OUT` and `DSSAT48.INP`.
2. **Soil data quality** will dominate result quality more than any code choice.
3. **Windows specifics:**
   - path-length buffers (F4);
   - OneDrive locking of run directories;
   - possible antivirus overhead (section 5).
   Keep the work root at `C:\dsn` or similar, outside OneDrive.
4. **Upstream drift.** Pin an engine version per release of this package, and record it in the netCDF global attributes.
5. **License.** This repo is MPL-2.0. DSSAT is BSD-3 and pythia is BSD-3, so they are compatible. DSSATTools is GPL-3.0: do not copy code from it.

## 10. Naming

Decided: the repository is renamed from `dssat-notebook` to **`xdssat`**. On 2026-11-05 PyPI returned 404 (free) for `xdssat`, so the name stays available if packaging is ever wanted. It follows the xarray naming convention (xclim, xesmf). `pydssat` was avoided because two GitHub projects already use it.

## 11. Proposed next decision points

1. Confirm Option A, with Option B as a later backend.
2. Pick the first vertical slice: crop, region, weather source (for example CHIRPS plus ERA5 or POWER), and soil source.
3. Confirm the package name.
4. Decide whether to vendor a pinned engine build in the repo or build it on first use.

## 12. Decisions taken after the review (2026-11-05)

1. **Option A, written from scratch.** No code from Alderman's `gridded` fork or from DSSATTools. Neither is used as a dependency. The only external DSSAT pieces are the official engine (`dssat-csm-os`, run as an executable) and its example data (`dssat-csm-data`, used as test fixtures). Reimplementing the 313k-line Fortran engine is not on the table.
2. **A living folder, not an installable package**, following the same pattern as `hybrid-bias-correction` and `precip-index`. Copy the folder anywhere and run the notebooks. Nothing is installed except the dependencies listed in `requirements.txt` / `environment.yml`, with mandatory and optional dependencies marked.
   - `src/`: Python modules for the analysis (engine runner, WTH/SOL/FileX writers, output readers, gridding, CF-compliant netCDF I/O, plotting).
   - `notebooks/`: numbered notebooks that run the analysis.
   - `docs/`: documentation, including this review.
3. **Repository name: `xdssat`.**

The one non-Python dependency is the `dscsm048` executable. A copyable folder still has to obey the engine's short-path limit (F4). The run directories therefore go to a configurable short work root, not inside the project folder.

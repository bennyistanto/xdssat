# xdssat

Gridded crop simulation with the DSSAT-CSM engine, driven from Python and xarray.

xdssat is a bridge between DSSAT and Python. It does not reimplement DSSAT: it writes the engine's own input
files for every grid cell and season, runs the unmodified `dscsm048` engine in parallel, and collects the
results into one CF netCDF file. Weather comes from AgERA5 (0.1°), soils from SoilGrids 2.0.

**Documentation: [bennyistanto.github.io/xdssat](https://bennyistanto.github.io/xdssat/)**, with the getting-started
guide, a case study (rice in Java, Indonesia) and a six-module DSSAT training with notebooks.

![Mean simulated yield of irrigated wet-season rice per 0.1° cell of Java, 1991-2020](docs/assets/java/index/xdssat-java-mean-yield-wet_irrigated.png)

*Case study: mean simulated yield of irrigated wet-season rice per 0.1° cell of Java, 1991-2020.*

![Simulated and observed rice yield for Java by harvest year, 1992-2020](docs/assets/xdssat/home/xdssat-home-java-yield.png)

*Simulated against observed (BDSP/BPS) rice yield for the whole of Java.*

## What is in this repository

| Folder | Contents |
|---|---|
| `src/` | The Python modules: weather, soil and experiment files, the parallel runner, netCDF output, gridded inputs |
| `scripts/` | Setup checks, building the engine, preparing inputs, running an area (`run_aoi.py`) |
| `configs/` | Experiment files: `rice-java.yml` (the reference, every setting explained), `rice-java-all-options.yml` (every option in use), and your own `xdssat.yml` (not committed). See [configs/README.md](configs/README.md) |
| `templates/` | Template experiment: `XDRI8601.RIX`, IRRI rice with irrigated and rainfed water levels |
| `tests/` | `pytest` suite; tests marked `engine` run the real engine |
| `notebooks/` | Engine check notebook |
| `docs/` | The documentation site (Quarto), including the training notebooks |
| `spikes/` | Small experiments behind numbers quoted in the docs |

## Quick start

Every setting of a run (machine paths, area, cultivars, N rates, water regimes, planting
calendar, scenarios such as CO2 or warming, years) lives in **one file**. Nobody has to edit
Python.

```bash
cp configs/rice-java.yml configs/xdssat.yml   # then fill in its paths section; every setting is explained
python scripts/check_setup.py      # checks the file and the machine, says what to fix and where
python scripts/build_engine.py     # if DSSAT is not installed
python scripts/run_aoi.py          # runs every cultivar x N rate x scenario of configs/xdssat.yml
```

xdssat has no built-in folders: each computer sets its own in `configs/xdssat.yml` (or with
`XDSSAT_*` environment variables). For further studies keep one file per study in `configs/`,
for example `configs/co2-java.yml`, and run it with
`python scripts/run_aoi.py --config configs/co2-java.yml`. Command-line flags
(`--sample 30 --years 2015 2016` for a quick test) win over the file. The tests:
`python -m pytest tests`.

The full setup, from an empty machine to a run, is in the
[getting-started guide](https://bennyistanto.github.io/xdssat/getting-started.html)
([docs/getting-started.md](docs/getting-started.md)).

## Not an official DSSAT product, and experimental

xdssat is an independent project, not developed or endorsed by the DSSAT Foundation. It is also experimental:
so far it has been tested on one case (rice in Java) and the training examples. Use it at your own risk, and
check its results against your own data before relying on them. If you publish results made
with it, please cite DSSAT as the DSSAT Foundation asks; see
[Acknowledgements and how to cite](https://bennyistanto.github.io/xdssat/acknowledgements.html).

## Licence

MPL-2.0. The DSSAT-CSM engine is BSD-3 (DSSAT Foundation) and is built from its own repository; xdssat does not
include it.

## Contact

Benny Istanto, [benny.istan.to](https://benny.istan.to). Questions and problems:
[issue tracker](https://github.com/bennyistanto/xdssat/issues).

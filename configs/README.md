# Experiment files

Every xdssat run is set up by one YAML file in this folder. It holds everything a
user changes: this computer's folders, the area, cultivars, N rates, water regimes,
planting calendar, scenarios and years. Nobody needs to edit Python.

| File | What it is |
|---|---|
| [`rice-java.yml`](rice-java.yml) | **The reference.** The rice-in-Java case study, with every setting explained: what it means, its range, what happens when it is too low or too high, and its default. The built-in defaults are exactly this file. |
| [`rice-java-all-options.yml`](rice-java-all-options.yml) | The same case with every setting written out and every kind of option in use (cultivar not in the template, per_cell calendar, zone table, all eight weather variables, all four kinds of change, cultivar coefficients, SLPF, and an example of every `prepare` section). A menu to copy from. |
| [`maize-example.yml`](maize-example.yml) | A maize run (CERES-Maize, `templates/XDMZ8201.MZX`) in East Java, the starting point for another crop. |
| [`cases/`](cases/) | One file per case study of the documentation (Niger millet, Kenya maize, Mekong rice, Luzon rice, Java maize as a rice-then-maize sequence), each with the commands that prepared its inputs and ran it in its header. |
| `xdssat.yml` | **Your own file**, read when no `--config` is given. Not committed (gitignored), so it can hold this computer's folders. Make it from `rice-java.yml`. |
| `<study>.yml` | Any study you want to keep and share, e.g. `co2-java.yml`. Run it with `--config configs/<study>.yml`. |

## Start

```bash
cp configs/rice-java.yml configs/xdssat.yml     # then fill in the paths section
python scripts/check_setup.py                   # checks the file and this computer
python scripts/run_aoi.py --check               # lists the run folders, runs nothing
python scripts/run_aoi.py --sample 30 --years 2015 2016   # quick test, about 35 s
python scripts/run_aoi.py                       # the full run
```

## Folders on several computers

The committed files leave `paths` empty (`null`): xdssat has no built-in folders.
Each computer fills them in its own `xdssat.yml`, or sets the environment variables
`XDSSAT_ENGINE`, `XDSSAT_WORK`, `XDSSAT_AGERA5`, `XDSSAT_SOILGRIDS`, `XDSSAT_INPUTS`,
`XDSSAT_CACHE` and `XDSSAT_BOUNDARIES`, which win over any file. A shared study file
then runs on every computer unchanged. A step that needs a folder that is not set stops
and says which line to fill.

## Prepared inputs

A run reads its data (weather, crop mask, soil, water regime, region, zones) only as
prepared datasets in `<cache>/inputs/`, named in the `inputs:` section of the file. See
which ones exist, and whether a file's choice meets the requirements:

```bash
python scripts/register_inputs.py --list
```

Each kind is made from your own files with a `prepare` section of the file and its
script; `--inspect <file>` shows what a file holds:

| Input | Section | Script |
|---|---|---|
| weather | `prepare > weather` | `python scripts/prepare_weather.py` |
| crop mask | `prepare > crop_mask` | `python scripts/prepare_crop_mask.py` |
| soil | `prepare > soil` | `python scripts/prepare_soils.py` |
| water regime, region, zones | `prepare > water_regime`, `region`, `zones` | `python scripts/prepare_layers.py` |

[Input data](https://bennyistanto.github.io/xdssat/xdssat/data.html) describes what each
input must be and the global datasets to use when you have none.

## Scope

- Any area for which the inputs are prepared. Water regime, region and zones are
  optional; without a water regime map every crop pixel takes `treatments > default_regime`.
- Templates are included for rice (CERES-Rice, `XDRI8601.RIX`, and `XDRW8601.RIX` with two
  more percolation rates), maize (CERES-Maize, `XDMZ8201.MZX`), pearl millet (CERES-Millet,
  `XDML8601.MLX`) and a rice-then-maize crop sequence (`XDRM8601.SQX`, run in DSSAT's mode Q),
  all written by `scripts/make_templates.py`. Another crop needs its own template FileX and
  crop model (`template` section); the
  [Another crop](https://bennyistanto.github.io/xdssat/xdssat/another-crop.html) page walks
  through one.
- The single-site experiments of the DSSAT training run from the notebooks
  (`docs/notebooks/`), not from these files.

"""Compare a run's simulated yields with observed yields by region and year, for any area.

    python scripts/compare_regions.py <run folder> --obs yields.csv
    python scripts/compare_regions.py <run folder> --obs yields.csv --region-input districts_kenya \
        --units t/ha --moisture 0.14 --min-years 5

The run folder's experiment.yml gives the inputs (crop mask, water regime, weather grid);
--region-input names a region input (a class map, prepared with scripts/prepare_layers.py)
whose class names match the region column of the observations. Without it, the whole run is
one region, "all", e.g. against a national yield series (FAOSTAT).

Observations (--obs): a CSV with columns region, year, yield, and optionally area (harvested
ha) and group (e.g. a province). --units: kg/ha, t/ha or ku/ha. --moisture: the moisture of
the observed yields (simulated HWAH is dry matter and is divided by 1 - moisture; 0.14 by
default, an assumption: check the statistics' own definition). --year-basis harvest (calendar
year of harvest, default) or season (agricultural year).

Writes <run>/compare_<region input or all>_years.csv and _regions.csv and prints: level
(area-weighted sim / obs of region means), spatial agreement of region means, and year-to-year
agreement (detrended and high-pass correlations, median over regions). src/evaluate.py
explains each step.
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import evaluate  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path, help="run folder (summary.csv, experiment.yml)")
    ap.add_argument("--obs", type=Path, required=True, help="CSV: region, year, yield [, area, group]")
    ap.add_argument("--region-input", help="region input whose class names are the region column")
    ap.add_argument("--config", type=Path, help="experiment file of the run, for run folders made before "
                                                 "runs kept their experiment.yml (default: <run>/experiment.yml)")
    ap.add_argument("--units", default="kg/ha", choices=list(evaluate.UNITS))
    ap.add_argument("--moisture", type=float, default=0.14, help="moisture of the observed yields")
    ap.add_argument("--year-basis", default="harvest", choices=["harvest", "season"])
    ap.add_argument("--min-years", type=int, default=5)
    ap.add_argument("--min-ha", type=float, default=0.0, help="regions with less simulated crop are left out")
    a = ap.parse_args(argv)

    f = a.config or a.run / "experiment.yml"
    if not f.is_file():
        raise SystemExit(f"{f} does not exist: give the run's experiment file with --config")
    exp = config.use(config.load(f))
    pieces = evaluate.crop_pieces(exp, a.region_input)
    summary = pd.read_csv(a.run / "summary.csv", low_memory=False)
    sim = evaluate.simulated(summary, pieces, a.moisture, a.year_basis)
    info = json.loads((a.run / "run_info.json").read_text()) if (a.run / "run_info.json").exists() else {}
    if a.year_basis == "harvest" and "years" in info.get("args", {}):
        y0, y1 = json.loads(info["args"]["years"])
        sim = sim[sim.year.between(y0 + 1, y1)]          # years with every season of the run inside
    obs = evaluate.read_observed(a.obs, a.units)
    years, regions, overall = evaluate.compare(sim, obs, pieces, a.min_years, a.min_ha)
    tag = a.region_input or "all"
    years.to_csv(a.run / f"compare_{tag}_years.csv", index=False)
    regions.to_csv(a.run / f"compare_{tag}_regions.csv", index=False)
    print(f"run {a.run.name}; {overall['regions']} regions with >= {a.min_years} observed years "
          f"({pieces.ha.sum():,.0f} ha of crop in {pieces.region.nunique()} regions); observed at moisture "
          f"{a.moisture}, year basis {a.year_basis}")
    if overall["regions"]:
        print(f"level: sim/obs of region means, area-weighted {overall['level']:.2f} "
              f"(p10 {overall['ratio_p10']:.2f}, p90 {overall['ratio_p90']:.2f})")
        print(f"spatial (region means): Pearson r {overall['spatial_r']:.2f}")
        print(f"year-to-year: median detrended r {overall['r_detrended_median']:.2f}, median high-pass r "
              f"{overall['r_highpass_median']:.2f}, share detrended r > 0 {overall['share_r_positive']:.2f}")
    print(f"wrote {a.run / f'compare_{tag}_regions.csv'}")


if __name__ == "__main__":
    main()

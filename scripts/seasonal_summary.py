"""Seasonal analysis of one or more run folders: spread over the years, per cell and per region.

    python scripts/seasonal_summary.py <run folder>
    python scripts/seasonal_summary.py <run folder> --below 3000             # share of poor years
    python scripts/seasonal_summary.py <run> --price 0.25 --fixed-cost 400 --n-cost 0.9 --irrigation-cost 0.05
    python scripts/seasonal_summary.py <run A> <run B> <run C> --out <folder>  # mean-variance of several runs

Writes into each run folder (src/seasonal.py explains every table):
    seasonal_cells.csv    per weather cell, water regime and season: n, mean, sd, cv, min, p05-p95,
                          max (and share_below) of the variable over the years
    seasonal_years.csv    per year: area-weighted mean per system (season_water) and over all ("all")
    seasonal_region.csv   statistics of those yearly means over the years
    seasonal_cdf.csv      cumulative distribution of the yearly means (rank / (n + 1))
    ..._margin.csv        the same for the gross margin per ha, with --price
With several run folders, also <out>/mean_variance.csv: mean and sd over the years per run
and system (the run label is the folder name).

--var picks the summary column (default HWAH, grain yield, kg/ha dry matter; e.g. NLCM for N
leached, IRCM for irrigation). Prices and costs are per ha in one currency; --price is per kg of
the variable (dry matter for HWAH).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import seasonal  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", type=Path, nargs="+", help="run folders (with summary.csv)")
    ap.add_argument("--var", default="HWAH", help="summary column (default HWAH)")
    ap.add_argument("--below", type=float, help="report the share of years below this value")
    ap.add_argument("--price", type=float, help="price per kg of the variable: adds the gross margin")
    ap.add_argument("--fixed-cost", type=float, default=0.0, help="fixed costs per ha")
    ap.add_argument("--n-cost", type=float, default=0.0, help="price per kg N applied (NICM)")
    ap.add_argument("--irrigation-cost", type=float, default=0.0, help="price per mm irrigation (IRCM)")
    ap.add_argument("--out", type=Path, help="folder for mean_variance.csv (several runs; default: the "
                                              "parent of the first run)")
    a = ap.parse_args(argv)
    eco = (seasonal.Economics(a.price, a.fixed_cost, a.n_cost, a.irrigation_cost)
           if a.price is not None else None)
    series = {}
    for run in a.runs:
        tables = seasonal.summarise(run, a.var, eco, a.below)
        for name, df in tables.items():
            df.to_csv(run / f"{name}.csv", index=False)
        series[run.name] = tables["seasonal_years"]
        reg = tables["seasonal_region"].set_index("system")
        print(f"{run.name}: {a.var} over {int(reg.loc['all', 'n'])} years, area-weighted: "
              f"mean {reg.loc['all', 'mean']:.0f}, sd {reg.loc['all', 'sd']:.0f}, "
              f"p10 {reg.loc['all', 'p10']:.0f}, p90 {reg.loc['all', 'p90']:.0f}")
    if len(a.runs) > 1:
        out = a.out or a.runs[0].parent
        out.mkdir(parents=True, exist_ok=True)
        mv = seasonal.mean_variance(series)
        mv.to_csv(out / "mean_variance.csv", index=False)
        print(f"mean-variance of {len(a.runs)} runs -> {out / 'mean_variance.csv'}")
        print(mv[mv.system == "all"][["run", "mean", "sd", "cv", "p10", "p90"]].round(2).to_string(index=False))


if __name__ == "__main__":
    main()

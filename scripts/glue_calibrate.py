"""Calibrate cultivar coefficients with GLUE against field experiments (src/glue.py).

    python scripts/glue_calibrate.py --crop MZCER048 --cultivar IB0035 \
        --experiment ../dssat-csm-data/Maize/UFGA8201.MZX:4 --n 1000
    python scripts/glue_calibrate.py --crop RICER048 --cultivar IB0012 \
        --experiment <own.RIX>:1 --experiment <own2.RIX>:2 --data <folder with Weather/ and Soil/>

Each --experiment is a FileX and a treatment number; its FileA (same name, A last) holds the
measurements. The weather files (<WSTA>*.WTH) and the soil profile of the treatment's field are
looked up in --data/Weather and --data/Soil (default: the dssat-csm-data folder next to xdssat).
Round 1 varies the phenology coefficients (P in the cultivar file's !Calibration row), round 2
the growth ones (G); --n sets per round.

Writes <out>/glue_<cultivar>_round<k>_{sets,sim,prob,posterior}.csv and prints the calibrated
coefficients as a scenario line:  cultivar_coefficients: {"<name>": {P1: ..., ...}}
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import filex  # noqa: E402
import glue  # noqa: E402


def experiment(spec: str, data: Path) -> glue.Experiment:
    path, _, trno = spec.partition(":")
    fx_path = Path(path)
    fx = filex.FileX.read(fx_path)
    t = int(trno or 1)
    wsta, soil_id = glue._field(fx, t)
    weather = sorted((data / "Weather").glob(f"{wsta[:4]}*.WTH"))
    if not weather:
        raise SystemExit(f"{fx_path.name}: no weather files {wsta[:4]}*.WTH in {data / 'Weather'}")
    soil = next((p for p in sorted((data / "Soil").glob("*.SOL"))
                 if f"*{soil_id}" in p.read_text(encoding="latin-1", errors="ignore")), None)
    if soil is None:
        raise SystemExit(f"{fx_path.name}: soil {soil_id} not found in {data / 'Soil'}")
    return glue.Experiment(fx_path, t, weather, soil)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--crop", required=True, help="cultivar file without .CUL, e.g. MZCER048")
    ap.add_argument("--cultivar", required=True, help="INGENO of the cultivar to calibrate")
    ap.add_argument("--experiment", action="append", required=True, help="FileX[:treatment] (repeatable)")
    ap.add_argument("--data", type=Path, default=ROOT.parent / "dssat-csm-data")
    ap.add_argument("--n", type=int, default=1000, help="parameter sets per round")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", type=Path, help="output folder (default <cache>/checks/glue)")
    a = ap.parse_args(argv)
    exps = [experiment(s, a.data) for s in a.experiment]
    out = a.out or config.CACHE_DIR / "checks" / "glue"
    out.mkdir(parents=True, exist_ok=True)
    res = glue.calibrate(exps, a.crop, a.cultivar, a.n, a.seed, a.workers)
    for k in (1, 2):
        r = res.get(f"round{k}")
        if r is None:
            continue
        for name, df in r.items():
            df.to_csv(out / f"glue_{a.cultivar}_round{k}_{name}.csv", index=False)
        print(f"round {k}:\n{r['posterior'].round(4).to_string(index=False)}")
    print("observed:\n" + res["observed"].to_string(index=False))
    co = ", ".join(f"{k}: {v:.4g}" for k, v in res["coefficients"].items())
    print(f'\nscenario line:  cultivar_coefficients: {{"<cultivar name>": {{{co}}}}}')


if __name__ == "__main__":
    main()

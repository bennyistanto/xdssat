"""Run the Java comparisons of the "Alternative input data" page.

    python docs/scripts/run_alternatives.py                  # all variants, configs/xdssat.yml for the folders
    python docs/scripts/run_alternatives.py --only baseline crop_worldcover
    python docs/scripts/run_alternatives.py --list           # the variants and what each changes

Every variant is a full Java run, agricultural years 2011-2020, uniform calendar, IR 58,
140 kg N, SLPF 1 (uncalibrated), that differs from the baseline in one input only. The
experiment file of each is written to <cache>/runs/alternatives/<variant>.yml (the folders
come from --config, by default configs/xdssat.yml) and run with scripts/run_aoi.py; its
results go to <cache>/runs/alternatives/<variant>/. A variant whose summary.csv exists is
skipped unless --overwrite. The inputs must be prepared first, as described on the Input
data page; --list names them.
"""
import argparse
import copy
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

LGRIP_REGIMES = {"irrigated": {"template_level": 1, "irrigation_classes": [2]},
                 "rainfed": {"template_level": 3, "irrigation_classes": [3]}}

# variant -> (group, label, the source changed, overrides of the experiment file)
VARIANTS = {
    "baseline": ("baseline", "National maps", "ATR/BPN rice fields, MoA irrigation, SoilGrids (WISE-scaled carbon), AgERA5 0.1°", {}),
    "crop_worldcover": ("crop mask", "WorldCover cropland", "ESA WorldCover 2021, class 40",
                        {"inputs": {"crop_mask": "cropland_worldcover2021_java",
                                    "soil": "soilgrids_java_cropland_worldcover2021_java"},
                         "treatments": {"default_regime": "rainfed"}}),
    "crop_spam": ("crop mask", "MapSPAM rice", "MapSPAM 2020 v2r2, rice physical area",
                  {"inputs": {"crop_mask": "rice_spam2020_java", "soil": "soilgrids_java_rice_spam2020_java"},
                   "treatments": {"default_regime": "rainfed"}}),
    "crop_modis": ("crop mask", "MODIS cropland", "MODIS MCD12Q1 2020, classes 12 and 14",
                   {"inputs": {"crop_mask": "cropland_mcd12q1_2020_java", "soil": "soilgrids_java_modis"},
                    "treatments": {"default_regime": "rainfed"}}),
    "water_lgrip": ("water regime", "LGRIP30", "GFSAD LGRIP30, classes 2 irrigated and 3 rainfed",
                    {"inputs": {"water_regime": "lgrip30_java"},
                     "treatments": {"water_regimes": LGRIP_REGIMES, "default_regime": "rainfed"}}),
    "water_spam": ("water regime", "MapSPAM irrigated share", "MapSPAM 2020 v2r2, irrigated over all rice area",
                   {"inputs": {"water_regime": "spam2020_rice_irrigated_java"},
                    "treatments": {"default_regime": "rainfed"}}),
    "soil_socnone": ("soil", "SoilGrids, carbon as is", "SoilGrids 2.0 without the WISE carbon factors",
                     {"inputs": {"soil": "soilgrids_java_socnone"}}),
    "soil_ibri": ("soil", "One IRRI profile", "IBRI910002 (the template's soil) for every cell",
                  {"inputs": {"soil": "ibri910002_java"}}),
    "soil_hc27": ("soil", "One HC27 profile", "HC27 HC_GEN0011 (loam, high fertility, 120 cm) for every cell",
                  {"inputs": {"soil": "hc27_loam_java"}}),
    "weather_025": ("weather", "AgERA5 at 0.25°", "AgERA5 regridded to 0.25° (area mean for rain)",
                    {"inputs": {"weather": "agera5_java_025", "soil": "soilgrids_java_agera5_java_025"}}),
}

COMMON = {"years": [2011, 2020], "cultivars": {"IR 58": "IB0012"}, "scenarios": [{"name": "baseline"}],
          "treatments": {"nitrogen_kg": [140]}, "seasons": {"mode": "uniform"}}


def merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "water_regimes":
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def experiment(base: dict, name: str, cache: Path) -> dict:
    d = merge(merge(base, COMMON), VARIANTS[name][3])
    d.setdefault("run", {})
    d["run"].update(output_folder=str(cache / "runs" / "alternatives" / name), sample=None)
    for k in ("prepare",):
        d.pop(k, None)
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=config.DEFAULT_FILE, help="experiment file with this computer's folders")
    ap.add_argument("--only", nargs="+", choices=list(VARIANTS), help="these variants only")
    ap.add_argument("--list", action="store_true", help="list the variants and stop")
    ap.add_argument("--overwrite", action="store_true", help="rerun variants that have results")
    a = ap.parse_args()
    if a.list:
        for k, (g, label, src, over) in VARIANTS.items():
            print(f"{k:16s} {g:13s} {label}: {src}; inputs {over.get('inputs', {})}")
        return
    base = yaml.safe_load(a.config.read_text(encoding="utf-8")) or {}
    cache = config.load(a.config).path("cache")
    folder = cache / "runs" / "alternatives"
    folder.mkdir(parents=True, exist_ok=True)
    for name in a.only or list(VARIANTS):
        out = folder / name
        if not a.overwrite and list(out.glob("*/summary.csv")):
            print(f"{name}: results exist, skipped", flush=True)
            continue
        f = folder / f"{name}.yml"
        f.write_text(f"# {VARIANTS[name][1]}: {VARIANTS[name][2]}\n"
                     + yaml.safe_dump(experiment(base, name, cache), sort_keys=False), encoding="utf-8")
        t0 = time.time()
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_aoi.py"), "--config", str(f)])
        print(f"{name}: exit {r.returncode}, {(time.time() - t0) / 60:.1f} min", flush=True)
        if r.returncode:
            sys.exit(r.returncode)


if __name__ == "__main__":
    main()

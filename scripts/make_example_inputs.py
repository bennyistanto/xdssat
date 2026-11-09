"""Build example-inputs/: small, ready-to-run input sets for five areas, from public datasets.

    python scripts/make_example_inputs.py                       # every area
    python scripts/make_example_inputs.py --area kenya_maize    # one area
    python scripts/make_example_inputs.py --overwrite           # make them again

Each area gets a folder example-inputs/<area>/ with
    experiment.yml            the experiment file: every section written out; its
                              paths > datasets points at this folder, so a run reads the
                              datasets here and writes its run folders to the user's cache
    inputs/<kind>/<name>/     the prepared weather, crop mask, soil (and water regime),
                              each with its input.json (source, licence, how it was made)

The areas (about 1 x 1 degree, agricultural years 2018-2020):
    kenya_maize    western Kenya highlands, rainfed maize (templates/XDMZ8201.MZX)
    mekong_rice    Mekong Delta, Viet Nam, irrigated rice (templates/XDRI8601.RIX)
    niger_millet   around Niamey and Sadore, Niger, rainfed pearl millet (templates/XDML8601.MLX)
    java_rice      north coast of West Java, Indonesia, rice (templates/XDRI8601.RIX)
    java_maize     East Java, Indonesia, maize (templates/XDMZ8201.MZX)

Sources, all CC BY 4.0 (licences checked on the primary records, 2026-11-09):
    weather    AgERA5 (Copernicus Climate Change Service, doi:10.24381/cds.6c68c9bb)
    soil       SoilGrids 2.0 (ISRIC, doi:10.5194/soil-7-217-2021)
    crop mask and irrigated share   MapSPAM 2020 v2r2 (IFPRI, doi:10.7910/DVN/SWPENT)

Needs on this computer, from configs/xdssat.yml or the XDSSAT_* variables: paths > agera5 (the
global AgERA5 archive), soilgrids (the SoilGrids mirror, with the tiles of every area:
python scripts/fetch_soilgrids.py --config example-inputs/<area>/experiment.yml --aoi <area>),
and inputs, whose spam2020/ folder holds the MapSPAM files the areas use, unzipped from
spam2020V2r2_global_physical_area.geotiff.zip: spam2020_V2r2_global_A_<CROP>_A.tif and _I.tif
for MAIZ, RICE and PMIL.

Size budget: under 10 MB per file and 20 MB for example-inputs/ in total (checked at the end).
"""
import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402

OUT = ROOT / "example-inputs"
YEARS = [2018, 2020]
MAX_FILE_MB, MAX_TOTAL_MB = 10, 20

AGERA5 = {
    "TMAX": {"files": "{agera5}/t2mmax/agera5_t2mmax_{year}.nc", "variable": "Temperature_Air_2m_Max_24h"},
    "TMIN": {"files": "{agera5}/t2mmin/agera5_t2mmin_{year}.nc", "variable": "Temperature_Air_2m_Min_24h"},
    "SRAD": {"files": "{agera5}/srad/agera5_srad_{year}.nc", "variable": "Solar_Radiation_Flux"},
    "RAIN": {"files": "{agera5}/precip/agera5_precip_{year}.nc", "variable": "Precipitation_Flux",
             "regrid": "mean"},
    "DEWP": {"files": "{agera5}/t2mdp/agera5_t2mdp_{year}.nc", "variable": "Dew_Point_Temperature_2m_Mean_24h"},
    "WIND": {"files": "{agera5}/ws10m/agera5_ws10m_{year}.nc", "variable": "Wind_Speed_10m_Mean_24h",
             "height_m": 10},
}
SPAM_SOURCE = "SPAM 2020 v2r2 (IFPRI, doi:10.7910/DVN/SWPENT), CC BY 4.0"
SPAM_NAME = {"MAIZ": "maize", "RICE": "rice", "PMIL": "pearl millet"}

MAIZE_LEVELS = {0: 0, 60: 3, 116: 1, 120: 4, 180: 5, 401: 2}
RICE_LEVELS = {0: 0, 35: 1, 70: 2, 105: 3, 140: 4}

# area -> settings. Calendars are placeholders for an example, not agronomic advice.
AREAS = {
    "kenya_maize": dict(
        title="western Kenya highlands, rainfed maize",
        bbox=[34.6, 0.1, 35.6, 1.1], crop="MAIZ", country="Kenya", soc_scale="none",
        weather_years=[2018, 2022], irrigated_share=False,
        template={"file": "templates/XDMZ8201.MZX", "crop_model": "MZCER048"},
        # H610 is a Kenyan hybrid; MZCER048.CUL calls its entry H610(UH)
        cultivars={"H610": "IB0060"},
        treatments={"nitrogen_kg": [60], "nitrogen_levels": MAIZE_LEVELS,
                    "water_regimes": {"irrigated": {"template_level": 4, "irrigation_classes": []},
                                      "rainfed": {"template_level": 0, "irrigation_classes": []}},
                    "default_regime": "rainfed", "initial_soil_water": 0.5},
        seasons={"year_starts": "08-01",
                 "calendar": [{"name": "long_rains", "date": "03-15", "regimes": ["rainfed"]}]},
        # long-season highland maize: allow up to 300 days of weather after sowing
        season_days=300),
    "mekong_rice": dict(
        title="Mekong Delta, Viet Nam, irrigated rice",
        bbox=[105.2, 9.7, 106.2, 10.7], crop="RICE", country="Viet Nam", soc_scale="none",
        weather_years=[2018, 2021], irrigated_share=True,
        template={"file": "templates/XDRI8601.RIX", "crop_model": "RICER048"},
        cultivars={"IR 64": "IB0015"},
        treatments={"nitrogen_kg": [105], "nitrogen_levels": RICE_LEVELS,
                    "water_regimes": {"irrigated": {"template_level": 1, "irrigation_classes": []},
                                      "rainfed": {"template_level": 3, "irrigation_classes": []}},
                    "default_regime": "rainfed", "initial_soil_water": 0.9},
        # dong_xuan: winter-spring crop; he_thu: summer-autumn crop
        seasons={"year_starts": "08-01",
                 "calendar": [{"name": "dong_xuan", "date": "12-01", "regimes": ["irrigated"]},
                              {"name": "he_thu", "date": "04-20", "regimes": ["irrigated", "rainfed"]}]},
        season_days=220),
    "niger_millet": dict(
        title="around Niamey and Sadore, Niger, rainfed pearl millet",
        bbox=[2.0, 13.0, 3.0, 14.0], crop="PMIL", country="Niger", soc_scale="none",
        weather_years=[2018, 2021], irrigated_share=False,
        template={"file": "templates/XDML8601.MLX", "crop_model": "MLCER048"},
        cultivars={"CIVT": "IB0044"},
        treatments={"nitrogen_kg": [0, 45], "nitrogen_levels": {0: 0, 45: 1},
                    "water_regimes": {"rainfed": {"template_level": 0, "irrigation_classes": []}},
                    "default_regime": "rainfed",
                    # a sandy soil at the start of the rains is dry
                    "initial_soil_water": 0.3},
        seasons={"year_starts": "01-01",
                 # 5 Jul, not the template's 17 Jun: 20 Jun sowed ahead of the AgERA5 rains (a 20-cell
                 # sample, 2018-2020, 45 kg N: 206 kg/ha sown 20 Jun, 920 kg/ha sown 5 Jul)
                 "calendar": [{"name": "rainy", "date": "07-05", "regimes": ["rainfed"]}]},
        season_days=220),
    "java_rice": dict(
        title="north coast of West Java, Indonesia, rice",
        bbox=[107.5, -7.0, 108.5, -6.0], crop="RICE", country="Indonesia", soc_scale="wise_indonesia",
        weather_years=[2018, 2021], irrigated_share=True,
        template={"file": "templates/XDRI8601.RIX", "crop_model": "RICER048"},
        # the settings of the Java case study (configs/rice-java.yml), on public data
        cultivars={"IR 58": "IB0012"},
        treatments={"nitrogen_kg": [140], "nitrogen_levels": RICE_LEVELS,
                    "water_regimes": {"irrigated": {"template_level": 1, "irrigation_classes": []},
                                      "rainfed": {"template_level": 3, "irrigation_classes": []}},
                    "default_regime": "rainfed", "initial_soil_water": 0.9},
        seasons={"year_starts": "08-01",
                 "calendar": [{"name": "wet", "date": "11-15", "regimes": ["irrigated", "rainfed"]},
                              {"name": "dry", "date": "04-01", "regimes": ["irrigated"]}]},
        season_days=220),
    "java_maize": dict(
        title="East Java, Indonesia, maize",
        bbox=[111.4, -8.0, 112.4, -7.0], crop="MAIZ", country="Indonesia", soc_scale="wise_indonesia",
        weather_years=[2018, 2021], irrigated_share=True,
        template={"file": "templates/XDMZ8201.MZX", "crop_model": "MZCER048"},
        cultivars={"McCurdy 84aa": "IB0035"},
        treatments={"nitrogen_kg": [120], "nitrogen_levels": MAIZE_LEVELS,
                    "water_regimes": {"irrigated": {"template_level": 4, "irrigation_classes": []},
                                      "rainfed": {"template_level": 0, "irrigation_classes": []}},
                    "default_regime": "rainfed", "initial_soil_water": 0.5},
        seasons={"year_starts": "08-01",
                 "calendar": [{"name": "wet", "date": "11-15", "regimes": ["irrigated", "rainfed"]},
                              {"name": "dry", "date": "03-15", "regimes": ["irrigated", "rainfed"]}]},
        season_days=220),
}


def experiment(area: str, a: dict) -> dict:
    """The complete experiment of an area (every section, nothing left to the rice defaults)."""
    d = copy.deepcopy(config.DEFAULTS)
    d["paths"] = {k: None for k in config.PATH_KEYS}
    d["paths"]["datasets"] = f"example-inputs/{area}"
    crop = SPAM_NAME[a["crop"]].replace(" ", "")
    names = {"weather": f"agera5_{area}", "crop_mask": f"{crop}_spam2020_{area}",
             "soil": f"soilgrids_{area}",
             "water_regime": f"spam2020_{crop}_irrigated_{area}" if a["irrigated_share"] else None,
             "region": None, "zones": None}
    d["aoi"] = {"name": area, "bbox": a["bbox"], "region": None}
    d["inputs"] = names
    spam = "{inputs}/spam2020/spam2020_V2r2_global_A_" + a["crop"]
    d["prepare"] = {
        "weather": {"name": names["weather"], "description": f"AgERA5 for {a['title']}, from the global archive",
                    "source": "AgERA5 (Copernicus Climate Change Service, doi:10.24381/cds.6c68c9bb), CC BY 4.0",
                    "box": None, "grid_step": None, "years": a["weather_years"], "station_code": "AGE5",
                    "temperature_height_m": 2, "variables": AGERA5},
        "crop_mask": {"name": names["crop_mask"], "files": spam + "_A.tif", "values": "ha", "box": None,
                      "source": f"{SPAM_SOURCE}: physical area of {SPAM_NAME[a['crop']]}, all technologies"},
        "water_regime": ({"name": names["water_regime"], "values": "ratio", "irrigated": spam + "_I.tif",
                          "total": spam + "_A.tif", "box": None,
                          "source": f"{SPAM_SOURCE}: {SPAM_NAME[a['crop']]} physical area, irrigated / all"}
                         if a["irrigated_share"] else None),
        "region": None, "zones": None,
        "soil": {"name": names["soil"], "method": "soilgrids", "soc_scale": a["soc_scale"],
                 "country": a["country"]},
    }
    d["template"] = a["template"]
    d["cultivars"] = a["cultivars"]
    d["treatments"].update(a["treatments"])
    if len(d["treatments"]["water_regimes"]) < 2:      # one regime: no share to split
        del d["treatments"]["share_regimes"]
    d["seasons"].update(a["seasons"])
    d["years"] = list(YEARS)
    d["run"].update({"workers": 8, "chunk_size": 200, "season_days": a["season_days"]})
    return d


HEADER = """\
# ==========================================================================================
# example-inputs/{area}/experiment.yml - {title}
# ==========================================================================================
# A ready-to-run example: the prepared inputs are in this folder (paths > datasets), made by
# python scripts/make_example_inputs.py from public data, all CC BY 4.0:
#   weather    AgERA5, Copernicus Climate Change Service, doi:10.24381/cds.6c68c9bb
#   soil       SoilGrids 2.0, ISRIC, doi:10.5194/soil-7-217-2021
#   crop mask{water}  MapSPAM 2020 v2r2, IFPRI, doi:10.7910/DVN/SWPENT
# Every section is written out; configs/rice-java.yml explains each setting, and
# docs/xdssat/examples.qmd describes this area. Uncalibrated (SLPF 1): results show how the
# model responds, not farm yields.
#
# Run it (engine, work and cache from your configs/xdssat.yml are not read here: give them
# as environment variables XDSSAT_ENGINE, XDSSAT_WORK, XDSSAT_CACHE):
#   python scripts/run_aoi.py --config example-inputs/{area}/experiment.yml --check
#   python scripts/run_aoi.py --config example-inputs/{area}/experiment.yml
# The run folders go to <cache>/runs. The prepare section says how the inputs were made; it
# needs the source archives only to make them again.

"""


def write_experiment(area: str, a: dict) -> Path:
    d = experiment(area, a)
    folder = OUT / area
    folder.mkdir(parents=True, exist_ok=True)
    text = HEADER.format(area=area, title=a["title"],
                         water=" and irrigated share" if a["irrigated_share"] else "")
    body = yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=100, default_flow_style=None)
    p = folder / "experiment.yml"
    p.write_text(text + body, encoding="utf-8")
    return p


def machine_env() -> dict:
    """The environment for the prepare scripts: this computer's folders, as XDSSAT_* variables
    (the example file sets none but datasets)."""
    m = config.load()
    env = dict(os.environ)
    for key, (var, _) in config.PATH_KEYS.items():
        if key == "datasets":
            env.pop(var, None)                     # the example's own folder must win
        elif m.has_path(key):
            env[var] = m.path(key).as_posix()
    return env


def run(cmd: list[str], env: dict) -> None:
    print(">", " ".join(cmd[1:]), flush=True)
    r = subprocess.run(cmd, cwd=ROOT, env=env)
    if r.returncode:
        raise SystemExit(f"failed ({r.returncode}): {' '.join(cmd[1:])}")


def tidy_manifests(folder: Path, env: dict) -> None:
    """Replace this computer's folders in input.json by {agera5}, {inputs} ... placeholders."""
    subs = {env[v].replace("\\", "/"): "{" + k + "}" for k, (v, _) in config.PATH_KEYS.items()
            if k != "datasets" and env.get(v)}
    for p in folder.glob("inputs/*/*/input.json"):
        text = p.read_text(encoding="utf-8")
        new = json.dumps(_replace(json.loads(text), subs), indent=2, ensure_ascii=False) + "\n"
        if new != text:
            p.write_text(new, encoding="utf-8")


def _replace(v, subs):
    if isinstance(v, str):
        s = v.replace("\\", "/")
        for old, new in sorted(subs.items(), key=lambda kv: -len(kv[0])):
            if s.lower().startswith(old.lower()):
                return new + s[len(old):]
        return v
    if isinstance(v, list):
        return [_replace(x, subs) for x in v]
    if isinstance(v, dict):
        return {k: _replace(x, subs) for k, x in v.items()}
    return v


def build(area: str, overwrite: bool, env: dict) -> None:
    a = AREAS[area]
    f = write_experiment(area, a)
    rel = f.relative_to(ROOT).as_posix()
    py = [sys.executable]
    ow = ["--overwrite"] if overwrite else []
    have = lambda kind: (OUT / area / "inputs" / kind).is_dir()  # noqa: E731
    if overwrite or not have("weather"):
        run(py + ["scripts/prepare_weather.py", "--config", rel] + ow, env)
    if overwrite or not have("crop_mask"):
        run(py + ["scripts/prepare_crop_mask.py", "--config", rel] + ow, env)
    if a["irrigated_share"] and (overwrite or not have("water_regime")):
        run(py + ["scripts/prepare_layers.py", "--config", rel, "--kind", "water_regime"] + ow, env)
    if overwrite or not have("soil"):
        run(py + ["scripts/prepare_soils.py", "--config", rel, "--aoi", area] + ow, env)
    tidy_manifests(OUT / area, env)


def check_sizes() -> list[str]:
    files = [p for p in OUT.rglob("*") if p.is_file()]
    big = [f"{p.relative_to(ROOT)}: {p.stat().st_size / 1e6:.1f} MB" for p in files
           if p.stat().st_size > MAX_FILE_MB * 1e6]
    total = sum(p.stat().st_size for p in files) / 1e6
    print(f"example-inputs/: {len(files)} files, {total:.1f} MB")
    for area in sorted(p.name for p in OUT.iterdir() if p.is_dir()):
        mb = sum(p.stat().st_size for p in (OUT / area).rglob("*") if p.is_file()) / 1e6
        print(f"  {area}: {mb:.1f} MB")
    return big + ([f"total {total:.1f} MB is over {MAX_TOTAL_MB} MB"] if total > MAX_TOTAL_MB else [])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", choices=list(AREAS), action="append", help="only this area (repeatable)")
    ap.add_argument("--overwrite", action="store_true", help="make the datasets again")
    ap.add_argument("--yaml-only", action="store_true", help="write the experiment files, prepare nothing")
    a = ap.parse_args(argv)
    env = None if a.yaml_only else machine_env()
    for area in a.area or AREAS:
        print(f"== {area}: {AREAS[area]['title']}", flush=True)
        if a.yaml_only:
            print("  wrote", write_experiment(area, AREAS[area]).relative_to(ROOT))
        else:
            build(area, a.overwrite, env)
    problems = check_sizes()
    if problems:
        raise SystemExit("size budget exceeded:\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    main()

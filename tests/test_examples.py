"""example-inputs/: every example loads, its shipped inputs pass the checks, the folder keeps
to its size budget and holds no machine paths; and (engine-marked) every example runs.

The examples are made by scripts/make_example_inputs.py; their experiment files read the
datasets of their own folder (paths > datasets), so a fresh clone needs only the engine.
"""
import importlib.util
import json
import re
from pathlib import Path

import pandas as pd
import pytest

import config
import inputs
from conftest import ROOT, has_engine

EXAMPLES = ROOT / "example-inputs"
FILES = sorted(EXAMPLES.glob("*/experiment.yml"))
IDS = [f.parent.name for f in FILES]


def _make_example_inputs():
    spec = importlib.util.spec_from_file_location("make_example_inputs", ROOT / "scripts" / "make_example_inputs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env(tmp_path) -> dict:
    """Run folders for a check: this computer's engine and work folders, a temporary cache."""
    env = {config.PATH_KEYS["cache"][0]: (tmp_path / "cache").as_posix()}
    for key in ("engine", "work"):
        if config.CURRENT.has_path(key):
            env[config.PATH_KEYS[key][0]] = config.CURRENT.path(key).as_posix()
        else:
            env[config.PATH_KEYS[key][0]] = (tmp_path / key).as_posix()
    return env


def test_every_area_of_the_script_is_shipped():
    assert sorted(_make_example_inputs().AREAS) == sorted(IDS)
    assert len(IDS) == 5


@pytest.mark.parametrize("f", FILES, ids=IDS)
def test_example_loads_and_its_inputs_pass_the_checks(f, tmp_path):
    exp = config.load(f, env=_env(tmp_path))
    assert exp.datasets() == f.parent                       # paths > datasets: its own folder
    assert exp.path("cache") == tmp_path / "cache"          # runs go elsewhere
    raw, _ = config.read_yaml(f)
    assert set(config.DEFAULTS) <= set(raw)                 # nothing left to the rice defaults
    assert raw["paths"] == {**{k: None for k in config.PATH_KEYS}, "datasets": f"example-inputs/{f.parent.name}"}
    problems = config.check_files(exp)
    if not has_engine:                                      # the cultivar check needs the engine
        problems = [p for p in problems if "cultivars" not in p]
    assert problems == []
    assert inputs.check(exp) == []
    ins = inputs.open_all(exp)
    for kind in ("weather", "crop_mask", "soil"):
        assert ins[kind] is not None, kind
    w, s, e, n = exp.bbox
    assert e - w <= 1.0 + 1e-9 and n - s <= 1.0 + 1e-9    # about 1 x 1 degree


def test_example_folder_keeps_to_the_size_budget_and_has_no_machine_paths():
    files = [p for p in EXAMPLES.rglob("*") if p.is_file() and "runs" not in p.parts]
    assert files
    big = [p for p in files if p.stat().st_size > 10e6]
    assert big == []
    assert sum(p.stat().st_size for p in files) < 20e6
    for p in EXAMPLES.glob("*/inputs/*/*/input.json"):
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"\b[A-Za-z]:[\\/]", text), f"{p}: a local drive path"
        meta = json.loads(text)
        assert "CC BY 4.0" in meta.get("source", "") or "CC-BY 4.0" in meta.get("source", ""), \
            f"{p}: no licence in source"


ENGINE_READY = has_engine and bool(FILES)


@pytest.mark.engine
@pytest.mark.skipif(not ENGINE_READY, reason="DSSAT engine not installed")
@pytest.mark.parametrize("f", FILES, ids=IDS)
def test_example_runs_end_to_end(f, tmp_path, monkeypatch):
    """A few cells of one year, through run_aoi as a user runs it."""
    for var, v in _env(tmp_path).items():
        monkeypatch.setenv(var, v)
    monkeypatch.delenv(config.PATH_KEYS["datasets"][0], raising=False)
    spec = importlib.util.spec_from_file_location("run_aoi", ROOT / "scripts" / "run_aoi.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    import management as mg
    monkeypatch.setattr(mg, "ANCHOR", mg.ANCHOR)              # run_aoi sets it to the example's year start
    exp = config.load(f)
    n = exp.data["treatments"]["nitrogen_kg"][0]
    out = tmp_path / "run"
    before = config.CURRENT
    try:
        run.main(["--config", str(f), "--sample", "4", "--years", "2019", "2019", "--n", str(n),
                  "--out", str(out)])
    finally:
        config.use(before)                                    # later tests use this computer's settings
    s = pd.read_csv(out / "summary.csv")
    failed = (out / "failed.csv").read_text().strip()          # empty when nothing failed
    assert len(s) > 0 and failed == ""
    assert (pd.to_numeric(s.HWAH, errors="coerce") > 0).all()
    info = json.loads((out / "run_info.json").read_text())
    assert info["crop"]["model"] == exp.data["template"]["crop_model"]
    assert (out / f"{out.name}.nc").is_file()


@pytest.mark.engine
@pytest.mark.skipif(not ENGINE_READY, reason="DSSAT engine not installed")
def test_scenarios_with_simulation_management_and_offset_run_end_to_end(tmp_path, monkeypatch):
    """java_maize with four scenarios: potential production, a later sowing, a denser stand."""
    import yaml
    import management as mg
    for var, v in _env(tmp_path).items():
        monkeypatch.setenv(var, v)
    monkeypatch.delenv(config.PATH_KEYS["datasets"][0], raising=False)
    monkeypatch.setattr(mg, "ANCHOR", mg.ANCHOR)
    src = EXAMPLES / "java_maize" / "experiment.yml"
    d = yaml.safe_load(src.read_text(encoding="utf-8"))
    d["paths"]["datasets"] = str(src.parent)
    d["template"]["file"] = str(ROOT / d["template"]["file"])
    d["run"]["output_folder"] = str(tmp_path / "runs")
    d["scenarios"] = [{"name": "baseline"},
                      {"name": "potential", "simulation": {"WATER": "N", "NITRO": "N"}},
                      {"name": "later", "planting_offset_days": 14},
                      {"name": "dense", "management": {"MP.PPOP": 9, "MP.PPOE": 9}}]
    f = tmp_path / "experiment.yml"
    f.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("run_aoi", ROOT / "scripts" / "run_aoi.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    before = config.CURRENT
    try:
        run.main(["--config", str(f), "--sample", "4", "--years", "2019", "2019"])
    finally:
        config.use(before)
    res = {}
    for name in ("baseline", "potential", "later", "dense"):
        folder = next((tmp_path / "runs").glob(f"*{'' if name == 'baseline' else '_' + name}"))
        if name == "baseline":
            folder = next(p for p in (tmp_path / "runs").iterdir() if p.name.endswith("_n120"))
        s = pd.read_csv(folder / "summary.csv").set_index(["cell", "season", "water"]).sort_index()
        res[name] = s
        assert (folder / "failed.csv").read_text().strip() == ""
    b = res["baseline"]
    assert (res["potential"].HWAH >= b.HWAH).all() and res["potential"].HWAH.sum() > b.HWAH.sum()
    shift = (pd.to_datetime(res["later"].PDAT.astype(str), format="%Y%j")
             - pd.to_datetime(b.PDAT.astype(str), format="%Y%j")).dt.days
    assert (shift == 14).all()
    assert (res["dense"]["MP.PPOP"] == 9).all() and not res["dense"].HWAH.equals(b.HWAH)


@pytest.mark.engine
@pytest.mark.skipif(not ENGINE_READY, reason="DSSAT engine not installed")
def test_daily_outputs_are_kept_per_season(tmp_path, monkeypatch):
    """run > daily: [plantgro, soilwat] writes one gzipped table per group, keyed by season."""
    import yaml
    import management as mg
    for var, v in _env(tmp_path).items():
        monkeypatch.setenv(var, v)
    monkeypatch.delenv(config.PATH_KEYS["datasets"][0], raising=False)
    monkeypatch.setattr(mg, "ANCHOR", mg.ANCHOR)
    src = EXAMPLES / "java_maize" / "experiment.yml"
    d = yaml.safe_load(src.read_text(encoding="utf-8"))
    d["paths"]["datasets"] = str(src.parent)
    d["template"]["file"] = str(ROOT / d["template"]["file"])
    d["run"]["daily"] = ["plantgro", "soilwat"]
    f = tmp_path / "experiment.yml"
    f.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("run_aoi", ROOT / "scripts" / "run_aoi.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    out = tmp_path / "run"
    before = config.CURRENT
    try:
        run.main(["--config", str(f), "--sample", "3", "--years", "2019", "2019", "--out", str(out)])
    finally:
        config.use(before)
    s = pd.read_csv(out / "summary.csv")
    pg = pd.read_csv(out / "daily_plantgro.csv.gz")
    sw = pd.read_csv(out / "daily_soilwat.csv.gz")
    assert {"cell", "stratum", "water", "season", "season_year", "date", "LAID"} <= set(pg.columns)
    assert len(sw) > 0
    seasons = s.groupby(["stratum", "season"]).size()
    assert set(map(tuple, pg[["stratum", "season"]].drop_duplicates().to_numpy())) == set(seasons.index)
    # the largest LAI of each season equals the season's LAIX
    lai = pg.groupby(["stratum", "season"]).LAID.max().round(1)
    laix = s.set_index(["stratum", "season"]).LAIX.round(1)
    assert (abs(lai.loc[laix.index] - laix) <= 0.11).all()


@pytest.mark.engine
@pytest.mark.skipif(not ENGINE_READY, reason="DSSAT engine not installed")
def test_rice_then_maize_sequence_runs_on_the_grid(tmp_path, monkeypatch):
    """templates/XDRM8601.SQX on java_rice: per stratum one mode-Q run; rice then maize every
    cycle, planted on 15 Nov and 15 Mar, soil carried over, only the requested cycles kept."""
    import yaml
    import management as mg
    import sequence
    tpl = ROOT / "templates" / "XDRM8601.SQX"
    t = sequence.sequence_table(__import__("filex").FileX.read(tpl))
    assert sorted(set(t.N)) == [1, 2] and list(t.R) == [1, 2, 1, 2]
    for var, v in _env(tmp_path).items():
        monkeypatch.setenv(var, v)
    monkeypatch.delenv(config.PATH_KEYS["datasets"][0], raising=False)
    monkeypatch.setattr(mg, "ANCHOR", mg.ANCHOR)
    src = EXAMPLES / "java_rice" / "experiment.yml"
    d = yaml.safe_load(src.read_text(encoding="utf-8"))
    d["paths"]["datasets"] = str(src.parent)
    d["template"]["file"] = str(tpl)
    d["treatments"]["water_regimes"] = {"irrigated": {"template_level": 1, "irrigation_classes": []},
                                        "rainfed": {"template_level": 2, "irrigation_classes": []}}
    d["years"] = [2018, 2019]
    f = tmp_path / "experiment.yml"
    f.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("run_aoi", ROOT / "scripts" / "run_aoi.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    out = tmp_path / "run"
    before = config.CURRENT
    try:
        assert config.check_files(config.load(f)) == []
        run.main(["--config", str(f), "--sample", "3", "--out", str(out)])
    finally:
        config.use(before)
    s = pd.read_csv(out / "summary.csv")
    assert len(s) == s.stratum.nunique() * 2 * 2                      # strata x cycles x crops
    assert set(s.season) == {"1_RI", "2_MZ"}
    doy = pd.to_numeric(s.PDAT) % 1000
    assert set(doy[s.season == "1_RI"]) == {319} and set(doy[s.season == "2_MZ"]) == {74}
    # labels come from the sowing dates: rice in its cycle year, maize the next March, no delay
    year = pd.to_numeric(s.PDAT) // 1000
    assert (year[s.season == "1_RI"] == s.season_year[s.season == "1_RI"]).all()
    assert (year[s.season == "2_MZ"] == s.season_year[s.season == "2_MZ"] + 1).all()
    assert (s.planting_delay_days.abs() <= 1).all()
    assert (pd.to_numeric(s.HWAH) > 0).all()
    assert (out / f"{out.name}.nc").is_file()


@pytest.mark.skipif(not ENGINE_READY, reason="DSSAT engine not installed")
def test_sequence_stops_early_when_the_weather_ends_too_soon(tmp_path, monkeypatch):
    """After the last cycle the engine starts one more first crop; if the weather ends before it
    can finish, the engine stops with "Missing required data" and the whole sequence is lost. The
    run must refuse up front instead: java_rice weather ends on 31 Dec 2021, which the season
    check accepts for 2018-2020, but the extra rice of 31 Dec 2020 given 400 days needs 2022."""
    import yaml
    import management as mg
    for var, v in _env(tmp_path).items():
        monkeypatch.setenv(var, v)
    monkeypatch.delenv(config.PATH_KEYS["datasets"][0], raising=False)
    monkeypatch.setattr(mg, "ANCHOR", mg.ANCHOR)
    src = EXAMPLES / "java_rice" / "experiment.yml"
    d = yaml.safe_load(src.read_text(encoding="utf-8"))
    d["paths"]["datasets"] = str(src.parent)
    d["template"]["file"] = str(ROOT / "templates" / "XDRM8601.SQX")
    d["treatments"]["water_regimes"] = {"irrigated": {"template_level": 1, "irrigation_classes": []},
                                        "rainfed": {"template_level": 2, "irrigation_classes": []}}
    d["years"] = [2018, 2020]
    d["run"]["season_days"] = 400
    d["seasons"]["calendar"][0]["date"] = "12-31"           # the first crop of the sequence
    f = tmp_path / "experiment.yml"
    f.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("run_aoi", ROOT / "scripts" / "run_aoi.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    before = config.CURRENT
    try:
        with pytest.raises(SystemExit, match=r"run one cycle fewer \(years: \[2018, 2019\]\)"):
            run.main(["--config", str(f), "--sample", "3", "--out", str(tmp_path / "run")])
    finally:
        config.use(before)

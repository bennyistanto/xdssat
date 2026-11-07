"""Tests for the experiment files (configs/), config.load and the run_aoi wiring.

The committed files set no folders (paths are null), so tests that need folders
take this computer's (configs/xdssat.yml or XDSSAT_*) or temporary ones. The
engine-marked test at the end runs the same quick Java run twice, once from
flags and once from a YAML file, and needs the prepared cache (AgERA5, rasters,
soils) as well as the engine.
"""
import importlib.util
import re

import pandas as pd
import pytest
import yaml

import config
from conftest import ROOT, has_engine


def _run_aoi():
    spec = importlib.util.spec_from_file_location("run_aoi", ROOT / "scripts" / "run_aoi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _machine_paths(tmp_path=None) -> dict:
    """This computer's folders where set, else (with ``tmp_path``) temporary ones."""
    out = {}
    for key in config.PATH_KEYS:
        if config.CURRENT.has_path(key):
            out[key] = config.CURRENT.path(key).as_posix()
        elif tmp_path is not None:
            out[key] = (tmp_path / key).as_posix()
    return out


def _example(tmp_path=None, path=None) -> dict:
    """An experiment file's content; with ``tmp_path``, its folders filled in."""
    d = yaml.safe_load((path or config.EXAMPLE_FILE).read_text(encoding="utf-8"))
    if tmp_path is not None:
        d["paths"] = _machine_paths(tmp_path)
    return d


def _set_path_env(monkeypatch, tmp_path):
    for key, folder in _machine_paths(tmp_path).items():
        monkeypatch.setenv(config.PATH_KEYS[key][0], folder)


def _write(tmp_path, data=None, text=None, name="x.yml"):
    p = tmp_path / name
    p.write_text(text if text is not None else yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- the example file


def test_example_loads_validates_and_equals_the_defaults(tmp_path):
    exp = config.load(config.EXAMPLE_FILE, env={})
    empty = config.load(_write(tmp_path, text="# nothing set\n"), env={})
    assert exp.data == empty.data                      # example = built-in defaults
    unset = config.check_files(exp)                    # no folders: the three a run needs are named
    assert len(unset) == 3 and all("is not set" in p for p in unset)
    assert [p.split("'")[1] for p in unset] == [f"paths > {k}" for k in config.RUN_PATHS]
    with_folders = config.load(config.EXAMPLE_FILE, env={config.PATH_KEYS[k][0]: v
                                                         for k, v in _machine_paths(tmp_path).items()})
    assert config.check_files(with_folders) == []
    assert exp.combinations() == [("IR 58", 140, {"name": "baseline"})]
    assert exp.aoi == "java" and exp.bbox == (105.0, -9.0, 114.7, -5.0) and exp.region == "Jawa"
    assert exp.years == list(range(1991, 2021)) and exp.anchor == (8, 1)
    assert exp.template_file == ROOT / "templates" / "XDRI8601.RIX"


def test_every_setting_of_the_example_has_a_comment_above_it():
    lines = config.EXAMPLE_FILE.read_text(encoding="utf-8").splitlines()
    _, where = config.read_yaml(config.EXAMPLE_FILE)
    for key in config._keys(config.DEFAULTS):
        n = where[key]
        assert lines[n - 2].lstrip().startswith("#"), f"no comment above {key} (line {n})"


def test_configs_have_no_em_dash():
    for f in (config.EXAMPLE_FILE, config.ALL_OPTIONS_FILE, config.CONFIG_DIR / "README.md"):
        assert "\u2014" not in f.read_text(encoding="utf-8"), f


def test_no_folder_is_built_in():
    """No machine folder in DEFAULTS, the committed configs, or the code."""
    assert all(v is None for v in config.DEFAULTS["paths"].values())
    for f in (config.EXAMPLE_FILE, config.ALL_OPTIONS_FILE):
        assert all(v is None for v in yaml.safe_load(f.read_text(encoding="utf-8"))["paths"].values()), f
    drive = re.compile(r"""["'][A-Za-z]:[/\\]""")      # a string literal that starts with a drive
    for f in sorted((ROOT / "src").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        hits = [ln for ln in f.read_text(encoding="utf-8").splitlines()
                if drive.search(ln) and not ln.lstrip().startswith("#")]
        assert not hits, f"{f.name}: {hits}"


def test_unset_folder_stops_with_a_plain_message(tmp_path):
    exp = config.load(_write(tmp_path, text="paths:\n  engine: null\n"), env={})
    with pytest.raises(config.PathNotSet) as e:
        exp.path("engine")
    msg = str(e.value)
    assert "'paths > engine' is not set" in msg and "line 2" in msg and "XDSSAT_ENGINE" in msg
    with pytest.raises(config.PathNotSet, match="Add to .*under 'paths:'"):
        exp.path("cache")
    assert exp.where("paths.engine").startswith("not set")


# ---------------------------------------------------------------- the all-options file


def test_all_options_file_uses_every_option(tmp_path):
    raw = _example(path=config.ALL_OPTIONS_FILE)
    assert set(config._keys(config.DEFAULTS)) <= set(config.read_yaml(config.ALL_OPTIONS_FILE)[1])
    exp = config.load(_write(tmp_path, _example(tmp_path, config.ALL_OPTIONS_FILE)), env={})
    assert config.check_files(exp) == []
    assert len(exp.combinations()) == 36
    scs = raw["scenarios"]
    env = {k: v for s in scs for k, v in (s.get("environment") or {}).items()}
    assert set(env) == set(config.ENVIRONMENT)
    assert {next(iter(v)) for v in env.values()} == set(config.CHANGE)
    used = {k for s in scs for k in s}
    assert used == set(config.SCENARIO_KEYS) - {"slpf_table"}     # slpf_table needs a local CSV
    co = [s["cultivar_coefficients"] for s in scs if "cultivar_coefficients" in s]
    assert any(isinstance(v, dict) for c in co for v in c.values())   # one cultivar only
    import management as mg
    from filex import FileX
    in_template = {code for _, code in mg.template_cultivars(FileX.read(exp.template_file)).values()}
    assert set(raw["cultivars"].values()) - in_template                 # a cultivar the template lacks
    assert raw["seasons"]["zone_table"] and raw["seasons"]["mode"] == "per_cell"


# ---------------------------------------------------------------- broken files


BROKEN = [
    ("run:\n  wokers: 4\n", ["line 2", "run > wokers", "Did you mean 'workers'"]),
    ("treatments:\n  nitrogen_kg: [150]\n", ["nitrogen_kg", "150 kg N/ha is not one of the rates",
                                             "0, 35, 70, 105, 140", "Example of a valid value"]),
    ("seasons:\n  calendar:\n    - {name: wet, date: 2015-11-15, regimes: [irrigated]}\n",
     ["line 3", "calendar > 1 > date", "MM-DD", "no year"]),
    ("seasons:\n  mode: percell\n", ["seasons > mode", "uniform, per_cell, by_zone"]),
    ("aoi:\n  bbox: [114.7, -5.0, 105.0, -9.0]\n", ["aoi > bbox", "west, south, east, north"]),
    ("years: [2020, 1991]\n", ["years", "first not after the last"]),
    ("cultivars:\n  IR 64: IB15\n", ["cultivars > IR 64", "6-character"]),
    ("scenarios:\n  - description: no name\n", ["scenarios > 1", "needs a name"]),
    ("scenarios:\n  - name: a\n  - name: a\n", ["same name"]),
    ("scenarios:\n  - name: hot\n    environment: {tmax: {increase: 2}}\n",
     ["environment > tmax", "add, subtract, multiply, replace"]),
    ("scenarios:\n  - name: co2\n    environment: {co2: {replace: 12345}}\n", ["4 characters"]),
    ("scenarios:\n  - name: s\n    slpf: 1.5\n", ["scenarios > 1 > slpf", "too large"]),
    ("scenarios:\n  - name: s\n    slpf: 0.7\n    slpf_table: t.csv\n", ["replaces slpf"]),
    ("treatments:\n  water_regimes:\n    irrigated: {template_level: 1, irrigation_classes: [1, 2]}\n"
     "    rainfed: {template_level: 3, irrigation_classes: [2, 4]}\n", ["class 2 is also given"]),
    ("seasons:\n  calendar:\n    - {name: wet, date: \"11-15\", regimes: [flooded]}\n",
     ["regimes", "irrigated, rainfed"]),
    ("paths:\n\tengine: C:/x\n", ["line 2", "tab"]),
    ("run: {workers: 4\n", ["not valid YAML", "line"]),
    ("speed: fast\n", ["'speed' is not a setting"]),
]


@pytest.mark.parametrize("text, expect", BROKEN, ids=[re.sub(r"\W+", "_", t)[:40] for t, _ in BROKEN])
def test_broken_file_fails_with_a_plain_message(tmp_path, text, expect):
    with pytest.raises(config.ConfigError) as e:
        config.load(_write(tmp_path, text=text), env={})
    msg = str(e.value)
    for part in expect:
        assert part in msg, f"{part!r} not in:\n{msg}"
    assert "Traceback" not in msg


def test_all_problems_are_reported_at_once(tmp_path):
    with pytest.raises(config.ConfigError) as e:
        config.load(_write(tmp_path, text="run:\n  workers: 0\n  chunk_size: 5000\nyears: [1991]\n"), env={})
    assert len(e.value.problems) == 3


def test_missing_file_says_how_to_make_one(tmp_path):
    with pytest.raises(config.ConfigError, match="Copy configs/rice-java.yml"):
        config.load(tmp_path / "nope.yml")


def test_template_checks_name_the_setting(tmp_path):
    d = _example(tmp_path)
    d["treatments"]["nitrogen_levels"][140] = 7
    exp = config.load(_write(tmp_path, d), env={})
    problems = config.check_files(exp)
    assert any("nitrogen_levels > 140" in p and "no fertilizer level 7" in p for p in problems)
    d = _example(tmp_path)
    d["template"]["file"] = "templates/NOPE.RIX"
    assert any("does not exist" in p for p in config.check_files(config.load(_write(tmp_path, d), env={})))


# ---------------------------------------------------------------- paths, overrides, defaults


def test_environment_variable_wins_over_the_file_and_says_so():
    exp = config.load(config.EXAMPLE_FILE, env={"XDSSAT_ENGINE": "D:/dssat48"})
    assert exp.path("engine") == config._abs("D:/dssat48")
    assert "XDSSAT_ENGINE" in exp.where("paths.engine")
    assert f"line {exp.lines['paths.engine']}" in exp.where("paths.engine")
    assert exp.where("paths.cache").startswith("not set (rice-java.yml line")


def test_left_out_keys_take_defaults_and_relative_paths_start_at_the_project(tmp_path):
    exp = config.load(_write(tmp_path, text="paths:\n  cache: mycache\nrun:\n  sample: 5\n"), env={})
    assert exp.path("cache") == ROOT / "mycache"
    assert not exp.has_path("engine")
    assert exp.data["run"]["sample"] == 5 and exp.data["run"]["workers"] == 32
    assert "not set in x.yml" in exp.where("run.workers")


def test_overrides_win_and_are_checked():
    exp = config.load(config.EXAMPLE_FILE, overrides={"run": {"sample": 30}, "years": [2015, 2016]}, env={})
    assert exp.data["run"]["sample"] == 30 and exp.years == [2015, 2016]
    assert exp.where("run.sample") == "command line"
    with pytest.raises(config.ConfigError, match="run > sample"):
        config.load(config.EXAMPLE_FILE, overrides={"run": {"sample": 0}}, env={})


def test_only_paths_reads_a_file_with_problems_elsewhere(tmp_path):
    p = _write(tmp_path, text="paths:\n  cache: D:/c\nrun:\n  wokers: 4\n")
    exp = config.load(p, env={}, only=("paths",))
    assert exp.path("cache") == config._abs("D:/c")


def test_use_switches_module_paths(tmp_path):
    before = config.CURRENT
    try:
        config.use(config.load(_write(tmp_path, text="paths:\n  cache: D:/elsewhere\n"), env={}))
        assert config.CACHE_DIR == config._abs("D:/elsewhere")
        assert config.SETTINGS["CACHE_DIR"] == ("XDSSAT_CACHE", config._abs("D:/elsewhere"))
        assert config.SETTINGS["ENGINE_DIR"] == ("XDSSAT_ENGINE", None)
        with pytest.raises(config.PathNotSet, match="paths > engine"):
            config.ENGINE_DIR                          # noqa: B018
    finally:
        config.use(before)


# ---------------------------------------------------------------- run_aoi wiring


def test_combinations_are_cultivars_by_n_by_scenarios(tmp_path):
    d = _example(tmp_path)
    d["cultivars"]["IR 64"] = "IB0015"
    d["treatments"]["nitrogen_kg"] = [0, 140]
    d["scenarios"] = [{"name": "baseline"}, {"name": "co2_550", "environment": {"co2": {"replace": 550}}}]
    exp = config.load(_write(tmp_path, d), env={})
    assert config.check_files(exp) == []
    assert len(exp.combinations()) == 8


def test_run_folder_names_match_the_flag_runs():
    r = _run_aoi()
    assert r.run_tag("IR 58", 140, {"name": "baseline"}) == ""
    assert r.run_tag("IR 64", 105, {"name": "baseline"}) == "_ir64_n105"
    assert (r.run_tag("IR 58", 140, {"name": "baseline", "slpf": 0.76, "slpf_rainfed": 0.58,
                                     "cultivar_coefficients": {"TCLDF": 19}})
            == "_slpf0.76_rf0.58_tcldf19")
    assert r.run_tag("IR 58", 140, {"name": "baseline", "slpf_table": "C:/x/slpf_temp_v1.csv",
                                    "cultivar_coefficients": {"TCLDF": 19.0}}) == "_slpf_temp_v1_tcldf19"
    assert r.run_tag("IR 58", 140, {"name": "co2_550"}) == "_co2_550"
    assert r.environment_changes({"environment": {"co2": {"replace": 550}, "tmax": {"add": 2}}}) == \
        {"ECO2": ("R", 550), "EMAX": ("A", 2)}


def test_flags_override_the_file(tmp_path, monkeypatch):
    r = _run_aoi()
    monkeypatch.setattr(config, "DEFAULT_FILE", tmp_path / "absent.yml")
    monkeypatch.delenv("XDSSAT_CONFIG", raising=False)
    _set_path_env(monkeypatch, tmp_path)
    a = r.parse_args(["--sample", "30", "--years", "2015", "2016", "--cultivar", "IR 64", "--n", "105",
                      "--slpf", "0.76", "--tcldf", "19", "--mode", "per_cell"])
    exp = r.experiment_from_args(a)
    assert exp.file is None
    assert exp.data["run"]["sample"] == 30 and exp.years == [2015, 2016]
    assert exp.data["cultivars"] == {"IR 64": "IB0015"}          # from the template's cultivars
    assert exp.data["treatments"]["nitrogen_kg"] == [105] and exp.data["seasons"]["mode"] == "per_cell"
    sc = exp.data["scenarios"][0]
    assert sc["slpf"] == 0.76 and sc["cultivar_coefficients"] == {"TCLDF": 19.0}
    args = r.legacy_args(exp, "IR 64", 105, sc, None)
    assert args["years"] == "[2015, 2016]" and args["slpf"] == "0.76" and args["tcldf"] == "19.0"
    assert args["cultivar"] == "IR 64" and args["n"] == "105" and args["slpf_rainfed"] == "None"
    with pytest.raises(SystemExit, match="aoi"):
        r.experiment_from_args(r.parse_args(["--aoi", "bali"]))
    with pytest.raises(SystemExit, match="replaces"):
        r.experiment_from_args(r.parse_args(["--slpf", "0.7", "--slpf-table", "t.csv"]))


def test_check_lists_the_runs_and_bad_files_stop_before_running(tmp_path, capsys):
    r = _run_aoi()
    d = _example(tmp_path)
    d["treatments"]["nitrogen_kg"] = [0, 140]
    r.main(["--config", str(_write(tmp_path, d)), "--check"])
    out = capsys.readouterr().out
    assert "2 run(s)" in out and "java_uniform_1991-2020_ir58_n0" in out and "settings OK" in out
    with pytest.raises(SystemExit) as e:
        r.main(["--config", str(_write(tmp_path, text="run:\n  wokers: 4\n", name="bad.yml")), "--check"])
    assert e.value.code == 2
    assert "Did you mean 'workers'" in capsys.readouterr().err
    with pytest.raises(SystemExit):                    # no folders set: named before anything runs
        r.main(["--config", str(config.EXAMPLE_FILE), "--check"])
    assert "'paths > engine' is not set" in capsys.readouterr().err


def test_resolved_copy_of_one_run_loads_back(tmp_path):
    r = _run_aoi()
    exp = config.load(config.EXAMPLE_FILE, env={})
    cv, n, sc = exp.combinations()[0]
    p = _write(tmp_path, text=r.one_combination(exp, cv, n, sc).to_yaml("# header\n"), name="experiment.yml")
    back = config.load(p, env={})
    assert back.data == exp.data


# ---------------------------------------------------------------- same run, flags vs YAML

CACHE_READY = config.CURRENT.has_path("cache") and all((config.CURRENT.path("cache") / p).exists()
                                                       for p in ("rasters/idn_rice.tif", "rasters/idn_irrigation_filled.tif",
                            "agera5/idn/TMAX", f"soils/{config.DEFAULTS['aoi']['name']}.SOL"))


@pytest.mark.engine
@pytest.mark.skipif(not (has_engine and CACHE_READY), reason="engine or prepared cache (AgERA5, rasters, soils) missing")
def test_sample_run_from_yaml_equals_run_from_flags(tmp_path, monkeypatch):
    r = _run_aoi()
    monkeypatch.setattr(config, "DEFAULT_FILE", tmp_path / "absent.yml")   # flags run: built-in defaults
    monkeypatch.delenv("XDSSAT_CONFIG", raising=False)
    _set_path_env(monkeypatch, tmp_path)                                   # ... with this computer's folders
    before = config.CURRENT
    try:
        flags = tmp_path / "flags"
        r.main(["--sample", "30", "--years", "2015", "2016", "--out", str(flags)])
        d = _example(tmp_path)
        d["years"] = [2015, 2016]
        d["run"]["sample"] = 30
        d["run"]["output_folder"] = str(tmp_path / "yaml")
        r.main(["--config", str(_write(tmp_path, d, name="quick.yml"))])
    finally:
        config.use(before)
    from_yaml = tmp_path / "yaml" / "java_uniform_2015-2016"
    a = pd.read_csv(flags / "summary.csv")
    b = pd.read_csv(from_yaml / "summary.csv")
    assert len(a) > 50
    pd.testing.assert_frame_equal(a, b)
    for f in ("strata.csv", "units.csv"):
        pd.testing.assert_frame_equal(pd.read_csv(flags / f), pd.read_csv(from_yaml / f))
    assert config.load(from_yaml / "experiment.yml", env={}).data["run"]["sample"] == 30

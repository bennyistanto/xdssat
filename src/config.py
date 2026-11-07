"""xdssat settings: one YAML experiment file per experiment, in configs/.

Everything a user changes lives in one experiment file. The committed ones are
in ``configs/`` (``configs/README.md`` lists them); each user keeps their own
copy, by default ``configs/xdssat.yml`` (gitignored), or passes any file with
``--config``. ``configs/rice-java.yml`` explains every key.

Which file is read
------------------
1. the file given to a script with ``--config``;
2. otherwise the file named by the environment variable ``XDSSAT_CONFIG``;
3. otherwise ``configs/xdssat.yml``, if it exists;
4. otherwise the built-in defaults below (``DEFAULTS``), which are exactly
   ``configs/rice-java.yml`` (tests/test_config.py checks this).

A key left out of the file takes its default. Inside the sections ``paths``,
``aoi``, ``template``, ``treatments``, ``seasons`` and ``run`` each key is
taken on its own; ``cultivars``, ``scenarios`` and ``years`` are taken whole.

Paths
-----
There are **no built-in folder locations**: every folder under ``paths`` comes
from the experiment file or from an environment variable, which wins over the
file (``XDSSAT_ENGINE``, ``XDSSAT_WORK``, ``XDSSAT_AGERA5``,
``XDSSAT_SOILGRIDS``, ``XDSSAT_INPUTS``, ``XDSSAT_CACHE``,
``XDSSAT_BOUNDARIES``), so one shared experiment file runs on several machines.
A folder that is needed but not set stops with ``PathNotSet``, a message that
says which line to edit. Relative paths are read from the project root.
``scripts/check_setup.py`` prints where each value came from.

The engine folder and the work root must be short paths outside OneDrive: the
engine keeps file paths in fixed ``CHARACTER*80`` to ``*102`` buffers (see
docs/feasibility-review.md, section 4).

Module attributes
-----------------
``ENGINE_DIR``, ``WORK_ROOT``, ``AGERA5_DIR``, ``SOILGRIDS_DIR``,
``INPUTS_DIR``, ``CACHE_DIR``, ``BOUNDARIES_DIR`` and ``CURRENT`` (the loaded
``Experiment``) are set at import from the default file. Reading a folder that
is not set raises ``PathNotSet``. ``use(experiment)`` switches them to another
file, as ``scripts/run_aoi.py --config`` does.
"""
from __future__ import annotations

import copy
import datetime as dt
import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = sys.platform.startswith("win")
CONFIG_DIR = ROOT / "configs"
DEFAULT_FILE = CONFIG_DIR / "xdssat.yml"              # each user's own file (gitignored)
EXAMPLE_FILE = CONFIG_DIR / "rice-java.yml"           # the reference; equals DEFAULTS
ALL_OPTIONS_FILE = CONFIG_DIR / "rice-java-all-options.yml"

EXE_NAME = "dscsm048.exe" if IS_WINDOWS else "dscsm048"
PROFILE_NAME = "DSSATPRO.V48" if IS_WINDOWS else "DSSATPRO.L48"

# Longest run-directory path we accept. The engine prepends directories to
# 12-character file names (e.g. GR000123.WTH) inside 80-character buffers;
# 60 leaves room for the name and a separator.
MAX_RUN_DIR_LEN = 60

# paths key -> (environment variable, module attribute)
PATH_KEYS = {
    "engine": ("XDSSAT_ENGINE", "ENGINE_DIR"),
    "work": ("XDSSAT_WORK", "WORK_ROOT"),
    "agera5": ("XDSSAT_AGERA5", "AGERA5_DIR"),
    "soilgrids": ("XDSSAT_SOILGRIDS", "SOILGRIDS_DIR"),
    "inputs": ("XDSSAT_INPUTS", "INPUTS_DIR"),
    "cache": ("XDSSAT_CACHE", "CACHE_DIR"),
    "boundaries": ("XDSSAT_BOUNDARIES", "BOUNDARIES_DIR"),
}
# Required by run_aoi.py; the others are needed only to prepare inputs.
RUN_PATHS = ("engine", "work", "cache")

# The default experiment: today's Java run. Keep in step with configs/rice-java.yml.
# No folder has a default: every machine sets its own (experiment file or environment).
DEFAULTS: dict = {
    "paths": {key: None for key in PATH_KEYS},
    "aoi": {"name": "java", "bbox": [105.0, -9.0, 114.7, -5.0], "region": "Jawa"},
    "template": {"file": "templates/XDRI8601.RIX", "crop_model": "RICER048"},
    "cultivars": {"IR 58": "IB0012"},
    "treatments": {
        "nitrogen_kg": [140],
        "nitrogen_levels": {0: 0, 35: 1, 70: 2, 105: 3, 140: 4},
        "water_regimes": {
            "irrigated": {"template_level": 1, "irrigation_classes": [1, 2, 3]},
            "rainfed": {"template_level": 3, "irrigation_classes": [4]},
        },
        "initial_soil_water": 0.9,
    },
    "seasons": {
        "mode": "uniform",
        "year_starts": "08-01",
        "calendar": [
            {"name": "wet", "date": "11-15", "regimes": ["irrigated", "rainfed"]},
            {"name": "dry", "date": "04-01", "regimes": ["irrigated"]},
        ],
        "onset": {"rain_mm": 50, "dasarian": 3, "transplant_after_days": 20,
                  "next_season_after_days": 120, "years": None},
        "zone_table": None,
        "zone_column": "aez",
    },
    "scenarios": [{"name": "baseline"}],
    "years": [1991, 2020],
    "run": {"workers": 32, "chunk_size": 200, "years_per_batch": 5, "sample": None,
            "seed": 1, "season_days": 220, "output_folder": None},
}

SECTIONS = ("paths", "aoi", "template", "treatments", "seasons", "run")   # merged key by key
WHOLE = ("cultivars", "scenarios", "years")                               # replaced whole
MODES = ("uniform", "per_cell", "by_zone")
# scenario environment key -> FileX *ENVIRONMENT MODIFICATIONS column (IPENV.for:1000)
ENVIRONMENT = {"daylength": "EDAY", "radiation": "ERAD", "tmax": "EMAX", "tmin": "EMIN",
               "rain": "ERAIN", "co2": "ECO2", "dewpoint": "EDEW", "wind": "EWIND"}
# how to change it -> engine code (Weather/WTHMOD.for WTHSET: A add, S subtract, M multiply, R replace)
CHANGE = {"add": "A", "subtract": "S", "multiply": "M", "replace": "R"}
SCENARIO_KEYS = ("name", "description", "environment", "cultivar_coefficients",
                 "slpf", "slpf_rainfed", "slpf_table")


class ConfigError(ValueError):
    """An experiment file a user has to fix. ``problems`` are plain-language messages."""

    def __init__(self, problems: list[str], file: Path | None = None):
        self.problems = problems
        self.file = file
        where = str(file) if file else "the settings"
        n = len(problems)
        super().__init__(f"{n} problem{'s' if n > 1 else ''} in {where}:\n\n" + "\n\n".join(problems))


class PathNotSet(ConfigError):
    """A folder under ``paths`` is needed but set nowhere."""


# ----------------------------------------------------------------------------
# the loaded experiment
# ----------------------------------------------------------------------------
@dataclass
class Experiment:
    data: dict                                   # resolved settings (paths are absolute strings)
    file: Path | None = None                     # the YAML file read, or None for built-in defaults
    lines: dict = field(default_factory=dict)    # "paths.engine" -> line number in the file
    sources: dict = field(default_factory=dict)  # "paths.engine" -> where the value came from

    # paths -------------------------------------------------------------
    def path(self, key: str) -> Path:
        """The folder ``paths > key``; raises ``PathNotSet`` when it is set nowhere."""
        v = self.data["paths"][key]
        if v is None:
            raise PathNotSet([path_not_set_message(self, key)], self.file)
        return Path(v)

    def has_path(self, key: str) -> bool:
        return self.data["paths"][key] is not None

    @property
    def template_file(self) -> Path:
        return _abs(self.data["template"]["file"])

    # area -------------------------------------------------------------
    @property
    def aoi(self) -> str:
        return self.data["aoi"]["name"]

    @property
    def bbox(self) -> tuple:
        return tuple(float(v) for v in self.data["aoi"]["bbox"])

    @property
    def region(self) -> str:
        return self.data["aoi"]["region"]

    # management -------------------------------------------------------
    @property
    def years(self) -> list[int]:
        y0, y1 = self.data["years"]
        return list(range(int(y0), int(y1) + 1))

    @property
    def anchor(self) -> tuple[int, int]:
        m, d = self.data["seasons"]["year_starts"].split("-")
        return int(m), int(d)

    def regime_of_class(self) -> dict[int, str]:
        """MoA irrigation class -> water regime name."""
        return {int(c): name for name, w in self.data["treatments"]["water_regimes"].items()
                for c in w["irrigation_classes"]}

    def water_level(self, regime: str) -> int:
        return int(self.data["treatments"]["water_regimes"][regime]["template_level"])

    def n_level(self, kg: int) -> int:
        return int(self.data["treatments"]["nitrogen_levels"][int(kg)])

    def combinations(self) -> list[tuple[str, int, dict]]:
        """Every (cultivar name, kg N, scenario) to run, one run folder each."""
        return [(cv, int(n), sc) for cv in self.data["cultivars"]
                for n in self.data["treatments"]["nitrogen_kg"] for sc in self.data["scenarios"]]

    def where(self, key: str) -> str:
        """Plain-language location of ``key`` ("xdssat.yml line 12", ...)."""
        return self.sources.get(key, "built-in default")

    def to_yaml(self, header: str = "") -> str:
        import yaml
        return header + yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True, width=100,
                                       default_flow_style=None)


# ----------------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------------
def default_file() -> Path | None:
    """The file used when no ``--config`` is given (None: built-in defaults)."""
    env = os.environ.get("XDSSAT_CONFIG")
    if env:
        return _abs(env)
    return DEFAULT_FILE if DEFAULT_FILE.is_file() else None


def load(path=None, overrides: dict | None = None, env=None, only=None) -> Experiment:
    """Read, check and resolve an experiment file.

    ``path``: the YAML file; None takes ``default_file()``. ``overrides``:
    settings given on the command line, in the same nested shape as the file
    (``{"run": {"sample": 30}}``); they win over the file and are checked the
    same way. ``env``: environment variables (default ``os.environ``).
    ``only``: read just these top-level sections of the file (the rest takes
    the defaults), e.g. ``("paths",)`` to report paths of a file with problems
    elsewhere. Raises ``ConfigError`` listing every problem found.
    """
    env = os.environ if env is None else env
    file = _abs(path) if path else default_file()
    raw, lines = ({}, {}) if file is None else read_yaml(file)
    if only:
        raw = {k: v for k, v in raw.items() if k in only}
        lines = {k: v for k, v in lines.items() if k.split(".")[0] in only}
    sources = {}
    if file is not None:
        for key in lines:
            sources[key] = f"{file.name} line {lines[key]}"
    over_lines = {}
    if overrides:
        raw = _merge(raw, overrides, whole_from=WHOLE)
        for key in _keys(overrides):
            sources[key] = "command line"
            over_lines[key] = None
    problems = validate(raw, file, lines)
    if problems:
        raise ConfigError(problems, file)
    data = _merge(DEFAULTS, raw, whole_from=WHOLE)
    _normalise(data)
    for key, (var, _) in PATH_KEYS.items():
        k = f"paths.{key}"
        if env.get(var):
            data["paths"][key] = env[var]
            sources[k] = (f"environment variable {var}"
                          + (f" (it overrides {sources[k]})" if k in sources else ""))
        if data["paths"][key] is not None:
            data["paths"][key] = _abs(data["paths"][key]).as_posix()
        else:
            sources[k] = "not set" + (f" ({sources[k]} is null)" if k in sources else "")
    for k in _keys(DEFAULTS):
        sources.setdefault(k, "built-in default" + ("" if file is None else f" (not set in {file.name})"))
    return Experiment(data, file, lines, sources)


def read_yaml(file: Path) -> tuple[dict, dict]:
    """The file's content and the line number of every key ("paths.engine": 12)."""
    import yaml
    file = Path(file)
    if not file.is_file():
        raise ConfigError([f"The experiment file {file} does not exist.\n"
                           f"  Copy configs/{EXAMPLE_FILE.name} to {file.name} and edit the copy, or "
                           "pass another file with --config (configs/README.md lists them)."], file)
    text = file.read_text(encoding="utf-8")
    if "\t" in text:
        n = text[: text.index("\t")].count("\n") + 1
        raise ConfigError([f"{file.name} line {n}: there is a tab character. YAML needs spaces for "
                           "indentation. Replace the tab with spaces."], file)
    try:
        node = yaml.compose(text)
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        at = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "an unknown place"
        what = getattr(e, "problem", None) or str(e)
        raise ConfigError([f"{file.name} is not valid YAML ({what}, at {at}).\n"
                           "  Common causes: a missing space after ':', a missing closing quote "
                           "or bracket, or a line indented differently from its neighbours.\n"
                           "  Compare that line with the same line in configs/rice-java.yml."], file) from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError([f"{file.name} must be a list of 'key: value' settings, like "
                           "configs/rice-java.yml."], file)
    lines = {}
    _node_lines(node, "", lines)
    return raw, lines


def use(exp: Experiment) -> Experiment:
    """Make ``exp`` the current experiment: module path attributes follow it."""
    global CURRENT, SETTINGS
    CURRENT = exp
    g = globals()
    for key, (_, attr) in PATH_KEYS.items():
        if exp.has_path(key):
            g[attr] = exp.path(key)
        else:
            g.pop(attr, None)           # reading it then goes to __getattr__: PathNotSet
    SETTINGS = {attr: (var, exp.path(key) if exp.has_path(key) else None)
                for key, (var, attr) in PATH_KEYS.items()}
    return exp


_ATTR_KEY = {attr: key for key, (_, attr) in PATH_KEYS.items()}


def __getattr__(name):
    """``config.ENGINE_DIR`` and friends when that folder is set nowhere."""
    if name in _ATTR_KEY:
        raise PathNotSet([path_not_set_message(CURRENT, _ATTR_KEY[name])], CURRENT.file)
    raise AttributeError(f"module 'config' has no attribute {name!r}")


def path_not_set_message(exp: "Experiment", key: str) -> str:
    var = PATH_KEYS[key][0]
    if exp.file is None:
        where = (f"No experiment file was found (configs/{DEFAULT_FILE.name}). Copy "
                 f"configs/{EXAMPLE_FILE.name} to configs/{DEFAULT_FILE.name} and set '{key}:' under "
                 "'paths:' to the folder on this computer")
    else:
        ln = exp.lines.get(f"paths.{key}")
        where = (f"Set it in {exp.file} line {ln}:  {key}: <folder>" if ln else
                 f"Add to {exp.file}, under 'paths:', the line   {key}: <folder>")
    return (f"The folder 'paths > {key}' is not set, and this step needs it.\n  {where},\n"
            f"  or set the environment variable {var}. python scripts/check_setup.py lists every path.")


def _excepthook(kind, value, tb, _default=sys.excepthook):
    """Scripts: print a ConfigError as the plain message it is, without a traceback."""
    if isinstance(value, ConfigError):
        print(f"\n{value}\n\nNothing was run. Fix the file and try again.", file=sys.stderr)
        raise SystemExit(2)
    _default(kind, value, tb)


sys.excepthook = _excepthook


def _load_at_import() -> Experiment:
    try:
        return load()
    except ConfigError as e:
        print(f"xdssat: {e}\n\nUsing the built-in settings instead. "
              "Run python scripts/check_setup.py to see every setting.", file=sys.stderr)
        return load_defaults()
    except ImportError:          # PyYAML missing: built-in settings still work
        return load_defaults()


def load_defaults() -> Experiment:
    """The built-in defaults (with path environment variables), no file read."""
    data = copy.deepcopy(DEFAULTS)
    sources = {k: "built-in default" for k in _keys(DEFAULTS)}
    for key, (var, _) in PATH_KEYS.items():
        if os.environ.get(var):
            data["paths"][key] = os.environ[var]
            sources[f"paths.{key}"] = f"environment variable {var}"
        if data["paths"][key] is not None:
            data["paths"][key] = _abs(data["paths"][key]).as_posix()
        else:
            sources[f"paths.{key}"] = "not set"
    _normalise(data)
    return Experiment(data, None, {}, sources)


# ----------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------
class _Checker:
    def __init__(self, file, lines):
        self.name = Path(file).name if file else "the settings"
        self.lines = lines
        self.problems: list[str] = []

    def bad(self, key: str, problem: str, example: str = ""):
        ln = self.lines.get(key)
        head = f"{self.name}" + (f" line {ln}" if ln else "") + f", setting '{key.replace('.', ' > ')}':"
        msg = f"{head}\n  {problem}"
        if example:
            msg += f"\n  Example of a valid value:  {example}"
        self.problems.append(msg)

    def unknown(self, prefix: str, given: dict, allowed):
        for k in given:
            if k not in allowed:
                near = difflib.get_close_matches(str(k), [str(a) for a in allowed], n=1)
                hint = f" Did you mean '{near[0]}'?" if near else ""
                key = f"{prefix}.{k}" if prefix else str(k)
                self.bad(key, f"'{k}' is not a setting xdssat knows.{hint} "
                              f"Allowed here: {', '.join(map(str, allowed))}.")

    def number(self, key, v, lo=None, hi=None, integer=False, example="", open_lo=False):
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        if ok and integer and isinstance(v, float) and not v.is_integer():
            ok = False
        if not ok:
            self.bad(key, f"{v!r} is not {'a whole number' if integer else 'a number'}.", example)
            return False
        if lo is not None and (v <= lo if open_lo else v < lo):
            self.bad(key, f"{v} is too small (must be {'more than' if open_lo else 'at least'} {lo}).", example)
            return False
        if hi is not None and v > hi:
            self.bad(key, f"{v} is too large (must be at most {hi}).", example)
            return False
        return True

    def text(self, key, v, example="", pattern=None, what=""):
        if not isinstance(v, str) or not v.strip():
            self.bad(key, f"{v!r} should be text{(' (' + what + ')') if what else ''}.", example)
            return False
        if pattern and not re.fullmatch(pattern, v):
            self.bad(key, f"'{v}' is not allowed here: {what}.", example)
            return False
        return True

    def month_day(self, key, v, example='"11-15"'):
        if isinstance(v, (dt.date, int)):
            self.bad(key, f"{v} should be a month and day in quotes, as \"MM-DD\" "
                          "(no year).", example)
            return False
        if not isinstance(v, str) or not re.fullmatch(r"\d\d-\d\d", v):
            self.bad(key, f"{v!r} should be a month and day as \"MM-DD\".", example)
            return False
        try:
            dt.date(2001, *(int(x) for x in v.split("-")))
        except ValueError:
            self.bad(key, f"'{v}' is not a real date (month 01-12, then a day that month has).", example)
            return False
        return True

    def year_pair(self, key, v, example="[1991, 2020]"):
        if (not isinstance(v, list) or len(v) != 2
                or not all(isinstance(y, int) and not isinstance(y, bool) for y in v)):
            self.bad(key, f"{v!r} should be two years in square brackets: the first and the last.", example)
            return False
        if not 1900 <= v[0] <= v[1] <= 2100:
            self.bad(key, f"{v} should be two years between 1900 and 2100, the first not after the last.",
                     example)
            return False
        return True


def validate(raw: dict, file=None, lines: dict | None = None) -> list[str]:
    """Plain-language problems with the settings in ``raw`` (an empty list when all is well)."""
    c = _Checker(file, lines or {})
    c.unknown("", raw, list(DEFAULTS))
    for sec in SECTIONS:
        if sec in raw and not isinstance(raw[sec], dict):
            c.bad(sec, f"'{sec}' should hold settings indented below it, like in configs/rice-java.yml.")
        elif sec in raw:
            c.unknown(sec, raw[sec], list(DEFAULTS[sec]))
    g = lambda sec: raw.get(sec) if isinstance(raw.get(sec), dict) else {}  # noqa: E731

    for k, v in g("paths").items():
        if k in PATH_KEYS and v is not None:
            c.text(f"paths.{k}", v, f"{k}: <folder on this computer>", what="a folder, or null")

    aoi = g("aoi")
    if "name" in aoi:
        c.text("aoi.name", aoi["name"], "name: java", r"[A-Za-z0-9_-]{1,20}",
               "letters, digits, '_' or '-', at most 20, no spaces (it is used in file names)")
    if "bbox" in aoi:
        b = aoi["bbox"]
        ex = "bbox: [105.0, -9.0, 114.7, -5.0]"
        if (not isinstance(b, list) or len(b) != 4
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)):
            c.bad("aoi.bbox", f"{b!r} should be four numbers in square brackets: "
                              "west longitude, south latitude, east longitude, north latitude.", ex)
        elif not (-180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90):
            c.bad("aoi.bbox", f"{b} is not a box: the order is west, south, east, north, in degrees "
                              "(west smaller than east, south smaller than north).", ex)
    if "region" in aoi:
        c.text("aoi.region", aoi["region"], "region: Jawa", what="a region name from idn_region_classes.csv")

    tp = g("template")
    if "file" in tp:
        c.text("template.file", tp["file"], "file: templates/XDRI8601.RIX", what="a FileX path")
    if "crop_model" in tp:
        c.text("template.crop_model", tp["crop_model"], "crop_model: RICER048", r"[A-Z0-9]{8}",
               "the 8-character model name of the cultivar file, e.g. RICER048 for RICER048.CUL")

    if "cultivars" in raw:
        cv = raw["cultivars"]
        ex = 'cultivars: {"IR 58": IB0012, "IR 64": IB0015}'
        if not isinstance(cv, dict) or not cv:
            c.bad("cultivars", "list at least one cultivar as  name: code  (the code is the "
                               "6-character INGENO of the cultivar file).", ex)
        else:
            for name, code in cv.items():
                if not isinstance(name, str) or not 0 < len(name) <= 16:
                    c.bad("cultivars", f"cultivar name {name!r} should be text of at most 16 characters.", ex)
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{6}", code):
                    c.bad(f"cultivars.{name}", f"{code!r} should be the 6-character cultivar code "
                                               "(INGENO) from the cultivar file, e.g. IB0012.", ex)

    tr = g("treatments")
    levels = tr.get("nitrogen_levels", DEFAULTS["treatments"]["nitrogen_levels"])
    if "nitrogen_levels" in tr:
        ex = "nitrogen_levels: {0: 0, 35: 1, 70: 2, 105: 3, 140: 4}"
        if not isinstance(levels, dict) or not levels:
            c.bad("treatments.nitrogen_levels", "should map kg N/ha to the fertilizer level (MF) "
                                                "of the template.", ex)
            levels = {}
        else:
            for kg, lv in levels.items():
                if not (isinstance(kg, int) and not isinstance(kg, bool) and kg >= 0):
                    c.bad("treatments.nitrogen_levels", f"{kg!r} should be a whole number of kg N/ha.", ex)
                c.number(f"treatments.nitrogen_levels.{kg}", lv, 0, 99, integer=True, example=ex)
    if "nitrogen_kg" in tr:
        nk = tr["nitrogen_kg"]
        nk = [nk] if isinstance(nk, int) and not isinstance(nk, bool) else nk
        ex = f"nitrogen_kg: [{', '.join(str(k) for k in list(levels)[:2])}]" if levels else "nitrogen_kg: [140]"
        if not isinstance(nk, list) or not nk:
            c.bad("treatments.nitrogen_kg", f"{nk!r} should be a list of N rates in kg N/ha.", ex)
        else:
            for kg in nk:
                if kg not in levels:
                    c.bad("treatments.nitrogen_kg",
                          f"{kg!r} kg N/ha is not one of the rates of the template "
                          f"(allowed: {', '.join(map(str, levels))}). To add a rate, add a fertilizer "
                          "level to the template FileX and list it under nitrogen_levels.", ex)
            if len(set(map(str, nk))) != len(nk):
                c.bad("treatments.nitrogen_kg", "an N rate is listed twice.", ex)
    regimes = tr.get("water_regimes", DEFAULTS["treatments"]["water_regimes"])
    if "water_regimes" in tr:
        ex = "irrigated: {template_level: 1, irrigation_classes: [1, 2, 3]}"
        if not isinstance(regimes, dict) or not regimes:
            c.bad("treatments.water_regimes", "should list each water regime with its template "
                                              "irrigation level and irrigation classes.", ex)
            regimes = {}
        else:
            seen = {}
            for name, w in regimes.items():
                key = f"treatments.water_regimes.{name}"
                if not isinstance(w, dict):
                    c.bad(key, f"{w!r} should hold template_level and irrigation_classes.", ex)
                    continue
                c.unknown(key, w, ["template_level", "irrigation_classes"])
                if "template_level" not in w or "irrigation_classes" not in w:
                    c.bad(key, "needs both template_level and irrigation_classes.", ex)
                    continue
                c.number(f"{key}.template_level", w["template_level"], 1, 99, integer=True, example=ex)
                cls = w["irrigation_classes"]
                if not isinstance(cls, list) or not all(isinstance(x, int) and not isinstance(x, bool)
                                                        for x in cls):
                    c.bad(f"{key}.irrigation_classes", f"{cls!r} should be a list of class numbers "
                                                       "(1-3 irrigated, 4 rainfed in the MoA map).", ex)
                    continue
                for x in cls:
                    if x in seen:
                        c.bad(f"{key}.irrigation_classes", f"class {x} is also given to '{seen[x]}'; "
                                                           "each class belongs to one regime.", ex)
                    seen[x] = name
    if "initial_soil_water" in tr:
        c.number("treatments.initial_soil_water", tr["initial_soil_water"], 0, 1,
                 example="initial_soil_water: 0.9")

    se = g("seasons")
    if "mode" in se and se["mode"] not in MODES:
        c.bad("seasons.mode", f"{se['mode']!r} is not a mode. Choose one of: {', '.join(MODES)}.",
              "mode: per_cell")
    if "year_starts" in se:
        c.month_day("seasons.year_starts", se["year_starts"], 'year_starts: "08-01"')
    if "calendar" in se:
        cal = se["calendar"]
        ex = '- {name: wet, date: "11-15", regimes: [irrigated, rainfed]}'
        if not isinstance(cal, list) or not cal:
            c.bad("seasons.calendar", "should be a list of seasons, each with name, date and regimes.", ex)
        else:
            names = []
            for i, s in enumerate(cal):
                key = f"seasons.calendar.{i + 1}"
                if not isinstance(s, dict):
                    c.bad(key, f"{s!r} should hold name, date and regimes.", ex)
                    continue
                c.unknown(key, s, ["name", "date", "regimes"])
                if not all(k in s for k in ("name", "date", "regimes")):
                    c.bad(key, "each season needs name, date and regimes.", ex)
                    continue
                c.text(f"{key}.name", s["name"], ex, r"[A-Za-z0-9_-]{1,12}",
                       "letters, digits, '_' or '-', at most 12")
                names.append(s["name"])
                c.month_day(f"{key}.date", s["date"], ex)
                rg = s["regimes"]
                if not isinstance(rg, list) or not rg or any(r not in regimes for r in rg):
                    c.bad(f"{key}.regimes", f"{rg!r} should be a list of water regimes from "
                                            f"treatments > water_regimes ({', '.join(regimes)}).", ex)
            if len(set(map(str, names))) != len(names):
                c.bad("seasons.calendar", "two seasons have the same name.", ex)
    on = se.get("onset") if isinstance(se.get("onset"), dict) else {}
    if "onset" in se and not isinstance(se["onset"], dict):
        c.bad("seasons.onset", "should hold the onset rule settings indented below it.")
    c.unknown("seasons.onset", on, list(DEFAULTS["seasons"]["onset"]))
    if "rain_mm" in on:
        c.number("seasons.onset.rain_mm", on["rain_mm"], 0, 1000, example="rain_mm: 50", open_lo=True)
    if "dasarian" in on:
        c.number("seasons.onset.dasarian", on["dasarian"], 1, 36, integer=True, example="dasarian: 3")
    if "transplant_after_days" in on:
        c.number("seasons.onset.transplant_after_days", on["transplant_after_days"], 0, 120,
                 integer=True, example="transplant_after_days: 20")
    if "next_season_after_days" in on:
        c.number("seasons.onset.next_season_after_days", on["next_season_after_days"], 0, 300,
                 integer=True, example="next_season_after_days: 120")
    if on.get("years") is not None:
        c.year_pair("seasons.onset.years", on["years"], "years: [1991, 2020]")
    zt = se.get("zone_table")
    if zt is not None:
        ex = '- {zone: 5, season: wet, date: "12-01"}'
        if isinstance(zt, str):
            pass
        elif isinstance(zt, list) and zt:
            for i, r in enumerate(zt):
                key = f"seasons.zone_table.{i + 1}"
                if not isinstance(r, dict) or set(r) != {"zone", "season", "date"}:
                    c.bad(key, f"{r!r} should hold exactly zone, season and date.", ex)
                    continue
                c.month_day(f"{key}.date", r["date"], ex)
        else:
            c.bad("seasons.zone_table", f"{zt!r} should be a CSV file (zone, season, month_day), "
                                        "a list of rows, or null.", ex)
    if "zone_column" in se:
        c.text("seasons.zone_column", se["zone_column"], "zone_column: aez")

    if "scenarios" in raw:
        scs = raw["scenarios"]
        ex = "- {name: baseline}"
        if not isinstance(scs, list) or not scs:
            c.bad("scenarios", "should be a list with at least one scenario, each starting with '- name:'.", ex)
        else:
            names = []
            for i, s in enumerate(scs):
                _check_scenario(c, i, s, names)
            if len(set(map(str, names))) != len(names):
                c.bad("scenarios", "two scenarios have the same name; each needs its own.", ex)

    if "years" in raw:
        c.year_pair("years", raw["years"], "years: [1991, 2020]")

    rn = g("run")
    if "workers" in rn:
        c.number("run.workers", rn["workers"], 1, 1024, integer=True, example="workers: 32")
    if "chunk_size" in rn:
        c.number("run.chunk_size", rn["chunk_size"], 1, 999, integer=True, example="chunk_size: 200")
    if "years_per_batch" in rn:
        c.number("run.years_per_batch", rn["years_per_batch"], 1, 200, integer=True,
                 example="years_per_batch: 5")
    if rn.get("sample") is not None:
        c.number("run.sample", rn["sample"], 1, None, integer=True, example="sample: 30")
    if "seed" in rn:
        c.number("run.seed", rn["seed"], 0, None, integer=True, example="seed: 1")
    if "season_days" in rn:
        c.number("run.season_days", rn["season_days"], 60, 400, integer=True, example="season_days: 220")
    if rn.get("output_folder") is not None:
        c.text("run.output_folder", rn["output_folder"], "output_folder: E:/temp/xdssat/runs", what="a folder")
    return c.problems


def _check_scenario(c: _Checker, i: int, s, names: list):
    key = f"scenarios.{i + 1}"
    ex = "- {name: warmer, environment: {tmax: {add: 2}, tmin: {add: 2}}}"
    if not isinstance(s, dict):
        c.bad(key, f"{s!r} should be a scenario starting with '- name:'.", ex)
        return
    c.unknown(key, s, list(SCENARIO_KEYS))
    if "name" not in s:
        c.bad(key, "every scenario needs a name.", ex)
    else:
        c.text(f"{key}.name", s["name"], "name: co2_550", r"[A-Za-z0-9_.-]{1,30}",
               "letters, digits, '.', '_' or '-', at most 30, no spaces (it is used in folder names)")
        names.append(s["name"])
    env = s.get("environment")
    if env is not None:
        if not isinstance(env, dict):
            c.bad(f"{key}.environment", "should list what to change, e.g. co2: {replace: 550}.", ex)
        else:
            c.unknown(f"{key}.environment", env, list(ENVIRONMENT))
            for var, how in env.items():
                k = f"{key}.environment.{var}"
                if var not in ENVIRONMENT:
                    continue
                if not isinstance(how, dict) or len(how) != 1 or next(iter(how)) not in CHANGE:
                    c.bad(k, f"{how!r} should say how to change {var}: one of "
                             f"{', '.join(CHANGE)} with a number.", f"{var}: {{add: 2}}")
                    continue
                v = next(iter(how.values()))
                if c.number(k, v, example=f"{var}: {{add: 2}}"):
                    txt = _num4(v)
                    if txt is None:
                        c.bad(k, f"{v} does not fit the 4 characters the engine reads "
                                 "(IPENV.for:1000); use fewer digits.", f"{var}: {{add: 1.5}}")
    co = s.get("cultivar_coefficients")
    if co is not None:
        exc = "cultivar_coefficients: {TCLDF: 19}"
        if not isinstance(co, dict) or not co:
            c.bad(f"{key}.cultivar_coefficients", "should map a coefficient name to a value, for every "
                                                  "cultivar, or a cultivar name to such a map.", exc)
        else:
            for name, v in co.items():
                if isinstance(v, dict):
                    for coef, x in v.items():
                        c.text(f"{key}.cultivar_coefficients.{name}", coef, exc, r"[A-Z0-9#]{1,6}",
                               "a coefficient name as in the cultivar file header, e.g. TCLDF")
                        c.number(f"{key}.cultivar_coefficients.{name}.{coef}", x, example=exc)
                else:
                    c.text(f"{key}.cultivar_coefficients", name, exc, r"[A-Z0-9#]{1,6}",
                           "a coefficient name as in the cultivar file header, e.g. TCLDF")
                    c.number(f"{key}.cultivar_coefficients.{name}", v, example=exc)
    for k in ("slpf", "slpf_rainfed"):
        if s.get(k) is not None:
            c.number(f"{key}.{k}", s[k], 0, 1, example=f"{k}: 0.76", open_lo=True)
    if s.get("slpf_table") is not None:
        c.text(f"{key}.slpf_table", s["slpf_table"], "slpf_table: E:/temp/xdssat/checks/slpf_table.csv",
               what="a CSV file with columns iy, ix, water, slpf")
        if s.get("slpf") not in (None, 1, 1.0) or s.get("slpf_rainfed") is not None:
            c.bad(f"{key}.slpf_table", "slpf_table gives SLPF per stratum and replaces slpf and "
                                       "slpf_rainfed; set only one of them.", "slpf_table: slpf_table.csv")
    if "description" in s and not isinstance(s["description"], str):
        c.bad(f"{key}.description", "should be text.", 'description: "CO2 at 550 ppm"')


def check_files(exp: Experiment) -> list[str]:
    """Problems that need the template and the cultivar file (both must exist)."""
    import filex
    c = _Checker(exp.file, exp.lines)
    tf = exp.template_file
    if not tf.is_file():
        c.bad("template.file", f"the template FileX {tf} does not exist.",
              "file: templates/XDRI8601.RIX")
        return c.problems
    t = filex.FileX.read(tf)
    trt = t.treatments()
    mf, mi = set(t.blocks("MF")) | {0}, set(t.blocks("MI"))
    for kg, lv in exp.data["treatments"]["nitrogen_levels"].items():
        if lv not in mf:
            c.bad(f"treatments.nitrogen_levels.{kg}", f"the template has no fertilizer level {lv} "
                  f"(it has {', '.join(map(str, sorted(mf)))}; 0 means no fertilizer).")
    for name, w in exp.data["treatments"]["water_regimes"].items():
        if w["template_level"] not in mi:
            c.bad(f"treatments.water_regimes.{name}.template_level",
                  f"the template has no irrigation level {w['template_level']} "
                  f"(it has {', '.join(map(str, sorted(mi)))}).")
    if not c.problems:
        for name in exp.data["treatments"]["water_regimes"]:
            for kg in exp.data["treatments"]["nitrogen_kg"]:
                sel = trt[(trt.MI == exp.water_level(name)) & (trt.MF == exp.n_level(kg))]
                if sel.empty:
                    c.bad("treatments.nitrogen_kg", f"the template has no treatment with irrigation level "
                          f"{exp.water_level(name)} ({name}) and fertilizer level {exp.n_level(kg)} "
                          f"({kg} kg N); add one to {tf.name}.")
    if any(s.get("environment") for s in exp.data["scenarios"]) and (trt.ME != 0).any():
        c.bad("scenarios", f"{tf.name} already has ENVIRONMENT MODIFICATIONS in its treatments; "
                           "a scenario cannot add its own. Remove them from the template.")
    for key in RUN_PATHS:
        if not exp.has_path(key):
            c.bad(f"paths.{key}", path_not_set_message(exp, key).split("\n")[0].replace(
                "this step needs it", "a run needs it"),
                f"{key}: <folder on this computer>  (or set {PATH_KEYS[key][0]})")
    cul = (exp.path("engine") / "Genotype" / f"{exp.data['template']['crop_model']}.CUL"
           if exp.has_path("engine") else None)
    if cul is not None and cul.is_file():
        text = cul.read_text(encoding="latin-1")
        codes = {ln[:6] for ln in text.splitlines() if ln[:1] not in ("@", "!", "*", "$", "") and ln.strip()}
        header = next((ln.split() for ln in text.splitlines() if ln.startswith("@VAR#")), [])
        for name, code in exp.data["cultivars"].items():
            if code not in codes:
                c.bad(f"cultivars.{name}", f"{code} is not in {cul.name}; check the INGENO in that file.")
        for i, s in enumerate(exp.data["scenarios"]):
            for k, v in (s.get("cultivar_coefficients") or {}).items():
                for coef in (v if isinstance(v, dict) else {k: v}):
                    if coef not in header:
                        c.bad(f"scenarios.{i + 1}.cultivar_coefficients",
                              f"{coef} is not a coefficient of {cul.name} "
                              f"(it has {', '.join(h for h in header[4:])}).")
                if isinstance(v, dict) and k not in exp.data["cultivars"]:
                    c.bad(f"scenarios.{i + 1}.cultivar_coefficients",
                          f"'{k}' is not one of the cultivars ({', '.join(exp.data['cultivars'])}).")
    for i, s in enumerate(exp.data["scenarios"]):
        if s.get("slpf_table") and not Path(s["slpf_table"]).is_file():
            c.bad(f"scenarios.{i + 1}.slpf_table", f"{s['slpf_table']} does not exist.")
    zt = exp.data["seasons"]["zone_table"]
    if isinstance(zt, str) and not Path(zt).is_file():
        c.bad("seasons.zone_table", f"{zt} does not exist.")
    return c.problems


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _abs(p) -> Path:
    p = Path(os.path.expanduser(str(p)))
    return p if p.is_absolute() else (ROOT / p)


def _num4(v) -> str | None:
    """``v`` as the engine's F4.0 field (4 characters), or None if it does not fit."""
    v = float(v)
    s = str(int(v)) if v.is_integer() else f"{v:.3g}"
    while len(s) > 4 and "." in s:
        s = s[:-1]
    s = s.rstrip(".")
    return s if len(s) <= 4 else None


def _merge(base: dict, top: dict, whole_from=()) -> dict:
    out = copy.deepcopy(base)
    for k, v in top.items():
        if k in SECTIONS and isinstance(v, dict) and isinstance(out.get(k), dict):
            for kk, vv in v.items():
                if kk == "onset" and isinstance(vv, dict) and isinstance(out[k].get(kk), dict):
                    out[k][kk] = {**out[k][kk], **copy.deepcopy(vv)}
                else:
                    out[k][kk] = copy.deepcopy(vv)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _normalise(d: dict):
    tr = d["treatments"]
    if isinstance(tr["nitrogen_kg"], int):
        tr["nitrogen_kg"] = [tr["nitrogen_kg"]]
    d["scenarios"] = [{"name": s["name"], **{k: v for k, v in s.items() if k != "name"}}
                      for s in d["scenarios"]]
    for s in d["scenarios"]:
        if s.get("slpf_table"):
            s["slpf_table"] = _abs(s["slpf_table"]).as_posix()
    zt = d["seasons"]["zone_table"]
    if isinstance(zt, str):
        d["seasons"]["zone_table"] = _abs(zt).as_posix()
    if d["run"]["output_folder"]:
        d["run"]["output_folder"] = _abs(d["run"]["output_folder"]).as_posix()
    tp = d["template"]
    tp["file"] = _abs(tp["file"]).as_posix()


def _keys(d: dict, prefix: str = "") -> list[str]:
    out = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        out.append(key)
        if isinstance(v, dict) and (prefix == "" and k in SECTIONS or prefix.count(".") == 1 and k == "onset"):
            out += _keys(v, key + ".")
    return out


def _node_lines(node, prefix: str, out: dict):
    import yaml
    if isinstance(node, yaml.MappingNode):
        for kn, vn in node.value:
            key = f"{prefix}{kn.value}"
            out[key] = kn.start_mark.line + 1
            _node_lines(vn, key + ".", out)
    elif isinstance(node, yaml.SequenceNode):
        for i, vn in enumerate(node.value):
            key = f"{prefix}{i + 1}"
            out[key] = vn.start_mark.line + 1
            _node_lines(vn, key + ".", out)


def engine_exe(engine_dir: Path | None = None) -> Path:
    """Path of the engine executable; raises if it is missing."""
    exe = Path(engine_dir or ENGINE_DIR) / EXE_NAME
    if not exe.is_file():
        raise FileNotFoundError(
            f"DSSAT engine not found at {exe}. Build it with `python scripts/build_engine.py`, "
            f"or correct paths > engine (now from {CURRENT.where('paths.engine')}).")
    return exe


def check_short_path(path: Path, limit: int = MAX_RUN_DIR_LEN) -> Path:
    """Raise if ``path`` is too long for the engine's fixed path buffers."""
    p = str(Path(path).resolve())
    if len(p) > limit:
        raise ValueError(
            f"Path {p!r} is {len(p)} characters; the DSSAT engine needs <= {limit}. "
            "Use a short work root such as C:/TEMP/dsn/work (paths > work in the experiment file).")
    return Path(p)


CURRENT: Experiment
SETTINGS: dict
use(_load_at_import())

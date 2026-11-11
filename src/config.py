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
engine keeps file paths in fixed ``CHARACTER*80`` to ``*102`` buffers
(``CSM_Main/CSM.for:96-97`` and ``Weather/IPWTH_alt.for:60-61`` in the engine source).

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
    # the folder holding inputs/<kind>/<name>/ (prepared datasets); null = the cache folder
    "datasets": ("XDSSAT_DATASETS", "DATASETS_DIR"),
}
# Required by run_aoi.py; the others are needed only to prepare inputs.
RUN_PATHS = ("engine", "work", "cache")

# The default experiment: the Java case study. Keep in step with configs/rice-java.yml.
# No folder has a default: every machine sets its own (experiment file or environment).
DEFAULTS: dict = {
    "paths": {key: None for key in PATH_KEYS},
    "aoi": {"name": "java", "bbox": [105.0, -9.0, 114.7, -5.0], "region": "Jawa"},
    # Prepared datasets in <paths > cache>/inputs/<kind>/<name>/ (src/inputs.py); names only.
    "inputs": {"weather": "agera5_idn", "crop_mask": "rice_atrbpn_idn", "soil": "soilgrids_java",
               "water_regime": "irrigation_moa_idn", "region": "islands_bbsdlp_idn",
               "zones": "aez_bbsdlp_idn"},
    # How to make prepared inputs from the user's own files (scripts/prepare_*.py); null = not used.
    "prepare": {"weather": None, "crop_mask": None, "water_regime": None, "region": None, "zones": None,
                "soil": {"name": None, "method": "soilgrids", "soc_scale": "wise_indonesia",
                         "country": "Indonesia"}},
    "template": {"file": "templates/XDRI8601.RIX", "crop_model": "RICER048"},
    "cultivars": {"IR 58": "IB0012"},
    "treatments": {
        "nitrogen_kg": [140],
        "nitrogen_levels": {0: 0, 35: 1, 70: 2, 105: 3, 140: 4},
        "water_regimes": {
            "irrigated": {"template_level": 1, "irrigation_classes": [1, 2, 3]},
            "rainfed": {"template_level": 3, "irrigation_classes": [4]},
        },
        "default_regime": None,
        "share_regimes": {"share": "irrigated", "rest": "rainfed"},
        "initial_soil_water": 0.9,
    },
    "seasons": {
        "mode": "uniform",
        "year_starts": "08-01",
        "calendar": [
            {"name": "wet", "date": "11-15", "regimes": ["irrigated", "rainfed"]},
            {"name": "dry", "date": "04-01", "regimes": ["irrigated"]},
        ],
        "onset": {"rain_mm": 50, "dasarian": 3, "planting_after_days": 20,
                  "next_season_after_days": 120, "years": None},
        "zone_table": None,
        "zone_column": "aez",
    },
    "scenarios": [{"name": "baseline"}],
    "years": [1991, 2020],
    "run": {"workers": 32, "chunk_size": 200, "years_per_batch": 5, "sample": None,
            "seed": 1, "season_days": 220, "output_folder": None, "daily": None},
}

SECTIONS = ("paths", "aoi", "inputs", "prepare", "template", "treatments", "seasons", "run")   # key by key
INPUT_OPTIONAL = ("zones", "water_regime", "region")
WHOLE = ("cultivars", "scenarios", "years")                               # replaced whole
MODES = ("uniform", "per_cell", "by_zone")
# scenario environment key -> FileX *ENVIRONMENT MODIFICATIONS column (IPENV.for:1000)
ENVIRONMENT = {"daylength": "EDAY", "radiation": "ERAD", "tmax": "EMAX", "tmin": "EMIN",
               "rain": "ERAIN", "co2": "ECO2", "dewpoint": "EDEW", "wind": "EWIND"}
# how to change it -> engine code (Weather/WTHMOD.for WTHSET: A add, S subtract, M multiply, R replace)
CHANGE = {"add": "A", "subtract": "S", "multiply": "M", "replace": "R"}
# run > daily: the engine's daily CSV groups (Data/OUTPUT.CDE, daily csv format files)
DAILY_GROUPS = ("plantgro", "plantn", "plantc", "plantgr2", "plantgrf", "soilwat", "soiltemp", "et",
                "soilni", "weather", "soilorg", "etphot", "mulch", "plantp", "soilpi", "n2o")
SCENARIO_KEYS = ("name", "description", "environment", "cultivar_coefficients",
                 "slpf", "slpf_rainfed", "slpf_table", "simulation", "management",
                 "planting_offset_days")
# scenario > simulation: a column of the template's *SIMULATION CONTROLS -> its allowed codes
# (None: a number). Codes from the engine's reader (IPSIM.for) and Data/SIMULATION.CDE.
# Left out on purpose: WTHER W/S/G (need .CLI/.WTG files xdssat does not make), DISES (needs
# pest files), management code D (the template's dates would then be read as days after
# planting), and the dates, years and outputs xdssat sets itself.
SIMULATION_FIELDS = {
    # OPTIONS
    "WATER": "YN", "NITRO": "YN", "SYMBI": "YNU", "PHOSP": "YN", "POTAS": "YN", "CHEM": "YN",
    "TILL": "YN", "CO2": "WMD",
    # METHODS
    "EVAPO": "FRSTZ", "INFIL": "RSN", "PHOTO": "CRL", "MESOM": "GP", "MESEV": "RS", "MESOL": "123",
    "METMP": "DRE", "MEGHG": "01", "NSWIT": None,
    # MANAGEMENT
    "PLANT": "AFR", "IRRIG": "ANRFPW", "FERTI": "NR", "RESID": "NR", "HARVS": "AMR",
    # AUTOMATIC MANAGEMENT: planting, irrigation, harvest
    "PH2OL": None, "PH2OU": None, "PH2OD": None, "PSTMX": None, "PSTMN": None,
    "IMDEP": None, "ITHRL": None, "ITHRU": None, "IRAMT": None, "IREFF": None,
    "HPCNP": None, "HPCNR": None,
}
# scenario > management: single-row fields of a factor level, as "<factor>.<column>"
MANAGEMENT_FIELDS = {
    "MP": ("PPOP", "PPOE", "PLME", "PLDS", "PLRS", "PLRD", "PLDP", "PLWT", "PAGE", "PENV", "PLPH", "SPRL"),
    "IC": ("ICRT", "ICND", "ICRN", "ICRE", "ICWD", "ICRES", "ICREN", "ICREP", "ICRIP", "ICRID"),
}


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

    def datasets(self) -> Path:
        """The folder that holds ``inputs/<kind>/<name>/``: ``paths > datasets``, else the cache."""
        return self.path("datasets") if self.has_path("datasets") else self.path("cache")

    def has_datasets(self) -> bool:
        return self.has_path("datasets") or self.has_path("cache")

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
    def region(self) -> str | None:
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
    _rename_old(raw, lines, file)
    if overrides:
        _rename_old(overrides, {}, None)
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


# Keys renamed when xdssat went beyond rice: old name -> new name. Files and run folders
# written with the old name still load; giving both names is an error.
RENAMED = {"seasons.onset.transplant_after_days": "seasons.onset.planting_after_days"}


def _rename_old(raw: dict, lines: dict, file) -> None:
    """Move settings written under an old key name to the new name (in place)."""
    for old, new in RENAMED.items():
        *parents, o = old.split(".")
        n = new.split(".")[-1]
        d = raw
        for k in parents:
            d = d.get(k) if isinstance(d, dict) else None
        if not isinstance(d, dict) or o not in d:
            continue
        if n in d:
            raise ConfigError([f"{new} and its old name {o} are both set; keep only {n}."], file)
        d[n] = d.pop(o)
        if old in lines:
            lines[new] = lines.pop(old)


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
    if not exp.has_path("datasets") and exp.has_path("cache"):
        g["DATASETS_DIR"] = exp.path("cache")
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
    if aoi.get("region") is not None:
        c.text("aoi.region", aoi["region"], "region: Jawa", what="a class name of the region input, or null")
    ins_ = g("inputs")
    if ins_.get("region", DEFAULTS["inputs"]["region"]) is None and \
            aoi.get("region", DEFAULTS["aoi"]["region"]) is not None:
        c.bad("aoi.region", f"'{aoi.get('region', DEFAULTS['aoi']['region'])}' names a region, but there is no "
                            "region input (inputs > region is null), so aoi > bbox alone decides which crop "
                            "counts. Set region: null, or name a region input.", "region: null")

    for k, v in g("inputs").items():
        if k not in DEFAULTS["inputs"]:
            continue
        if v is None and k in INPUT_OPTIONAL:
            continue
        if v is None:
            c.bad(f"inputs.{k}", f"a run needs a {k.replace('_', ' ')} input; null is allowed only for "
                                 f"{', '.join(INPUT_OPTIONAL)}.", f"{k}: {DEFAULTS['inputs'][k]}")
        else:
            c.text(f"inputs.{k}", v, f"{k}: {DEFAULTS['inputs'][k]}", r"[A-Za-z0-9_.-]{1,60}",
                   "the name of a prepared dataset: letters, digits, '.', '_' or '-'")

    pw = g("prepare").get("weather")
    if pw is not None:
        _check_prepare_weather(c, pw)
    pc = g("prepare").get("crop_mask")
    if pc is not None:
        _check_prepare_crop_mask(c, pc)
    ps = g("prepare").get("soil")
    if ps is not None:
        _check_prepare_soil(c, ps)
    for kind in ("water_regime", "region", "zones"):
        pl = g("prepare").get(kind)
        if pl is not None:
            _check_prepare_layer(c, kind, pl)

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
                c.number(f"{key}.template_level", w["template_level"], 0, 99, integer=True, example=ex)
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
    if tr.get("default_regime") is not None and isinstance(regimes, dict) and tr["default_regime"] not in regimes:
        c.bad("treatments.default_regime", f"{tr['default_regime']!r} should be one of the water regimes "
                                           f"({', '.join(regimes)}), or null.", "default_regime: rainfed")
    if "share_regimes" in tr:
        sr = tr["share_regimes"]
        ex = "share_regimes: {share: irrigated, rest: rainfed}"
        if not isinstance(sr, dict) or set(sr) != {"share", "rest"}:
            c.bad("treatments.share_regimes", "should name the regime of the share and of the rest.", ex)
        elif isinstance(regimes, dict):
            for k, v in sr.items():
                if v not in regimes:
                    c.bad(f"treatments.share_regimes.{k}", f"{v!r} should be one of the water regimes "
                                                           f"({', '.join(regimes)}).", ex)
            if sr["share"] == sr["rest"]:
                c.bad("treatments.share_regimes", "share and rest should be two different regimes.", ex)
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
    if "planting_after_days" in on:
        c.number("seasons.onset.planting_after_days", on["planting_after_days"], 0, 120,
                 integer=True, example="planting_after_days: 20")
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
        c.text("run.output_folder", rn["output_folder"], "output_folder: <cache>/runs", what="a folder")
    if rn.get("daily") is not None:
        exd = "daily: [plantgro, soilwat]"
        dg = rn["daily"]
        if not isinstance(dg, list) or not dg or any(g not in DAILY_GROUPS for g in dg):
            c.bad("run.daily", f"{dg!r} should be a list of daily output groups: "
                               f"{', '.join(DAILY_GROUPS)}; or null for none.", exd)
    return c.problems


PREPARE_WEATHER_KEYS = ("name", "description", "source", "box", "grid_step", "years", "station_code",
                        "temperature_height_m", "variables", "points", "utc_offset_hours", "wind_height_m")
SOURCE_KEYS = ("files", "variable", "units", "regrid", "height_m", "daily")
POINT_KEYS = ("file", "lat", "lon", "columns", "units", "missing", "date_format")
DAILY_HOW = ("max", "min", "mean", "sum", "accumulated")
REGRID = ("exact", "mean", "linear", "nearest")


PREPARE_CROP_KEYS = ("name", "description", "source", "files", "values", "classes", "aggregate", "split",
                     "box", "layer", "class_column", "select", "pixel_m", "subpixels")
CROP_VALUES = ("classes", "fraction", "percent", "ha", "m2", "km2", "polygons")


PREPARE_SOIL_KEYS = ("name", "description", "source", "method", "soc_scale", "country", "file", "map",
                     "classes", "id_column", "layer", "profile")
SOIL_DEPTHS = ("0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm")


PREPARE_LAYER_KEYS = ("name", "description", "source", "files", "values", "classes", "class_column", "layer",
                      "pixel_m", "box", "irrigated", "total")


def _check_prepare_layer(c: _Checker, kind: str, pl):
    key = f"prepare.{kind}"
    ex = f"{kind}: {{name: my_{kind}, files: ..., values: classes}}"
    if not isinstance(pl, dict):
        c.bad(key, "should hold name, files and values indented below it, or be null.", ex)
        return
    c.unknown(key, pl, list(PREPARE_LAYER_KEYS))
    if "name" not in pl:
        c.bad(key, f"needs a name: the {kind.replace('_', ' ')} input to write.", f"name: my_{kind}")
    else:
        c.text(f"{key}.name", pl["name"], f"name: my_{kind}", r"[A-Za-z0-9_.-]{1,60}",
               "letters, digits, '.', '_' or '-'")
    v = pl.get("values", "classes")
    allowed = ("classes", "fraction", "percent", "ratio") if kind == "water_regime" else ("classes",)
    if v not in allowed:
        c.bad(f"{key}.values", f"{v!r} should be {' or '.join(allowed)}"
                               + (": classes for a class map, fraction or percent for an irrigated-share "
                                  "raster, ratio for irrigated / total area rasters." if kind == "water_regime"
                                  else " (a class map)."), "values: classes")
        return
    if v == "ratio":
        for k in ("irrigated", "total"):
            if not pl.get(k):
                c.bad(f"{key}.{k}", f"values: ratio needs {k}: the raster of {k} crop area.",
                      f'{k}: "{{inputs}}/spam/..."')
        if pl.get("files"):
            c.bad(f"{key}.files", "values: ratio takes irrigated and total, not files.")
    else:
        if not pl.get("files"):
            c.bad(f"{key}.files", "needs files: the map (a raster, or for classes also a vector file).",
                  'files: "{inputs}/maps/irrigation.tif"')
        for k in ("irrigated", "total"):
            if pl.get(k):
                c.bad(f"{key}.{k}", f"{k} is used only with values: ratio.")
    if v != "classes":
        for k in ("classes", "class_column", "layer", "pixel_m"):
            if pl.get(k) is not None:
                c.bad(f"{key}.{k}", f"{k} is used only with values: classes.")
    elif str(pl.get("files", "")).lower().endswith((".shp", ".gpkg", ".geojson", ".json", ".parquet", ".fgb",
                                                     ".gdb", ".kml")) and not pl.get("class_column"):
        c.bad(f"{key}.class_column", "a vector map needs class_column: the column holding the class.",
              "class_column: IRRIGATION")
    if pl.get("pixel_m") is not None:
        c.number(f"{key}.pixel_m", pl["pixel_m"], 5, 10000, example="pixel_m: 100")
    if pl.get("box") is not None:
        b = pl["box"]
        if (not isinstance(b, list) or len(b) != 4
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)
                or not (b[0] < b[2] and b[1] < b[3])):
            c.bad(f"{key}.box", f"{b!r} should be [west, south, east, north] in degrees, or null for "
                                "aoi > bbox.", "box: [105.0, -9.0, 114.7, -5.0]")


def _check_prepare_soil(c: _Checker, ps):
    key = "prepare.soil"
    ex = "soil: {name: hc27_java, method: sol, file: ..., profile: HC_GEN0011}"
    if not isinstance(ps, dict):
        c.bad(key, "should hold method and its settings indented below it, or be null.", ex)
        return
    c.unknown(key, ps, list(PREPARE_SOIL_KEYS))
    m = ps.get("method", "soilgrids")
    if m not in ("soilgrids", "sol"):
        c.bad(f"{key}.method", f"{m!r} should be soilgrids (SoilGrids 2.0 by pedotransfer) or sol (your own "
                               ".SOL file of profiles).", "method: sol")
        return
    if ps.get("name") is not None:
        c.text(f"{key}.name", ps["name"], "name: hc27_java", r"[A-Za-z0-9_.-]{1,60}",
               "letters, digits, '.', '_' or '-'")
    if "country" in ps:
        c.text(f"{key}.country", ps["country"], "country: Indonesia", r".{1,11}", "at most 11 characters")
    if m == "soilgrids":
        for k in ("file", "map", "classes", "id_column", "layer", "profile"):
            if ps.get(k) is not None:
                c.bad(f"{key}.{k}", f"{k} is used only with method: sol.")
        s = ps.get("soc_scale", "none")
        if isinstance(s, dict):
            c.unknown(f"{key}.soc_scale", s, list(SOIL_DEPTHS))
            for d in SOIL_DEPTHS:
                if d not in s:
                    c.bad(f"{key}.soc_scale", f"a factor for every SoilGrids depth is needed; {d} is missing.",
                          "soc_scale: {0-5cm: 0.7, 5-15cm: 0.7, 15-30cm: 0.7, 30-60cm: 0.63, 60-100cm: 0.63, "
                          "100-200cm: 0.47}")
                else:
                    c.number(f"{key}.soc_scale.{d}", s[d], 0, 3, open_lo=True, example=f"{d}: 0.7")
        elif s not in ("wise_indonesia", "none", None):
            c.bad(f"{key}.soc_scale", f"{s!r} should be wise_indonesia (factors fitted to WISE profiles of "
                                      "Indonesia), none (SoilGrids as is) or six factors by depth.", "soc_scale: none")
        return
    # method: sol
    if ps.get("soc_scale") not in (None, "none"):
        c.bad(f"{key}.soc_scale", "soc_scale is used only with method: soilgrids.")
    if not ps.get("name"):
        c.bad(f"{key}.name", "method: sol needs a name: the soil input to write (then use it in inputs > soil).",
              "name: hc27_java")
    if not ps.get("file"):
        c.bad(f"{key}.file", "method: sol needs file: the .SOL file holding the profiles.",
              'file: "{inputs}/soils/HC.SOL"')
    else:
        c.text(f"{key}.file", ps["file"], 'file: "{inputs}/soils/HC.SOL"', what="a .SOL file path")
    has_map, has_prof = ps.get("map") is not None, ps.get("profile") is not None
    if has_map == has_prof:
        c.bad(key, "method: sol needs either map (which profile where) or profile (one profile for every "
                   "cell), not both and not neither.", "profile: HC_GEN0011")
    if has_prof:
        c.text(f"{key}.profile", ps["profile"], "profile: HC_GEN0011", r".{10}",
               "the 10-character profile ID after * in the .SOL file")
    if has_map:
        mp = str(ps["map"]).lower()
        vector = mp.endswith((".shp", ".gpkg", ".geojson", ".json", ".parquet", ".fgb", ".gdb", ".kml"))
        if vector and not ps.get("id_column"):
            c.bad(f"{key}.id_column", "a vector soil map needs id_column: the column holding the profile ID.",
                  "id_column: SOIL_ID")
        if not vector and not ps.get("classes"):
            c.bad(f"{key}.classes", "a raster soil map needs classes: a CSV with columns code and soil_id.",
                  'classes: "{inputs}/soilmap/codes.csv"')
    elif any(ps.get(k) is not None for k in ("classes", "id_column", "layer")):
        c.bad(key, "classes, id_column and layer go with map.")


def _check_prepare_crop_mask(c: _Checker, pc):
    key = "prepare.crop_mask"
    ex = "crop_mask: {name: cropland_java, files: ..., values: classes, classes: [40], aggregate: 10}"
    if not isinstance(pc, dict):
        c.bad(key, "should hold name, files and values indented below it, or be null.", ex)
        return
    c.unknown(key, pc, list(PREPARE_CROP_KEYS))
    for k, e in (("name", "name: cropland_java"), ("files", 'files: "{inputs}/worldcover/*_Map.tif"'),
                 ("values", "values: classes")):
        if k not in pc:
            c.bad(key, f"needs {k}.", e)
    if "name" in pc:
        c.text(f"{key}.name", pc["name"], "name: cropland_java", r"[A-Za-z0-9_.-]{1,60}",
               "letters, digits, '.', '_' or '-'")
    if "files" in pc:
        c.text(f"{key}.files", pc["files"], 'files: "{inputs}/worldcover/*_Map.tif"',
               what="a file path or pattern (* for several tiles; {inputs} etc. are the paths section)")
    v = pc.get("values")
    if v is not None and v not in CROP_VALUES:
        c.bad(f"{key}.values", f"{v!r} should be one of {', '.join(CROP_VALUES)}: classes for a land-cover map, "
                               "fraction or percent for a crop share, ha, m2 or km2 for a crop area per pixel, "
                               "polygons for a vector file of fields.", "values: classes")
    cls = pc.get("classes")
    if v == "classes":
        if not isinstance(cls, list) or not cls or not all(isinstance(x, int) and not isinstance(x, bool)
                                                           for x in cls):
            c.bad(f"{key}.classes", "values: classes needs the list of crop class codes of the map, e.g. "
                                    "[40] for ESA WorldCover Cropland or [12, 14] for MODIS MCD12Q1 IGBP.",
                  "classes: [40]")
    elif cls:
        c.bad(f"{key}.classes", f"classes is used only with values: classes (here values is {v!r}).")
    for k in ("aggregate", "split"):
        if pc.get(k) is not None:
            c.number(f"{key}.{k}", pc[k], 1, 1000, integer=True, example=f"{k}: 10")
    if (pc.get("aggregate") or 1) > 1 and (pc.get("split") or 1) > 1:
        c.bad(f"{key}.split", "use aggregate (sum source pixels into larger ones) or split (cut each source "
                              "pixel into smaller ones), not both.")
    if v == "polygons" and (pc.get("aggregate") or pc.get("split")):
        c.bad(key, "aggregate and split are for rasters; for polygons set pixel_m (the output pixel size).")
    if v != "polygons":
        for k in ("layer", "class_column", "select", "pixel_m", "subpixels"):
            if pc.get(k) is not None:
                c.bad(f"{key}.{k}", f"{k} is used only with values: polygons.")
    if pc.get("pixel_m") is not None:
        c.number(f"{key}.pixel_m", pc["pixel_m"], 5, 10000, example="pixel_m: 100")
    if pc.get("subpixels") is not None:
        c.number(f"{key}.subpixels", pc["subpixels"], 2, 20, integer=True, example="subpixels: 5")
    if pc.get("select") is not None and not isinstance(pc["select"], dict):
        c.bad(f"{key}.select", "should map a column to the values to keep.", "select: {landuse: [sawah]}")
    if pc.get("box") is not None:
        b = pc["box"]
        if (not isinstance(b, list) or len(b) != 4
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)
                or not (b[0] < b[2] and b[1] < b[3])):
            c.bad(f"{key}.box", f"{b!r} should be [west, south, east, north] in degrees, or null for "
                                "aoi > bbox.", "box: [105.0, -9.0, 114.7, -5.0]")


def _check_prepare_weather(c: _Checker, pw):
    key = "prepare.weather"
    ex = "weather: {name: era5land_java, variables: {TMAX: {files: ..., variable: ...}, ...}}"
    if not isinstance(pw, dict):
        c.bad(key, "should hold name and variables indented below it, or be null.", ex)
        return
    c.unknown(key, pw, list(PREPARE_WEATHER_KEYS))
    if "name" not in pw:
        c.bad(key, "needs a name: the name of the weather dataset to make (then use it in inputs > weather).",
              "name: era5land_java")
    else:
        c.text(f"{key}.name", pw["name"], "name: era5land_java", r"[A-Za-z0-9_.-]{1,60}",
               "letters, digits, '.', '_' or '-'")
    if pw.get("box") is not None:
        b = pw["box"]
        if (not isinstance(b, list) or len(b) != 4
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)
                or not (b[0] < b[2] and b[1] < b[3])):
            c.bad(f"{key}.box", f"{b!r} should be [west, south, east, north] in degrees, or null for "
                                "aoi > bbox.", "box: [105.0, -9.0, 114.7, -5.0]")
    if pw.get("grid_step") is not None:
        c.number(f"{key}.grid_step", pw["grid_step"], 0, 5, example="grid_step: 0.1", open_lo=True)
    if pw.get("years") is not None:
        c.year_pair(f"{key}.years", pw["years"], "years: [1990, 2021]")
    if "station_code" in pw:
        c.text(f"{key}.station_code", pw["station_code"], "station_code: XDWX", r"[A-Za-z0-9]{4}",
               "4 letters or digits (the institute code in the weather files)")
    if "temperature_height_m" in pw:
        c.number(f"{key}.temperature_height_m", pw["temperature_height_m"], 0, 100,
                 example="temperature_height_m: 2", open_lo=True)
    if pw.get("utc_offset_hours") is not None:
        c.number(f"{key}.utc_offset_hours", pw["utc_offset_hours"], -12, 14, example="utc_offset_hours: 7")
    if pw.get("wind_height_m") is not None:
        c.number(f"{key}.wind_height_m", pw["wind_height_m"], 0, 100, example="wind_height_m: 2", open_lo=True)
    if pw.get("points") is not None:
        if pw.get("variables"):
            c.bad(key, "give either variables (gridded files) or points (stations, NASA POWER points), not both.")
        if not pw.get("grid_step"):
            c.bad(f"{key}.grid_step", "points need grid_step: the cell size of the weather grid the points "
                                      "are spread on.", "grid_step: 0.1")
        pts = pw["points"]
        exp_ = '- {file: "<folder>/station.WTH"}'
        if not isinstance(pts, list) or not pts:
            c.bad(f"{key}.points", "should be a list of point sources, each with file.", exp_)
            return
        for i, q in enumerate(pts, 1):
            k = f"{key}.points.{i}"
            if not isinstance(q, dict) or "file" not in q:
                c.bad(k, "each point needs file: a DSSAT .WTH file or a CSV table.", exp_)
                continue
            c.unknown(k, q, list(POINT_KEYS))
            c.text(f"{k}.file", q["file"], exp_, what="a file name")
            if "lat" in q:
                c.number(f"{k}.lat", q["lat"], -90, 90, example="lat: -6.9")
            if "lon" in q:
                c.number(f"{k}.lon", q["lon"], -180, 180, example="lon: 107.6")
            for d in ("columns", "units"):
                if q.get(d) is not None:
                    if not isinstance(q[d], dict):
                        c.bad(f"{k}.{d}", f"{d} should map a DSSAT variable to a "
                                          f"{'column name' if d == 'columns' else 'unit'}.",
                              f"{d}: {{TMAX: tmax}}" if d == "columns" else f'{d}: {{SRAD: "MJ m-2"}}')
                    else:
                        c.unknown(f"{k}.{d}", q[d], ["SRAD", "TMAX", "TMIN", "RAIN", "DEWP", "WIND"])
        return
    vs = pw.get("variables")
    if not isinstance(vs, dict) or not vs:
        c.bad(f"{key}.variables", "should list where each weather variable comes from: SRAD, TMAX, TMIN and "
                                  "RAIN are needed, DEWP and WIND are optional (or give points).",
              "TMAX: {files: \"<folder>/tmax_{year}.nc\", variable: t2m}")
        return
    known = ("SRAD", "TMAX", "TMIN", "RAIN", "DEWP", "WIND")
    c.unknown(f"{key}.variables", vs, list(known))
    for need in ("SRAD", "TMAX", "TMIN", "RAIN"):
        if need not in vs:
            c.bad(f"{key}.variables", f"{need} is missing; a run needs SRAD, TMAX, TMIN and RAIN.",
                  f"{need}: {{files: ..., variable: ...}}")
    for var, src in vs.items():
        k = f"{key}.variables.{var}"
        if var not in known:
            continue
        exs = f"{var}: {{files: \"<folder>/{var.lower()}_{{year}}.nc\", variable: {var.lower()}}}"
        if not isinstance(src, dict):
            c.bad(k, "should hold files and variable.", exs)
            continue
        c.unknown(k, src, list(SOURCE_KEYS))
        if "files" not in src or "variable" not in src:
            c.bad(k, "needs files (a file name, with {year} where the year goes if there is one file per "
                     "year) and variable (its name inside the file; see prepare_weather.py --inspect).", exs)
            continue
        c.text(f"{k}.files", src["files"], exs, what="a file name pattern")
        v = src["variable"]
        if var == "WIND" and isinstance(v, list):
            if len(v) != 2 or not all(isinstance(x, str) for x in v):
                c.bad(f"{k}.variable", "for wind components give two names, [u, v].", "variable: [u10, v10]")
        elif not isinstance(v, str) or not v:
            c.bad(f"{k}.variable", f"{v!r} should be the variable's name inside the file"
                                   + (" (or [u, v] for wind components)" if var == "WIND" else "") + ".", exs)
        if src.get("units") is not None:
            c.text(f"{k}.units", src["units"], 'units: "K"', what="the unit of the values in the file")
        if src.get("regrid") is not None and src["regrid"] not in REGRID:
            c.bad(f"{k}.regrid", f"{src['regrid']!r} is not a method; use one of {', '.join(REGRID)}, or "
                                 "leave it out for the default.", "regrid: mean")
        if "height_m" in src:
            c.number(f"{k}.height_m", src["height_m"], 0, 100, example="height_m: 10", open_lo=True)
        if src.get("daily") is not None and src["daily"] not in DAILY_HOW:
            c.bad(f"{k}.daily", f"{src['daily']!r} should be one of {', '.join(DAILY_HOW)}: how the sub-daily "
                                "values make a day (max for TMAX, min for TMIN, mean for DEWP and WIND, sum "
                                "or accumulated for RAIN and SRAD).", "daily: max")


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
        c.text(f"{key}.slpf_table", s["slpf_table"], "slpf_table: <cache>/checks/districts/slpf_table.csv",
               what="a CSV file with columns iy, ix, water, slpf")
        if s.get("slpf") not in (None, 1, 1.0) or s.get("slpf_rainfed") is not None:
            c.bad(f"{key}.slpf_table", "slpf_table gives SLPF per stratum and replaces slpf and "
                                       "slpf_rainfed; set only one of them.", "slpf_table: slpf_table.csv")
    if "description" in s and not isinstance(s["description"], str):
        c.bad(f"{key}.description", "should be text.", 'description: "CO2 at 550 ppm"')
    sim = s.get("simulation")
    if sim is not None:
        exs = "simulation: {WATER: N, NITRO: N}"
        if not isinstance(sim, dict) or not sim:
            c.bad(f"{key}.simulation", "should map a simulation-control column to its value.", exs)
        else:
            for name, v in sim.items():
                k = f"{key}.simulation.{name}"
                if name not in SIMULATION_FIELDS:
                    c.bad(k, f"'{name}' is not a simulation setting xdssat can change; use one of "
                             f"{', '.join(SIMULATION_FIELDS)}.", exs)
                    continue
                codes = SIMULATION_FIELDS[name]
                if codes is None:
                    c.number(k, v, example=f"{name}: 50")
                elif not (isinstance(v, (str, bool, int)) and str(_code(v)) in codes and len(str(_code(v))) == 1):
                    c.bad(k, f"{v!r} should be one of {', '.join(codes)}.", f"{name}: {codes[0]}")
    man = s.get("management")
    if man is not None:
        exm = "management: {MP.PPOP: 5.5}"
        if not isinstance(man, dict) or not man:
            c.bad(f"{key}.management", "should map '<factor>.<column>' to a value.", exm)
        else:
            for name, v in man.items():
                f, _, col = str(name).partition(".")
                if col not in MANAGEMENT_FIELDS.get(f, ()):
                    c.bad(f"{key}.management", f"'{name}' is not a field xdssat can change; use "
                          + "; ".join(f"{g}.{x}" for g, cols in MANAGEMENT_FIELDS.items() for x in cols[:3])
                          + " ... (planting details MP, initial conditions IC).", exm)
                elif not isinstance(v, (int, float, str)) or isinstance(v, bool):
                    c.bad(f"{key}.management.{name}", f"{v!r} should be a number or a code.", exm)
    if s.get("planting_offset_days") is not None:
        c.number(f"{key}.planting_offset_days", s["planting_offset_days"], -180, 180, integer=True,
                 example="planting_offset_days: 14")


def _code(v):
    """A YAML value as a one-letter code (YAML reads Y / N / yes / no as booleans)."""
    if isinstance(v, bool):
        return "Y" if v else "N"
    t = str(v).strip().upper()
    return {"YES": "Y", "TRUE": "Y", "NO": "N", "FALSE": "N"}.get(t, t)


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
    if tf.suffix.upper() == ".SQX":       # a crop sequence: water regimes name sequence treatments
        import sequence
        have = set(sequence.sequence_table(t).N)
        for name, w in exp.data["treatments"]["water_regimes"].items():
            if w["template_level"] not in have:
                c.bad(f"treatments.water_regimes.{name}.template_level",
                      f"the sequence template has no sequence treatment {w['template_level']} "
                      f"(it has {', '.join(map(str, sorted(have)))}).")
    else:
        trt = t.treatments()
        mf, mi = set(t.blocks("MF")) | {0}, set(t.blocks("MI")) | {0}
        for kg, lv in exp.data["treatments"]["nitrogen_levels"].items():
            if lv not in mf:
                c.bad(f"treatments.nitrogen_levels.{kg}", f"the template has no fertilizer level {lv} "
                      f"(it has {', '.join(map(str, sorted(mf)))}; 0 means no fertilizer).")
        for name, w in exp.data["treatments"]["water_regimes"].items():
            if w["template_level"] not in mi:
                c.bad(f"treatments.water_regimes.{name}.template_level",
                      f"the template has no irrigation level {w['template_level']} "
                      f"(it has {', '.join(map(str, sorted(mi)))}; 0 means no irrigation).")
        if not c.problems:
            for name in exp.data["treatments"]["water_regimes"]:
                for kg in exp.data["treatments"]["nitrogen_kg"]:
                    sel = trt[(trt.MI == exp.water_level(name)) & (trt.MF == exp.n_level(kg))]
                    if sel.empty:
                        c.bad("treatments.nitrogen_kg", f"the template has no treatment with irrigation "
                              f"level {exp.water_level(name)} ({name}) and fertilizer level "
                              f"{exp.n_level(kg)} ({kg} kg N); add one to {tf.name}.")
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
    try:
        import inputs
        c.problems += inputs.check(exp)
    except ImportError:
        pass
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

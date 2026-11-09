"""GLUE calibration of cultivar coefficients against field experiments, on the real engine.

The method of DSSAT's GLUE tool (R code, https://github.com/DSSAT/glue, commit e86093e),
as the learning track's port (docs/notebooks/glue.py) has it, on xdssat's own modules:

* ``ranges``: coefficients of the cultivar file with their calibration flag (``!Calibration``
  row: P phenology, G growth, N not calibrated), the ``MINIMA`` / ``MAXIMA`` rows and the
  cultivar's own values (``Uniform.r``).
* ``sample``: parameter sets drawn uniformly in the range; round 1 varies the P coefficients,
  round 2 fixes them at the round-1 best set and varies the G coefficients.
* ``run_sets``: every set on every experiment, many sets per FileX as cultivar variants
  (``genotype.write_variants``) of the experiment's own treatment.
* ``observed``: the experiment's measurements from its FileA (anthesis and maturity as days
  after planting; yield, biomass, LAI ...).
* ``likelihood``: the normal density of each measurement, ``exp(-(M - S)^2 / (2 var)) /
  sqrt(2 pi var)`` (``Calculation.r``) with the tool's variances (``MeasurementVariance_All.csv``:
  anthesis sd 3 d, maturity sd 7 d, weights and LAI by coefficient of variation), multiplied
  within an experiment, normalised over the sets, multiplied across experiments
  (``LikelihoodCalculation.r``).
* ``posterior_summary``: per coefficient the maximum-probability value, posterior mean and sd.

``calibrate`` runs both rounds. The result can go into a scenario as
``cultivar_coefficients`` or into an own line of the cultivar file.
"""
from __future__ import annotations

import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import config
import filex
import genotype
import runner

VARIANCE = {
    "ADAP": ("sd", 3.0, 1), "MDAP": ("sd", 7.0, 1),
    "PWAM": ("cv", 0.3, 2), "HWAM": ("cv", 0.3, 2), "CWAM": ("cv", 0.3, 2),
    "LAIX": ("cv", 0.4, 2), "HWUM": ("cv", 0.1, 2),
}
OBSERVED = ("HWAM", "CWAM", "PWAM", "LAIX", "HWUM")


@dataclass
class Experiment:
    """A field experiment to calibrate on: its FileX, the treatment, and the files it reads."""
    filex: Path
    trno: int = 1
    weather: list = field(default_factory=list)        # .WTH files the FileX names
    soil: Path | None = None                            # a .SOL file with the FileX's profile
    filea: Path | None = None                           # default: the FileX with A last
    simulation_overrides: dict | None = None
    id: str | None = None

    @property
    def name(self) -> str:
        return self.id or f"{Path(self.filex).stem}_{self.trno}"

    @property
    def filea_path(self) -> Path:
        p = Path(self.filex)
        return Path(self.filea) if self.filea else p.with_suffix(p.suffix[:-1] + "A")


def _date(v) -> pd.Timestamp:
    v = int(v)
    if v > 1_000_000:
        return pd.to_datetime(str(v), format="%Y%j")
    return pd.Timestamp(filex.decode_date(f"{v:05d}"))


def read_table(path: Path) -> pd.DataFrame:
    """The first @-headed table of a FileA or FileT, numbers parsed, -99 to NaN."""
    rows, header = [], None
    for ln in Path(path).read_text(encoding="latin-1").splitlines():
        if ln.startswith("@"):
            header = header or ln[1:].split()
            continue
        if header is None or not ln.strip() or ln.lstrip().startswith(("!", "*")):
            continue
        rows.append(ln.split())
    df = pd.DataFrame([r[:len(header)] for r in rows], columns=header)
    return df.apply(pd.to_numeric, errors="coerce").replace(-99, np.nan)


def ranges(crop: str, cultivar: str, engine_dir: Path | None = None) -> pd.DataFrame:
    """Coefficient, flag (P / G / N), min, max and the cultivar's own value."""
    lines = (Path(engine_dir or config.ENGINE_DIR) / "Genotype" / f"{crop}.CUL").read_text(
        encoding="latin-1").splitlines()
    head = next(ln for ln in lines if ln.startswith("@VAR#"))
    flags = next((ln for ln in lines if ln.lower().startswith("!calibration")), "")
    toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"\S+", head)]
    names = [t[0] for t in toks[4:]]                        # after VAR#, name, EXPNO, ECO#

    def values(line):
        out, prev = {}, None
        for name, _, end in toks:
            if prev is not None and name in names:
                out[name] = line.ljust(end)[prev + 1:end].strip()
            prev = end
        return out

    mins = values(next(ln for ln in lines if ln.startswith("999991")))
    maxs = values(next(ln for ln in lines if ln.startswith("999992")))
    own = values(next(ln for ln in lines if ln.startswith(cultivar)))
    fl = values(flags.ljust(len(head)))
    return pd.DataFrame([{"coef": n, "flag": (fl.get(n) or "N").upper(), "min": float(mins[n]),
                          "max": float(maxs[n]), "default": float(own[n])} for n in names])


def sample(table: pd.DataFrame, n: int, round_: int, fixed: dict | None = None, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    want = "P" if round_ == 1 else "G"
    out = {}
    for r in table.itertuples():
        if r.flag == want:
            out[r.coef] = rng.uniform(r.min, r.max, n)
        elif fixed and r.coef in fixed:
            out[r.coef] = np.full(n, fixed[r.coef])
        else:
            out[r.coef] = np.full(n, r.default)
    sets = pd.DataFrame(out)
    sets.insert(0, "set", range(1, n + 1))
    return sets


def _field(fx: filex.FileX, trno: int) -> tuple[str, str]:
    """(WSTA, ID_SOIL) of the treatment's field level."""
    fl = int(fx.treatments().loc[trno, "FL"])
    row = next(ln for ln in fx.blocks("FL")[fl] if not ln.startswith("@"))
    return row[12:20].strip(), row[69:79].strip()


def _run_chunk(args):
    e, base, crop, k, sets, tag = args
    d = Path(config.WORK_ROOT) / "glue" / f"{tag}_{e.name}_{k:03d}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for f in [e.filex, *e.weather] + ([e.soil] if e.soil else []):
        shutil.copy2(f, d / Path(f).name)
    codes = {f"GL{int(s):04d}": int(s) for s in sets.set}
    coefs = [c for c in sets.columns if c != "set"]
    genotype.write_variants(d, base, {c: {k_: sets.loc[sets.set == s, k_].iloc[0] for k_ in coefs}
                                      for c, s in codes.items()}, crop)
    fx = filex.FileX.read(e.filex)
    wsta, soil_id = _field(fx, e.trno)
    pdate = pd.Timestamp(fx.planting_date(int(fx.treatments().loc[e.trno, "MP"])))
    u = pd.DataFrame({"cell": list(codes), "name": [f"set {s}" for s in codes.values()], "wsta": wsta,
                      "soil_id": soil_id, "pdate": pdate, "trno_template": e.trno, "CU.INGENO": list(codes)})
    ext = Path(e.filex).suffix.upper()
    code = "GL" + Path(e.filex).stem[2:8]
    built = filex.build_filex(fx, u, exp_code=code, title=f"GLUE {tag}",
                              simulation_overrides=e.simulation_overrides)
    (d / f"{code}{ext}").write_text(built.text, encoding="latin-1")
    res = runner.run_engine(d, f"{code}{ext}")
    if res.summary.empty:
        raise RuntimeError(f"GLUE run failed for {e.name} chunk {k}:\n{res.error_out[-1500:]}")
    s = res.summary.merge(built.units[["TRNO", "CU.INGENO"]], on="TRNO")
    s["set"] = s["CU.INGENO"].map(codes)
    s["exp"] = e.name
    for c, col in (("ADAP", "ADAT"), ("MDAP", "MDAT")):
        s[c] = [(_date(v) - pdate).days if pd.notna(pd.to_numeric(v, errors="coerce")) and int(v) > 0 else np.nan
                for v in s[col]]
    shutil.rmtree(d, ignore_errors=True)
    return s


def run_sets(experiments: list[Experiment], sets: pd.DataFrame, base: str, crop: str, tag: str,
             per_filex: int = 99, workers: int = 8) -> pd.DataFrame:
    """Every parameter set on every experiment; one row per (set, experiment)."""
    jobs = [(e, base, crop, k, sets.iloc[i:i + per_filex], tag)
            for e in experiments for k, i in enumerate(range(0, len(sets), per_filex))]
    with ThreadPoolExecutor(workers) as ex:
        df = pd.concat(list(ex.map(_run_chunk, jobs)), ignore_index=True)
    for c in OBSERVED:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["set", "exp", "ADAP", "MDAP", *OBSERVED]]


def observed(experiments: list[Experiment]) -> pd.DataFrame:
    """Measured values of each experiment's treatment (FileA), as in ``run_sets``."""
    rows = []
    for e in experiments:
        a = read_table(e.filea_path)
        a = a[a.TRNO == e.trno].iloc[0]
        fx = filex.FileX.read(e.filex)
        pdate = pd.Timestamp(fx.planting_date(int(fx.treatments().loc[e.trno, "MP"])))

        def dap(v):
            if pd.isna(v):
                return np.nan
            v = int(v)
            d = pd.Timestamp(pdate.year, 1, 1) + pd.Timedelta(days=v - 1) if v < 1000 else _date(v)
            return (d - pdate).days
        rows.append({"exp": e.name, "ADAP": dap(a.get("ADAT")), "MDAP": dap(a.get("MDAT")),
                     **{c: float(a[c]) if c in a and pd.notna(a[c]) else np.nan for c in OBSERVED}})
    return pd.DataFrame(rows)


def likelihood(sim: pd.DataFrame, obs: pd.DataFrame, round_: int) -> pd.DataFrame:
    """GLUE probability of every parameter set (columns set, L_<exp> ..., prob)."""
    names = [k for k, v in VARIANCE.items() if v[2] == round_]
    m = sim.merge(obs, on="exp", suffixes=("", "_obs"))
    lik = np.ones(len(m))
    for n in names:
        o, s = m[n + "_obs"].to_numpy(float), m[n].to_numpy(float)
        kind, v, _ = VARIANCE[n]
        var = v ** 2 if kind == "sd" else (o * v) ** 2
        with np.errstate(invalid="ignore", divide="ignore"):
            dens = np.exp(-(o - s) ** 2 / (2 * var)) / np.sqrt(2 * np.pi * var)
        dens = np.where(np.isnan(o), 1.0, np.where(np.isnan(s), 0.0, dens))   # missing measurement
        lik = lik * dens * 10000.0                                            # Calculation.r constant
    wide = pd.DataFrame({"set": m.set, "exp": m.exp, "L": lik}).pivot(index="set", columns="exp", values="L")
    prob = np.ones(len(wide))
    for c in wide.columns:
        tot = wide[c].sum()
        prob = prob * (wide[c] / tot if tot > 0 else 0)
    wide = wide.add_prefix("L_")
    wide["prob"] = prob / prob.sum() if prob.sum() > 0 else prob
    return wide.reset_index()


def posterior_summary(sets: pd.DataFrame, prob: pd.DataFrame, coefs: list[str]) -> pd.DataFrame:
    t = sets.merge(prob[["set", "prob"]], on="set")
    best = t.loc[t.prob.idxmax()]
    rows = []
    for c in coefs:
        w = t.prob.to_numpy()
        mu = float(np.sum(w * t[c]))
        rows.append({"coef": c, "best": float(best[c]), "mean": mu,
                     "sd": float(np.sqrt(np.sum(w * (t[c] - mu) ** 2))),
                     "prior_min": float(t[c].min()), "prior_max": float(t[c].max())})
    return pd.DataFrame(rows)


def calibrate(experiments: list[Experiment], crop: str, cultivar: str, n: int = 1000, seed: int = 1,
              workers: int = 8) -> dict:
    """Both GLUE rounds; returns the tables of each round and the calibrated coefficients."""
    table = ranges(crop, cultivar)
    obs = observed(experiments)
    out, fixed = {"ranges": table, "observed": obs}, {}
    for round_ in (1, 2):
        coefs = list(table.coef[table.flag == ("P" if round_ == 1 else "G")])
        if not coefs:
            continue
        sets = sample(table, n, round_, fixed, seed + round_)
        sim = run_sets(experiments, sets, cultivar, crop, f"r{round_}", workers=workers)
        prob = likelihood(sim, obs, round_)
        post = posterior_summary(sets, prob, coefs)
        out[f"round{round_}"] = {"sets": sets, "sim": sim, "prob": prob, "posterior": post}
        fixed.update(dict(zip(post.coef, post.best)))
    out["coefficients"] = fixed
    return out

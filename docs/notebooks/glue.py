"""GLUE (Generalized Likelihood Uncertainty Estimation) for DSSAT cultivar coefficients, in Python.

A port of the method of DSSAT's GLUE tool (R code at https://github.com/DSSAT/glue, commit e86093e),
following its files:

- ``Uniform.r``: random parameter sets drawn uniformly between the ``MINIMA`` and ``MAXIMA`` rows of
  the cultivar file. Each coefficient carries a calibration flag in the cultivar file's
  ``!Calibration`` row: ``P`` (phenology), ``G`` (growth), ``N`` (not calibrated, kept at the
  cultivar's value). Round 1 varies the P coefficients only; round 2 fixes them at the round-1 best
  set and varies the G coefficients.
- ``Calculation.r``: the likelihood of one simulated value against one measurement is the normal
  density ``exp(-(M - S)^2 / (2 var)) / sqrt(2 pi var)``.
- ``MeasurementVariance_All.csv``: the variances. Anthesis and maturity in days after planting with
  standard deviations of 3 and 7 days; yields, weights and LAI with a coefficient of variation
  (variance = (M x CV)^2).
- ``IntegratedLikelihoodCalculation1.r`` / ``Combination.r``: per treatment, the likelihoods of the
  measurements are multiplied; ``LikelihoodCalculation.r``: each treatment's likelihoods are
  normalised over the parameter sets, multiplied across treatments and normalised again.
- ``OptimalParameterSet.r``: the result is the parameter set with the largest probability.

Differences from the R tool, on purpose: the measurements are read from the summary output joined
to the FileA here, not from ``Evaluate.OUT``; the first-pod date (``PD1T``) is not used; and the engine
runs many parameter sets per FileX (one cultivar per set) in parallel instead of one batch per set.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

import learnlib as L

# MeasurementVariance_All.csv (GLUE e86093e): name -> (sd in days, cv, round)
VARIANCE = {
    "ADAP": ("sd", 3.0, 1), "MDAP": ("sd", 7.0, 1),
    "PWAM": ("cv", 0.3, 2), "HWAM": ("cv", 0.3, 2), "CWAM": ("cv", 0.3, 2),
    "LAIX": ("cv", 0.4, 2), "HWUM": ("cv", 0.1, 2),
}
GLUE_COMMIT = "e86093e400fbbdfc8e2c751c98acc2136835c3af"


def glue_url(rel: str, line: int | None = None) -> str:
    return f"https://github.com/DSSAT/glue/blob/{GLUE_COMMIT}/{rel}" + (f"#L{line}" if line else "")


def ranges(crop: str, cultivar: str) -> pd.DataFrame:
    """Coefficient names, calibration flags, MINIMA, MAXIMA and the cultivar's own values."""
    lines = (Path(L.config.ENGINE_DIR) / "Genotype" / f"{crop}.CUL").read_text(encoding="latin-1").splitlines()
    head = next(ln for ln in lines if ln.startswith("@VAR#"))
    flags = next(ln for ln in lines if ln.lower().startswith("!calibration"))
    toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"\S+", head)]
    names = toks[4:]                       # after VAR#, the name, EXPNO and ECO#

    def values(line):
        out = {}
        prev_end = None
        for name, start, end in toks:
            if prev_end is not None and name in dict((n[0], 0) for n in names):
                out[name] = line[prev_end:end].strip()
            prev_end = end
        return out

    mins = values(next(ln for ln in lines if ln.startswith("999991")))
    maxs = values(next(ln for ln in lines if ln.startswith("999992")))
    own = values(next(ln for ln in lines if ln.startswith(cultivar)))
    fl = values(flags.ljust(len(head)))
    rows = [{"coef": n, "flag": fl.get(n, "N") or "N", "min": float(mins[n]), "max": float(maxs[n]),
             "default": float(own[n])} for n, _, _ in names]
    return pd.DataFrame(rows)


def sample(rng_table: pd.DataFrame, n: int, round_: int, fixed: dict | None = None,
           seed: int = 1) -> pd.DataFrame:
    """``n`` parameter sets: uniform for the flag of this round (P in round 1, G in round 2)."""
    rng = np.random.default_rng(seed)
    want = "P" if round_ == 1 else "G"
    out = {}
    for _, r in rng_table.iterrows():
        if r.flag == want:
            out[r.coef] = rng.uniform(r["min"], r["max"], n)
        elif fixed and r.coef in fixed:
            out[r.coef] = np.full(n, fixed[r.coef])
        else:
            out[r.coef] = np.full(n, r["default"])
    sets = pd.DataFrame(out)
    sets.insert(0, "set", range(1, n + 1))
    return sets


def _stage(d, e):
    (L.stage_course if e.get("source") == "course" else L.stage)(d, *e["files"])


def _chunk_run(args):
    e, base, crop, chunk, sets, tag = args
    exp, ext, soil_id, template_trno = e["exp"], e["ext"], e["soil"], e.get("trno", 1)
    d = L.work_dir(f"glue_{tag}_{e.get('id', exp)}_{chunk:03d}")
    _stage(d, e)
    codes = {f"GL{int(s):04d}": s for s in sets.set}
    coefs = [c for c in sets.columns if c != "set"]
    variants = {c: {k: sets.loc[sets.set == s, k].iloc[0] for k in coefs} for c, s in codes.items()}
    L.write_cultivar_variants(d, base, variants, crop)
    fx = L.filex.FileX.read(d / f"{exp}{ext}")
    pdate = pd.Timestamp(fx.planting_date(int(fx.treatments().loc[template_trno, "MP"])))
    wsta = _wsta(fx)
    u = pd.DataFrame({"cell": list(codes), "name": [f"set {s}" for s in codes.values()], "wsta": wsta,
                      "soil_id": soil_id, "pdate": pdate, "trno_template": template_trno, "CU.INGENO": list(codes)})
    name, u = L.write_built(d, fx, u, "GL" + exp[2:8], ext, title=f"GLUE round {tag}",
                            simulation_overrides=e.get("sim_overrides"))
    res = L.runner.run_engine(d, name, outputs="summary")
    s = res.summary
    if s.empty:
        raise RuntimeError(f"GLUE run failed for {exp} chunk {chunk}:\n{res.error_out[-1500:]}")
    s = s.merge(u[["TRNO", "CU.INGENO"]], on="TRNO")
    s["set"] = s["CU.INGENO"].map(codes)
    s["PDAT_ts"] = pdate
    return s


def _wsta(fx) -> str:
    blk = fx.blocks("FL")
    first = next(iter(blk.values()))
    for ln in first:
        if ln[:2].strip() == "1" and not ln.startswith("@"):
            return ln[12:20].strip()
    raise ValueError("no field row")


def run_sets(experiments: list[dict], sets: pd.DataFrame, base: str, crop: str, tag: str,
             per_filex: int = 99, workers: int = 16) -> pd.DataFrame:
    """Run every parameter set on every experiment; one row per (set, experiment)."""
    jobs = []
    for e in experiments:
        for k, start in enumerate(range(0, len(sets), per_filex)):
            jobs.append((e, base, crop, k, sets.iloc[start:start + per_filex], tag))
    with ThreadPoolExecutor(workers) as ex:
        parts = list(ex.map(_chunk_run, jobs))
    out = []
    for e, p in zip([j[0] for j in jobs], parts):
        p = p.assign(exp=e.get("id", e["exp"]))
        out.append(p)
    df = pd.concat(out, ignore_index=True)
    dap = lambda col: [(L._date(v) - p).days if pd.notna(v) else np.nan for v, p in zip(df[col], df.PDAT_ts)]
    df["ADAP"] = dap("ADAT")
    df["MDAP"] = dap("MDAT")
    for c in ("HWAM", "CWAM", "PWAM", "LAIX", "HWUM"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["set", "exp", "ADAP", "MDAP", "HWAM", "CWAM", "PWAM", "LAIX", "HWUM"]]


def observed(experiments: list[dict]) -> pd.DataFrame:
    """Measured values of each experiment (FileA, template treatment), same columns as ``run_sets``."""
    rows = []
    for e in experiments:
        root = L.COURSE if e.get("source") == "course" else L.CSM_DATA
        a = L._read_table(root / e["filea"])
        a = a[a.TRNO == e.get("trno", 1)].iloc[0]
        fx = L.filex.FileX.read(root / e["files"][0])
        pdate = pd.Timestamp(fx.planting_date(int(fx.treatments().loc[e.get("trno", 1), "MP"])))
        year = pdate.year

        def dap(v):
            if pd.isna(v):
                return np.nan
            v = int(v)
            d = pd.Timestamp(year, 1, 1) + pd.Timedelta(days=v - 1) if v < 1000 else L._date(v)
            return (d - pdate).days
        rows.append({"exp": e.get("id", e["exp"]), "ADAP": dap(a.get("ADAT")), "MDAP": dap(a.get("MDAT")),
                     **{c: float(a[c]) if c in a and pd.notna(a[c]) else np.nan for c in ("HWAM", "CWAM", "PWAM", "LAIX", "HWUM")}})
    return pd.DataFrame(rows)


def likelihood(sim: pd.DataFrame, obs: pd.DataFrame, round_: int) -> pd.DataFrame:
    """GLUE probability of every parameter set (columns: set, L_<exp> ..., prob)."""
    names = [k for k, v in VARIANCE.items() if v[2] == round_]
    m = sim.merge(obs, on="exp", suffixes=("", "_obs"))
    per = pd.DataFrame({"set": m.set, "exp": m.exp})
    lik = np.ones(len(m))
    for n in names:
        o, s = m[n + "_obs"].to_numpy(float), m[n].to_numpy(float)
        kind, v, _ = VARIANCE[n]
        var = v ** 2 if kind == "sd" else (o * v) ** 2
        dens = np.exp(-(o - s) ** 2 / (2 * var)) / np.sqrt(2 * np.pi * var)
        dens = np.where(np.isnan(o), 1.0, np.where(np.isnan(s), 0.0, dens))   # missing measurement: no information
        lik = lik * dens * 10000.0                                            # Calculation.r constant
    per["L"] = lik
    wide = per.pivot(index="set", columns="exp", values="L")
    prob = np.ones(len(wide))
    for c in wide.columns:
        tot = wide[c].sum()
        prob = prob * (wide[c] / tot if tot > 0 else 0)
    wide = wide.add_prefix("L_")
    wide["prob"] = prob / prob.sum() if prob.sum() > 0 else prob
    return wide.reset_index()


def posterior_summary(sets: pd.DataFrame, prob: pd.DataFrame, coefs: list[str]) -> pd.DataFrame:
    """Prior range, maximum-probability value, posterior mean and sd per coefficient."""
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

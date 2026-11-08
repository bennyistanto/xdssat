"""Fit the soil fertility factor SLPF separately for irrigated and rainfed rice
against district yields, from three or more runs that differ only in SLPF.

    python scripts/fit_slpf.py --runs <cache>/runs/java_per_cell_1991-2020_slpf0.63_tcldf19 \
        <cache>/runs/java_per_cell_1991-2020_slpf0.73_tcldf19 \
        <cache>/runs/java_per_cell_1991-2020_slpf0.83_tcldf19

Method
1. Each simulated season (cell, water regime, season, season year) has its
   yield at the SLPF values of the runs; a polynomial through the points
   (degree = runs - 1, at most 3, least squares beyond that) gives its yield
   at any SLPF in between (SLPF only scales growth, so
   the season, its dates and harvest year stay the same).
2. Seasons go to district and calendar year of harvest exactly as in
   scripts/compare_districts.py (rice ha per cell x regime x district from the
   100 m rasters; dry matter / (1 - moisture) = GKG, ku/ha).
3. For a pair (SLPF irrigated, SLPF rainfed) the district mean over the years
   with observations is compared with the BDSP district mean; the objective is
   the rice-area weighted mean squared log ratio over districts (>= --min-ha
   rice, >= 5 observed years). Grid search over the run range.
4. Temperature model ("temp"): SLPF of a cell = s_regime + b x (Tmin_cell - TREF),
   clipped to the SLPF range of the runs, where Tmin_cell is the cell's mean
   season minimum temperature (TMINA of the middle run). Each season's
   polynomial is evaluated at its own cell's SLPF; three parameters fitted by
   Nelder-Mead from the pair optimum (b = 0). A statistical correction of the
   model's long-season yield bonus in cool cells, not a mechanism.
5. Leave-one-province-out: refit without each province and score its districts,
   for every model, to see whether extra parameters predict or only fit.

Writes <XDSSAT_CACHE>/checks/districts/fit_slpf.json, fit_slpf_grid.csv and
slpf_table.csv (iy, ix, water, tmin, slpf of the temp model, for --slpf-table).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

import config  # noqa: E402
import compare_districts as cdm  # noqa: E402

KEY = ["iy", "ix", "water", "season", "season_year"]
TREF = 23.0                         # C, typical lowland season Tmin; b is SLPF per C away from it


def load_runs(runs: list[Path]) -> tuple[np.ndarray, pd.DataFrame, tuple[int, int]]:
    s_vals, frames, years = [], [], None
    for r in runs:
        info = json.loads((r / "run_info.json").read_text())
        s_vals.append(float(info["args"]["slpf"]))
        y0, y1 = json.loads(info["args"]["years"])
        years = years or (y0, y1)
        d = pd.read_csv(r / "summary.csv", low_memory=False, usecols=KEY + ["HWAH", "HDAT", "MDAT"])
        d["HWAH"] = pd.to_numeric(d.HWAH, errors="coerce").where(lambda v: v >= 0)
        hd = pd.to_numeric(d.HDAT, errors="coerce")
        d["year"] = (hd.where(hd > 1000000, pd.to_numeric(d.MDAT, errors="coerce")) // 1000)
        frames.append(d.set_index(KEY))
    order = np.argsort(s_vals)
    s = np.array(s_vals)[order]
    y = pd.concat([frames[i].HWAH.rename(f"y{k}") for k, i in enumerate(order)], axis=1, join="inner")
    y["year"] = frames[order[len(order) // 2]].year.reindex(y.index)
    return s, y.dropna().reset_index(), years


def district_terms(s, y, pieces, years, obs, moisture, min_ha):
    """Per district: mean over observed years of (A_irr . [1,s,s^2] + A_rf . [...]) / W, as coefficients."""
    deg = min(len(s) - 1, 3)
    V = np.vander(s, deg + 1, increasing=True)               # rows: SLPF points; cols: 1, s, s^2 ...
    ys = y[[f"y{k}" for k in range(len(s))]].to_numpy().T
    coef = np.linalg.lstsq(V, ys, rcond=None)[0].T           # per season: c0 .. c_deg
    cs = [f"c{k}" for k in range(deg + 1)]
    y = y.assign(**{c: coef[:, k] for k, c in enumerate(cs)})
    m = y.merge(pieces, on=["iy", "ix", "water"])
    m = m[m.year.between(years[0] + 1, years[1])]
    for c in cs:
        m[c] = m[c] * m.ha / (1 - moisture) / 100.0          # ku/ha GKG x ha
    g = m.groupby(["code", "year", "water"])[cs + ["ha"]].sum().reset_index()
    W = g.groupby(["code", "year"]).ha.sum().rename("W")
    g = g.join(W, on=["code", "year"])
    for c in cs:
        g[c] = g[c] / g.W
    g = g.merge(obs, on=["code", "year"])                     # observed years only
    n = g.groupby("code").year.nunique().rename("n")
    t = g.pivot_table(index="code", columns="water", values=cs, aggfunc="sum").fillna(0.0)
    t = t.div(n.reindex(t.index), axis=0)
    ob = g.drop_duplicates(["code", "year"]).groupby("code").obs_ku_ha.mean()
    ha = pieces.groupby("code").ha.sum()
    keep = (n >= 5) & (ha.reindex(n.index) >= min_ha)
    t = t[keep.reindex(t.index).fillna(False)]
    out = pd.DataFrame(index=t.index)
    for w in ("irrigated", "rainfed"):
        for c in cs:
            out[f"{w[:3]}_{c}"] = t[(c, w)] if (c, w) in t.columns else 0.0
    out["obs"] = ob.reindex(out.index)
    out["ha"] = ha.reindex(out.index)
    return out


def predict(t: pd.DataFrame, si, sr) -> np.ndarray:
    si, sr = np.atleast_1d(si)[:, None], np.atleast_1d(sr)[:, None]
    p = 0.0
    for k in range(4):
        if f"irr_c{k}" in t:
            p = p + t[f"irr_c{k}"].to_numpy() * si ** k + t[f"rai_c{k}"].to_numpy() * sr ** k
    return p                                                  # (candidates, districts)


def score(t, si, sr) -> np.ndarray:
    e = np.log(predict(t, si, sr) / t.obs.to_numpy()) ** 2
    return (e * t.ha.to_numpy()).sum(axis=1) / t.ha.sum()


def fit(t, grid, joint: bool):
    if not joint:
        sc = score(t, grid, grid)
        k = int(np.argmin(sc))
        return grid[k], grid[k], sc[k]
    gi, gr = np.meshgrid(grid, grid, indexing="ij")
    sc = score(t, gi.ravel(), gr.ravel())
    k = int(np.argmin(sc))
    return gi.ravel()[k], gr.ravel()[k], sc[k]


def stats(t, si, sr) -> dict:
    p = predict(t, si, sr)[0]
    o = t.obs.to_numpy()
    w = t.ha.to_numpy()
    return dict(level=float(np.average(p / o, weights=w)), rmse_log=float(np.sqrt(np.average(np.log(p / o) ** 2, weights=w))),
                spatial_r=float(np.corrcoef(p, o)[0, 1]), sd_sim=float(p.std()), sd_obs=float(o.std()))


def cell_tmin(run: Path) -> pd.Series:
    d = pd.read_csv(run / "summary.csv", low_memory=False, usecols=["iy", "ix", "TMINA"])
    return pd.to_numeric(d.TMINA, errors="coerce").groupby([d.iy, d.ix]).mean().rename("tmin")


def row_terms(s, y, pieces, years, obs, moisture, min_ha, tmin):
    """Season x district rows with polynomial coefficients (ku/ha GKG), the weight
    that turns them into district means over observed years, and the cell Tmin.
    Returns (rows, districts, M) with M a sparse (districts x rows) matrix."""
    from scipy import sparse
    deg = min(len(s) - 1, 3)
    V = np.vander(s, deg + 1, increasing=True)
    coef = np.linalg.lstsq(V, y[[f"y{k}" for k in range(len(s))]].to_numpy().T, rcond=None)[0].T
    cs = [f"c{k}" for k in range(deg + 1)]
    y = y.assign(**{c: coef[:, k] / (1 - moisture) / 100.0 for k, c in enumerate(cs)})
    m = y.merge(pieces, on=["iy", "ix", "water"])
    m = m[m.year.between(years[0] + 1, years[1])].merge(obs, on=["code", "year"])
    m["W"] = m.groupby(["code", "year"]).ha.transform("sum")
    n = m.groupby("code").year.nunique()
    ha = pieces.groupby("code").ha.sum()
    keep = n[(n >= 5) & (ha.reindex(n.index) >= min_ha)].index
    m = m[m.code.isin(keep)].reset_index(drop=True)
    m["w"] = m.ha / m.W / m.code.map(n)
    m = m.join(tmin, on=["iy", "ix"])
    dist = pd.DataFrame({"obs": m.drop_duplicates(["code", "year"]).groupby("code").obs_ku_ha.mean()})
    dist["ha"] = ha.reindex(dist.index)
    row_of = {c: i for i, c in enumerate(dist.index)}
    M = sparse.csr_matrix((m.w.to_numpy(), (m.code.map(row_of).to_numpy(), np.arange(len(m)))),
                          shape=(len(dist), len(m)))
    return m[["code", "iy", "ix", "water", "tmin"] + cs], dist, M


def slpf_of(rows, si, sr, b, lo, hi):
    base = np.where(rows.water.to_numpy() == "irrigated", si, sr)
    return np.clip(base + b * (rows.tmin.to_numpy() - TREF), lo, hi)


def predict_rows(rows, M, si, sr, b, lo, hi):
    sv = slpf_of(rows, si, sr, b, lo, hi)
    yv = sum(rows[f"c{k}"].to_numpy() * sv ** k for k in range(4) if f"c{k}" in rows)
    return M @ yv


def score_rows(rows, dist, M, params, lo, hi, sel=None):
    p = predict_rows(rows, M, *params, lo, hi)
    o, w = dist.obs.to_numpy(), dist.ha.to_numpy()
    if sel is not None:
        p, o, w = p[sel], o[sel], w[sel]
    return float(np.sum(w * np.log(p / o) ** 2) / w.sum())


def fit_temp(rows, dist, M, start, lo, hi, sel=None):
    from scipy.optimize import minimize
    f = lambda x: score_rows(rows, dist, M, x, lo, hi, sel)  # noqa: E731
    r = minimize(f, np.asarray(start, float), method="Nelder-Mead",
                 options=dict(xatol=1e-4, fatol=1e-7, maxiter=2000))
    return r.x, r.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, nargs="+", required=True, help="three or more runs")
    ap.add_argument("--obs", type=Path, default=config.CACHE_DIR / "obs")
    ap.add_argument("--moisture", type=float, default=0.14)
    ap.add_argument("--min-ha", type=float, default=1000.0)
    ap.add_argument("--step", type=float, default=0.005)
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "checks" / "districts")
    a = ap.parse_args()

    s, y, years = load_runs(a.runs)
    pieces = cdm.pieces()
    obs = cdm.obs_long(a.obs / "bdsp_rice_yield_adm2.csv", "obs_ku_ha")[["code", "year", "obs_ku_ha"]]
    prov = pd.read_csv(a.obs / "bdsp_rice_yield_adm2.csv", dtype={"code": str}).set_index("code").province
    t = district_terms(s, y, pieces, years, obs, a.moisture, a.min_ha)
    grid = np.round(np.arange(s[0], s[-1] + 1e-9, a.step), 4)
    print(f"SLPF points {s.tolist()}, {len(y)} seasons, {len(t)} districts ({t.ha.sum():,.0f} ha), grid {grid[0]}-{grid[-1]}")

    one = fit(t, grid, joint=False)
    two = fit(t, grid, joint=True)
    res = {"slpf_points": s.tolist(), "districts": len(t), "runs": [str(r) for r in a.runs],
           "one": dict(slpf=one[0], objective=one[2], **stats(t, one[0], one[1])),
           "two": dict(slpf_irrigated=two[0], slpf_rainfed=two[1], objective=two[2], **stats(t, two[0], two[1]))}
    for k in ("one", "two"):
        print(k, {kk: round(float(v), 4) for kk, v in res[k].items()})
    edge = [v for v in (two[0], two[1]) if v in (grid[0], grid[-1])]
    if edge:
        print(f"WARNING: optimum on the grid edge ({edge}); extend the SLPF range of the runs")

    # leave one province out
    cv = []
    p_of = prov.reindex(t.index)
    for p in sorted(p_of.dropna().unique()):
        tr, te = t[p_of != p], t[p_of == p]
        o1, o2 = fit(tr, grid, False), fit(tr, grid, True)
        cv.append(dict(province=p, districts=len(te), one_slpf=o1[0], two_irr=o2[0], two_rf=o2[1],
                       one_rmse_log=float(np.sqrt(score(te, o1[0], o1[1])[0])),
                       two_rmse_log=float(np.sqrt(score(te, o2[0], o2[1])[0]))))
    cv = pd.DataFrame(cv)
    w = cv.districts
    res["cv"] = cv.to_dict("records")
    print("\nleave-one-province-out:\n" + cv.round(3).to_string(index=False))
    print(f"held-out rmse(log), district-weighted: one SLPF {np.average(cv.one_rmse_log, weights=w):.3f}, "
          f"two {np.average(cv.two_rmse_log, weights=w):.3f}")

    # temperature model
    tmin = cell_tmin(a.runs[len(a.runs) // 2])
    rows, dist, M = row_terms(s, y, pieces, years, obs, a.moisture, a.min_ha, tmin)
    lo, hi = float(s[0]), float(s[-1])
    b0 = score_rows(rows, dist, M, (two[0], two[1], 0.0), lo, hi)
    xt, ft = fit_temp(rows, dist, M, (two[0], two[1], 0.0), lo, hi)
    pt = predict_rows(rows, M, *xt, lo, hi)
    o, w = dist.obs.to_numpy(), dist.ha.to_numpy()
    sv = slpf_of(rows, *xt, lo, hi)
    res["temp"] = dict(slpf_irrigated=xt[0], slpf_rainfed=xt[1], slope_per_C=xt[2], tref=TREF, objective=ft,
                       level=float(np.average(pt / o, weights=w)), rmse_log=float(np.sqrt(ft)),
                       spatial_r=float(np.corrcoef(pt, o)[0, 1]), sd_sim=float(pt.std()), sd_obs=float(o.std()),
                       share_rows_clipped=float(np.mean((sv <= lo + 1e-9) | (sv >= hi - 1e-9))))
    print(f"\ntemp model check: row machinery at the pair optimum with b=0 gives {b0:.4f} (pair {two[2]:.4f})")
    print("temp", {kk: round(float(v), 4) for kk, v in res["temp"].items()})
    pdist = prov.reindex(dist.index)
    for c in res["cv"]:
        sel = (pdist != c["province"]).to_numpy()
        x, _ = fit_temp(rows, dist, M, (two[0], two[1], 0.0), lo, hi, sel)
        c.update(temp_irr=x[0], temp_rf=x[1], temp_slope=x[2],
                 temp_rmse_log=float(np.sqrt(score_rows(rows, dist, M, x, lo, hi, ~sel))))
    cv = pd.DataFrame(res["cv"])
    print("\nleave-one-province-out with the temp model:\n" + cv.round(3).to_string(index=False))
    print(f"held-out rmse(log), district-weighted: one {np.average(cv.one_rmse_log, weights=cv.districts):.3f}, "
          f"two {np.average(cv.two_rmse_log, weights=cv.districts):.3f}, "
          f"temp {np.average(cv.temp_rmse_log, weights=cv.districts):.3f}")
    res["cv"] = cv.to_dict("records")
    strata = pd.read_csv(a.runs[len(a.runs) // 2] / "strata.csv")[["iy", "ix", "water"]].drop_duplicates()
    tab = strata.join(tmin, on=["iy", "ix"])
    tab["slpf"] = np.round(slpf_of(tab, *xt, lo, hi), 3)
    tab.to_csv(a.out / "slpf_table.csv", index=False)
    print(f"SLPF table: {len(tab)} strata, slpf {tab.slpf.min()}-{tab.slpf.max()} -> {a.out / 'slpf_table.csv'}")

    gi, gr = np.meshgrid(grid, grid, indexing="ij")
    pd.DataFrame({"slpf_irrigated": gi.ravel(), "slpf_rainfed": gr.ravel(),
                  "objective": score(t, gi.ravel(), gr.ravel())}).to_csv(a.out / "fit_slpf_grid.csv", index=False)
    (a.out / "fit_slpf.json").write_text(json.dumps(res, indent=1, default=float))
    print(f"wrote {a.out / 'fit_slpf.json'}")


if __name__ == "__main__":
    main()

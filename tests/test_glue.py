"""GLUE calibration (src/glue.py) on the real engine: UFGA8201 maize, irrigated high N."""
import numpy as np
import pandas as pd
import pytest

from conftest import DATA, needs_engine

pytestmark = [pytest.mark.engine, needs_engine]


def test_glue_rounds_on_the_maize_experiment():
    import glue
    import genotype
    v = genotype.variants_text("IB0035", {"GL0001": {"P1": 210.0, "G2": 800}}, "MZCER048")
    row = next(ln for ln in v.splitlines() if ln.startswith("GL0001"))
    assert {"210.0", "800.0"} <= set(row.split())          # P1 and G2 of the new variant
    e = glue.Experiment(DATA / "Maize" / "UFGA8201.MZX", 4,
                        sorted((DATA / "Weather").glob("UFGA8*.WTH")), DATA / "Soil" / "SOIL.SOL")
    obs = glue.observed([e])
    assert obs.ADAP.iloc[0] > 0 and obs.HWAM.iloc[0] > 0
    res = glue.calibrate([e], "MZCER048", "IB0035", n=16, seed=3, workers=4)
    t = res["ranges"].set_index("coef")
    for k, flag in ((1, "P"), (2, "G")):
        r = res[f"round{k}"]
        assert abs(r["prob"].prob.sum() - 1) < 1e-9 and len(r["sim"]) == 16
        for c in t.index[t.flag == flag]:
            assert t.loc[c, "min"] <= res["coefficients"][c] <= t.loc[c, "max"]
    # the most probable round-1 set is at least as close to the observed anthesis as the median set
    s1 = res["round1"]["sim"].merge(res["round1"]["prob"][["set", "prob"]], on="set")
    err = (s1.ADAP - obs.ADAP.iloc[0]).abs()
    assert err[s1.prob.idxmax()] <= err.median()

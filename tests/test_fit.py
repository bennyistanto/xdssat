"""scripts/fit_slpf.py: the grid fit recovers known SLPF values on synthetic districts."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location(
    "fit_slpf", Path(__file__).resolve().parents[1] / "scripts" / "fit_slpf.py")
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)


def _table(true_irr, true_rf, irr_share):
    # yield of each regime: 100 * s^1.5 ku/ha-ish (convex, like the engine), as a quadratic in s
    s = np.array([0.6, 0.75, 0.9])
    c = np.polyfit(s, 100 * s ** 1.5, 2)[::-1]                # c0, c1, c2
    rows = []
    for k, f in enumerate(irr_share):
        a_irr, a_rf = f * c * 1.1, (1 - f) * c * 0.9          # irrigated potential 10% higher
        obs = (f * 1.1 * 100 * true_irr ** 1.5 + (1 - f) * 0.9 * 100 * true_rf ** 1.5)
        rows.append(dict(irr_c0=a_irr[0], irr_c1=a_irr[1], irr_c2=a_irr[2],
                         rai_c0=a_rf[0], rai_c1=a_rf[1], rai_c2=a_rf[2], obs=obs, ha=1000.0 + k))
    return pd.DataFrame(rows, index=[f"d{k}" for k in range(len(irr_share))])


def test_fit_recovers_two_factors():
    t = _table(0.80, 0.65, np.linspace(0.1, 1.0, 12))
    grid = np.round(np.arange(0.6, 0.9 + 1e-9, 0.005), 4)
    si, sr, obj = fs.fit(t, grid, joint=True)
    assert abs(si - 0.80) <= 0.005 and abs(sr - 0.65) <= 0.005 and obj < 1e-4
    one = fs.fit(t, grid, joint=False)
    assert one[2] > obj                                        # one shared factor fits worse
    st = fs.stats(t, si, sr)
    assert abs(st["level"] - 1) < 0.01 and st["spatial_r"] > 0.99


def test_row_model_matches_and_applies_slope():
    from scipy import sparse
    # two districts, three rows (seasons): yield = 100 * s exactly (c1 = 100)
    rows = pd.DataFrame({"code": ["a", "a", "b"], "iy": 0, "ix": [0, 1, 2],
                         "water": ["irrigated", "rainfed", "irrigated"], "tmin": [23.0, 23.0, 18.0],
                         "c0": 0.0, "c1": 100.0, "c2": 0.0})
    M = sparse.csr_matrix(np.array([[0.5, 0.5, 0.0], [0.0, 0.0, 1.0]]))
    p0 = fs.predict_rows(rows, M, 0.8, 0.6, 0.0, 0.5, 0.9)
    assert np.allclose(p0, [70.0, 80.0])                     # b = 0: regime values only
    p1 = fs.predict_rows(rows, M, 0.8, 0.6, 0.02, 0.5, 0.9)
    assert np.allclose(p1, [70.0, 70.0])                     # 5 C cooler: SLPF 0.8 - 0.1
    p2 = fs.predict_rows(rows, M, 0.8, 0.6, 0.1, 0.5, 0.9)
    assert np.allclose(p2[1], 50.0)                          # clipped at the lower bound

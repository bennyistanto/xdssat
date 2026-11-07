"""Tests for soils.py: pedotransfer, texture class, root growth, profile writing."""
import numpy as np
import pandas as pd
import pytest

import soils
from sol import read_sol, write_sol


def test_saxton_rawls_orders_and_loam_value():
    sand, clay = np.meshgrid(np.linspace(0.05, 0.9, 18), np.linspace(0.05, 0.6, 12))
    keep = sand + clay <= 0.95
    ll, dul, sat, ks = soils.saxton_rawls(sand[keep], clay[keep], 2.0)
    assert (ll > 0).all() and (ll < dul).all() and (dul < sat).all() and (sat < 0.7).all()
    assert (ks > 0).all()
    # loam, 40% sand, 20% clay, 2.5% OM, by hand:
    # t33 = -0.1004 + 0.039 + 0.0275 + 0.006 - 0.0135 + 0.03616 + 0.299 = 0.29376
    # dul = t33 + 1.283 t33^2 - 0.374 t33 - 0.015 = 0.27961
    _, dul, _, _ = soils.saxton_rawls(0.4, 0.2, 2.5)
    assert float(dul) == pytest.approx(0.27961, abs=1e-5)


def test_ksat_rises_with_sand():
    ks = soils.saxton_rawls(np.array([0.2, 0.5, 0.8]), np.array([0.2, 0.15, 0.05]), 1.5)[3]
    assert ks[0] < ks[1] < ks[2]


@pytest.mark.parametrize("sand,silt,clay,code", [
    (92, 5, 3, "S"), (82, 12, 6, "LS"), (65, 25, 10, "SL"), (40, 40, 20, "L"),
    (20, 65, 15, "SIL"), (5, 88, 7, "SI"), (60, 12, 28, "SCL"), (32, 34, 34, "CL"),
    (10, 58, 32, "SICL"), (52, 6, 42, "SC"), (6, 48, 46, "SIC"), (20, 20, 60, "C"),
])
def test_texture_class(sand, silt, clay, code):
    assert soils.texture_class(sand, silt, clay) == code


def test_root_growth_factor():
    g = soils.root_growth_factor(soils.LAYER_BOTTOMS)
    assert g[0] == g[1] == 1.0
    assert g[2] == pytest.approx(np.exp(-0.02 * 22.5))
    assert (np.diff(g) <= 0).all()


def _row():
    vals = {"clay": [300, 320, 350, 380, 400, 410], "sand": [300, 290, 270, 250, 240, 230],
            "silt": [400, 390, 380, 370, 360, 360], "soc": [180, 140, 100, 60, 40, 30],
            "bdod": [120, 125, 130, 135, 140, 140], "cfvo": [50, 50, 60, 70, 80, 90],
            "nitrogen": [180, 150, 110, 80, 60, 50], "phh2o": [60, 61, 62, 63, 64, 65],
            "cec": [200, 190, 180, 170, 160, 150]}
    r = {"iy": 131, "ix": 123}
    for p, v in vals.items():
        for d, x in zip(soils.DEPTHS, v):
            r[f"{p}_{d}"] = x / soils.PROPS[p]
    return r


def test_profile_round_trips_through_sol(tmp_path):
    prof = soils.make_profile(_row(), -6.6, 106.8, soils.profile_id(131, 123))
    assert prof.id == "XS01310123" and len(prof.id) == 10
    L = prof.layers
    assert list(L.SLB) == [5, 15, 30, 60, 100, 200]
    assert L.SLOC.iloc[0] == pytest.approx(1.8 * soils.SOC_SCALE["0-5cm"])
    assert L.SLOC.iloc[-1] == pytest.approx(0.3 * soils.SOC_SCALE["100-200cm"])
    assert L.SLNI.iloc[0] == pytest.approx(0.18)
    raw = soils.make_profile(_row(), -6.6, 106.8, "XS01310123", soc_scale=None)
    assert raw.layers.SLOC.iloc[0] == pytest.approx(1.8)
    assert L.SBDM.iloc[0] == pytest.approx(1.2) and L.SLCL.iloc[0] == pytest.approx(30.0)
    write_sol(tmp_path / "SOIL.SOL", [prof])
    back = read_sol(tmp_path / "SOIL.SOL")[prof.id]
    assert np.allclose(back.layers.SDUL, L.SDUL.round(3))
    assert back.texture == prof.texture


def test_fill_from_nearest():
    a = pd.DataFrame([_row(), {**_row(), "iy": 140, "ix": 130}])
    a.loc[1, "clay_0-5cm"] = np.nan
    f = soils.fill_from_nearest(a)
    assert not f[[c for c in f if c.startswith("clay")]].isna().any().any()
    assert f.loc[1, "filled_from"] == "131,123"


@pytest.mark.skipif(not soils.cache_path("clay", "0-5cm").exists(),
                    reason="SoilGrids regional cache not built (scripts/build_soilgrids_cache.py)")
def test_regional_cache_matches_source_tiles():
    rng = np.random.default_rng(0)
    lon, lat = rng.uniform(106.5, 111.0, 3000), rng.uniform(-7.6, -6.4, 3000)
    x, y = (np.asarray(v) for v in soils._to_igh.transform(lon, lat))
    a = soils.CachedLayer(soils.cache_path("clay", "0-5cm")).sample(x, y)
    b = soils.SoilGridsLayer(soils.vrt_path("clay", "0-5cm")).sample(x, y)
    assert np.array_equal(a, b)
    assert (a != soils.NODATA).mean() > 0.5


def test_soc_factors_take_band_medians():
    pairs = pd.DataFrame({"band": ["0-30"] * 3 + ["30-100"] * 2 + ["100-200"],
                          "ratio": [0.5, 0.7, 0.9, 0.6, 0.8, 0.4]})
    f = soils.soc_factors(pairs)
    assert f["0-5cm"] == f["15-30cm"] == 0.7
    assert f["60-100cm"] == pytest.approx(0.7) and f["100-200cm"] == 0.4


@pytest.mark.skipif(not soils.cache_path("soc", "0-5cm").exists(),
                    reason="SoilGrids regional cache not built (scripts/build_soilgrids_cache.py)")
def test_soc_scale_constants_match_wise_evidence():
    from conftest import DATA
    pairs = soils.wise_soc_pairs(DATA / "Soil" / "WI.SOL")
    assert pairs.id.nunique() == 78 and len(pairs) == 420
    f = soils.soc_factors(pairs)
    for d, v in soils.SOC_SCALE.items():
        assert f[d] == pytest.approx(v, abs=0.005)

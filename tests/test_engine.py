"""Tests that run the real DSSAT engine on rice fixtures from dssat-csm-data.

The central check: rebuilding a fixture experiment through xdssat's writers
(weather rewritten with 7-digit dates, soil rewritten into SOIL.SOL, FileX
rebuilt treatment by treatment from the template) gives the same simulated
dates, yields and water and N totals as running the original files.
"""
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import DATA, needs_engine
from filex import FileX, decode_date
from runner import run_chunk, run_engine
from sol import read_sol, write_sol
from wth import read_wth, write_wth

pytestmark = [pytest.mark.engine, needs_engine]

RICE = sorted((DATA / "Rice").glob("*.RIX")) if DATA.is_dir() else []
COMPARE = ["PDAT", "EDAT", "ADAT", "MDAT", "HDAT", "HWAM", "HWAH", "CWAM",
           "PRCP", "ETCP", "IRCM", "NUCM", "NICM"]


def _fixture_parts(fx: FileX):
    """WSTA (4 chars), soil ID and simulation start date of field level 1."""
    r1 = [ln for ln in fx.blocks("FL")[1] if not ln.startswith("@")][0]
    wsta, soil_id = r1[12:16], r1[69:79]
    sm = [ln for ln in fx.blocks("SM")[1] if not ln.startswith("@")][0]
    sdate = decode_date(sm[33:38])
    return wsta, soil_id, sdate


def _weather(wsta: str, year: int) -> pd.DataFrame:
    """The fixture's weather for ``year`` and, if present, the next year."""
    parts = []
    for y in (year, year + 1):
        p = DATA / "Weather" / f"{wsta}{y % 100:02d}01.WTH"
        if p.exists():
            parts.append(read_wth(p))
    st = parts[0][0]
    return st, pd.concat([w for _, w in parts])


def _soil(soil_id: str):
    for p in sorted((DATA / "Soil").glob("*.SOL")):
        found = read_sol(p, ids=[soil_id])
        if found:
            return p, found[soil_id]
    raise FileNotFoundError(soil_id)


@pytest.mark.parametrize("path", RICE, ids=[p.stem for p in RICE])
def test_rebuilt_fixture_matches_original(path, short_tmp):
    fx = FileX.read(path)
    wsta, soil_id, sdate = _fixture_parts(fx)
    sol_path, soil = _soil(soil_id)

    # original files, run as DSSAT ships them
    a = short_tmp / "a"
    a.mkdir()
    shutil.copy(path, a)
    for w in (DATA / "Weather").glob(f"{wsta}*.WTH"):
        shutil.copy(w, a)
    shutil.copy(sol_path, a / sol_path.name)
    orig = run_engine(a, path.name)
    assert orig.returncode == 0, orig.error_out[-800:]

    # rebuilt through xdssat
    st, weather = _weather(wsta, sdate.year)
    trt = fx.treatments()
    trt = trt[[fx.planting_date(int(m)) is not None for m in trt["MP"]]]
    units = pd.DataFrame({
        "cell": trt.index, "wsta": "GR000001", "soil_id": soil_id,
        "pdate": [pd.Timestamp(fx.planting_date(int(m))) for m in trt["MP"]],
        "trno_template": trt.index, "name": trt["TNAME"].values,
    })

    def write_inputs(run_dir, u):
        write_wth(run_dir / "GR000001.WTH", weather, st["INSI"], st["LAT"], st["LONG"],
                  tav=st.get("TAV"), amp=st.get("AMP"))
        write_sol(run_dir / "SOIL.SOL", [soil])

    res = run_chunk(1, units, fx, write_inputs, work_root=short_tmp)
    assert len(res.failed) == 0, res.failed.get("error")

    a_s = orig.summary.set_index("TRNO").loc[units["trno_template"]]
    b_s = res.summary.set_index("trno_template").loc[units["trno_template"]]
    cols = [c for c in COMPARE if c in a_s and c in b_s]
    assert len(cols) >= 6
    pd.testing.assert_frame_equal(
        b_s[cols].astype(float).reset_index(drop=True),
        a_s[cols].astype(float).reset_index(drop=True), check_names=False)


def _toy_inputs(soil_id="IBRI910002"):
    st, w = read_wth(DATA / "Weather" / "IRMZ8601.WTH")
    idx = pd.date_range("1999-01-01", "2002-12-31", freq="D")
    base = w.reset_index(drop=True)
    long = base.iloc[np.minimum(idx.dayofyear.values - 1, 364)].set_index(idx)
    soils = read_sol(DATA / "Soil" / "SOIL.SOL", ids=[soil_id])

    def write_inputs(run_dir, u):
        for c in u.drop_duplicates("cell").itertuples():
            write_wth(run_dir / f"{c.wsta}.WTH", long, "GRID", 15.7, 120.9)
        write_sol(run_dir / "SOIL.SOL", [p for k, p in soils.items() if k in set(u.soil_id)])
    return write_inputs, soils


def test_shifted_seasons_plant_on_requested_dates_and_bad_unit_is_isolated(short_tmp):
    fx = FileX.read(DATA / "Rice" / "IRMZ8601.RIX")
    rows = []
    for c in range(12):
        for y in (2000, 2001):
            for doy in (40, 160, 280):
                rows.append(dict(cell=c, wsta=f"GR{c:06d}", soil_id="IBRI910002",
                                 pdate=pd.Timestamp(y, 1, 1) + pd.Timedelta(days=doy + c),
                                 trno_template=5))
    units = pd.DataFrame(rows)
    units.loc[7, "soil_id"] = "XXXX000000"   # not in SOIL.SOL: engine stops with STOP 99
    write_inputs, soils = _toy_inputs()
    res = run_chunk(1, units, fx, write_inputs, work_root=short_tmp, soils=soils, ic_water=0.9)

    assert len(res.failed) == 1
    assert res.failed.iloc[0]["soil_id"] == "XXXX000000"
    assert "IPSOIL" in res.failed.iloc[0]["error"]
    assert len(res.summary) == len(units) - 1
    pdat = pd.to_datetime(res.summary["PDAT"].astype(int).astype(str), format="%Y%j")
    assert (pdat.values == pd.to_datetime(res.summary["pdate"]).values).all()
    assert (res.summary["HWAH"].astype(float) > 1000).all()


def test_daily_outputs_are_keyed_by_unit(short_tmp):
    fx = FileX.read(DATA / "Rice" / "IRMZ8601.RIX")
    units = pd.DataFrame({"cell": [1, 2], "wsta": ["GR000001", "GR000002"],
                          "soil_id": "IBRI910002", "trno_template": 5,
                          "pdate": pd.to_datetime(["2000-02-10", "2000-03-01"])})
    write_inputs, _ = _toy_inputs()
    res = run_chunk(1, units, fx, write_inputs, work_root=short_tmp, outputs="daily")
    pg = res.daily["plantgro"]
    assert set(pg["cell"]) == {1, 2}
    assert {"date", "LAID", "GSTD"} <= set(pg.columns)
    first = pg.groupby("cell")["date"].min()
    assert first[1] < first[2]


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "XDRI8601.RIX"


def test_rice_template_matches_source_and_rainfed_gets_no_irrigation(short_tmp):
    """templates/XDRI8601.RIX = IRMZ8601 + treatment 11 (rainfed, bunded)."""
    res = {}
    for name, path in (("orig", DATA / "Rice" / "IRMZ8601.RIX"), ("tpl", TEMPLATE)):
        d = short_tmp / name
        d.mkdir()
        shutil.copy(path, d / "IRMZ8601.RIX")
        for w in (DATA / "Weather").glob("IRMZ*.WTH"):
            shutil.copy(w, d)
        shutil.copy(DATA / "Soil" / "SOIL.SOL", d)
        r = run_engine(d, "IRMZ8601.RIX")
        assert r.returncode == 0, r.error_out[-500:]
        res[name] = r.summary.set_index("TRNO")
    cols = [c for c in COMPARE if c in res["orig"]]
    pd.testing.assert_frame_equal(res["tpl"].loc[1:10, cols].astype(float),
                                  res["orig"].loc[1:10, cols].astype(float))
    t5, t11 = res["tpl"].loc[5], res["tpl"].loc[11]
    assert float(t11["IRCM"]) == 0 and float(t5["IRCM"]) > 0
    assert float(t11["NICM"]) == float(t5["NICM"])        # same fertiliser


def _template_runs(short_tmp, template, units, name):
    write_inputs, soils = _toy_inputs()
    d = short_tmp / name
    d.mkdir()
    res = run_chunk(1, units, template, write_inputs, work_root=d, soils=soils, ic_water=0.9)
    assert len(res.failed) == 0, res.failed.get("error")
    return res.summary.set_index("cell")[["PDAT", "ADAT", "MDAT", "HWAH", "CWAM"]].astype(float)


def test_environment_modifications_change_the_season(short_tmp):
    """Scenario environment: A 0 changes nothing, CO2 raises yield, warming shortens the season."""
    from filex import with_environment
    tpl = FileX.read(TEMPLATE)
    units = pd.DataFrame({"cell": [1, 2], "wsta": ["GR000001", "GR000002"], "soil_id": "IBRI910002",
                          "trno_template": 16, "pdate": pd.to_datetime(["2000-02-10", "2001-02-20"])})
    base = _template_runs(short_tmp, tpl, units, "base")
    same = _template_runs(short_tmp, with_environment(tpl, {"EMAX": ("A", 0)}), units, "same")
    co2 = _template_runs(short_tmp, with_environment(tpl, {"ECO2": ("R", 700)}), units, "co2")
    warm = _template_runs(short_tmp, with_environment(tpl, {"EMAX": ("A", 3), "EMIN": ("A", 3)}),
                          units, "warm")
    pd.testing.assert_frame_equal(same, base)
    assert (co2.PDAT == base.PDAT).all() and (co2.HWAH > base.HWAH).all()
    assert (warm.MDAT < base.MDAT).all()


def test_cultivar_override_equals_template_cultivar_level(short_tmp):
    """IR 64 by CU.INGENO override on cultivar level 1 = IR 64 from its own level (treatment 26)."""
    tpl = FileX.read(TEMPLATE)
    units = pd.DataFrame({"cell": [1], "wsta": ["GR000001"], "soil_id": "IBRI910002",
                          "pdate": pd.to_datetime(["2000-02-10"])})
    own = _template_runs(short_tmp, tpl, units.assign(trno_template=26), "own")
    over = _template_runs(short_tmp, tpl, units.assign(trno_template=16, **{"CU.INGENO": "IB0015",
                                                                           "CU.CNAME": "IR 64"}), "over")
    ir58 = _template_runs(short_tmp, tpl, units.assign(trno_template=16), "ir58")
    pd.testing.assert_frame_equal(over, own)
    assert not own.equals(ir58)

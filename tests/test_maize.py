"""CERES-Maize: the maize template and xdssat's writers, checked on the real engine.

templates/XDMZ8201.MZX is DSSAT's UFGA8201.MZX (Gainesville 1982, N x irrigation;
Hoogenboom et al., 2019, section 12.1) plus automatic irrigation (MI 4 with SM 2,
IRRIG A), rainfed without irrigation (MI 0) and urea levels of 60, 120, 180 kg N
(scripts/make_templates.py). The fixture data are dssat-csm-data's Gainesville
weather (UFGA8201.WTH) and soil IBMZ910014 (Soil/SOIL.SOL).
"""
import shutil
from pathlib import Path

import pandas as pd
import pytest

from conftest import DATA, needs_engine
from filex import FileX
from runner import run_chunk, run_engine
from sol import read_sol, write_sol
from wth import read_wth, write_wth

pytestmark = [pytest.mark.engine, needs_engine]

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "XDMZ8201.MZX"
SOURCE = DATA / "Maize" / "UFGA8201.MZX"
COMPARE = ["PDAT", "EDAT", "ADAT", "MDAT", "HDAT", "HWAM", "HWAH", "CWAM",
           "PRCP", "ETCP", "IRCM", "NUCM", "NICM"]
# grid treatments of the template: (water regime, kg N) -> TRNO (make_templates.MZ_TRNO)
RATES = (0, 60, 116, 120, 180, 401)
GRID = {(w, n): 7 + i for i, (w, n) in enumerate((w, n) for w in ("irrigated", "rainfed") for n in RATES)}


def _run_file(d: Path, fx_path: Path):
    """Run a FileX (saved as UFGA8201.MZX) with the Gainesville weather and soil."""
    d.mkdir()
    shutil.copy(fx_path, d / "UFGA8201.MZX")
    for w in (DATA / "Weather").glob("UFGA8*.WTH"):
        shutil.copy(w, d)
    shutil.copy(DATA / "Soil" / "SOIL.SOL", d)
    r = run_engine(d, "UFGA8201.MZX")
    assert r.returncode == 0, r.error_out[-800:]
    return r.summary.set_index("TRNO")


def test_template_is_the_source_experiment_and_its_levels_act(short_tmp):
    """Treatments 1-6 equal UFGA8201; the grid treatments irrigate, fertilise and respond as written."""
    orig = _run_file(short_tmp / "orig", SOURCE)
    tpl = _run_file(short_tmp / "tpl", TEMPLATE)
    cols = [c for c in COMPARE if c in orig]
    pd.testing.assert_frame_equal(tpl.loc[1:6, cols].astype(float), orig.loc[1:6, cols].astype(float))
    assert len(tpl) == 6 + len(GRID)
    s = tpl.astype({"IRCM": float, "NICM": float, "HWAH": float})
    for (w, n), t in GRID.items():
        assert s.loc[t, "NICM"] == pytest.approx(n, abs=1), (w, n)    # the N the level says
        if w == "irrigated":
            assert s.loc[t, "IRCM"] > 0, t                               # automatic irrigation
        else:
            assert s.loc[t, "IRCM"] == 0, t                              # rainfed: no water applied
    for w in ("irrigated", "rainfed"):                                   # more N, more grain (0-120)
        y = [s.loc[GRID[(w, n)], "HWAH"] for n in (0, 60, 120)]
        assert y[0] < y[1] < y[2], (w, y)
    # same N, more water: auto-irrigated at least the yield of rainfed on the Gainesville sand
    assert s.loc[GRID[("irrigated", 120)], "HWAH"] >= s.loc[GRID[("rainfed", 120)], "HWAH"]


def test_rebuilt_template_seasons_match_the_template_run(short_tmp):
    """The template's grid treatments rebuilt through build_filex (7-digit weather, SOIL.SOL,
    one treatment per unit) give the same seasons as the template run as DSSAT ships it."""
    tpl_fx = FileX.read(TEMPLATE)
    assert tpl_fx.crop_code() == "MZ"
    direct = _run_file(short_tmp / "direct", TEMPLATE)
    st, w = read_wth(DATA / "Weather" / "UFGA8201.WTH")
    soil = read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBMZ910014"])["IBMZ910014"]
    trnos = sorted(GRID.values())
    units = pd.DataFrame({"cell": trnos, "wsta": "GR000001", "soil_id": "IBMZ910014",
                          "pdate": pd.Timestamp("1982-02-26"), "trno_template": trnos})

    def write_inputs(run_dir, u):
        write_wth(run_dir / "GR000001.WTH", w, st["INSI"], st["LAT"], st["LONG"],
                  tav=st.get("TAV"), amp=st.get("AMP"))
        write_sol(run_dir / "SOIL.SOL", [soil])

    res = run_chunk(1, units, tpl_fx, write_inputs, work_root=short_tmp)
    assert len(res.failed) == 0, res.failed.get("error")
    a = direct.loc[trnos]
    b = res.summary.set_index("trno_template").loc[trnos]
    cols = [c for c in COMPARE if c in a and c in b]
    pd.testing.assert_frame_equal(b[cols].astype(float).reset_index(drop=True),
                                  a[cols].astype(float).reset_index(drop=True), check_names=False)


def test_shifted_maize_seasons_with_soil_initial_water(short_tmp):
    """Seasons moved to other sowing dates, with initial soil water rebuilt from the soil
    (as run_aoi does): all run, sow on the requested day, and only irrigated ones get water."""
    tpl_fx = FileX.read(TEMPLATE)
    st, w = read_wth(DATA / "Weather" / "UFGA8201.WTH")
    soils = read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBMZ910014"])
    rows = []
    for regime in ("irrigated", "rainfed"):
        for day in ("1982-03-10", "1982-04-15"):
            rows.append(dict(cell=len(rows), wsta="GR000001", soil_id="IBMZ910014", water=regime,
                             pdate=pd.Timestamp(day), trno_template=GRID[(regime, 120)]))
    units = pd.DataFrame(rows)

    def write_inputs(run_dir, u):
        write_wth(run_dir / "GR000001.WTH", w, st["INSI"], st["LAT"], st["LONG"],
                  tav=st.get("TAV"), amp=st.get("AMP"))
        write_sol(run_dir / "SOIL.SOL", list(soils.values()))

    res = run_chunk(1, units, tpl_fx, write_inputs, work_root=short_tmp, soils=soils, ic_water=0.5)
    assert len(res.failed) == 0, res.failed.get("error")
    s = res.summary
    pdat = pd.to_datetime(s["PDAT"].astype(int).astype(str), format="%Y%j")
    assert (pdat.values == pd.to_datetime(s["pdate"]).values).all()
    assert (s.loc[s.water == "irrigated", "IRCM"].astype(float) > 0).all()
    assert (s.loc[s.water == "rainfed", "IRCM"].astype(float) == 0).all()
    assert (s["HWAH"].astype(float) > 0).all()
    assert (s["MODEL"].str.strip() == "MZCER048").all()


def _days_after_planting_copy(tmp_path) -> Path:
    """XDMZ8201.MZX with the irrigation and fertilizer of treatment 4 given as days after
    planting (management codes IRRIG D and FERTI D) instead of calendar dates."""
    import datetime as dt
    from filex import decode_date
    text = TEMPLATE.read_text(encoding="latin-1").splitlines()
    plant = dt.date(1982, 2, 26)
    out, section, hdr = [], None, ""
    for ln in text:
        if ln.startswith("*"):
            section = ln[1:].split()[0]
        if ln.startswith("@"):
            hdr = ln
        if section in ("IRRIGATION", "FERTILIZERS") and ln[:3].strip() in ("1", "2", "3") \
                and (hdr.startswith("@I IDATE") or hdr.startswith("@F FDATE")):
            d = decode_date(ln[3:8])
            ln = ln[:3] + f"{(d - plant).days:>5}" + ln[8:]
        if section == "SIMULATION" and ln.startswith(" 1 MA"):
            ln = " 1 MA              R     D     D     N     M"
        out.append(ln)
    p = tmp_path / "XDMZ8201.MZX"
    p.write_text("\n".join(out) + "\n", encoding="latin-1")
    return p


def test_days_after_planting_management_is_not_shifted_as_dates(short_tmp, tmp_path):
    """Code D (days after planting) keeps its numbers when a season moves; the same season
    with calendar dates (code R) and with days after planting gives the same result."""
    from filex import build_filex, management_codes
    d_path = _days_after_planting_copy(tmp_path)
    d_fx, r_fx = FileX.read(d_path), FileX.read(TEMPLATE)
    assert management_codes(d_fx, 1) == {"MI": "D", "MF": "D", "MR": "N", "MH": "M"}
    units = pd.DataFrame({"cell": [1], "wsta": "GR000001", "soil_id": "IBMZ910014",
                          "pdate": pd.Timestamp("1982-03-20"), "trno_template": 4})
    built = build_filex(d_fx, units).text
    assert "    17 FE001" in built            # 17 days after planting (82074 - 82057), not moved
    st, w = read_wth(DATA / "Weather" / "UFGA8201.WTH")
    soil = read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBMZ910014"])["IBMZ910014"]

    def write_inputs(run_dir, u):
        write_wth(run_dir / "GR000001.WTH", w, st["INSI"], st["LAT"], st["LONG"],
                  tav=st.get("TAV"), amp=st.get("AMP"))
        write_sol(run_dir / "SOIL.SOL", [soil])

    res = {}
    for name, fx in (("R", r_fx), ("D", d_fx)):
        d = short_tmp / name
        d.mkdir()
        r = run_chunk(1, units, fx, write_inputs, work_root=d)
        assert len(r.failed) == 0, r.failed.get("error")
        res[name] = r.summary.iloc[0]
    for c in ("PDAT", "ADAT", "MDAT", "HWAH", "CWAM", "IRCM", "NICM", "NUCM"):
        assert float(res["D"][c]) == float(res["R"][c]), c
    assert float(res["D"]["IRCM"]) > 0 and float(res["D"]["NICM"]) == pytest.approx(401, abs=1)


def test_simulation_switches_and_management_fields_act_on_the_engine(short_tmp):
    """Potential production (water and N off) yields more than water- and N-limited rainfed
    maize at 0 kg N; a wetter auto-irrigation threshold applies more water; population matters."""
    tpl = FileX.read(TEMPLATE)
    st, w = read_wth(DATA / "Weather" / "UFGA8201.WTH")
    soil = read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBMZ910014"])["IBMZ910014"]

    def write_inputs(run_dir, u):
        write_wth(run_dir / "GR000001.WTH", w, st["INSI"], st["LAT"], st["LONG"],
                  tav=st.get("TAV"), amp=st.get("AMP"))
        write_sol(run_dir / "SOIL.SOL", [soil])

    def season(name, trno, sim=None, **cols):
        u = pd.DataFrame({"cell": [1], "wsta": "GR000001", "soil_id": "IBMZ910014",
                          "pdate": pd.Timestamp("1982-02-26"), "trno_template": trno, **cols})
        d = short_tmp / name
        d.mkdir()
        r = run_chunk(1, u, tpl, write_inputs, work_root=d, simulation_overrides=sim)
        assert len(r.failed) == 0, r.failed.get("error")
        return r.summary.iloc[0]

    limited = season("limited", GRID[("rainfed", 0)])
    potential = season("potential", GRID[("rainfed", 0)], {"WATER": "N", "NITRO": "N"})
    assert float(potential.HWAH) > 1.5 * float(limited.HWAH)
    auto50 = season("auto50", GRID[("irrigated", 180)])
    auto80 = season("auto80", GRID[("irrigated", 180)], {"ITHRL": 80})
    assert float(auto80.IRCM) > float(auto50.IRCM) > 0
    sparse = season("sparse", GRID[("irrigated", 180)], None, **{"MP.PPOP": 3.0, "MP.PPOE": 3.0})
    assert float(sparse.HWAH) != float(auto50.HWAH)


def test_short_coefficient_names_are_overridden(short_tmp):
    """cultivar_coefficients on a short header name (P1): the field is as wide as its values
    (READS.for:997), so P1 200 instead of 259 brings anthesis earlier."""
    from genotype import cultivar_text, write_cultivar_files
    line = next(ln for ln in cultivar_text({"IB0035": {"P1": 200.0}}, "MZCER048").splitlines()
                if ln.startswith("IB0035"))
    assert line.split()[5] == "200.0"                  # after code, name (2 words), EXPNO, ECO#
    tpl = FileX.read(TEMPLATE)
    st, w = read_wth(DATA / "Weather" / "UFGA8201.WTH")
    soil = read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBMZ910014"])["IBMZ910014"]
    units = pd.DataFrame({"cell": [1], "wsta": "GR000001", "soil_id": "IBMZ910014",
                          "pdate": pd.Timestamp("1982-02-26"), "trno_template": GRID[("irrigated", 180)]})
    out = {}
    for name, over in (("own", None), ("p1", {"IB0035": {"P1": 200.0}})):
        def write_inputs(run_dir, u, over=over):
            write_wth(run_dir / "GR000001.WTH", w, st["INSI"], st["LAT"], st["LONG"],
                      tav=st.get("TAV"), amp=st.get("AMP"))
            write_sol(run_dir / "SOIL.SOL", [soil])
            write_cultivar_files(run_dir, over, "MZCER048")
        d = short_tmp / name
        d.mkdir()
        r = run_chunk(1, units, tpl, write_inputs, work_root=d)
        assert len(r.failed) == 0, r.failed.get("error")
        out[name] = int(r.summary.iloc[0].ADAT)
    assert out["p1"] < out["own"]

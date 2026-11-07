"""File-format tests that do not need the engine."""
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import DATA, needs_data
from filex import (FileX, TooManyLevels, build_filex, decode_date, encode_date,
                   field_rows, level_blocks)
from fixedwidth import format_row, parse_header
from runner import split_chunks
from sol import read_sol, write_sol
from wth import read_wth, tav_amp, write_wth


# --- fixedwidth ---------------------------------------------------------------
def test_parse_header_matches_parse_headers_rules():
    cols = parse_header("@DATE  SRAD  TMAX  TMIN  RAIN")
    assert [(c.name, c.start, c.end) for c in cols] == [
        ("DATE", 1, 5), ("SRAD", 7, 11), ("TMAX", 13, 17), ("TMIN", 19, 23), ("RAIN", 25, 29)]
    # the first column includes the '@' and any leading blanks
    assert parse_header("@ INSI      LAT")[0].name == "INSI"
    assert parse_header("@ INSI      LAT")[0].end == 6
    # trailing dots are dropped from names
    assert parse_header("@L ID_FIELD WSTA....  FLSA")[2].name == "WSTA"


def test_format_row_right_aligns_and_reduces_decimals():
    cols = parse_header("@  SLB  SLLL  SSKS")
    row = format_row(cols, {"SLB": 5, "SLLL": 0.258, "SSKS": 12.345},
                     {"SLB": ".0f", "SLLL": ".3f", "SSKS": ".3f"})
    assert row == "     5 0.258 12.35"


# --- dates -------------------------------------------------------------------
@pytest.mark.parametrize("txt,date", [
    ("86033", dt.date(1986, 2, 2)), ("05001", dt.date(2005, 1, 1)),
    ("40366", dt.date(2040, 12, 31)), ("41001", dt.date(1941, 1, 1)),
    ("2020060", dt.date(2020, 2, 29)), ("-99", None), ("0", None)])
def test_decode_date_follows_y2k_rule(txt, date):
    assert decode_date(txt) == date


def test_encode_date_roundtrip():
    for d in (dt.date(1979, 1, 1), dt.date(2000, 12, 31), dt.date(2024, 2, 29)):
        assert decode_date(encode_date(d)) == d
        assert decode_date(encode_date(d, 7)) == d
    with pytest.raises(ValueError):
        encode_date(dt.date(2041, 1, 1))


# --- weather -----------------------------------------------------------------
def _toy_weather(days=800):
    idx = pd.date_range("1999-06-01", periods=days, freq="D")
    rng = np.random.default_rng(1)
    return pd.DataFrame({"SRAD": rng.uniform(5, 25, days), "TMAX": rng.uniform(28, 34, days),
                         "TMIN": rng.uniform(20, 24, days), "RAIN": rng.gamma(0.5, 10, days)},
                        index=idx)


@pytest.mark.parametrize("seven", [True, False])
def test_wth_roundtrip(tmp_path, seven):
    w = _toy_weather()
    w.iloc[3, 0] = np.nan
    p = write_wth(tmp_path / "GR000001.WTH", w, "GR01", -6.2, 106.8, elev=10, seven_digit=seven)
    st, back = read_wth(p)
    assert st["INSI"] == "GR01" and st["LAT"] == -6.2 and st["LONG"] == 106.8
    assert back.index.equals(w.index.rename("date"))
    assert np.isnan(back.iloc[3, 0])
    np.testing.assert_allclose(back.values, w.round(1).values, atol=0.051)
    text = p.read_text()
    assert text.startswith("$WEATHER") == seven
    first = [ln for ln in text.splitlines() if ln[:1].isdigit()][0]
    assert first.startswith("1999152" if seven else "99152")


def test_wth_rejects_gaps(tmp_path):
    w = _toy_weather().drop(pd.Timestamp("1999-06-10"))
    with pytest.raises(ValueError, match="consecutive"):
        write_wth(tmp_path / "X.WTH", w, "X", 0, 0)


def test_tav_amp_uses_full_monthly_range():
    idx = pd.date_range("2001-01-01", "2001-12-31", freq="D")
    t = np.where(idx.month == 1, 20.0, np.where(idx.month == 7, 30.0, 25.0))
    tav, amp = tav_amp(t, t, idx)
    assert amp == pytest.approx(10.0)   # warmest minus coolest month, not halved


@needs_data
def test_wth_reads_fixture():
    st, w = read_wth(DATA / "Weather" / "IRMZ8601.WTH")
    assert st["INSI"] == "IRMZ" and st["TAV"] == 27.0 and len(w) == 365
    assert w.loc["1986-01-01", "TMAX"] == 32.5


# --- soil --------------------------------------------------------------------
@needs_data
def test_sol_roundtrip_all_profiles(tmp_path):
    profiles = read_sol(DATA / "Soil" / "SOIL.SOL")
    assert len(profiles) > 50
    p = write_sol(tmp_path / "SOIL.SOL", profiles.values())
    back = read_sol(p)
    assert set(back) == set(profiles)
    for pid, a in profiles.items():
        b = back[pid]
        cols = [c for c in a.layers.columns if c in b.layers.columns and c != "SLMH"]
        x = a.layers[cols].astype(float).fillna(-99).values
        y = b.layers[cols].astype(float).fillna(-99).values
        np.testing.assert_allclose(y, x, atol=0.051, err_msg=pid)
        assert a.depth == b.depth and a.texture == b.texture


@needs_data
def test_sol_fixture_values():
    p = read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBRI910002"])["IBRI910002"]
    assert list(p.layers["SLB"]) == [5, 20, 35, 50]
    assert p.layers["SDUL"].iloc[0] == 0.389
    assert p.surface["SLRO"] == 87.0


# --- FileX -------------------------------------------------------------------
def _all_filex():
    return sorted(p for p in DATA.glob("*/*.??X") if p.suffix[1:3].isalpha())


@needs_data
def test_field_rows_reproduce_every_fixture():
    """Rebuilding each *FIELDS level from its own values gives the same text
    up to column 79, for every FileX in dssat-csm-data."""
    checked = 0
    for path in _all_filex():
        fx = FileX.read(path)
        if fx.section("FIELDS") is None:
            continue
        for lvl, block in fx.blocks("FL").items():
            rows = [ln for ln in block if not ln.startswith("@")]
            if len(rows) < 2 or len(rows[0]) < 79:
                continue
            r1 = rows[0]
            new1, _ = field_rows(block, lvl, r1[3:11], r1[12:20].rstrip() or "XXXX",
                                 r1[69:79], None, None, None)
            assert new1[:79].rstrip() == r1[:79].rstrip(), f"{path.name} level {lvl}"
            checked += 1
    assert checked > 100


@needs_data
def test_level_blocks_keep_interleaved_tables():
    fx = FileX.read(DATA / "Rice" / "IRMZ8601.RIX")
    blocks = fx.blocks("MI")
    assert list(blocks) == [1, 2]
    b2 = blocks[2]
    assert b2[0].startswith("@I  EFIR") and b2[2].startswith("@I IDATE")
    assert sum(not ln.startswith("@") for ln in b2) == 21
    sm = fx.blocks("SM")[1]
    assert any("AUTOMATIC MANAGEMENT" in ln for ln in sm)


def _units(n_cells, n_dates, start="2001-01-15"):
    rows = []
    for c in range(n_cells):
        for k in range(n_dates):
            rows.append(dict(cell=c, wsta=f"GR{c:06d}", soil_id="IBRI910002",
                             pdate=pd.Timestamp(start) + pd.Timedelta(days=k + 100 * c),
                             trno_template=1))
    return pd.DataFrame(rows)


@needs_data
def test_build_shifts_all_dates_by_planting_offset():
    fx = FileX.read(DATA / "Rice" / "IRMZ8601.RIX")
    text = build_filex(fx, _units(1, 1, "2001-02-15"), exp_code="XD000001").text
    # template transplanting 86036 (5 Feb) -> 15 Feb 2001 = +10 days
    assert " 1 01046   -99" in text           # PDATE
    assert " 1 01043 IR010" in text           # puddling, 3 days before
    assert "S 01043" in text                  # SDATE
    assert "01039 01053" in text              # PFRST PLAST window
    assert "XD000001RI" in text.splitlines()[0]


@needs_data
def test_too_many_levels_raises_and_chunker_avoids_it():
    fx = FileX.read(DATA / "Rice" / "IRMZ8601.RIX")
    units = _units(2, 60)   # 120 distinct planting dates
    with pytest.raises(TooManyLevels):
        build_filex(fx, units)
    chunks = split_chunks(units, 200)
    assert sum(len(c) for c in chunks) == len(units)
    for c in chunks:
        build_filex(fx, c)  # must not raise


def test_chunker_caps_initial_condition_levels_per_soil():
    from runner import split_chunks
    import pandas as pd
    n = 40                                       # 40 cells, each its own soil, 5 dates
    units = pd.DataFrame([dict(cell=c, soil_id=f"S{c:08d}", trno_template=5,
                               pdate=pd.Timestamp(2000, 1, 1) + pd.Timedelta(days=30 * k))
                          for c in range(n) for k in range(5)])
    assert len(split_chunks(units, 999)) == 1
    chunks = split_chunks(units, 999, per_soil_ic=True)
    assert all(len(set(zip(c.soil_id, c.pdate))) <= 99 for c in chunks)
    assert sum(len(c) for c in chunks) == len(units) and len(chunks) == 3


def test_ic_from_soil_keeps_template_mineral_n_by_depth():
    """Rebuilt initial conditions follow the template's NH4/NO3 profile by depth
    (IRMZ8601: 10.1 ppm NH4 to 20 cm, 0.7 below), not the top layer everywhere."""
    import filex as fxm
    from sol import SoilProfile
    fx = fxm.FileX.read(DATA / "Rice" / "IRMZ8601.RIX")
    block = fx.blocks("IC")[1]
    bottoms = [5, 15, 30, 60, 100, 200]
    layers = pd.DataFrame({"SLB": bottoms, "SLLL": 0.2, "SDUL": 0.4, "SSAT": 0.5, "SLOC": 1.0})
    soil = SoilProfile(id="XS00000001", layers=layers)
    rows = fxm._ic_from_soil(block, 1, 0, soil, 0.5)
    hdr = next(r for r in rows if "ICBL" in r)
    cols = {c.name: c for c in fxm.parse_header(hdr)}
    data = rows[rows.index(hdr) + 1:]
    got = {int(fxm._span(r, cols["ICBL"])): float(fxm._span(r, cols["SNH4"])) for r in data}
    assert got == {5: 10.1, 15: 10.1, 30: 0.7, 60: 0.7, 100: 0.7, 200: 0.7}
    sh2o = [float(fxm._span(r, cols["SH2O"])) for r in data]
    assert sh2o == [0.3] * 6


def test_cultivar_override_changes_one_field():
    import config
    import genotype
    from conftest import has_engine
    if not has_engine or not (config.ENGINE_DIR / "Genotype" / "RICER048.CUL").exists():
        pytest.skip("engine Genotype folder not found")
    base = genotype.cultivar_text().splitlines()
    new = genotype.cultivar_text({"IB0012": {"TCLDF": 19.0}}).splitlines()
    changed = [(a, b) for a, b in zip(base, new) if a != b]
    assert len(changed) == 1 and changed[0][0].startswith("IB0012")
    a, b = changed[0]
    assert len(a.rstrip()) == len(b.rstrip())
    hdr = next(x for x in base if x.startswith("@VAR#"))
    end = hdr.index("TCLDF") + len("TCLDF")
    assert b[end - 5:end] == " 19.0" and a[end - 5:end] == " 15.0"
    with pytest.raises(KeyError):
        genotype.cultivar_text({"XX9999": {"TCLDF": 19.0}})


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "XDRI8601.RIX"


def test_environment_section_layout_and_dates_move_with_planting():
    from filex import with_environment
    fx = with_environment(FileX.read(TEMPLATE), {"ECO2": ("R", 550), "EMAX": ("A", 1.5)}, "co2")
    assert (fx.treatments()["ME"] == 1).all()
    names = [s.name for s in fx.sections]
    assert names.index("ENVIRONMENT MODIFICATIONS") == names.index("SIMULATION CONTROLS") - 1
    row = fx.section("ENVIRONMENT").body[1]
    # FORMAT (I3,I5,5(1X,A1,F4.0),1X,A1,F4.0,2(1X,A1,F4.0)) (IPENV.for:1000)
    assert row[:8] == " 1 86033"
    pairs = [(row[9 + 6 * i], row[10 + 6 * i:14 + 6 * i]) for i in range(8)]
    assert pairs == [("A", "   0"), ("A", "   0"), ("A", " 1.5"), ("A", "   0"), ("A", "   0"),
                     ("R", " 550"), ("A", "   0"), ("A", "   0")]
    units = pd.DataFrame({"cell": [1, 2], "wsta": ["GR000001", "GR000002"], "soil_id": "IBRI910002",
                          "trno_template": 16, "pdate": pd.to_datetime(["2000-02-26", "2001-03-08"])})
    text = build_filex(fx, units).text
    env = text.split("*ENVIRONMENT MODIFICATIONS")[1].split("*")[0].splitlines()
    odates = [ln[3:8] for ln in env if ln[:2].strip().isdigit()]
    # template: SDATE 86033 is 3 days before PDATE 86036; ODATE keeps that offset for every unit
    assert odates == [encode_date((d - pd.Timedelta(days=3)).date()) for d in units.pdate]
    with pytest.raises(ValueError):
        with_environment(fx, {"ECO2": ("R", 600)})       # already has environment modifications

"""Crop sequences (src/sequence.py) on the real engine, mode Q.

The central check, as for single seasons (tests/test_engine.py): a DSSAT sequence experiment
rebuilt through xdssat (weather rewritten as one 7-digit multi-year file, soil rewritten into
SOIL.SOL, the sequence rebuilt crop by crop) gives the same seasons as the original run as
DSSAT ships it. ITHY7501.SQX: soybean - chickpea - fallow at ICRISAT, Patancheru; chickpea is
sown in an automatic planting window, the fallow has no planting date.
"""
import shutil

import pandas as pd
import pytest

from conftest import DATA, needs_engine
from filex import FileX
from sequence import build_sequence, components, label_seasons, run_sequence, write_batch
from sol import read_sol, write_sol
from wth import read_wth, write_wth

pytestmark = [pytest.mark.engine, needs_engine]

SQX = DATA / "Sequence" / "ITHY7501.SQX"
YEARS = 5
COMPARE = ["PDAT", "ADAT", "MDAT", "HDAT", "HWAM", "CWAM", "PRCM", "ETCM", "NUCM", "NLCM", "SWXM"]


def _measured_weather_copy(path, out):
    """ITHY7501 with 5 cycles, 1 replicate and measured weather (WTHER M instead of WGEN)."""
    lines = path.read_text(encoding="latin-1").replace("\x1a", "").splitlines()
    for i, ln in enumerate(lines):
        if ln[3:5] == "GE" and ln[:3].strip().isdigit():
            ln = ln[:15] + f"{YEARS:>5} {1:>5}" + ln[26:]
        if ln[3:5] == "ME" and ln[:3].strip().isdigit():
            ln = ln[:19] + "M" + ln[20:]
        lines[i] = ln
    out.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return out


def test_rebuilt_sequence_matches_the_original(short_tmp):
    a = short_tmp / "orig"
    a.mkdir()
    orig = _measured_weather_copy(SQX, a / "ITHY7501.SQX")
    for w in sorted((DATA / "Weather").glob("ITHY*.WTH")):
        shutil.copy(w, a)
    shutil.copy(DATA / "Soil" / "SOIL.SOL", a)
    fx = FileX.read(orig)
    comp = components(fx, 1)
    ra = run_sequence(a, "ITHY7501.SQX", len(comp))
    assert ra.returncode == 0 and len(ra.summary) == YEARS * len(comp), ra.error_out[-800:]

    b = short_tmp / "built"
    b.mkdir()
    parts = [read_wth(w) for w in sorted((DATA / "Weather").glob("ITHY*.WTH"))[:YEARS + 1]]
    st = parts[0][0]
    write_wth(b / "GR000001.WTH", pd.concat([w for _, w in parts]), st["INSI"], st["LAT"], st["LONG"],
              tav=st.get("TAV"), amp=st.get("AMP"))
    write_sol(b / "SOIL.SOL", [read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBCH910010"])["IBCH910010"]])
    built = build_sequence(fx, 1, "GR000001", "IBCH910010", nyears=YEARS, exp_code="XDSQ0001")
    (b / "XDSQ0001.SQX").write_text(built.text, encoding="latin-1")
    rb = run_sequence(b, "XDSQ0001.SQX", len(built.components))
    assert rb.returncode == 0 and len(rb.summary) == len(ra.summary), rb.error_out[-800:]

    sa, sb = label_seasons(ra.summary, comp), label_seasons(rb.summary, built.components)
    assert list(sa.crop_order) == [1, 2, 3] * YEARS and list(sa.CR) == list(sb.CR)
    cols = [c for c in COMPARE if c in sa and c in sb]
    num = lambda d: d[cols].apply(pd.to_numeric, errors="coerce").astype(float)  # noqa: E731  fallow: NA
    pd.testing.assert_frame_equal(num(sb), num(sa))
    # chickpea is sown in its automatic window only when the soil allows (some cycles have none)
    ch = pd.to_numeric(sa.loc[sa.CR.str.strip() == "CH", "PDAT"], errors="coerce")
    assert ch.notna().any() and len(ch) == YEARS


def test_sequence_moves_with_the_first_sowing(short_tmp):
    """Planted 30 days later: every crop's planting moves 30 days, the cycle stays a cycle."""
    d = short_tmp / "moved"
    d.mkdir()
    fx = FileX.read(_measured_weather_copy(SQX, short_tmp / "ITHY7501.SQX"))
    parts = [read_wth(w) for w in sorted((DATA / "Weather").glob("ITHY*.WTH"))[:4]]
    st = parts[0][0]
    write_wth(d / "GR000001.WTH", pd.concat([w for _, w in parts]), st["INSI"], st["LAT"], st["LONG"],
              tav=st.get("TAV"), amp=st.get("AMP"))
    write_sol(d / "SOIL.SOL", [read_sol(DATA / "Soil" / "SOIL.SOL", ids=["IBCH910010"])["IBCH910010"]])
    base = build_sequence(fx, 1, "GR000001", "IBCH910010", nyears=2)
    moved = build_sequence(fx, 1, "GR000001", "IBCH910010", nyears=2, pdate=pd.Timestamp("1975-07-25"))
    out = {}
    for name, built in (("base", base), ("moved", moved)):
        (d / f"{name.upper()[:4]}0001.SQX").write_text(built.text, encoding="latin-1")
        r = run_sequence(d, f"{name.upper()[:4]}0001.SQX", len(built.components))
        assert r.returncode == 0, r.error_out[-800:]
        out[name] = label_seasons(r.summary, built.components)
    soy = lambda s: pd.to_datetime(s.loc[s.crop_order == 1, "PDAT"].astype(int).astype(str), format="%Y%j")  # noqa: E731
    assert ((soy(out["moved"]).values - soy(out["base"]).values) / pd.Timedelta(days=1) == 30).all()
    assert write_batch(d, "X.SQX", 3).read_text().count("X.SQX") == 3

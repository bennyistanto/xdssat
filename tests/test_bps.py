"""BPS web-table CSV parser (scripts/compare_bps.py)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "compare_bps", Path(__file__).resolve().parents[1] / "scripts" / "compare_bps.py")
compare_bps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_bps)

CSV = """﻿38 Provinsi,Produktivitas (Kuintal/Hektar),,,
,Padi,,,
,2000,2001,1993,
JAWA BARAT,49.12,49.5,50.27,
BANTEN,-,42.78,-,
INDONESIA,44.01,43.88,43.78,
,,,,
Catatan,<ul>,<li>Padi : Kualitas produksi gabah kering giling</li>,,
"""


def test_read_bps(tmp_path):
    p = tmp_path / "bps.csv"
    p.write_text(CSV, encoding="utf-8")
    t = compare_bps.read_bps(p)
    assert set(t.province) == {"JAWA BARAT", "BANTEN", "INDONESIA"}
    assert len(t) == 3 + 1 + 3                      # "-" dropped
    jb = t[t.province == "JAWA BARAT"].set_index("year").bps_ku_ha
    assert list(jb.index) == [1993, 2000, 2001] and jb[1993] == 50.27
    assert t[t.province == "BANTEN"].year.tolist() == [2001]

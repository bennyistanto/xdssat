"""Fetch district rice harvested area, production and yield from the Ministry of
Agriculture BDSP portal (https://bdsp2.pertanian.go.id/bdsp/id/lokasi).

    python scripts/fetch_bdsp.py                  # all provinces, three indicators
    python scripts/fetch_bdsp.py --prov 31 32 33 34 35 36
    python scripts/fetch_bdsp.py --parse-only     # rebuild the table from the saved pages

The portal's "Lokasi" search (Cari) posts the form to lokasi/result and returns
an HTML table: one row per district ("<BPS code> - <name>"), one column per year,
values as text with a comma decimal separator. The page names BPS (Badan Pusat
Statistik) as the data source. One request per province and indicator, sent one
at a time with a pause (--pause, default 3 s). Codes come from the portal's own
lookups (komoditas/getKomBySubsektor, indikator/getIndiByKomSubsek,
satuan/getSatuan, lokasi/getProv), saved in lookups.json.

Writes to --out (default <XDSSAT_CACHE>/obs/bdsp):
    raw/<indicator>_<prov>.html    each reply as received (reruns skip these)
    lookups.json
    bdsp_padi_adm2_long.csv        indicator, unit, sheet (province), code, name,
                                   year, raw (text as shown), value (parsed; 0 -> empty)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

BASE = "https://bdsp2.pertanian.go.id/bdsp/id/"
SUBSEKTOR = ("01", "Tanaman Pangan")
KOMODITAS = "PADI"
STATUS = ("6", "Angka Tetap")          # the form's default status
YEARS = (1970, 2026)


class Portal:
    def __init__(self, pause: float):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "xdssat research script (python-requests)"
        self.pause = pause

    def call(self, method: str, path: str, tries: int = 4, **kw) -> requests.Response:
        for k in range(tries):
            try:
                r = self.s.request(method, BASE + path, timeout=180, **kw)
                r.raise_for_status()
                time.sleep(self.pause)
                return r
            except requests.RequestException as e:
                wait = self.pause * 4 * (k + 1)
                print(f"  {path}: {e}; retry in {wait:.0f} s", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"{path} failed {tries} times")


def lookups(p: Portal, path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    p.call("GET", "lokasi")
    kom = p.call("POST", "komoditas/getKomBySubsektor", data={"subsektorcd": SUBSEKTOR[0]}).json()
    padi = next(k["fkomcd"] for k in kom if k["fkomnm"].strip().upper() == KOMODITAS)
    ind = p.call("POST", "indikator/getIndiByKomSubsek", data={"subsektorcd": SUBSEKTOR[0], "komcd": padi}).json()
    for i in ind:
        sat = p.call("POST", "satuan/getSatuan",
                     data={"indikatorcd": i["findicd"], "subsektorcd": SUBSEKTOR[0], "komcd": padi}).json()
        i["satuan"] = sat[0]
    prov = p.call("GET", "lokasi/getProv").json()
    out = dict(komoditas=padi, indikator=ind, prov=prov)
    path.write_text(json.dumps(out, indent=1))
    return out


def form(lk: dict, ind: dict, prov: dict) -> dict:
    return dict(subsektor=SUBSEKTOR[0], subsektorcd=SUBSEKTOR[0], subsektornm=SUBSEKTOR[1],
                level="03", levelnm="Kabupaten", prov=prov["fkode_prop"], provnm=prov["nama_prop"],
                satuan=ind["satuan"]["fkeyid"], satuannm=ind["satuan"]["fsatuan"],
                sts_angka=STATUS[0], sts_angkanm=STATUS[1],
                tahunAwal=str(YEARS[0]), tahunAkhir=str(YEARS[1]),
                komoditas=lk["komoditas"], komoditasnm=KOMODITAS,
                indikator=ind["findicd"], indikatornm=ind["findinm"])


def parse_page(html: str) -> pd.DataFrame:
    """The result table, every cell as text."""
    from lxml import html as lh
    doc = lh.fromstring(html)
    best = None
    for tb in doc.iter("table"):
        rows = [[" ".join(c.text_content().split()) for c in tr if c.tag in ("td", "th")]
                for tr in tb.iter("tr")]
        rows = [r for r in rows if r]
        if rows and "Lokasi" in rows[0] and (best is None or len(rows) > len(best)):
            best = rows
    if best is None:
        raise ValueError("no result table (a header row with 'Lokasi') in the page")
    hdr, body = best[0], [r for r in best[1:] if len(r) == len(best[0])]
    return pd.DataFrame(body, columns=hdr)


def to_long(raw_dir: Path, lk: dict) -> pd.DataFrame:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from clean_moa_yields import parse_value
    import re
    names = {i["findicd"]: (i["findinm"], i["satuan"]["fsatuan"]) for i in lk["indikator"]}
    rows = []
    for f in sorted(raw_dir.glob("*.html")):
        indcd, prov = f.stem.split("_")
        t = parse_page(f.read_text(encoding="utf-8"))
        years = [c for c in t.columns if c.isdigit()]
        for vals in t.to_dict("records"):
            m = re.match(r"^\s*(\d+)\s*-\s*(.+?)\s*$", str(vals["Lokasi"]))
            if not m:
                continue
            for y in years:
                rows.append((names[indcd][0], names[indcd][1], prov, m.group(1), m.group(2), int(y), vals[y]))
    d = pd.DataFrame(rows, columns=["indicator", "unit", "sheet", "code", "name", "year", "raw"])
    d["value"] = d.raw.map(parse_value)
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "obs" / "bdsp")
    ap.add_argument("--prov", nargs="+", help="province codes (default all)")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between requests")
    ap.add_argument("--parse-only", action="store_true")
    a = ap.parse_args()
    raw = a.out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    p = Portal(a.pause)
    lk = lookups(p, a.out / "lookups.json")

    if not a.parse_only:
        provs = [x for x in lk["prov"] if not a.prov or x["fkode_prop"] in a.prov]
        todo = [(i, pr) for pr in provs for i in lk["indikator"]
                if not (raw / f"{i['findicd']}_{pr['fkode_prop']}.html").exists()]
        print(f"{len(todo)} requests to send ({len(provs)} provinces x {len(lk['indikator'])} indicators, "
              f"cached ones skipped), pause {a.pause} s", flush=True)
        t0 = time.time()
        for k, (i, pr) in enumerate(todo, 1):
            r = p.call("POST", "lokasi/result", data=form(lk, i, pr))
            n = len(parse_page(r.text))
            (raw / f"{i['findicd']}_{pr['fkode_prop']}.html").write_text(r.text, encoding="utf-8")
            print(f"{k}/{len(todo)} {i['findinm']} {pr['nama_prop']}: {n} rows, {len(r.text) / 1e3:.0f} kB, "
                  f"{time.time() - t0:.0f} s", flush=True)

    d = to_long(raw, lk)
    d.to_csv(a.out / "bdsp_padi_adm2_long.csv", index=False)
    print(f"parsed {d.groupby('indicator').code.nunique().to_dict()} districts, "
          f"{d[d.value.notna()].groupby('indicator').size().to_dict()} non-zero values -> "
          f"{a.out / 'bdsp_padi_adm2_long.csv'}")


if __name__ == "__main__":
    main()

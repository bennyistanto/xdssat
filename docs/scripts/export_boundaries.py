"""Small boundary files for the location maps of the case-study pages (docs/cases/data/<case>/).

    python docs/scripts/export_boundaries.py niger kenya
    python docs/scripts/export_boundaries.py all --source D:/temp/boundaries/EdgeMatch

Source: edge-matched administrative boundaries compiled from OCHA's Common Operational Datasets
(COD-AB, HDX), one shapefile per level (adm0/adm0_polygons.shp, adm1/..., adm2/...). The files are
large (1-2 GB), so each level is read through a bounding-box filter, clipped and simplified.

Writes per case:
  countries.geojson  the case's country and its neighbours (locator map), name + iso3
  adm1.geojson       first-level units around the box, name + label point
  adm2.geojson       second-level units around the box, name + label point
  boundaries.json    box, country, and the source / licence / URL of each country's boundaries
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import pyogrio
from shapely.geometry import MultiPolygon
from shapely.geometry import box as shapely_box
from shapely import make_valid, set_precision
from shapely.ops import polylabel
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "cases" / "data"
SOURCE = Path("D:/temp/boundaries/EdgeMatch")

# case: (iso3 of the country, study box west, south, east, north); boxes as in configs/cases/*.yml
CASES = {
    "niger": ("NER", (2.0, 13.0, 3.0, 14.0)),
    "kenya": ("KEN", (34.6, 0.1, 35.6, 1.1)),
    "mekong": ("VNM", (105.2, 9.7, 106.2, 10.7)),
    "luzon": ("PHL", (120.5, 16.0, 121.5, 17.0)),
}
# cases in Indonesia use the Java case's own boundaries (docs/java/data), not the OCHA set
LOCAL = {
    "javamaize": ("IDN", (111.4, -8.0, 112.4, -7.0)),
}


def _read(source: Path, level: str, bbox, columns):
    f = source / level / f"{level}_polygons.shp"
    return pyogrio.read_dataframe(f, bbox=bbox, columns=columns)


def _polygons(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [p for g in geom.geoms for p in _polygons(g)]
    return []


def _clockwise(geom, tol: float = 0.0, min_area: float = 1e-4):
    """Simplify, snap to 0.001 degree, repair, drop slivers (islets below min_area square degrees),
    then orient every outer ring clockwise: d3-geo (Observable Plot) fills the whole sphere for an
    anticlockwise ring, and tiny islands collapsed by rounding can end up either way."""
    if geom is None or geom.is_empty:
        return geom
    if tol:
        geom = geom.simplify(tol, preserve_topology=True)
    geom = make_valid(set_precision(geom, 0.001))
    polys = [orient(p, sign=-1.0) for p in _polygons(geom) if p.area >= min_area]
    return MultiPolygon(polys) if len(polys) > 1 else (polys[0] if polys else None)


def _labelled(g: gpd.GeoDataFrame, name: str, tol: float, clip) -> dict:
    g = g.clip(clip)
    g = g[~g.geometry.is_empty].copy()
    g["geometry"] = g.geometry.apply(lambda x: _clockwise(x, tol, min_area=1e-6))
    g = g[g.geometry.notna()]
    pts = g.geometry.representative_point()
    out = gpd.GeoDataFrame({"name": g[name].astype(str), "lon": pts.x.round(3), "lat": pts.y.round(3)},
                           geometry=g.geometry, crs=g.crs)
    return json.loads(out.to_json(drop_id=True))


def _round(obj, nd=3):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, list):
        return [_round(v, nd) for v in obj]
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    return obj


def _ocha(case: str, source: Path):
    """Country, neighbours and admin units of a case from the edge-matched OCHA shapefiles."""
    iso3, (w, s, e, n) = CASES[case]
    c = _read(source, "adm0", (w, s, e, n), ["iso_3", "adm0_name"])
    country = c[c.iso_3 == iso3]
    cw, cs, ce, cn = country.total_bounds
    ext = (cw - 2, cs - 2, ce + 2, cn + 2)             # every country within 2 degrees (locator map)
    near = _read(source, "adm0", ext, ["iso_3", "adm0_name"]).clip(shapely_box(*ext))
    near = near.rename(columns={"adm0_name": "name", "iso_3": "iso3"})
    a1 = _read(source, "adm1", (w - 1.0, s - 1.0, e + 1.0, n + 1.0),
               ["adm1_name", "iso_3", "src_name", "src_lic", "src_url", "src_update"])
    a2 = _read(source, "adm2", (w - 0.3, s - 0.3, e + 0.3, n + 0.3), ["adm2_name", "adm1_name", "iso_3"])
    sources = (a1.drop(columns="geometry").drop_duplicates("iso_3")
               .set_index("iso_3")[["src_name", "src_lic", "src_url", "src_update"]].astype(str).to_dict("index"))
    return (str(country.adm0_name.iloc[0]), (cw, cs, ce, cn), near, a1, a2, sources,
            "Edge-matched administrative boundaries from OCHA COD-AB (HDX)")


def _local(case: str, folder: Path):
    """A case inside the Java case's own boundaries (docs/java/data: Java outline, provinces, districts)."""
    iso3, (w, s, e, n) = LOCAL[case]
    outline = gpd.read_file(folder / "java_outline.geojson")
    near = gpd.GeoDataFrame({"name": ["Java"], "iso3": [iso3]}, geometry=outline.geometry.values, crs=outline.crs)
    a1 = gpd.read_file(folder / "provinces.geojson").rename(columns={"name": "adm1_name"})
    a1 = a1[a1.intersects(shapely_box(w - 1.0, s - 1.0, e + 1.0, n + 1.0))]
    a2 = gpd.read_file(folder / "districts.geojson").rename(columns={"name": "adm2_name"})
    a2 = a2[a2.intersects(shapely_box(w - 0.3, s - 0.3, e + 0.3, n + 0.3))]
    sources = {iso3: {"src_name": "Indonesian province and district boundaries with BPS codes "
                                  "(idn_bnd_adm1, idn_bnd_adm2), as on the Java rice pages",
                      "src_lic": "not documented in this repository", "src_url": "../java/data.qmd",
                      "src_update": "-"}}
    return ("Java", tuple(float(v) for v in near.total_bounds), near, a1, a2, sources,
            "The Java case's province and district boundaries (docs/java/data)")


def export(case: str, source: Path = SOURCE) -> dict:
    iso3, (w, s, e, n) = (CASES | LOCAL)[case]
    out = OUT / case
    out.mkdir(parents=True, exist_ok=True)
    cname, (cw, cs, ce, cn), near, a1, a2, sources, dataset = (
        _local(case, ROOT / "docs" / "java" / "data") if case in LOCAL else _ocha(case, source))
    near = near.copy()
    near["geometry"] = near.geometry.apply(lambda x: _clockwise(x, 0.02, min_area=1e-3))
    near = near[near.geometry.notna()]
    # label point: inside the part of each country that lies well within the locator frame
    # (the frame is the country extent plus 4%, as in locatorMap in docs/assets/js/dl.js)
    pad = 0.04 * max(ce - cw, cn - cs)
    W, H = ce - cw + 2 * pad, cn - cs + 2 * pad
    inner = shapely_box(cw - pad + 0.02 * W, cs - pad + 0.04 * H, ce + pad - 0.02 * W, cn + pad - 0.04 * H)

    def label_point(g):
        """most interior point of the largest visible piece; none when under 4% of the frame shows"""
        part = g.intersection(inner)
        pieces = _polygons(part)
        if not pieces or part.area < 0.04 * inner.area:
            return None
        return polylabel(max(pieces, key=lambda p: p.area), tolerance=0.01)

    lab = [label_point(g) for g in near.geometry]
    near = near.assign(lon=[round(p.x, 3) if p else None for p in lab],
                       lat=[round(p.y, 3) if p else None for p in lab])
    countries = json.loads(near[["name", "iso3", "lon", "lat", "geometry"]].to_json(drop_id=True))
    # admin 1 and 2 around the box
    a1box = (w - 1.0, s - 1.0, e + 1.0, n + 1.0)
    a2box = (w - 0.3, s - 0.3, e + 0.3, n + 0.3)
    files = {"countries.geojson": countries,
             "adm1.geojson": _labelled(a1, "adm1_name", 0.005, shapely_box(*a1box)),
             "adm2.geojson": _labelled(a2, "adm2_name", 0.002, shapely_box(*a2box))}
    for name, gj in files.items():
        (out / name).write_text(json.dumps(_round(gj), separators=(",", ":")))
    # which first-level units the box covers, and how much of each lies inside it (equal-area CRS)
    bx = gpd.GeoSeries([shapely_box(w, s, e, n)], crs=a1.crs).to_crs("EPSG:6933").iloc[0]
    eq = a1.to_crs("EPSG:6933")
    cover = [{"name": str(r.adm1_name), "share_of_unit_in_box": round(r.geometry.intersection(bx).area / r.geometry.area, 3),
              "share_of_box": round(r.geometry.intersection(bx).area / bx.area, 3)}
             for r in eq.itertuples() if r.geometry.intersects(bx) and r.geometry.intersection(bx).area > 0]
    cover = sorted([c for c in cover if c["share_of_box"] >= 0.005], key=lambda c: -c["share_of_box"])
    info = {"case": case, "iso3": iso3, "country": cname, "box": [w, s, e, n],
            "box_adm1": cover,
            "country_bounds": [round(float(v), 3) for v in (cw, cs, ce, cn)],
            "dataset": dataset, "sources": sources,
            "files": {k: (out / k).stat().st_size for k in files}}
    (out / "boundaries.json").write_text(json.dumps(info, indent=1))
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="+", choices=[*CASES, *LOCAL, "all"])
    ap.add_argument("--source", type=Path, default=SOURCE, help="folder with adm0/ adm1/ adm2/ shapefiles")
    a = ap.parse_args(argv)
    for case in ([*CASES, *LOCAL] if "all" in a.cases else a.cases):
        info = export(case, a.source)
        print(case, json.dumps({k: info[k] for k in ("country", "box", "files", "sources")}))


if __name__ == "__main__":
    main()

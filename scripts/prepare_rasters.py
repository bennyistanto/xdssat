"""Burn rice fields, soil and regions onto the 100 m equal-area grid.

    python scripts/prepare_rasters.py                    # all of Indonesia
    python scripts/prepare_rasters.py --aoi java --out C:/TEMP/x   # a window, for testing

Inputs (under paths > inputs of the experiment file, or XDSSAT_INPUTS):
    idn-lbs/idn_phy_luasbakusawah_atrbpn_2024_a.gpkg   rice fields, ATR/BPN 2024
    idn-soil/idn_phy_tanahsemidetil_50k_bbsdlp_a.shp   soil, BBSDLP 1:50k (no Papua)
    idn-soil/idn_phy_agroecological_zone_bbsdlp_a.shp  island groups and AEZ zones
    idn-lbs/idn_phy_ricefield_java_{irrigated,rainfed}_moa_a.shp
                                                       Java irrigation type, Ministry
                                                       of Agriculture (attributes *_09)

Outputs (in --out, default <XDSSAT_CACHE>/rasters):
    <name>_rice.tif     band 1 share of the 1 ha pixel under rice (percent),
                        band 2 rice field type (rasters.RICE_CLASSES)
    <name>_soil.tif     dominant soil (JNTNH1) code, table in <name>_soil_classes.csv
    <name>_region.tif   island group code, table in <name>_region_classes.csv
    <name>_aez.tif      agro-ecological zone (SIMBOL) code, table in <name>_aez_classes.csv
    <name>_irrigation.tif  MoA irrigation type (Java only), table in
                        <name>_irrigation_classes.csv
    <name>_irrigation_filled.tif  band 1 MoA class where Java rice (LBS) has none
                        taken from the nearest MoA pixel; band 2 that distance
                        in 100 m pixels (0 = own class, capped at 255)
    <name>_province.tif, <name>_province_filled.tif
                        province (PROVINSI of the MoA files, Java only), table in
                        <name>_province_classes.csv; filled like irrigation. Used to
                        compare with BPS provincial statistics (scripts/compare_bps.py)
    <name>_adm2.tif, <name>_adm2_filled.tif
                        district (admin2) BPS code (a2bps) from --adm2 (latest
                        boundaries), uint16; the filled copy gives Java rice
                        outside every polygon the nearest district. Used by
                        scripts/compare_districts.py
    <name>_check.csv    rice area burned vs polygon area, per rice class
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import rasters  # noqa: E402

RICE = "idn-lbs/idn_phy_luasbakusawah_atrbpn_2024_a.gpkg"
SOIL = "idn-soil/idn_phy_tanahsemidetil_50k_bbsdlp_a.shp"
AEZ = "idn-soil/idn_phy_agroecological_zone_bbsdlp_a.shp"
MOA = {"irrigated": "idn-lbs/idn_phy_ricefield_java_irrigated_moa_a.shp",
       "rainfed": "idn-lbs/idn_phy_ricefield_java_rainfed_moa_a.shp"}
# IRIGASI_09 in the MoA files, ordered from most to least controlled water.
ADM2 = "idn_bnd_adm2.shp"      # in paths > boundaries (BPS-coded admin2 districts)
MOA_CLASSES = {"Irigasi Teknis": 1, "Irigasi Semi Teknis": 2, "Irigasi Sederhana": 3,
               "Tadah Hujan": 4}

# Extent of the MoA irrigated / rainfed rice maps, which cover Java only (a property of
# that dataset, not a run setting; the run area is aoi > bbox in the experiment file).
MOA_BOX = (105.0, -9.0, 114.7, -5.0)


def coded(gdf, column, out_csv, title_case=False):
    """Add an integer code column (1..n, sorted by name) and write the lookup table."""
    names = gdf[column].fillna("(none)").astype(str).str.split().str.join(" ")
    if title_case:      # the soil map spells some classes both ways, e.g. "Mediteran haplik"
        names = names.str.title()
    cats = sorted(names.unique())
    codes = {n: i + 1 for i, n in enumerate(cats)}
    gdf = gdf.assign(code=names.map(codes).astype("int32"))
    area = gdf.geometry.area / 1e4
    table = (pd.DataFrame({"name": names, "code": gdf["code"], "ha": area})
               .groupby(["code", "name"], as_index=False)
               .agg(polygons=("ha", "size"), polygon_ha=("ha", "sum")))
    table.to_csv(out_csv, index=False)
    return gdf, table


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aoi", choices=sorted(rasters.AOI), help="burn only this window")
    ap.add_argument("--out", type=Path, default=config.CACHE_DIR / "rasters")
    ap.add_argument("--inputs", type=Path, default=config.INPUTS_DIR)
    ap.add_argument("--layers", nargs="+",
                    default=["region", "aez", "soil", "rice", "irrigation", "irrigation_filled",
                             "province", "province_filled", "adm2", "adm2_filled"])
    ap.add_argument("--adm2", type=Path, help="admin2 boundaries with a2bps codes "
                    "(default <paths > boundaries>/idn_bnd_adm2.shp)")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    grid, name, bbox = rasters.IDN, "idn", None
    if a.aoi:
        bbox = rasters.AOI[a.aoi]
        grid, _ = rasters.IDN.window(*bbox)
        name = a.aoi
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)  # noqa: E731
    log(f"grid {name}: {grid.width} x {grid.height} pixels, bounds {grid.bounds}")

    def load(rel, columns, encoding=None, use_bbox=True):
        t0 = time.time()
        g = pyogrio.read_dataframe(a.inputs / rel, columns=columns,
                                   bbox=bbox if use_bbox else None, encoding=encoding)
        g = g[g.geometry.notna() & ~g.geometry.is_empty]
        g = rasters.to_grid_crs(g)
        log(f"read {rel}: {len(g)} polygons, {time.time() - t0:.0f} s")
        return g

    if "region" in a.layers or "aez" in a.layers:
        aez = load(AEZ, ["SIMBOL", "KML_FOLDER"], encoding="latin1")
        if "region" in a.layers:
            g, t = coded(aez, "KML_FOLDER", a.out / f"{name}_region_classes.csv")
            rasters.burn(g, "code", a.out / f"{name}_region.tif", grid, workers=a.workers, log=log)
        if "aez" in a.layers:
            g, t = coded(aez, "SIMBOL", a.out / f"{name}_aez_classes.csv")
            rasters.burn(g, "code", a.out / f"{name}_aez.tif", grid, workers=a.workers, log=log)

    if "soil" in a.layers:
        soil = load(SOIL, ["JNTNH1"])
        g, t = coded(soil, "JNTNH1", a.out / f"{name}_soil_classes.csv", title_case=True)
        rasters.burn(g, "code", a.out / f"{name}_soil.tif", grid, dtype="uint16",
                     workers=a.workers, log=log)

    if "irrigation" in a.layers:
        # The MoA files are Java only and in a custom Mercator, so read them whole.
        moa = pd.concat([load(p, ["IRIGASI_09", "INTENS_09"], use_bbox=False)
                         for p in MOA.values()], ignore_index=True)
        unknown = set(moa["IRIGASI_09"]) - set(MOA_CLASSES)
        if unknown:
            raise ValueError(f"unmapped IRIGASI_09 values: {unknown}")
        moa["code"] = moa["IRIGASI_09"].map(MOA_CLASSES).astype("int32")
        t = (moa.assign(ha=moa.geometry.area / 1e4,
                        intensity=moa["INTENS_09"].str.lower().str.replace(" ", "").str.replace("thn", "th"))
                .groupby(["code", "IRIGASI_09", "intensity"], as_index=False)
                .agg(polygons=("ha", "size"), polygon_ha=("ha", "sum"))
                .rename(columns={"IRIGASI_09": "name"}))
        t.to_csv(a.out / f"{name}_irrigation_classes.csv", index=False)
        log("MoA irrigation classes:\n" + t.round(0).to_string(index=False))
        rasters.burn(moa, "code", a.out / f"{name}_irrigation.tif", grid, workers=a.workers, log=log)

    if "province" in a.layers:
        prov = pd.concat([load(p, ["PROVINSI"], use_bbox=False) for p in MOA.values()], ignore_index=True)
        g, t = coded(prov, "PROVINSI", a.out / f"{name}_province_classes.csv")
        log("MoA provinces:\n" + t.round(0).to_string(index=False))
        rasters.burn(g, "code", a.out / f"{name}_province.tif", grid, workers=a.workers, log=log)

    if "adm2" in a.layers:
        t0 = time.time()
        a.adm2 = a.adm2 or config.BOUNDARIES_DIR / ADM2
        adm = pyogrio.read_dataframe(a.adm2, columns=["a2bps", "a2name"], bbox=bbox)
        adm = adm[adm.geometry.notna() & ~adm.geometry.is_empty & (adm.a2bps.astype(str) != "0")]
        adm = rasters.to_grid_crs(adm)
        adm["code"] = adm.a2bps.astype(int)
        log(f"read {a.adm2.name}: {len(adm)} districts, {time.time() - t0:.0f} s")
        rasters.burn(adm, "code", a.out / f"{name}_adm2.tif", grid, dtype="uint16", workers=a.workers, log=log)

    if "rice" in a.layers:
        rice = load(RICE, ["JSWH", "Shape_Area"])
        rice["code"] = rice["JSWH"].map(lambda v: rasters.RICE_CLASSES.get(v, 9)).astype("int32")
        unknown = set(rice["JSWH"].dropna()) - set(k for k in rasters.RICE_CLASSES if k)
        if unknown:
            raise ValueError(f"unmapped JSWH values: {unknown}")
        path = a.out / f"{name}_rice.tif"
        rasters.burn(rice, "code", path, grid, factor=5, workers=a.workers, log=log,
                     descriptions=["rice share of pixel (percent of 1 ha)", "rice field type"])
        share = rasters.read(path, band=1).astype("float64")
        cls = rasters.read(path, band=2)
        burned = pd.Series(np.bincount(cls.ravel(), weights=share.ravel() / 100.0,
                                       minlength=10)[1:], index=range(1, 10))
        poly = rice.assign(ha=rice.geometry.area / 1e4).groupby("code").ha.sum()
        attr = rice.groupby("code").Shape_Area.sum() / 1e4
        chk = pd.DataFrame({"class": [rasters.RICE_CLASS_NAMES[c] for c in range(1, 10)],
                            "polygon_ha": poly.reindex(range(1, 10)).values,
                            "shape_area_ha": attr.reindex(range(1, 10)).values,
                            "raster_ha": burned.values}, index=range(1, 10))
        chk.loc["total"] = ["total", chk.polygon_ha.sum(), chk.shape_area_ha.sum(), chk.raster_ha.sum()]
        floor = share == 1
        chk.loc["touched_only"] = ["pixels touched, no 20 m centre inside (share 1)",
                                   np.nan, np.nan, floor.sum() / 100.0]
        chk.loc["touched_only", "pixels"] = int(floor.sum())
        chk.loc["total", "pixels"] = int((share > 0).sum())
        chk.to_csv(a.out / f"{name}_check.csv")
        log("rice area check (ha):\n" + chk.round(0).to_string())

    for layer in ("irrigation", "province", "adm2"):
        if f"{layer}_filled" not in a.layers:
            continue
        # MoA covers Java only: fill inside the island group "Jawa" of the region layer.
        java, _ = grid.window(*MOA_BOX)
        regions = pd.read_csv(a.out / f"{name}_region_classes.csv").set_index("name").code
        rice = rasters.read(a.out / f"{name}_rice.tif", java)
        region = rasters.read(a.out / f"{name}_region.tif", java)
        src = rasters.read(a.out / f"{name}_{layer}.tif", java)
        todo = (rice > 0) & (region == regions["Jawa"])
        filled, dist = rasters.fill_nearest(src, todo)
        rasters.write_like(a.out / f"{name}_{layer}_filled.tif", java,
                           [filled, np.minimum(np.ceil(dist), 255)], "uint16" if layer == "adm2" else "uint8",
                           [f"MoA {layer} class, gaps filled from nearest",
                            "fill distance (100 m pixels, 0 = own class)"], window_of=grid)
        w = rice[todo].astype("float64") / 100.0
        moved = dist[todo] > 0
        km = dist[todo][moved] / 10.0
        log(f"{layer}: Java rice {w.sum():,.0f} ha; filled from nearest pixel {w[moved].sum():,.0f} ha "
            f"({moved.mean() * 100:.1f}% of pixels); distance km median {np.median(km):.2f}, "
            f"p90 {np.percentile(km, 90):.2f}, max {km.max():.1f}")
    if a.out == config.CACHE_DIR / "rasters":
        import inputs
        log("registered as prepared inputs (src/inputs.py):")
        inputs.register_existing(config.CACHE_DIR, name, config.CURRENT.aoi, log=log)


if __name__ == "__main__":
    main()

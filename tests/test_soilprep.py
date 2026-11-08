"""Tests for soil inputs from your own profiles (src/soilprep.py) and per-cell soil maps.

Synthetic only: a 4 x 5 cell weather grid (tests/test_input_datasets.py), a crop
mask in degrees, two profiles, and soil maps as a raster on another grid and as
polygons. The map puts profile 1 west of 107.23E, profile 2 to 107.35E, and
nothing east of it.
"""
import numpy as np
import pandas as pd
import pytest
import rasterio
import yaml
from rasterio.transform import Affine

import config
import inputs
import soilprep
import soils
from sol import write_sol
from test_input_datasets import _weather

BBOX = [106.95, -6.35, 107.45, -5.95]


def _profile(sid, clay=40.0):
    row = {}
    for p in soils.PROPS:
        for d in soils.DEPTHS:
            row[f"{p}_{d}"] = {"clay": clay, "sand": 30.0, "silt": 100 - 30.0 - clay, "soc": 15.0, "bdod": 1.3,
                               "cfvo": 2.0, "nitrogen": 1.5, "phh2o": 6.0, "cec": 20.0}[p]
    return soils.make_profile(row, -6.1, 107.1, sid, soc_scale=None, country="Test")


def _setup(tmp_path):
    cache = tmp_path / "cache"
    _weather(cache, "w_x")
    # crop: 1 ha in every 0.01 degree pixel, none in the sea cell (0, 0)
    crop = np.ones((40, 50), "float32")
    crop[:10, :10] = 0
    (cache / "r").mkdir(parents=True, exist_ok=True)
    with rasterio.open(cache / "r" / "crop.tif", "w", driver="GTiff", width=50, height=40, count=1,
                       dtype="float32", crs="EPSG:4326", transform=Affine(0.01, 0, 106.95, 0, -0.01, -5.95)) as r:
        r.write(crop, 1)
    inputs.write_manifest("crop_mask", "c_deg", {"file": "r/crop.tif", "units_per_ha": 1}, cache)
    src = tmp_path / "src"
    src.mkdir()
    write_sol(src / "my.SOL", [_profile("TESTSOIL01"), _profile("TESTSOIL02", 20.0), _profile("TESTSOIL03")])
    # code raster on its own grid (origin 0.002 degree off the crop mask's)
    x0 = 106.952
    lon = x0 + (np.arange(52) + 0.5) * 0.01
    codes = np.where(lon < 107.23, 1, np.where(lon < 107.35, 2, 0)).astype("uint8")
    with rasterio.open(src / "codes.tif", "w", driver="GTiff", width=52, height=42, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=Affine(0.01, 0, x0, 0, -0.01, -5.94), nodata=0) as r:
        r.write(np.broadcast_to(codes, (42, 52)), 1)
    pd.DataFrame({"code": [1, 2], "soil_id": ["TESTSOIL01", "TESTSOIL02"]}).to_csv(src / "codes.csv", index=False)
    return cache, src


def _exp(tmp_path, prep, **inputs_):
    d = {"paths": {"cache": str(tmp_path / "cache"), "inputs": str(tmp_path / "src")},
         "aoi": {"name": "t", "bbox": BBOX, "region": "Jawa"}, "years": [2015, 2015],
         "inputs": {"weather": "w_x", "crop_mask": "c_deg", **inputs_}, "prepare": {"soil": prep}}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return config.load(p, env={})


def _prep(tmp_path, soil):
    exp = _exp(tmp_path, soil)
    soilprep.prepare_sol(exp, soilprep.plan(exp), "w_x", "c_deg", log=lambda s: None)
    return inputs.open_dataset("soil", soil["name"], exp.path("cache"))


def _expected_map(m, share_12, filled_14):
    m = m.set_index(["iy", "ix"])
    assert (0, 0) not in m.index and len(m) == 19
    for iy in range(4):
        for ix in range(5):
            if (iy, ix) == (0, 0):
                continue
            r = m.loc[(iy, ix)]
            assert r.soil_id == ("TESTSOIL01" if ix <= 2 else "TESTSOIL02"), (iy, ix)
            assert r.ha == pytest.approx(100.0)
    assert m.loc[(1, 2)].share == pytest.approx(share_12)
    assert (m.loc[(1, 4)].filled_from == filled_14) if filled_14 else pd.isna(m.loc[(1, 4)].filled_from)
    assert pd.isna(m.loc[(1, 3)].filled_from)


def test_one_profile_for_every_cell(tmp_path):
    cache, src = _setup(tmp_path)
    s = _prep(tmp_path, {"name": "one", "method": "sol", "file": "{inputs}/my.SOL", "profile": "TESTSOIL02"})
    m = s.soil_map()
    assert len(m) == 19 and set(m.soil_id) == {"TESTSOIL02"} and m.ha.sum() == pytest.approx(1900.0)
    assert s.meta["profiles"] == 1 and s.crop_mask == "c_deg" and s.weather == "w_x"
    assert soilprep.sol_ids(s.file()) == ["TESTSOIL02"]          # only the profiles used are kept


def test_raster_map_on_another_grid(tmp_path):
    _setup(tmp_path)
    s = _prep(tmp_path, {"name": "r", "method": "sol", "file": "{inputs}/my.SOL", "map": "{inputs}/codes.tif",
                         "classes": "{inputs}/codes.csv"})
    m = s.soil_map()
    # the 1.1 km crop pixels are read as 10 x 10 sub-points (rasters.subdivision): the code
    # raster's class 1 pixels reach 107.232E, so 82% of cell (1, 2) (80% at pixel centres), and
    # class 2 reaches 2% into cell (1, 4), which is then on the map, not filled
    _expected_map(m, 0.82, None)
    assert m.set_index(["iy", "ix"]).loc[(1, 4)].share == pytest.approx(0.02)
    assert (m.filled_from.fillna("") == "").all()            # every cell has some mapped crop
    assert sorted(soilprep.sol_ids(s.file())) == ["TESTSOIL01", "TESTSOIL02"]


def test_vector_map(tmp_path):
    import geopandas as gpd
    import shapely
    _, src = _setup(tmp_path)
    gpd.GeoDataFrame({"SOIL": ["TESTSOIL01", "TESTSOIL02"]},
                     geometry=[shapely.box(106.9, -6.4, 107.235, -5.9), shapely.box(107.235, -6.4, 107.345, -5.9)],
                     crs="EPSG:4326").to_file(src / "soils.gpkg")
    s = _prep(tmp_path, {"name": "v", "method": "sol", "file": "{inputs}/my.SOL", "map": "{inputs}/soils.gpkg",
                         "id_column": "SOIL"})
    _expected_map(s.soil_map(), 0.8, "1,3")      # burned on the crop grid: edges at 107.235 and 107.345


def test_cells_get_their_own_copies(tmp_path):
    _setup(tmp_path)
    s = _prep(tmp_path, {"name": "one", "method": "sol", "file": "{inputs}/my.SOL", "profile": "TESTSOIL01"})
    ids = s.ids([1, 2], [1, 3])
    assert ids == ["XS00010001", "XS00020003"]
    profs = s.profiles()
    assert set(ids) <= set(profs) and len(profs) == 19
    a, b = profs[ids[0]], profs[ids[1]]
    a.surface["SLPF"] = 0.5                                   # per-cell SLPF must not leak to other cells
    assert b.surface["SLPF"] == 1.0 and a.id == ids[0] and b.id == ids[1]
    assert a.layers.SLCL.tolist() == b.layers.SLCL.tolist()


def test_problems_in_plain_words(tmp_path):
    _, src = _setup(tmp_path)
    with pytest.raises(config.ConfigError, match="'NOSUCHSOIL' is not a profile of my.SOL"):
        _prep(tmp_path, {"name": "x", "method": "sol", "file": "{inputs}/my.SOL", "profile": "NOSUCHSOIL"})
    pd.DataFrame({"code": [1, 2], "soil_id": ["TESTSOIL01", "MISSING001"]}).to_csv(src / "bad.csv", index=False)
    with pytest.raises(config.ConfigError, match="not in my.SOL: MISSING001"):
        _prep(tmp_path, {"name": "x", "method": "sol", "file": "{inputs}/my.SOL", "map": "{inputs}/codes.tif",
                         "classes": "{inputs}/bad.csv"})
    write_sol(src / "deep.SOL", [_profile("DEEPSOIL01")])               # 6 layers, then 24 as text
    lines = (src / "deep.SOL").read_text().splitlines()
    h = next(i for i, ln in enumerate(lines) if ln.startswith("@  SLB") and "SLLL" in ln)
    rows = lines[h + 1: h + 7]
    (src / "deep.SOL").write_text("\n".join(lines[: h + 1] + rows * 4 + lines[h + 7:]) + "\n")
    with pytest.raises(config.ConfigError, match="engine limit is 20"):
        _prep(tmp_path, {"name": "x", "method": "sol", "file": "{inputs}/deep.SOL", "profile": "DEEPSOIL01"})
    # settings
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, {"method": "sol", "file": "a.SOL", "map": "m.tif", "profile": "TESTSOIL01",
                        "soc_scale": "wise_indonesia"})
    msg = str(e.value)
    assert "needs a name" in msg and "not both" in msg and "only with method: soilgrids" in msg
    with pytest.raises(config.ConfigError) as e:
        _exp(tmp_path, {"method": "soilgrids", "soc_scale": {"0-5cm": 0.7}, "profile": "X"})
    assert "100-200cm is missing" in str(e.value) and "only with method: sol" in str(e.value)
    with pytest.raises(config.ConfigError, match="a raster soil map needs classes"):
        _exp(tmp_path, {"name": "x", "method": "sol", "file": "a.SOL", "map": "m.tif"})


def test_check_names_a_map_profile_missing_from_the_file(tmp_path):
    cache, _ = _setup(tmp_path)
    s = _prep(tmp_path, {"name": "one", "method": "sol", "file": "{inputs}/my.SOL", "profile": "TESTSOIL01"})
    m = s.soil_map()
    m.loc[0, "soil_id"] = "GONE000001"
    m.to_csv(s.file("map"), index=False)
    exp = _exp(tmp_path, None, soil="one")
    msg = "\n".join(inputs.check(exp))
    assert "GONE000001" in msg and "Prepare it again" in msg


def test_soc_scale_settings():
    assert soilprep.soc_scale("none") is None and soilprep.soc_scale(None) is None
    assert soilprep.soc_scale("wise_indonesia") == soils.SOC_SCALE
    f = {d: 0.5 for d in soils.DEPTHS}
    assert soilprep.soc_scale(f) == f
    assert config.DEFAULTS["prepare"]["soil"]["soc_scale"] == "wise_indonesia"


def test_soil_made_with_other_sub_points_is_refused(tmp_path):
    import json
    _setup(tmp_path)
    s = _prep(tmp_path, {"name": "one", "method": "sol", "file": "{inputs}/my.SOL", "profile": "TESTSOIL01"})
    assert s.meta["sub_points"] == 10                       # 0.01 degree crop pixels: 10 x 10 sub-points
    exp = _exp(tmp_path, None, soil="one")
    assert not [p for p in inputs.check(exp) if "sub-points" in p]
    p = inputs.manifest_path("soil", "one", exp.path("cache"))
    meta = json.loads(p.read_text())
    meta.pop("sub_points")                                  # a soil made before sub-points existed
    p.write_text(json.dumps(meta))
    msg = "\n".join(inputs.check(exp))
    assert "1 x 1 point(s)" in msg and "10 x 10 sub-points" in msg and "--overwrite" in msg

"""Coarse crop pixels over fine class maps: sub-points (rasters.crop_pixels sub=,
rasters.subdivision). Synthetic rasters in degrees."""
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

import rasters


def _tif(path, arr, t, crs="EPSG:4326"):
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=arr.dtype, crs=crs, transform=t) as r:
        r.write(arr[None])
    return path


def _setup(tmp_path):
    # four crop pixels of 0.05 degree (about 5.5 km), 10 ha each, in one row
    crop = _tif(tmp_path / "crop.tif", np.full((1, 4), 10.0, "float32"), Affine(0.05, 0, 107.0, 0, -0.05, -6.0))
    # class map at 0.005 degree: class 1 west of 107.03, class 2 east of it
    lon = 107.0 + (np.arange(40) + 0.5) * 0.005
    cls = np.broadcast_to(np.where(lon < 107.03, 1, 2).astype("uint8"), (10, 40)).copy()
    lay = _tif(tmp_path / "cls.tif", cls, Affine(0.005, 0, 107.0, 0, -0.005, -6.0))
    return crop, lay


def test_subdivision_from_the_crop_pixel_size():
    assert rasters.subdivision(rasters.Grid(0, 0, 10, 10, 100.0)) == 1          # Java's 100 m mask
    modis = rasters.Grid(107.0, -6.0, 10, 10, 0.0044915764, "EPSG:4326")      # about 497 m at 6 S
    assert rasters.subdivision(modis) == 5
    assert rasters.subdivision(rasters.Grid(107.0, -6.0, 4, 4, 1 / 12, "EPSG:4326")) == 10   # cap
    assert rasters.subdivision(modis, cap=3) == 3
    assert rasters.pixel_metres(rasters.Grid(0, 0, 1, 1, 100.0)) == 100.0


def test_sub_points_give_class_fractions_and_split_cells(tmp_path):
    crop, lay = _setup(tmp_path)
    g = rasters.Grid(107.0, -6.0, 4, 1, 0.05, "EPSG:4326")
    # weather cells of 0.1 degree: the 4 crop pixels fall into 2 cells (2 each)
    wlat, wlon = np.array([-6.05]), np.array([107.05, 107.15])
    ok = np.ones((1, 2), bool)
    centre = rasters.aggregate(crop, {"irr": lay}, wlat, wlon, ok, grid=g, units_per_ha=1.0, class_band=None)
    subd = rasters.aggregate(crop, {"irr": lay}, wlat, wlon, ok, grid=g, units_per_ha=1.0, class_band=None, sub=10)
    for t in (centre, subd):
        assert t.ha.sum() == pytest.approx(40.0)
    # at the centre the first pixel (107.000-107.050, centre 107.025) is wholly class 1;
    # with sub-points 60% of it is class 1 (west of 107.03) and 40% class 2
    c = centre.groupby("irr").ha.sum()
    s = subd.groupby("irr").ha.sum()
    assert c[1] == pytest.approx(10.0) and c[2] == pytest.approx(30.0)
    assert s[1] == pytest.approx(6.0) and s[2] == pytest.approx(34.0)
    assert set(zip(subd.iy, subd.ix)) == {(0, 0), (0, 1)}


def test_sub_points_spread_a_pixel_over_two_weather_cells(tmp_path):
    # one 0.1 degree crop pixel centred on the edge between two 0.1 degree weather cells
    crop = _tif(tmp_path / "c.tif", np.full((1, 1), 8.0, "float32"), Affine(0.1, 0, 107.05, 0, -0.1, -6.0))
    g = rasters.Grid(107.05, -6.0, 1, 1, 0.1, "EPSG:4326")
    wlat, wlon = np.array([-6.05]), np.array([107.05, 107.15])
    one = rasters.aggregate(crop, {}, wlat, wlon, np.ones((1, 2), bool), grid=g, units_per_ha=1.0, class_band=None)
    four = rasters.aggregate(crop, {}, wlat, wlon, np.ones((1, 2), bool), grid=g, units_per_ha=1.0,
                             class_band=None, sub=4)
    assert len(one) == 1 and one.ha.sum() == pytest.approx(8.0)
    assert four.set_index("ix").ha.to_dict() == pytest.approx({0: 4.0, 1: 4.0})

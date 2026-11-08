"""SoilGrids for any area: area points from any weather grid, and sampling that
uses area caches where they cover a point and the mirror's tiles elsewhere
(src/soils.py). Synthetic rasters only, in plain metres."""
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

import soils


def _tif(path, value, left, top, w=10, h=10, res=10.0):
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="int16",
                       transform=Affine(res, 0, left, 0, -res, top), nodata=soils.NODATA) as r:
        r.write(np.full((1, h, w), value, "int16"))
    return path


def test_area_points_follow_any_weather_grid():
    lat = np.array([16.25, 16.0, 15.75, 15.5])
    lon = np.array([120.0, 120.25, 120.5])
    valid = np.ones((4, 3), bool)
    valid[0, 0] = False                                   # sea
    plon, plat = soils.area_points(lat, lon, valid)
    assert plon.size == 4 * 11                            # corners of the 11 land cells
    assert set(np.round(plon, 3)) == {119.875, 120.125, 120.375, 120.625}
    plon, plat = soils.area_points(lat, lon, valid, bbox=[120.2, 15.7, 120.6, 16.1], centres=True)
    # cells whose centre is in the box widened by one cell: rows 0-3, columns 0-2, minus the sea
    assert plon.size == 5 * 11


def test_caches_first_then_the_mirror(tmp_path):
    a = _tif(tmp_path / "a.tif", 1, 0, 100)              # x 0-100, y 0-100
    b = _tif(tmp_path / "b.tif", 2, 50, 100)             # x 50-150, overlaps a
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    _tif(tiles / "t0.tif", 7, 0, 100, w=30, h=10)
    vrt = tmp_path / "clay_0-5cm_mean.vrt"
    vrt.write_text(f"""<VRTDataset rasterXSize="40" rasterYSize="10">
  <GeoTransform>0, 10, 0, 100, 0, -10</GeoTransform>
  <VRTRasterBand dataType="Int16" band="1">
    <ComplexSource><SourceFilename relativeToVRT="1">tiles/t0.tif</SourceFilename>
      <SrcRect xOff="0" yOff="0" xSize="30" ySize="10"/><DstRect xOff="0" yOff="0" xSize="30" ySize="10"/></ComplexSource>
    <ComplexSource><SourceFilename relativeToVRT="1">tiles/t1.tif</SourceFilename>
      <SrcRect xOff="0" yOff="0" xSize="10" ySize="10"/><DstRect xOff="30" yOff="0" xSize="10" ySize="10"/></ComplexSource>
  </VRTRasterBand>
</VRTDataset>""")
    lay = soils.CachedLayer([a, b], fallback=vrt)
    x = np.array([5.0, 75.0, 120.0, 250.0, 355.0, 500.0])
    y = np.full(6, 50.0)
    got = lay.sample(x, y)
    # a first where both cover; b where only b does; the mirror tile beyond both; a missing
    # mirror tile and a point off the mirror give no data
    assert got.tolist() == [1, 1, 2, 7, soils.NODATA, soils.NODATA]
    assert lay.missing == 2
    only = soils.CachedLayer(a)                          # no fallback: outside the cache is missing
    assert only.sample(np.array([5.0, 250.0]), np.array([50.0, 50.0])).tolist() == [1, soils.NODATA]
    assert only.missing == 1


def test_a_point_on_a_tile_edge_reads_the_pixel_below_it(tmp_path):
    """Two tiles stacked; a point exactly on their shared edge belongs to the lower one
    (floor of its row), whichever tile the index lists first, and a cache agrees."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    _tif(tiles / "top.tif", 1, 0, 100)                   # rows 0-9  (y 100-0)
    _tif(tiles / "bot.tif", 2, 0, 0)                     # rows 10-19 (y 0 to -100)
    vrt = tmp_path / "x_0-5cm_mean.vrt"
    vrt.write_text("""<VRTDataset rasterXSize="10" rasterYSize="20">
  <GeoTransform>0, 10, 0, 100, 0, -10</GeoTransform>
  <VRTRasterBand dataType="Int16" band="1">
    <ComplexSource><SourceFilename relativeToVRT="1">tiles/top.tif</SourceFilename>
      <SrcRect xOff="0" yOff="0" xSize="10" ySize="10"/><DstRect xOff="0" yOff="0" xSize="10" ySize="10"/></ComplexSource>
    <ComplexSource><SourceFilename relativeToVRT="1">tiles/bot.tif</SourceFilename>
      <SrcRect xOff="0" yOff="0" xSize="10" ySize="10"/><DstRect xOff="0" yOff="10" xSize="10" ySize="10"/></ComplexSource>
  </VRTRasterBand>
</VRTDataset>""")
    x, y = np.array([35.0, 35.0, 35.0]), np.array([0.0, 0.5, -0.5])
    assert soils.SoilGridsLayer(vrt).sample(x, y).tolist() == [2, 1, 2]
    cache = _tif(tmp_path / "cache.tif", 2, 0, 20, w=10, h=4)     # rows 8-11, shifted origin
    with rasterio.open(cache, "r+") as r:
        a = r.read(1)
        a[:2] = 1                                                   # rows 8-9 are the top tile
        r.write(a, 1)
    assert soils.CachedLayer([cache], fallback=vrt).sample(x, y).tolist() == [2, 1, 2]


def test_cached_areas_puts_the_original_first(tmp_path):
    for area in ("luzon", "idn", "abc"):
        d = tmp_path / "soilgrids" / area
        d.mkdir(parents=True)
        (d / "clay_0-5cm_mean.tif").write_bytes(b"")
    names = [p.parent.name for p in soils.cached_areas("clay", "0-5cm", tmp_path)]
    assert names == ["idn", "abc", "luzon"]
    assert soils.cached_areas("sand", "0-5cm", tmp_path) == []

"""Tests for loading state boundaries and taking polygons apart."""

import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pytest
import shapefile  # type: ignore[import-untyped]  # pyshp ships no type hints
import shapely

from squares import data
from squares.arrays import FloatArray, Segments


def clockwise_box(west: float, south: float, east: float,
                  north: float) -> list[tuple[float, float]]:
    """A closed ring around a box, clockwise as shapefiles expect for outer rings."""
    return [(west, south), (west, north), (east, north), (east, south), (west, south)]


def write_zipped_shapefile(folder: Path, records: list[tuple[str, str, list[tuple[float, float]]]]) -> Path:
    """Write a polygon shapefile with STUSPS and NAME fields, zipped like the
    Census download, and return the zip's path."""
    base: Path = folder / "states"
    writer: shapefile.Writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("STUSPS", "C", size=2)
    writer.field("NAME", "C", size=50)
    for abbr, name, ring in records:
        writer.poly([ring])
        writer.record(abbr, name)
    writer.close()
    zip_path: Path = folder / "states.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for ext in (".shp", ".shx", ".dbf"):
            z.write(base.with_suffix(ext), f"states{ext}")
    return zip_path


def test_load_states_skips_non_states_and_sorts_by_name(tmp_path: Path) -> None:
    """DC and territories are dropped and the rest come back alphabetically."""
    path: Path = write_zipped_shapefile(tmp_path, [
        ("WI", "Wisconsin", clockwise_box(-92, 43, -88, 46)),
        ("DC", "District of Columbia", clockwise_box(-77.1, 38.8, -76.9, 39.0)),
        ("AL", "Alabama", clockwise_box(-88, 31, -85, 35)),
        ("PR", "Puerto Rico", clockwise_box(-67, 18, -65, 18.5)),
    ])
    states: list[data.State] = data.load_states(path)
    assert [s.abbr for s in states] == ["AL", "WI"]
    assert states[1].name == "Wisconsin"
    assert states[1].geom.bounds == pytest.approx((-92, 43, -88, 46))


def test_load_states_repairs_invalid_outlines(tmp_path: Path) -> None:
    """A self-crossing (bow-tie) outline is turned into a valid geometry."""
    bow_tie: list[tuple[float, float]] = [(0, 0), (0, 2), (2, 0), (2, 2), (0, 0)]
    path: Path = write_zipped_shapefile(tmp_path, [("XX", "Bowtie", bow_tie)])
    (st,) = data.load_states(path)
    assert st.geom.is_valid
    assert st.geom.area == pytest.approx(2.0)


def test_fetch_downloads_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is downloaded the first time and reused afterwards."""
    calls: list[str] = []

    def fake_download(url: str, filename: Path) -> None:
        """Stand-in for urlretrieve that writes a placeholder file."""
        calls.append(url)
        Path(filename).write_bytes(b"zip")

    monkeypatch.setattr(urllib.request, "urlretrieve", fake_download)
    first: Path = data.fetch(tmp_path / "data")
    second: Path = data.fetch(tmp_path / "data")
    assert first == second
    assert first.read_bytes() == b"zip"
    assert calls == [data.URL]
    assert not list((tmp_path / "data").glob("*.part"))


def test_polygons_of_a_polygon() -> None:
    """A single polygon comes back as itself."""
    poly: shapely.Polygon = shapely.box(0, 0, 1, 1)
    assert data.polygons(poly) == [poly]


def test_polygons_splits_multipolygons_and_drops_other_shapes() -> None:
    """Pieces of a collection that aren't polygons (or are empty) are ignored."""
    a: shapely.Polygon = shapely.box(0, 0, 1, 1)
    b: shapely.Polygon = shapely.box(2, 2, 3, 3)
    assert data.polygons(shapely.MultiPolygon([a, b])) == [a, b]
    mixed: shapely.GeometryCollection = shapely.GeometryCollection(
        [a, shapely.LineString([(0, 0), (5, 5)]), shapely.Point(1, 1)])
    assert data.polygons(mixed) == [a]
    assert data.polygons(shapely.Polygon()) == []


def test_segments_cover_every_ring() -> None:
    """A square with a square hole has 4 + 4 edges, each joining up with the next."""
    poly: shapely.Polygon = shapely.Polygon(
        [(0, 0), (4, 0), (4, 4), (0, 4)], holes=[[(1, 1), (1, 2), (2, 2), (2, 1)]])
    x0, y0, x1, y1 = data.segments(poly)
    assert len(x0) == 8
    # Within each ring, every edge starts where the previous one ended.
    for ring in (slice(0, 4), slice(4, 8)):
        starts: FloatArray = np.column_stack([x0[ring], y0[ring]])
        ends: FloatArray = np.column_stack([x1[ring], y1[ring]])
        np.testing.assert_array_equal(np.roll(ends, 1, axis=0), starts)


def test_segments_trace_the_outline() -> None:
    """The segments of a box run exactly along its sides."""
    seg: Segments = data.segments(shapely.box(-1, -2, 3, 5))
    x0, y0, x1, y1 = seg
    lengths: FloatArray = np.hypot(x1 - x0, y1 - y0)
    assert lengths.sum() == pytest.approx(2 * (4 + 7))

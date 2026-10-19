"""Tests for the latitude/longitude-aligned box solver."""

import numpy as np
import pytest
import shapely

from squares import aligned, geodesy
from squares.aligned import AlignedBox
from squares.arrays import BoolArray, FloatArray, Segments
from squares.data import segments
from tests.helpers import contains_ring, rectangle

# Slightly smaller than the solver's result, so touching boundaries don't
# count as poking out.
SHRINK: float = 1 - 1e-6


def shrunk(box: AlignedBox) -> AlignedBox:
    """The same box, a hair smaller."""
    return AlignedBox(box.lon, box.lat, box.half_height * SHRINK, box.half_width * SHRINK)


def test_box_edges() -> None:
    """The edges come from the center and half-sizes."""
    box: AlignedBox = AlignedBox(lon=-90.0, lat=45.0, half_height=1.0, half_width=2.0)
    assert (box.south, box.north, box.west, box.east) == (44.0, 46.0, -92.0, -88.0)


def test_side_is_the_meridian_arc() -> None:
    """The reported side length is the north-south distance."""
    box: AlignedBox = AlignedBox(-90.0, 45.0, 1.0, 1.5)
    assert box.side_mi() == pytest.approx(geodesy.meridian_arc(44.0, 46.0) / geodesy.METERS_PER_MILE)


def test_north_edge_is_shorter_than_south_edge() -> None:
    """Meridians converge, so the top of a box is narrower than its bottom."""
    box: AlignedBox = AlignedBox(-90.0, 45.0, 1.0, 1.5)
    assert box.width_mi(box.north) < box.width_mi(box.lat) < box.width_mi(box.south)


def test_ring_is_closed_and_runs_along_the_edges() -> None:
    """The outline has n points per side, ends where it starts, and stays on the edges."""
    box: AlignedBox = AlignedBox(-90.0, 45.0, 1.0, 1.5)
    ring: FloatArray = box.ring(8)
    assert ring.shape == (33, 2)
    np.testing.assert_array_equal(ring[0], ring[-1])
    on_ew_edge: BoolArray = np.isclose(ring[:, 0], box.west) | np.isclose(ring[:, 0], box.east)
    on_ns_edge: BoolArray = np.isclose(ring[:, 1], box.south) | np.isclose(ring[:, 1], box.north)
    assert np.all(on_ew_edge | on_ns_edge)


def test_guides_are_the_two_meridians_and_two_parallels() -> None:
    """Guide lines run along the box's edges and extend past it."""
    box: AlignedBox = AlignedBox(-90.0, 45.0, 1.0, 1.5)
    guides: list[FloatArray] = box.guides(extend=2.0)
    assert len(guides) == 4
    np.testing.assert_allclose(guides[0][:, 0], box.west)
    np.testing.assert_allclose(guides[1][:, 0], box.east)
    np.testing.assert_allclose(guides[2][:, 1], box.south)
    np.testing.assert_allclose(guides[3][:, 1], box.north)
    assert guides[0][:, 1].min() < box.south and guides[0][:, 1].max() > box.north


def test_min_aspect_uses_the_latitude_nearest_the_equator() -> None:
    """The smallest aspect in a band is at its equatorward edge (or the equator)."""
    assert float(aligned._min_aspect(10.0, 20.0)) == pytest.approx(float(geodesy.aspect(10.0)))
    assert float(aligned._min_aspect(-20.0, -10.0)) == pytest.approx(float(geodesy.aspect(10.0)))
    assert float(aligned._min_aspect(-5.0, 5.0)) == pytest.approx(float(geodesy.aspect(0.0)))


def test_wide_rectangle_is_limited_by_its_height() -> None:
    """In a short, wide band the box fills the full height."""
    result: aligned.Result = aligned.largest_aligned_box(rectangle(-93.0, 40.0, -90.0, 42.0))
    assert result.box.half_height == pytest.approx(1.0, abs=2e-5)
    expected_mi: float = geodesy.meridian_arc(40.0, 42.0) / geodesy.METERS_PER_MILE
    assert result.box.side_mi() == pytest.approx(expected_mi, abs=0.01)


def test_tall_rectangle_is_limited_by_its_width_and_sits_at_the_bottom() -> None:
    """In a tall, narrow strip the box fills the width, and sits as far south as
    possible, where a degree of longitude is longest."""
    box: AlignedBox = aligned.largest_aligned_box(rectangle(-91.0, 30.0, -90.0, 40.0)).box
    assert box.half_width == pytest.approx(0.5, abs=5e-5)
    # Moving up costs very little size, so allow some slack in position.
    assert box.south == pytest.approx(30.0, abs=5e-3)
    # Height and middle width agree: that is the definition of "square".
    assert box.width_mi(box.lat) == pytest.approx(box.side_mi(), rel=1e-6)


IRREGULAR: shapely.Polygon = shapely.Polygon([
    (-104.0, 39.0), (-101.5, 38.6), (-99.0, 39.4), (-99.4, 40.8), (-100.6, 41.0),
    (-101.2, 42.1), (-102.9, 41.7), (-103.4, 40.6), (-104.3, 40.2),
])


def test_result_lies_inside_an_irregular_polygon() -> None:
    """The box fits (as judged by shapely) and its touching points are on the boundary."""
    result: aligned.Result = aligned.largest_aligned_box(IRREGULAR)
    assert contains_ring(IRREGULAR, shrunk(result.box).ring(64))
    assert len(result.contacts) >= 2
    for lon, lat in result.contacts:
        assert IRREGULAR.exterior.distance(shapely.Point(lon, lat)) < 1e-6


def test_beats_a_brute_force_grid_search() -> None:
    """No center on a fine grid gives a bigger box than the solver found, and
    the solver's answer is not much better than the grid's best."""
    tol: float = 1e-5
    result: aligned.Result = aligned.largest_aligned_box(IRREGULAR, tol=tol)
    minx, miny, maxx, maxy = IRREGULAR.bounds
    n: int = 150
    gx, gy = np.meshgrid(np.linspace(minx, maxx, n), np.linspace(miny, maxy, n))
    cx: FloatArray = gx.ravel()
    cy: FloatArray = gy.ravel()
    inside: BoolArray = shapely.contains_xy(IRREGULAR, cx, cy)
    seg: Segments = segments(IRREGULAR)
    grid_best: float = float(aligned._max_half_height(cx[inside], cy[inside], seg).max())
    spacing: float = max((maxx - minx), (maxy - miny)) / (n - 1)
    assert result.box.half_height >= grid_best - tol
    assert result.box.half_height <= grid_best + spacing


def test_picks_the_biggest_piece() -> None:
    """With a small island and a big mainland, the box goes in the mainland."""
    island: shapely.Polygon = rectangle(-80.0, 30.0, -79.8, 30.2)
    mainland: shapely.Polygon = rectangle(-90.0, 30.0, -86.0, 33.0)
    box: AlignedBox = aligned.largest_aligned_box(shapely.MultiPolygon([island, mainland])).box
    assert mainland.contains(shapely.Point(box.lon, box.lat))
    assert box.half_height == pytest.approx(1.5, abs=2e-5)


def test_a_box_in_a_hole_is_avoided() -> None:
    """A lake (hole) in the middle pushes the box aside."""
    outer: list[tuple[float, float]] = [(-100, 40), (-96, 40), (-96, 44), (-100, 44)]
    lake: list[tuple[float, float]] = [(-98.2, 41.8), (-97.8, 41.8), (-97.8, 42.2), (-98.2, 42.2)]
    poly: shapely.Polygon = shapely.Polygon(outer, holes=[lake])
    box: AlignedBox = aligned.largest_aligned_box(poly).box
    assert contains_ring(poly, shrunk(box).ring(64))
    assert not shapely.box(box.west, box.south, box.east, box.north).contains(
        shapely.Point(-98.0, 42.0))


def test_empty_geometry_is_an_error() -> None:
    """There is nothing to put a box in."""
    with pytest.raises(ValueError):
        aligned.largest_aligned_box(shapely.MultiPolygon())

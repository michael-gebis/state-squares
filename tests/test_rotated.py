"""Tests for the true-square solver (rotated and north-facing)."""

import numpy as np
import pytest
import shapely

from squares import geodesy, rotated
from squares.arrays import FloatArray, IntArray
from squares.rotated import Square
from tests.helpers import contains_ring, rectangle

SHRINK: float = 1 - 1e-6


def shrunk(sq: Square) -> Square:
    """The same square, a hair smaller."""
    return Square(sq.lon, sq.lat, sq.theta, sq.half_side * SHRINK)


def point_boundary(lon: FloatArray, lat: FloatArray) -> rotated._Boundary:
    """A boundary made of zero-length segments at the given points, for testing
    projection without going through a polygon."""
    bnd: rotated._Boundary = rotated._Boundary.__new__(rotated._Boundary)
    bnd.lonlat = (lon, lat, lon, lat)
    bnd.p0 = geodesy.ecef(lon, lat)
    bnd.p1 = bnd.p0
    bnd.box = (lon, lat, lon, lat)
    return bnd


def project_points(lon: FloatArray, lat: FloatArray, center: tuple[float, float],
                   theta: float) -> tuple[FloatArray, FloatArray]:
    """Project points into the tilted tangent plane at `center`."""
    bnd: rotated._Boundary = point_boundary(lon, lat)
    idx: IntArray = np.arange(len(lon))
    u0, v0, _, _ = bnd.project(idx, np.array([center[0]]), np.array([center[1]]),
                               np.array([theta]))
    return u0[:, 0], v0[:, 0]


# --- the tangent-plane frames and projection ---------------------------------


@pytest.mark.parametrize(("lon", "lat", "theta"), [
    (-90.0, 45.0, 0.0), (10.0, -30.0, 0.7), (179.0, 70.0, 1.5), (0.0, 0.0, 0.3)])
def test_frames_are_orthonormal_and_right_handed(lon: float, lat: float, theta: float) -> None:
    """The normal and the two in-plane axes are unit length, perpendicular,
    and u x v points up."""
    center, normal, ax_u, ax_v = (f[0] for f in rotated._frames(
        np.array([lon]), np.array([lat]), np.array([theta])))
    for axis in (normal, ax_u, ax_v):
        assert float(np.linalg.norm(axis)) == pytest.approx(1.0)
    assert float(normal @ ax_u) == pytest.approx(0.0, abs=1e-12)
    assert float(normal @ ax_v) == pytest.approx(0.0, abs=1e-12)
    assert float(ax_u @ ax_v) == pytest.approx(0.0, abs=1e-12)
    np.testing.assert_allclose(np.cross(ax_u, ax_v), normal, atol=1e-12)
    np.testing.assert_allclose(center, geodesy.ecef(lon, lat))


def test_projection_puts_the_center_at_the_origin() -> None:
    """The square's center projects to (0, 0)."""
    u, v = project_points(np.array([-89.6]), np.array([44.6]), (-89.6, 44.6), 0.4)
    assert float(u[0]) == pytest.approx(0.0, abs=1e-6)
    assert float(v[0]) == pytest.approx(0.0, abs=1e-6)


def test_projection_is_true_to_scale_near_the_center() -> None:
    """A point 10 km due north lands 10 km up the v axis (to well under a meter)."""
    lon, lat, _ = geodesy.GEOD.fwd(-89.6, 44.6, 0.0, 10_000.0)
    u, v = project_points(np.array([lon]), np.array([lat]), (-89.6, 44.6), 0.0)
    assert float(u[0]) == pytest.approx(0.0, abs=1e-3)
    assert float(v[0]) == pytest.approx(10_000.0, abs=0.1)


def test_unproject_and_project_are_inverses() -> None:
    """Plane -> lon/lat -> plane returns the starting points."""
    sq: Square = Square(lon=-100.0, lat=40.0, theta=0.6, half_side=50_000.0)
    rng: np.random.Generator = np.random.default_rng(8)
    u: FloatArray = rng.uniform(-200_000, 200_000, 50)
    v: FloatArray = rng.uniform(-200_000, 200_000, 50)
    lon, lat = sq.unproject(u, v)
    back_u, back_v = project_points(lon, lat, (sq.lon, sq.lat), sq.theta)
    np.testing.assert_allclose(back_u, u, atol=1e-4)
    np.testing.assert_allclose(back_v, v, atol=1e-4)


def test_near_filters_segments_by_box() -> None:
    """Only segments whose bounds meet the query box are kept."""
    bnd: rotated._Boundary = point_boundary(np.array([0.0, 5.0, 10.0]), np.array([0.0, 5.0, 10.0]))
    kept: IntArray = bnd.near(np.arange(3), 4.0, 4.0, 11.0, 11.0)
    assert kept.tolist() == [1, 2]


def test_center_slack_grows_with_cell_size() -> None:
    """Bigger cells allow centers to be further from the cell center."""
    lat: FloatArray = np.array([45.0])
    small: float = float(rotated._center_slack(lat, 0.01, 0.01)[0])
    big: float = float(rotated._center_slack(lat, 0.1, 0.1)[0])
    assert 0 < small < big
    # About sqrt((0.01° of longitude)^2 + (0.01° of latitude)^2) at 45°N.
    assert small == pytest.approx(np.hypot(787.0, 1111.0), rel=0.03)


# --- the Square itself ---------------------------------------------------------


def test_unproject_origin_is_the_center() -> None:
    """(0, 0) in the square's plane is its center."""
    sq: Square = Square(lon=-75.0, lat=35.0, theta=0.2, half_side=1_000.0)
    lon, lat = sq.unproject(np.array([0.0]), np.array([0.0]))
    assert float(lon[0]) == pytest.approx(-75.0)
    assert float(lat[0]) == pytest.approx(35.0)


def test_sides_are_equal_and_twice_the_half_side() -> None:
    """All four geodesic sides agree, and for a small square match 2 x half-side."""
    sq: Square = Square(lon=-100.0, lat=40.0, theta=0.3, half_side=25_000.0)
    sides: FloatArray = sq.sides_mi()
    assert sides.shape == (4,)
    np.testing.assert_allclose(sides, sides.mean(), rtol=1e-6)
    assert float(sides.mean()) == pytest.approx(50_000 / geodesy.METERS_PER_MILE, rel=1e-4)


def test_big_square_sides_still_agree_closely() -> None:
    """Even an Alaska-sized square has sides equal to within a few feet."""
    sq: Square = Square(lon=-152.0, lat=64.0, theta=0.14, half_side=420_000.0)
    sides: FloatArray = sq.sides_mi()
    assert float(np.ptp(sides)) * 5280 < 10


def test_corner_angles_show_the_spherical_excess() -> None:
    """The angles are equal and just over 90°; their excess over 360° matches
    area / R² for a curved surface."""
    sq: Square = Square(lon=-105.5, lat=39.0, theta=0.0, half_side=220_000.0)
    angles: FloatArray = sq.corner_angles()
    assert np.all(angles > 90.0)
    np.testing.assert_allclose(angles, angles.mean(), atol=1e-3)
    side_m: float = float(sq.sides_mi().mean()) * geodesy.METERS_PER_MILE
    gaussian_r2: float = float(geodesy.meridional_radius(39.0) * geodesy.prime_vertical_radius(39.0))
    expected_excess: float = float(np.degrees(side_m**2 / gaussian_r2))
    assert float(angles.sum()) - 360.0 == pytest.approx(expected_excess, rel=0.02)


@pytest.mark.parametrize(("theta_deg", "expected"), [
    (0.0, 0.0), (30.0, 30.0), (45.0, 45.0), (60.0, -30.0), (89.0, -1.0)])
def test_rotation_is_reported_between_minus_and_plus_45(theta_deg: float, expected: float) -> None:
    """A square looks the same turned by 90°, so tilts are folded into (-45°, 45°]."""
    sq: Square = Square(0.0, 0.0, float(np.radians(theta_deg)), 1.0)
    assert sq.rotation_deg() == pytest.approx(expected)


def test_ring_is_closed_and_passes_through_the_corners() -> None:
    """The outline starts and ends at the same corner and visits all four."""
    sq: Square = Square(lon=-90.0, lat=45.0, theta=0.5, half_side=30_000.0)
    ring: FloatArray = sq.ring(16)
    assert ring.shape == (65, 2)
    np.testing.assert_allclose(ring[0], ring[-1])
    for corner in sq.corners():
        assert float(np.min(np.hypot(*(ring - corner).T))) < 1e-9


def test_guides_line_up_with_the_sides() -> None:
    """Each guide line runs along one side, through two corners."""
    sq: Square = Square(lon=-90.0, lat=45.0, theta=0.5, half_side=30_000.0)
    guides: list[FloatArray] = sq.guides()
    assert len(guides) == 4
    corners: FloatArray = sq.corners()
    for guide in guides:
        line: shapely.LineString = shapely.LineString(guide)
        touching: int = sum(line.distance(shapely.Point(c)) < 1e-6 for c in corners)
        assert touching == 2


# --- the solver ----------------------------------------------------------------


def test_finds_a_square_shaped_state_exactly() -> None:
    """If the polygon is itself a true square, the answer is that square."""
    target: Square = Square(lon=-100.0, lat=40.0, theta=0.4, half_side=30_000.0)
    poly: shapely.Polygon = shapely.Polygon(target.ring(64))
    result: Square = rotated.largest_square(poly).square
    assert result.half_side == pytest.approx(target.half_side, abs=15.0)
    assert result.lon == pytest.approx(target.lon, abs=1e-3)
    assert result.lat == pytest.approx(target.lat, abs=1e-3)
    assert result.rotation_deg() == pytest.approx(np.degrees(0.4), abs=0.1)


def test_north_facing_square_in_a_diamond() -> None:
    """In a square turned 45°, the best north-facing square has half the
    diamond's diagonal as its side, while the rotated search finds the diamond."""
    diamond: Square = Square(lon=-100.0, lat=40.0, theta=np.pi / 4, half_side=30_000.0)
    poly: shapely.Polygon = shapely.Polygon(diamond.ring(64))
    north: Square = rotated.largest_square(poly, rotate=False).square
    assert north.theta == 0.0
    assert north.half_side == pytest.approx(30_000.0 / np.sqrt(2), rel=2e-3)
    free: Square = rotated.largest_square(poly).square
    assert free.half_side == pytest.approx(30_000.0, abs=15.0)
    assert abs(free.rotation_deg()) == pytest.approx(45.0, abs=0.1)


TRIANGLE: shapely.Polygon = shapely.Polygon([(-104.0, 38.0), (-99.0, 38.5), (-102.0, 41.5)])


def test_result_lies_inside_and_touches_the_boundary() -> None:
    """The square fits (as judged by shapely) and its touching points are on
    the boundary."""
    result: rotated.Result = rotated.largest_square(TRIANGLE)
    assert contains_ring(TRIANGLE, shrunk(result.square).ring(64))
    assert len(result.contacts) >= 2
    for lon, lat in result.contacts:
        assert TRIANGLE.exterior.distance(shapely.Point(lon, lat)) < 1e-6


def test_rotating_never_does_worse_than_facing_north() -> None:
    """Allowing rotation can only help."""
    north: float = rotated.largest_square(TRIANGLE, rotate=False).square.half_side
    free: float = rotated.largest_square(TRIANGLE).square.half_side
    assert free >= north - 10.0


def test_bigger_than_any_random_square_that_fits() -> None:
    """No randomly placed square that fits inside is bigger than the answer."""
    best: float = rotated.largest_square(TRIANGLE).square.half_side
    rng: np.random.Generator = np.random.default_rng(9)
    minx, miny, maxx, maxy = TRIANGLE.bounds
    tried: int = 0
    while tried < 40:
        lon: float = float(rng.uniform(minx, maxx))
        lat: float = float(rng.uniform(miny, maxy))
        if not TRIANGLE.contains(shapely.Point(lon, lat)):
            continue
        tried += 1
        theta: float = float(rng.uniform(0, np.pi / 2))
        # Grow a square at this spot until it no longer fits.
        lo: float = 0.0
        hi: float = 400_000.0
        for _ in range(30):
            mid: float = (lo + hi) / 2
            if contains_ring(TRIANGLE, Square(lon, lat, theta, mid).ring(32)):
                lo = mid
            else:
                hi = mid
        assert lo <= best + 10.0


def test_picks_the_biggest_piece() -> None:
    """With an island and a mainland, the square goes in the mainland."""
    island: shapely.Polygon = rectangle(-80.0, 30.0, -79.9, 30.1)
    mainland: shapely.Polygon = rectangle(-90.0, 30.0, -89.0, 31.0)
    sq: Square = rotated.largest_square(shapely.MultiPolygon([island, mainland])).square
    assert mainland.contains(shapely.Point(sq.lon, sq.lat))


def test_empty_geometry_is_an_error() -> None:
    """There is nothing to put a square in."""
    with pytest.raises(ValueError):
        rotated.largest_square(shapely.MultiPolygon())

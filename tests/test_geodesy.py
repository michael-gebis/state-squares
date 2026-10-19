"""Tests for the WGS84 ellipsoid formulas, checked against published values."""

import numpy as np
import pytest

from squares import geodesy
from squares.arrays import FloatArray

# Semi-minor (polar) radius of WGS84.
B: float = 6356752.314245


def test_constants_match_wgs84() -> None:
    """The ellipsoid constants are the official WGS84 ones."""
    assert geodesy.A == 6378137.0
    assert 1 / geodesy.F == pytest.approx(298.257223563)
    assert geodesy.E2 == pytest.approx(0.00669437999014, rel=1e-11)


def test_radii_at_equator() -> None:
    """At the equator, east-west curvature has radius a; north-south a(1 - e^2)."""
    assert float(geodesy.prime_vertical_radius(0.0)) == pytest.approx(geodesy.A)
    assert float(geodesy.meridional_radius(0.0)) == pytest.approx(geodesy.A * (1 - geodesy.E2))


def test_radii_are_equal_at_the_poles() -> None:
    """At a pole the surface curves the same way in every direction."""
    expected: float = geodesy.A / np.sqrt(1 - geodesy.E2)
    assert float(geodesy.prime_vertical_radius(90.0)) == pytest.approx(expected)
    assert float(geodesy.meridional_radius(-90.0)) == pytest.approx(expected)


def test_radii_work_element_by_element_on_arrays() -> None:
    """Array input gives array output of the same shape."""
    lats: FloatArray = np.array([[0.0, 30.0], [60.0, 90.0]])
    assert geodesy.prime_vertical_radius(lats).shape == (2, 2)
    assert geodesy.meridional_radius(lats).shape == (2, 2)
    assert float(geodesy.meridional_radius(lats)[1, 0]) == pytest.approx(
        float(geodesy.meridional_radius(60.0)))


def test_aspect_at_equator_is_m_over_n() -> None:
    """At the equator a degree of longitude is slightly longer than a degree of latitude."""
    assert float(geodesy.aspect(0.0)) == pytest.approx(1 - geodesy.E2)


def test_aspect_is_symmetric_and_grows_toward_the_poles() -> None:
    """aspect depends only on |lat| and increases away from the equator."""
    lats: FloatArray = np.linspace(0.0, 85.0, 50)
    values: FloatArray = geodesy.aspect(lats)
    assert np.all(np.diff(values) > 0)
    np.testing.assert_allclose(geodesy.aspect(-lats), values)


def test_aspect_at_60_degrees_is_about_two() -> None:
    """cos(60°) = 1/2, so it takes about two degrees of longitude to match one of latitude."""
    assert float(geodesy.aspect(60.0)) == pytest.approx(2.0, rel=0.005)


def test_quarter_meridian_length() -> None:
    """Equator to pole along a meridian is 10,001,965.729 m on WGS84."""
    assert geodesy.meridian_arc(0.0, 90.0) == pytest.approx(10_001_965.729, abs=0.01)


def test_meridian_arc_adds_up_and_ignores_direction() -> None:
    """Arcs can be split at any latitude, and their length doesn't depend on order."""
    whole: float = geodesy.meridian_arc(10.0, 30.0)
    assert geodesy.meridian_arc(10.0, 20.0) + geodesy.meridian_arc(20.0, 30.0) == pytest.approx(whole)
    assert geodesy.meridian_arc(30.0, 10.0) == pytest.approx(whole)


def test_one_degree_of_latitude_at_45() -> None:
    """A degree of latitude around 45°N is about 111.13 km."""
    assert geodesy.meridian_arc(44.5, 45.5) == pytest.approx(111_132, rel=1e-4)


def test_equator_circumference() -> None:
    """All the way around the equator is 2πa = 40,075,016.686 m."""
    assert geodesy.parallel_arc(0.0, 360.0) == pytest.approx(40_075_016.686, abs=0.01)


def test_parallel_arc_shrinks_with_latitude() -> None:
    """At 60°N a degree of longitude is about half as long as at the equator --
    slightly more than half, because the Earth's flattening makes the east-west
    radius of curvature grow toward the poles."""
    expected: float = float(geodesy.prime_vertical_radius(60.0)) * 0.5 * np.radians(1.0)
    assert geodesy.parallel_arc(60.0, 1.0) == pytest.approx(expected)
    ratio: float = geodesy.parallel_arc(60.0, 1.0) / geodesy.parallel_arc(0.0, 1.0)
    assert 0.5 < ratio < 0.502  # 0.50126


@pytest.mark.parametrize(
    ("lon", "lat", "xyz"),
    [
        (0.0, 0.0, (geodesy.A, 0.0, 0.0)),
        (90.0, 0.0, (0.0, geodesy.A, 0.0)),
        (180.0, 0.0, (-geodesy.A, 0.0, 0.0)),
        (0.0, 90.0, (0.0, 0.0, B)),
        (0.0, -90.0, (0.0, 0.0, -B)),
    ],
)
def test_ecef_known_points(lon: float, lat: float, xyz: tuple[float, float, float]) -> None:
    """Earth-centered coordinates of points on the equator and at the poles."""
    np.testing.assert_allclose(geodesy.ecef(lon, lat), xyz, atol=1e-6)


def test_ecef_shape() -> None:
    """n points give an (n, 3) array."""
    lons: FloatArray = np.linspace(-180, 180, 7)
    assert geodesy.ecef(lons, np.zeros(7)).shape == (7, 3)


def test_ecef_points_lie_on_the_ellipsoid() -> None:
    """x²/a² + y²/a² + z²/b² = 1 for every surface point."""
    rng: np.random.Generator = np.random.default_rng(1)
    p: FloatArray = geodesy.ecef(rng.uniform(-180, 180, 100), rng.uniform(-90, 90, 100))
    radius: FloatArray = (p[:, 0] ** 2 + p[:, 1] ** 2) / geodesy.A**2 + p[:, 2] ** 2 / B**2
    np.testing.assert_allclose(radius, 1.0, rtol=1e-12)


def test_to_surface_undoes_ecef() -> None:
    """Going to 3-D and back returns the same longitude and latitude."""
    rng: np.random.Generator = np.random.default_rng(2)
    lon: FloatArray = rng.uniform(-179, 179, 200)
    lat: FloatArray = rng.uniform(-89, 89, 200)
    back_lon, back_lat = geodesy.to_surface(geodesy.ecef(lon, lat))
    np.testing.assert_allclose(back_lon, lon, atol=1e-9)
    np.testing.assert_allclose(back_lat, lat, atol=1e-9)


def test_to_surface_follows_the_line_of_sight() -> None:
    """Moving a point toward or away from the Earth's center doesn't change where it lands."""
    p: FloatArray = geodesy.ecef(np.array([-89.6, 120.0]), np.array([44.6, -33.0]))
    for scale in (0.5, 1.0, 1.7):
        lon, lat = geodesy.to_surface(p * scale)
        np.testing.assert_allclose(lon, [-89.6, 120.0], atol=1e-9)
        np.testing.assert_allclose(lat, [44.6, -33.0], atol=1e-9)

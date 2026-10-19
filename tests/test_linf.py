"""Tests for the L-infinity ("square") distance to segments."""

import numpy as np
import pytest

from squares.arrays import FloatArray
from squares.linf import closest_on_segments, segment_distance


def seg(u0: float, v0: float, u1: float, v1: float) -> tuple[FloatArray, ...]:
    """One segment from (u0, v0) to (u1, v1), as the (u0, v0, du, dv) arrays the
    functions take."""
    return (np.array([u0]), np.array([v0]), np.array([u1 - u0]), np.array([v1 - v0]))


@pytest.mark.parametrize(
    ("ends", "expected"),
    [
        ((3.0, -1.0, 3.0, 1.0), 3.0),  # vertical segment straight out to the right
        ((-1.0, -2.0, 1.0, -2.0), 2.0),  # horizontal segment below
        ((4.0, 0.0, 0.0, 4.0), 2.0),  # crosses the diagonal at (2, 2)
        ((-5.0, 1.0, 5.0, 1.0), 1.0),  # passes above the origin
        ((-1.0, -1.0, 1.0, 1.0), 0.0),  # goes through the origin
        ((2.0, 7.0, 2.0, 7.0), 7.0),  # zero length: just a point
        ((1.0, 3.0, 2.0, 4.0), 3.0),  # parallel to the diagonal
        ((-3.0, 1.0, -1.0, -1.0), 1.0),  # parallel to the other diagonal
    ],
)
def test_known_distances(ends: tuple[float, float, float, float], expected: float) -> None:
    """Hand-worked examples, including the awkward parallel and zero-length cases."""
    assert float(segment_distance(*seg(*ends))[0]) == pytest.approx(expected)
    dist, _ = closest_on_segments(*seg(*ends))
    assert float(dist[0]) == pytest.approx(expected)


def test_closest_point_location() -> None:
    """The closest point on the diagonal-crossing segment is its middle."""
    _, t = closest_on_segments(*seg(4.0, 0.0, 0.0, 4.0))
    assert float(t[0]) == pytest.approx(0.5)


def random_segments(n: int, seed: int) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    """n random segments around the origin."""
    rng: np.random.Generator = np.random.default_rng(seed)
    u0: FloatArray = rng.uniform(-10, 10, n)
    v0: FloatArray = rng.uniform(-10, 10, n)
    return u0, v0, rng.uniform(-8, 8, n), rng.uniform(-8, 8, n)


def test_matches_brute_force_sampling() -> None:
    """The exact minimum agrees with densely sampling each segment."""
    u0, v0, du, dv = random_segments(300, seed=3)
    exact: FloatArray = segment_distance(u0, v0, du, dv)
    t: FloatArray = np.linspace(0, 1, 20001)[:, None]
    sampled: FloatArray = np.maximum(np.abs(u0 + t * du), np.abs(v0 + t * dv)).min(axis=0)
    step: FloatArray = np.hypot(du, dv) / 20000
    assert np.all(exact <= sampled + 1e-12)
    assert np.all(sampled - exact <= step)


def test_returned_t_gives_the_returned_distance() -> None:
    """Plugging t back into the segment reproduces the reported distance."""
    u0, v0, du, dv = random_segments(300, seed=4)
    dist, t = closest_on_segments(u0, v0, du, dv)
    assert np.all((t >= 0) & (t <= 1))
    at_t: FloatArray = np.maximum(np.abs(u0 + t * du), np.abs(v0 + t * dv))
    np.testing.assert_allclose(at_t, dist, atol=1e-12)


def test_both_functions_agree() -> None:
    """The fast distance-only version gives the same distances."""
    u0, v0, du, dv = random_segments(500, seed=5)
    np.testing.assert_allclose(segment_distance(u0, v0, du, dv),
                               closest_on_segments(u0, v0, du, dv)[0])


def test_broadcasting_many_centers_against_many_segments() -> None:
    """Arguments broadcast, so (centers, 1) against (segments,) gives a grid."""
    u0, v0, du, dv = random_segments(6, seed=6)
    shifts: FloatArray = np.array([[0.0], [1.0], [2.0]])
    grid: FloatArray = segment_distance(u0 - shifts, v0, du, dv)
    assert grid.shape == (3, 6)
    np.testing.assert_allclose(grid[1], segment_distance(u0 - 1.0, v0, du, dv))


def test_distance_is_symmetric_under_reflection() -> None:
    """Mirroring the segments in either axis or swapping axes changes nothing."""
    u0, v0, du, dv = random_segments(200, seed=7)
    base: FloatArray = segment_distance(u0, v0, du, dv)
    np.testing.assert_allclose(segment_distance(-u0, v0, -du, dv), base)
    np.testing.assert_allclose(segment_distance(u0, -v0, du, -dv), base)
    np.testing.assert_allclose(segment_distance(v0, u0, dv, du), base)

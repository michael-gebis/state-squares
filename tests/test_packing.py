"""Tests for packing several equal squares into a state."""

import numpy as np
import pytest
import shapely
from pyproj import Proj

from squares import geodesy, packing, rotated
from squares.arrays import BoolArray, FloatArray, IntArray
from squares.linf import segment_distance
from squares.rotated import Square
from tests.helpers import rectangle

# About 80 km east-west by 111 km north-south: room for a 2 x 2 grid of
# squares about 39 km on a side.
STATE: shapely.Polygon = rectangle(-90.5, 44.0, -89.5, 45.0)
GRID_SIDE_M: float = 78_800 / 2  # half the width along the north edge


@pytest.fixture(scope="module")
def plane() -> packing._Plane:
    """The test state on its stereographic map."""
    return packing._Plane(STATE)


@pytest.fixture(scope="module")
def four() -> packing.Packing:
    """Four squares packed into the test state by a short search."""
    return packing.pack_squares(STATE, 4, seconds=4, seed=1)


def random_layout(plane: packing._Plane, n: int, seed: int) -> packing.Layout:
    """n squares at random spots and angles inside the state."""
    rng: np.random.Generator = np.random.default_rng(seed)
    x, y = plane.sample(rng, n)
    return np.column_stack([x, y, rng.uniform(0, np.pi / 2, n)])


def test_runs_expand_ranges() -> None:
    """Each run becomes its consecutive integers, tagged with its owner;
    empty runs vanish."""
    owner, values = packing._runs(np.array([7, 8, 9]), np.array([10, 0, 5]),
                                  np.array([2, 0, 3]))
    assert owner.tolist() == [7, 7, 9, 9, 9]
    assert values.tolist() == [10, 11, 5, 6, 7]


def test_scale_factor_matches_proj(plane: packing._Plane) -> None:
    """The fitted scale factor agrees with PROJ's own to 1e-8."""
    layout: packing.Layout = random_layout(plane, 200, 0)
    lon, lat = plane.lonlat(layout[:, 0], layout[:, 1])
    exact: FloatArray = np.asarray(Proj(plane.crs).get_factors(lon, lat).meridional_scale)
    assert np.abs(plane.scale(layout[:, 0], layout[:, 1]) - exact).max() < 1e-8


def test_near_finds_exactly_the_close_segments(plane: packing._Plane) -> None:
    """The grid lookup returns the same (square, segment) pairs as checking
    every segment's bounding box."""
    layout: packing.Layout = random_layout(plane, 50, 1)
    x, y = layout[:, 0], layout[:, 1]
    reach: FloatArray = np.random.default_rng(2).uniform(1_000, 60_000, 50)
    sq, seg = plane.near(x, y, reach)
    mask: BoolArray = ((plane.hi_x >= (x - reach)[:, None]) & (plane.lo_x <= (x + reach)[:, None])
                       & (plane.hi_y >= (y - reach)[:, None])
                       & (plane.lo_y <= (y + reach)[:, None]))
    want_sq, want_seg = np.nonzero(mask)
    assert sorted(zip(sq.tolist(), seg.tolist())) == sorted(zip(want_sq.tolist(),
                                                                want_seg.tolist()))
    assert np.all(np.diff(sq) >= 0)  # grouped by square


def test_room_matches_brute_force(plane: packing._Plane) -> None:
    """The room for each square equals the smallest L-infinity distance to
    any boundary segment (capped), with no shortcuts."""
    layout: packing.Layout = random_layout(plane, 100, 3)
    x, y, phi = layout[:, 0], layout[:, 1], layout[:, 2]
    cap: float = 30_000.0
    c: FloatArray = np.cos(phi)
    s: FloatArray = np.sin(phi)

    def along_sides(px: FloatArray, py: FloatArray) -> tuple[FloatArray, FloatArray]:
        """Points relative to each square's center, along its sides."""
        dx: FloatArray = px[:, None] - x
        dy: FloatArray = py[:, None] - y
        return c * dx + s * dy, c * dy - s * dx

    u0, v0 = along_sides(plane.x0, plane.y0)
    u1, v1 = along_sides(plane.x1, plane.y1)
    brute: FloatArray = segment_distance(u0, v0, u1 - u0, v1 - v0).min(axis=0)
    want: FloatArray = np.minimum(brute / plane.scale(x, y), cap)
    assert plane.room(x, y, phi, cap) == pytest.approx(want, abs=1e-6)


def test_room_is_zero_outside(plane: packing._Plane) -> None:
    """A square centered outside the state has no room at all."""
    minx, miny, _, _ = plane.shape.bounds
    room: FloatArray = plane.room(np.array([minx - 1_000.0]), np.array([miny - 1_000.0]),
                                  np.array([0.0]), 50_000.0)
    assert room.tolist() == [0.0]


def test_room_of_a_centered_square(plane: packing._Plane) -> None:
    """At the map's center, a north-facing square's room on the map matches
    the exact answer from the single-square solver (which works in the
    square's own tangent plane), and is a bit under half the state's width."""
    room: FloatArray = plane.room(np.array([0.0]), np.array([0.0]), np.array([0.0]), 1e6)
    exact: float = rotated._finish(STATE, -90.0, 44.5, 0.0)[0]
    assert room[0] == pytest.approx(exact, rel=1e-4)
    assert 0.98 < room[0] / (geodesy.parallel_arc(44.5, 1.0) / 2) < 1.0


def test_pair_limit_for_squares_side_by_side() -> None:
    """Two unturned squares 10 apart can each have half-side 5; turned 45
    degrees, their corners meet first, at 10 / (2 sqrt 2)."""
    zero: FloatArray = np.zeros(1)
    one: FloatArray = np.ones(1)
    ten: FloatArray = np.full(1, 10.0)
    best_flat: float = max(float(packing._pair_limit(zero, zero, zero, ten, zero, zero, one, one,
                                                     a)[0]) for a in range(4))
    assert best_flat == pytest.approx(5.0)
    q: FloatArray = np.full(1, np.pi / 4)
    best_turned: float = max(float(packing._pair_limit(zero, zero, q, ten, zero, q, one, one,
                                                       a)[0]) for a in range(4))
    assert best_turned == pytest.approx(10 / (2 * np.sqrt(2)))


def map_square(square: FloatArray, h: float) -> shapely.Polygon:
    """One layout row (x, y, angle) drawn as a square of half-side h on the
    map (no scale factor), as a shapely polygon."""
    x, y, phi = (float(v) for v in square)
    corners: FloatArray = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * h
    rot: FloatArray = np.array([[np.cos(phi), -np.sin(phi)], [np.sin(phi), np.cos(phi)]])
    return shapely.Polygon(corners @ rot.T + [x, y])


def test_pair_limits_are_where_squares_touch() -> None:
    """For random pairs, squares just under the limit are apart and squares
    just over it overlap; the matrix is symmetric with an infinite diagonal."""
    rng: np.random.Generator = np.random.default_rng(4)
    layout: packing.Layout = np.column_stack([rng.uniform(0, 100, 8), rng.uniform(0, 100, 8),
                                              rng.uniform(0, np.pi / 2, 8)])
    limits, _ = packing._pair_limits(layout, np.ones(8))
    assert np.array_equal(limits, limits.T)
    assert np.all(np.isinf(np.diag(limits)))
    for i in range(8):
        for j in range(i + 1, 8):
            h: float = float(limits[i, j])
            small: list[shapely.Polygon] = [map_square(layout[k], h * (1 - 1e-6)) for k in (i, j)]
            big: list[shapely.Polygon] = [map_square(layout[k], h * (1 + 1e-6)) for k in (i, j)]
            assert not small[0].intersects(small[1])
            assert big[0].intersects(big[1])


def test_best_step_solves_a_small_linear_program() -> None:
    """Maximize t with t - z <= 1 and t + z <= 3: the answer is t = 2, z = 1."""
    starts: IntArray = np.array([0, 2, 4])
    index: IntArray = np.array([0, 1, 0, 1])
    value: FloatArray = np.array([-1.0, 1.0, 1.0, 1.0])
    z: FloatArray | None = packing._best_step(2, starts, index, value, np.array([1.0, 3.0]), 5.0)
    assert z is not None
    assert z == pytest.approx([1.0, 2.0])


def test_polish_only_ever_grows_the_size(plane: packing._Plane) -> None:
    """Polishing a random layout makes it bigger, and reports the size the
    returned layout really has."""
    layout: packing.Layout = random_layout(plane, 4, 5)
    cap: float = 50_000.0
    before: float = packing._size(plane, layout, cap)
    after_layout, after = packing._polish(plane, layout, cap, rho=5_000.0)
    assert after > before
    assert after == pytest.approx(packing._size(plane, after_layout, cap))


def test_exact_half_side_agrees_with_the_single_square_solver() -> None:
    """For one square, the bisection with shapely lands on the same half-side
    as the L-infinity distance in the square's tangent plane."""
    lon, lat, theta = -90.1, 44.6, 0.3
    want: float = rotated._finish(STATE, lon, lat, theta)[0]
    got: float = packing.exact_half_side(STATE, [Square(lon, lat, theta, want)], want)
    assert got == pytest.approx(want, abs=0.1)


def test_fits_rejects_overlaps_and_spills() -> None:
    """Squares must stay inside and apart."""
    a: Square = Square(-90.2, 44.5, 0.0, 10_000.0)
    beside: Square = Square(-89.8, 44.5, 0.0, 10_000.0)
    assert packing.fits(STATE, [a, beside])
    assert not packing.fits(STATE, [a, Square(-90.15, 44.5, 0.0, 10_000.0)])
    assert not packing.fits(STATE, [Square(-90.45, 44.5, 0.0, 10_000.0)])


def test_pack_squares_finds_a_good_valid_packing(four: packing.Packing) -> None:
    """Four squares come out nearly the 2 x 2 grid, really fit, are all the
    same size, and stay under the area limit."""
    assert len(four.squares) == 4
    assert packing.fits(STATE, four.squares)
    side_m: float = four.side_mi() * geodesy.METERS_PER_MILE
    assert side_m > 0.97 * GRID_SIDE_M
    assert four.side_mi() < four.upper_mi
    assert four.search_mi == pytest.approx(four.side_mi(), rel=1e-3)
    assert np.ptp(four.sides_mi()) < 0.01
    assert four.hops > 0 and four.workers == 1


def test_pack_squares_reports_progress() -> None:
    """The logger hears about runs, and on_best gets every improvement."""
    lines: list[str] = []
    bests: list[list[Square]] = []
    packing.pack_squares(STATE, 2, seconds=1, seed=2, log=lines.append, on_best=bests.append)
    assert lines and all(line.startswith("worker 1, run") for line in lines)
    assert bests and all(len(b) == 2 for b in bests)
    sizes: list[float] = [b[0].half_side for b in bests]
    assert sizes == sorted(sizes)


@pytest.mark.slow  # starting worker processes takes a few seconds each
def test_pack_squares_in_parallel() -> None:
    """Two worker processes both report back, and the result is valid."""
    lines: list[str] = []
    result: packing.Packing = packing.pack_squares(STATE, 2, seconds=2, seed=3, workers=2,
                                                   log=lines.append)
    assert packing.fits(STATE, result.squares)
    assert {line.split(",")[0] for line in lines} == {"worker 1", "worker 2"}
    assert result.workers == 2


def test_contacts_show_where_the_packing_is_tight(four: packing.Packing) -> None:
    """A tight packing touches the state's edge, and some squares touch each
    other."""
    edge, touching = packing.contacts(STATE, four.squares)
    assert edge.shape[1] == 2 and len(edge) > 0
    assert len(touching) > 0
    assert all(STATE.buffer(0.01).contains(shapely.Point(p)) for p in edge)

"""Tests for fitting one state inside another."""

from dataclasses import replace

import numpy as np
import pytest
import shapely
from shapely.geometry.base import BaseGeometry

from squares import geodesy, nesting
from squares.arrays import FloatArray, IntArray
from squares.data import State
from tests.helpers import rectangle, state

# A host about 340 km east-west by 111 km north-south, and a guest about
# 46 km by 278 km: the guest fits only when turned a quarter turn, and then
# with room for a copy about 23% bigger (its length against the host's).
WIDE: State = state("WD", rectangle(-92.0, 39.5, -88.0, 40.5), "Wide")
TALL: State = state("TL", rectangle(-100.25, 33.75, -99.75, 36.25), "Tall")
# Small enough to fit easily; and bigger than the tall guest but too long
# (444 km) to fit at all, even on the host's diagonal (358 km).
SMALL: State = state("SM", rectangle(-80.2, 30.0, -80.0, 30.2), "Small")
LONG: State = state("LG", rectangle(-110.0, 45.0, -109.5, 49.0), "Long")


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A coarser grid and sweep and fewer starting points, so searches take
    a second, not a minute."""
    monkeypatch.setattr(nesting, "GRID_PIXELS", 150)
    monkeypatch.setattr(nesting, "SWEEP_STEP_DEG", 3.0)
    monkeypatch.setattr(nesting, "STARTS", 3)


@pytest.fixture(scope="module")
def wide() -> nesting.Outline:
    """The wide host, ready to measure."""
    return nesting.Outline(WIDE)


@pytest.fixture(scope="module")
def tall() -> nesting.Outline:
    """The tall guest, ready to move."""
    return nesting.Outline(TALL)


@pytest.mark.parametrize(("theta", "expected"), [(0.1, 5.7296), (3 * np.pi / 2, -90.0),
                                                 (-0.5, -28.6479), (np.pi, 180.0)])
def test_rotation_deg_wraps(theta: float, expected: float) -> None:
    """Turns are reported between -180 and 180 degrees."""
    assert nesting.Placement(0.0, 0.0, theta).rotation_deg() == pytest.approx(expected, abs=1e-4)


def test_tangent_plane_round_trip() -> None:
    """Projecting points onto the tangent plane and back returns them."""
    plane: nesting._Tangent = nesting._Tangent(-89.5, 44.5)
    lonlat: FloatArray = np.array([[-89.5, 44.5], [-91.0, 43.0], [-87.2, 46.1]])
    flat: FloatArray = plane.project(lonlat)
    assert np.allclose(flat[0], 0.0, atol=1e-6)
    back: FloatArray = np.array([plane.lonlat(float(x), float(y)) for x, y in flat])
    assert np.allclose(back, lonlat, atol=1e-9)


def test_center_is_the_middle(tall: nesting.Outline) -> None:
    """A rectangle's center is its middle, and its reach is the half-diagonal."""
    assert tall.lon == pytest.approx(-100.0, abs=1e-6)
    assert tall.lat == pytest.approx(35.0, abs=0.01)
    half_diagonal: float = float(np.hypot(22_800, 139_000))
    assert tall.reach == pytest.approx(half_diagonal, rel=0.01)


def test_staying_home_changes_nothing(tall: nesting.Outline) -> None:
    """Placed at its own center, unturned and at full size, a state lands
    exactly where it was."""
    moved: BaseGeometry = tall.moved(nesting.Placement(tall.lon, tall.lat, 0.0))
    original: FloatArray = shapely.get_coordinates(shapely.segmentize(TALL.geom, 0.02))
    assert np.allclose(shapely.get_coordinates(moved), original, atol=1e-9)


def test_moving_keeps_the_shape(tall: nesting.Outline) -> None:
    """Moved far north and turned, distances within the state change by less
    than 0.05%, and its area by less than 0.01%."""
    moved: BaseGeometry = tall.moved(nesting.Placement(-120.0, 60.0, 1.0))
    a: FloatArray = shapely.get_coordinates(shapely.segmentize(TALL.geom, 0.02))
    b: FloatArray = shapely.get_coordinates(moved)
    i: IntArray = np.arange(len(a))
    j: IntArray = (i * 7 + len(a) // 2) % len(a)
    before: FloatArray = geodesy.GEOD.inv(a[i, 0], a[i, 1], a[j, 0], a[j, 1])[2]
    after: FloatArray = geodesy.GEOD.inv(b[i, 0], b[i, 1], b[j, 0], b[j, 1])[2]
    far: IntArray = np.flatnonzero(before > 20_000)
    assert np.abs(after[far] / before[far] - 1).max() < 5e-4
    moved_area: float = abs(float(geodesy.GEOD.geometry_area_perimeter(moved)[0]))
    assert moved_area / tall.area == pytest.approx(1.0, abs=1e-4)


def test_home_undoes_moving(tall: nesting.Outline) -> None:
    """Tracing a moved, turned and scaled state back home gives its own
    outline, to well under a millimeter."""
    place: nesting.Placement = nesting.Placement(-120.0, 60.0, 1.0, 0.8)
    back: BaseGeometry = tall.home(place, tall.moved(place))
    error: float = float(np.abs(shapely.get_coordinates(back)
                                - shapely.get_coordinates(tall.flat)).max())
    assert error < 1e-4


def test_grid_margin_leaves_room_to_hang_off(wide: nesting.Outline) -> None:
    """The long guest can't be slid around the grid of the host alone, but
    with a margin it can, overhanging; the pixels stay the same size."""
    long: nesting.Outline = nesting.Outline(LONG)
    plain: nesting._Grid = nesting._Grid(wide, 150)
    roomy: nesting._Grid = nesting._Grid(wide, 150, margin=long.reach)
    assert roomy.pixel == pytest.approx(plain.pixel)
    assert plain.stray(long, 0.0, 1.0)[0] == np.inf
    count: float = roomy.stray(long, np.pi / 2, 1.0)[0]
    assert 0 < count < np.inf


def test_least_spill_fits_when_it_can(quick: None, wide: nesting.Outline,
                                      tall: nesting.Outline) -> None:
    """A guest that fits when turned is placed where it spills nothing."""
    place: nesting.Placement = nesting.least_spill(wide, tall)
    assert nesting.spilled(wide, tall, place) == 0.0
    assert nesting.fits(wide, tall, place)


def test_least_spill_lays_a_long_guest_along_the_host(quick: None,
                                                      wide: nesting.Outline) -> None:
    """The long guest can't fit, but it spills no more than when laid along
    the host's length, centered."""
    long: nesting.Outline = nesting.Outline(LONG)
    place: nesting.Placement = nesting.least_spill(wide, long)
    along: nesting.Placement = nesting.Placement(wide.lon, wide.lat, np.pi / 2)
    assert 0 < nesting.spilled(wide, long, place) <= nesting.spilled(wide, long, along) * 1.001


def test_nested_states_move_together(wide: nesting.Outline) -> None:
    """A small state placed a degree east of the wide state's center rides
    along when the wide state is moved and turned a quarter turn: it ends up
    north of the new center, still inside, and the wide state lands exactly
    where it would alone."""
    small: nesting.Outline = nesting.Outline(SMALL)
    places: list[nesting.Placement] = [nesting.Placement(-100.0, 45.0, np.pi / 2),
                                       nesting.Placement(wide.lon + 1.0, wide.lat, 0.0)]
    outer, inner = nesting.nested([wide, small], places)
    assert outer.equals(wide.moved(places[0]))
    assert outer.contains(inner)
    x, y = wide.plane.project(np.array([[wide.lon + 1.0, wide.lat]]))[0]
    expected: tuple[float, float] = nesting._Tangent(-100.0, 45.0).lonlat(-y, x)  # turned
    assert expected[1] > 45.7  # about 85 km north
    middle: shapely.Point = inner.centroid
    assert (middle.x, middle.y) == pytest.approx(expected, abs=2e-4)


def test_scale_limit_agrees_with_lonlat(wide: nesting.Outline, tall: nesting.Outline) -> None:
    """The biggest scale found in the host's plane is borne out by an
    independent check in lon/lat: a hair smaller fits, a hair bigger doesn't."""
    place: nesting.Placement = nesting.Placement(wide.lon, wide.lat, np.pi / 2, 1.0)
    s: float = nesting.scale_limit(wide, tall, place)
    assert s == pytest.approx(1.23, abs=0.02)
    host: BaseGeometry = shapely.segmentize(WIDE.geom, 0.02)
    assert host.contains(tall.moved(replace(place, scale=s * 0.9999)))
    assert not host.contains(tall.moved(replace(place, scale=s * 1.0001)))


def test_scale_limit_without_a_turn(wide: nesting.Outline, tall: nesting.Outline) -> None:
    """Unturned and centered on 40°N, the tall guest must shrink until its
    northern half fits in the half degree of latitude north of there."""
    s: float = nesting.scale_limit(wide, tall, nesting.Placement(-90.0, 40.0, 0.0, 1.0))
    north_reach: float = tall.flat.bounds[3]  # meters from its center to its north edge
    assert s == pytest.approx(geodesy.meridian_arc(40.0, 40.5) / north_reach, rel=0.002)


def test_scale_limit_is_zero_off_the_host(wide: nesting.Outline, tall: nesting.Outline) -> None:
    """Centered far outside the host, no copy fits."""
    assert nesting.scale_limit(wide, tall, nesting.Placement(-70.0, 30.0, 0.0, 1.0)) == 0.0


@pytest.mark.parametrize(("n", "expected"), [(1, 1), (7, 8), (243, 243), (601, 625), (98, 100)])
def test_good_fft_sizes(n: int, expected: int) -> None:
    """FFT sizes are rounded up to products of 2, 3 and 5."""
    assert nesting._good_size(n) == expected


def test_paint_covers_the_polygon() -> None:
    """A painted square covers about its own area in pixels."""
    ring: FloatArray = np.array([[10.0, 10.0], [30.0, 10.0], [30.0, 30.0], [10.0, 30.0]])
    mask: FloatArray = nesting._paint([ring], 0.0, 0.0, 1.0, 40, 40)
    assert 400 <= mask.sum() <= 441
    assert mask[20, 20] == 1 and mask[5, 5] == 0


def test_starts_are_spread_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting turns are the lowest counts, skipping ones too close to a
    better pick (around the circle too), and never unusable turns."""
    monkeypatch.setattr(nesting, "SWEEP_STEP_DEG", 1.0)
    counts: FloatArray = np.full(360, 100.0)
    counts[[10, 12, 200, 357, 90]] = [1.0, 0.0, 5.0, 2.0, 3.0]
    counts[300:] = np.where(counts[300:] == 100.0, np.inf, counts[300:])
    picks: list[int] = nesting._starts(counts).tolist()
    assert picks[:4] == [12, 357, 90, 200]
    assert 10 not in picks and all(np.isfinite(counts[picks]))
    assert len(picks) == nesting.STARTS


def test_nelder_mead_finds_the_peak() -> None:
    """The simplex search climbs to the top of a smooth hill."""
    def hill(p: FloatArray) -> float:
        """A hill peaking at (3, -2)."""
        return float(-(p[0] - 3) ** 2 - 4 * (p[1] + 2) ** 2)

    p, top = nesting._nelder_mead(hill, np.array([0.0, 0.0]), np.array([1.0, 1.0]), 1e-6, 500)
    assert np.allclose(p, [3.0, -2.0], atol=1e-4)
    assert top == pytest.approx(0.0, abs=1e-7)


def test_grid_counts_stray_pixels(quick: None, wide: nesting.Outline,
                                  tall: nesting.Outline) -> None:
    """On the grid, the guest has stray pixels at every slide unturned, but
    none turned a quarter turn, and then its center lands near the host's."""
    grid: nesting._Grid = nesting._Grid(wide)
    assert grid.stray(tall, 0.0, 1.0)[0] > 0
    count, x, y = grid.stray(tall, np.pi / 2, 1.0)
    assert count == 0
    assert abs(y) < 40_000 and abs(x) < 60_000
    assert grid.biggest(tall, np.pi / 2) == pytest.approx(1.23, abs=0.04)


def test_best_fit_turns_the_guest(quick: None, wide: nesting.Outline,
                                  tall: nesting.Outline) -> None:
    """The search finds the quarter turn, and the guest fits with room for
    a copy about 23% bigger."""
    fit: nesting.Fit = nesting.best_fit(wide, tall)
    assert fit.fits
    assert fit.placement.scale == pytest.approx(1.23, abs=0.02)
    assert abs(fit.placement.rotation_deg()) == pytest.approx(90.0, abs=2.0)
    assert shapely.segmentize(WIDE.geom, 0.02).contains(
        tall.moved(replace(fit.placement, scale=1.0)))


def test_nest_stops_at_the_biggest_fit(quick: None) -> None:
    """Guests are tried biggest first, and once one fits, smaller ones are
    skipped unless every one is wanted.  The host itself, and guests at
    least as big as it, are never tried."""
    big: State = state("BG", rectangle(-95.0, 39.0, -87.0, 41.0))
    guests: list[State] = [SMALL, TALL, WIDE, LONG, big]
    fits: list[nesting.Fit] = nesting.nest(WIDE, guests)
    assert [f.guest.abbr for f in fits] == ["LG", "TL"]  # 16,800 and 12,700 km2
    assert [f.fits for f in fits] == [False, True]
    every: list[nesting.Fit] = nesting.nest(WIDE, guests, every=True)
    assert [f.guest.abbr for f in every] == ["LG", "TL", "SM"]
    assert every[2].fits

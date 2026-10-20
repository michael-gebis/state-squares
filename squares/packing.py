"""Pack several equal squares into a state, as big as they can be.

How big can `count` true squares be -- all the same size, each free to turn,
none overlapping -- and still all fit inside a state?  Problems like this are
famously hard: even for 17 squares in a square, the best known answer (side
4.6756) has never been proven best.  So this is a search, not a proof.  Every
answer is checked to be a real packing, so it is a lower limit on the best
possible size; the state's area gives an upper limit; the true best lies
somewhere in between.

How the search works:

* Everything happens on a stereographic map centered on the state.  It is
  conformal, so a small true square shows up as a square, just scaled by the
  map's scale factor k (1 near the center, about 1.0009 at Wisconsin's far
  corners).
* A layout is each square's center and rotation.  The biggest common size
  for a layout is the smallest of these limits: for each square, the size at
  which it hits the state's edge (the rotated L-infinity distance the
  single-square solver uses), and for each pair of squares, the size at which
  they touch (from the separating-axis test).
* Polishing: sequential linear programming.  Linearize every limit that is
  close to the smallest, solve a small linear program for the move (inside a
  trust region) that raises the smallest one the most, take it if it really
  helps, and repeat.
* Exploring: start from a greedy layout, then keep shaking up the layout --
  jiggle every square, or pick one up and drop it into the roomiest spot --
  polish, and keep the change if it helps, or now and then even if it hurts a
  little (basin hopping, a relative of simulated annealing).  When a run stops
  improving, start the next one either fresh or from a hard shake of the best
  layout so far.  Each worker process runs its own search.
* Finishing: turn the best layout into true squares on the ellipsoid and find
  their largest common size directly with shapely, so the map's tiny
  distortions can't hide an overlap.
"""

from __future__ import annotations

import functools
import itertools
import queue
import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, replace
from multiprocessing import Manager, get_context
from typing import Protocol

import highspy
import numpy as np
import shapely
from numpy.polynomial import polynomial
from pyproj import Proj, Transformer
from shapely.geometry.base import BaseGeometry

from . import geodesy
from .arrays import BoolArray, FloatArray, IntArray
from .data import polygons, segments
from .linf import closest_on_segments, segment_distance
from .rotated import MAX_SEG_DEG, SQRT2, Square, _Boundary

LONLAT: str = "+proj=longlat +ellps=WGS84 +no_defs"
# Steps for the numerical derivatives: 1 m sideways, and a turn that moves a
# corner of a 50-mile square by about half a meter.
STEP_M: float = 1.0
STEP_RAD: float = 1e-5
# Stop polishing when the trust region shrinks below this many meters, or when
# the linear program foresees a gain of less than this; while exploring, stop
# sooner.
RHO_MIN: float = 0.5
STOP_GAIN: float = 0.01
COARSE_RHO_MIN: float = 5.0
COARSE_STOP_GAIN: float = 2.0
# While exploring, give up on a polish that after this many steps is still
# this far (as a fraction of the half-side) below the layout it would replace.
ABANDON_STEPS: int = 8
ABANDON_SHORTFALL: float = 0.03
# A run ends after this many hops in a row without real improvement.
STALE_HOPS: int = 80
# Share of new runs that start from a hard shake of the best layout so far
# (the rest start fresh).
KICK_SHARE: float = 0.5
# Squares measured at once against the boundary (limits memory use).
ROOM_CHUNK: int = 256
# The grid for finding boundary segments near a square has this many cells
# across the state's longer side.
GRID_CELLS: int = 64
# Candidate spots kept on hand for dropping a square into the roomiest one.
POOL_SIZE: int = 20_000
# How many of them to try each time a square is dropped back in.
DROP_SPOTS: int = 2_500
# The packing's outlines, when checked against the state with shapely, use
# this many points per side.
CHECK_POINTS: int = 128
# Two squares, or a square and the boundary, this close count as touching.
# (Generous, because the final check on the ellipsoid can shrink a packing
# found on the map by several meters.)
CONTACT_GAP_M: float = 30.0  # about 100 feet

# A layout of n squares: an (n, 3) array of map x, map y (meters) and the
# angle (radians) of each square's first side, counterclockwise from map east.
Layout = FloatArray
# Where the search reports progress: called with one line of text.
Logger = Callable[[str], None]


@dataclass(frozen=True)
class Packing:
    """The best packing found: the squares, all the same size, and a few
    facts about the search."""

    squares: list[Square]
    upper_mi: float  # side at which the squares' total area equals the state's
    search_mi: float  # side the search found on the map, before the exact check
    seconds: float
    hops: int  # shake-and-polish rounds tried
    workers: int

    def sides_mi(self) -> FloatArray:
        """Every side of every square, in miles (geodesic, corner to corner)."""
        return np.concatenate([sq.sides_mi() for sq in self.squares])

    def side_mi(self) -> float:
        """The squares' side length in miles (the average; they agree within feet)."""
        return float(self.sides_mi().mean())


def _runs(owner: IntArray, first: IntArray, counts: IntArray) -> tuple[IntArray, IntArray]:
    """Expand runs of consecutive integers: run r is first[r], first[r] + 1,
    ... (counts[r] of them) and belongs to owner[r].  Returns (owners,
    integers), one entry per integer, in order."""
    total: int = int(counts.sum())
    run_start: IntArray = np.cumsum(counts) - counts
    values: IntArray = np.arange(total) - np.repeat(run_start - first, counts)
    return np.repeat(owner, counts), values


class _Plane:
    """The state on a stereographic map centered on it, chopped into short
    straight segments, plus what the search needs to measure squares there."""

    x0: FloatArray  # segment starts and ends, map meters
    y0: FloatArray
    x1: FloatArray
    y1: FloatArray
    lo_x: FloatArray  # each segment's bounding box
    lo_y: FloatArray
    hi_x: FloatArray
    hi_y: FloatArray
    crs: str  # the map projection, as a PROJ string
    shape: BaseGeometry  # the state on the map
    area: float  # square meters, on the ellipsoid
    wide: float  # the widest segment's east-west extent
    tall: float  # the tallest segment's north-south extent
    # A grid for `near`: segments are sorted by the grid cell holding their
    # southwest corner (row by row), and cell_start[c] is where cell c begins.
    cell: float  # cell size, map meters
    grid_x0: float
    grid_y0: float
    cols: int
    rows: int
    cell_start: IntArray
    _fwd: Transformer
    _inv: Transformer
    _k_coef: FloatArray
    _k_norm: float

    def __init__(self, geom: BaseGeometry) -> None:
        """Set up the map, project the densified outline onto it, and fit the
        map's scale factor."""
        minx, miny, maxx, maxy = geom.bounds
        lon0: float = (minx + maxx) / 2
        lat0: float = (miny + maxy) / 2
        crs: str = f"+proj=stere +lat_0={lat0} +lon_0={lon0} +k_0=1 +ellps=WGS84 +units=m +no_defs"
        self.crs = crs
        self._fwd = Transformer.from_crs(LONLAT, crs, always_xy=True)
        self._inv = Transformer.from_crs(crs, LONLAT, always_xy=True)

        dense: BaseGeometry = shapely.segmentize(geom, MAX_SEG_DEG)
        parts: list[tuple[FloatArray, FloatArray, FloatArray, FloatArray]] = [
            segments(p) for p in polygons(dense)]
        lon0s, lat0s, lon1s, lat1s = (np.concatenate(col) for col in zip(*parts))
        x0, y0 = self._fwd.transform(lon0s, lat0s)
        x1, y1 = self._fwd.transform(lon1s, lat1s)
        lo_x: FloatArray = np.minimum(x0, x1)
        lo_y: FloatArray = np.minimum(y0, y1)
        self.grid_x0, self.grid_y0 = float(lo_x.min()), float(lo_y.min())
        self.cell = max(float(lo_x.max()) - self.grid_x0, float(lo_y.max()) - self.grid_y0,
                        1.0) / GRID_CELLS
        col: IntArray = ((lo_x - self.grid_x0) // self.cell).astype(int)
        row: IntArray = ((lo_y - self.grid_y0) // self.cell).astype(int)
        self.cols, self.rows = int(col.max()) + 1, int(row.max()) + 1
        key: IntArray = row * self.cols + col
        order: IntArray = np.argsort(key, kind="stable")
        self.cell_start = np.searchsorted(key[order], np.arange(self.rows * self.cols + 1))
        self.x0, self.y0, self.x1, self.y1 = x0[order], y0[order], x1[order], y1[order]
        self.lo_x, self.hi_x = np.minimum(self.x0, self.x1), np.maximum(self.x0, self.x1)
        self.lo_y, self.hi_y = np.minimum(self.y0, self.y1), np.maximum(self.y0, self.y1)
        self.wide = float((self.hi_x - self.lo_x).max())
        self.tall = float((self.hi_y - self.lo_y).max())
        self.shape = shapely.transform(dense, self._project)
        shapely.prepare(self.shape)
        self.area = abs(float(geodesy.GEOD.geometry_area_perimeter(geom)[0]))

        # The scale factor varies smoothly, so a small polynomial in x and y
        # matches PROJ's own value to about one part in ten million.
        lon, lat = np.meshgrid(np.linspace(minx - 0.5, maxx + 0.5, 40),
                               np.linspace(miny - 0.5, maxy + 0.5, 40))
        k: FloatArray = np.asarray(Proj(crs).get_factors(lon.ravel(), lat.ravel()).meridional_scale)
        x, y = self._fwd.transform(lon.ravel(), lat.ravel())
        self._k_norm = float(np.abs(np.concatenate([x, y])).max())
        basis: FloatArray = polynomial.polyvander2d(x / self._k_norm, y / self._k_norm, [4, 4])
        self._k_coef = np.linalg.lstsq(basis, k, rcond=None)[0].reshape(5, 5)

    def _project(self, lonlat: FloatArray) -> FloatArray:
        """Project an (n, 2) array of lon/lat onto the map."""
        return np.column_stack(self._fwd.transform(lonlat[:, 0], lonlat[:, 1]))

    def scale(self, x: FloatArray, y: FloatArray) -> FloatArray:
        """The map's scale factor (map meters per ground meter) at each point."""
        k: FloatArray = np.asarray(polynomial.polyval2d(
            np.asarray(x) / self._k_norm, np.asarray(y) / self._k_norm, self._k_coef),
            dtype=np.float64)
        return k

    def lonlat(self, x: FloatArray, y: FloatArray) -> tuple[FloatArray, FloatArray]:
        """Map points back to (longitude, latitude)."""
        lon, lat = self._inv.transform(x, y)
        return np.asarray(lon), np.asarray(lat)

    def north(self, x: FloatArray, y: FloatArray) -> FloatArray:
        """The direction of true north at each point, as an angle (radians)
        counterclockwise from map east."""
        lon, lat = self.lonlat(x, y)
        nx, ny = self._fwd.transform(lon, lat + 1e-4)
        return np.arctan2(np.asarray(ny) - y, np.asarray(nx) - x)

    def sample(self, rng: np.random.Generator, n: int) -> tuple[FloatArray, FloatArray]:
        """n points scattered uniformly (on the map) over the state."""
        minx, miny, maxx, maxy = self.shape.bounds
        xs: list[FloatArray] = []
        ys: list[FloatArray] = []
        have: int = 0
        while have < n:
            x: FloatArray = rng.uniform(minx, maxx, 2 * n)
            y: FloatArray = rng.uniform(miny, maxy, 2 * n)
            keep: BoolArray = shapely.contains_xy(self.shape, x, y)
            xs.append(x[keep])
            ys.append(y[keep])
            have += int(keep.sum())
        return np.concatenate(xs)[:n], np.concatenate(ys)[:n]

    def near(self, x: FloatArray, y: FloatArray,
             reach: FloatArray) -> tuple[IntArray, IntArray]:
        """Every (square, segment) pair where the segment's bounding box comes
        within reach meters of the square's center (x, y), as two index arrays
        sorted by square."""
        # Grid cells whose segments could reach the box around each square (a
        # segment sits in the cell of its southwest corner, and none is wider
        # than `wide` or taller than `tall`).
        c0: IntArray = np.clip((x - reach - self.wide - self.grid_x0) // self.cell, 0,
                               self.cols - 1).astype(int)
        c1: IntArray = np.clip((x + reach - self.grid_x0) // self.cell, -1,
                               self.cols - 1).astype(int)
        r0: IntArray = np.clip((y - reach - self.tall - self.grid_y0) // self.cell, 0,
                               self.rows - 1).astype(int)
        r1: IntArray = np.clip((y + reach - self.grid_y0) // self.cell, -1,
                               self.rows - 1).astype(int)
        # In each grid row, a square's cells c0..c1 hold one contiguous run of
        # segments.  Expand (square, row) pairs, then each pair's run.
        owner: IntArray
        row: IntArray
        owner, row = _runs(np.arange(len(x)), r0, np.maximum(r1 - r0 + 1, 0))
        first: IntArray = self.cell_start[row * self.cols + c0[owner]]
        last: IntArray = self.cell_start[row * self.cols + c1[owner] + 1]
        sq, seg = _runs(owner, first, np.maximum(last - first, 0))
        keep: BoolArray = ((self.hi_x[seg] >= (x - reach)[sq]) & (self.lo_x[seg] <= (x + reach)[sq])
                           & (self.hi_y[seg] >= (y - reach)[sq])
                           & (self.lo_y[seg] <= (y + reach)[sq]))
        return sq[keep], seg[keep]

    def distances(self, seg: IntArray, x: FloatArray, y: FloatArray,
                  phi: FloatArray) -> FloatArray:
        """L-infinity distances (map meters) from squares centered at (x, y)
        and turned by phi to the segments seg, measured along each square's
        own sides.  The arguments broadcast together."""
        c: FloatArray = np.cos(phi)
        s: FloatArray = np.sin(phi)
        dx0: FloatArray = self.x0[seg] - x
        dy0: FloatArray = self.y0[seg] - y
        dx1: FloatArray = self.x1[seg] - x
        dy1: FloatArray = self.y1[seg] - y
        u0: FloatArray = c * dx0 + s * dy0
        v0: FloatArray = c * dy0 - s * dx0
        u1: FloatArray = c * dx1 + s * dy1
        v1: FloatArray = c * dy1 - s * dx1
        return segment_distance(u0, v0, u1 - u0, v1 - v0)

    def room(self, x: FloatArray, y: FloatArray, phi: FloatArray, cap: float) -> FloatArray:
        """For each square (center x, y and angle phi), the biggest true
        half-side (ground meters) it could have and still fit, or `cap` if
        that is bigger.  Zero when the center is outside the state."""
        out: FloatArray = np.zeros(len(x))
        k: FloatArray = self.scale(x, y)
        inside: IntArray = np.flatnonzero(shapely.contains_xy(self.shape, x, y))
        for chunk in np.array_split(inside, max(1, len(inside) // ROOM_CHUNK)):
            # A segment farther than this (straight-line) can't be closer than
            # cap in L-infinity distance, so it can be skipped.
            sq, seg = self.near(x[chunk], y[chunk], SQRT2 * cap * k[chunk])
            owner: IntArray = chunk[sq]
            d: FloatArray = self.distances(seg, x[owner], y[owner], phi[owner]) / k[owner]
            best: FloatArray = np.full(len(chunk), cap)
            np.minimum.at(best, sq, d)
            out[chunk] = best
        return out


def _axis_limit(alpha: FloatArray, dx: FloatArray, dy: FloatArray, phi_i: FloatArray,
                phi_j: FloatArray, k_i: FloatArray, k_j: FloatArray) -> FloatArray:
    """The biggest common true half-side at which two squares, whose centers
    differ by (dx, dy), are still kept apart along the direction alpha.

    Projected onto that direction, a square of half-side h turned by phi
    covers h (|cos(alpha - phi)| + |sin(alpha - phi)|) either side of its
    center; they stay apart while the two reaches add up to less than the
    distance between the centers."""
    gap: FloatArray = np.abs(np.cos(alpha) * dx + np.sin(alpha) * dy)
    reach_i: FloatArray = np.abs(np.cos(alpha - phi_i)) + np.abs(np.sin(alpha - phi_i))
    reach_j: FloatArray = np.abs(np.cos(alpha - phi_j)) + np.abs(np.sin(alpha - phi_j))
    return gap / (k_i * reach_i + k_j * reach_j)


def _pair_limit(x_i: FloatArray, y_i: FloatArray, phi_i: FloatArray, x_j: FloatArray,
                y_j: FloatArray, phi_j: FloatArray, k_i: FloatArray, k_j: FloatArray,
                axis: IntArray | int) -> FloatArray:
    """_axis_limit along one of the four sides' directions: 0 and 1 are
    square i's sides, 2 and 3 are square j's."""
    ax: IntArray = np.asarray(axis)
    alpha: FloatArray = np.where(ax < 2, phi_i, phi_j) + (ax % 2) * (np.pi / 2)
    return _axis_limit(alpha, x_j - x_i, y_j - y_i, phi_i, phi_j, k_i, k_j)


def _pair_limits(layout: Layout, k: FloatArray) -> tuple[FloatArray, IntArray]:
    """For every pair of squares, the biggest common half-side at which they
    don't overlap, and which side's direction separates them.

    Two squares are apart exactly when some side direction of one of them
    separates them (the separating-axis theorem), so the limit is the best
    of the four directions.  Returns (limits, axes), each n x n; the
    diagonal is infinite."""
    x, y, phi = layout[:, 0], layout[:, 1], layout[:, 2]
    each: FloatArray = _pair_limit(x[:, None], y[:, None], phi[:, None], x[None, :], y[None, :],
                                   phi[None, :], k[:, None], k[None, :],
                                   np.arange(4)[:, None, None])  # (4, n, n)
    axes: IntArray = np.argmax(each, axis=0)
    limits: FloatArray = np.take_along_axis(each, axes[None], axis=0)[0]
    np.fill_diagonal(limits, np.inf)
    return limits, axes


def _size(plane: _Plane, layout: Layout, cap: float) -> float:
    """The biggest common true half-side (ground meters) for a layout:
    the smallest of all the room and pair limits."""
    k: FloatArray = plane.scale(layout[:, 0], layout[:, 1])
    room: FloatArray = plane.room(layout[:, 0], layout[:, 1], layout[:, 2], cap)
    return min(float(room.min()), float(_pair_limits(layout, k)[0].min()))


def _tight(plane: _Plane, layout: Layout, h: float, cap: float, slack: float) -> IntArray:
    """The squares within `slack` meters of limiting the size: the ones
    wedged against the edge or each other.  (The rest are "rattlers" with
    room to spare, and moving them alone can't help.)"""
    k: FloatArray = plane.scale(layout[:, 0], layout[:, 1])
    room: FloatArray = plane.room(layout[:, 0], layout[:, 1], layout[:, 2], cap)
    limits: FloatArray = _pair_limits(layout, k)[0]
    return np.flatnonzero((room <= h + slack) | (limits <= h + slack).any(axis=1))


def _room_rows(plane: _Plane, layout: Layout, h: float,
               margin: float) -> tuple[IntArray, FloatArray, FloatArray]:
    """Linearized edge limits for the polishing step: one for each boundary
    segment within `margin` of stopping a square.

    Returns (square index, value, gradient with respect to the square's x,
    y and angle) for each such segment."""
    # Six nudged copies of each square: +x, -x, +y, -y, +angle, -angle.
    nudge: FloatArray = np.array([[STEP_M, 0, 0], [-STEP_M, 0, 0], [0, STEP_M, 0],
                                  [0, -STEP_M, 0], [0, 0, STEP_RAD], [0, 0, -STEP_RAD]])
    # Over a meter's nudge the scale factor barely changes, so one per square will do.
    k: FloatArray = plane.scale(layout[:, 0], layout[:, 1])
    sq, seg = plane.near(layout[:, 0], layout[:, 1], SQRT2 * (h + margin) * k)
    base: FloatArray = plane.distances(seg, layout[sq, 0], layout[sq, 1], layout[sq, 2]) / k[sq]
    close: BoolArray = base <= h + margin
    sq, seg, base = sq[close], seg[close], base[close]
    trial: FloatArray = layout[sq][:, None, :] + nudge  # (pairs, 6, 3)
    d: FloatArray = (plane.distances(seg[:, None], trial[..., 0], trial[..., 1], trial[..., 2])
                     / k[sq][:, None])
    grads: FloatArray = np.column_stack([(d[:, 0] - d[:, 1]) / (2 * STEP_M),
                                         (d[:, 2] - d[:, 3]) / (2 * STEP_M),
                                         (d[:, 4] - d[:, 5]) / (2 * STEP_RAD)])
    return sq, base, grads


def _pair_rows(plane: _Plane, layout: Layout, h: float,
               margin: float) -> tuple[IntArray, IntArray, FloatArray, FloatArray]:
    """Linearized pair limits for the polishing step, for every pair within
    `margin` of touching, each along its current separating direction.

    Returns (i, j, value, gradient with respect to x_i, y_i, angle_i, x_j,
    y_j, angle_j)."""
    k: FloatArray = plane.scale(layout[:, 0], layout[:, 1])
    limits, axes = _pair_limits(layout, k)
    i, j = np.nonzero(np.triu(limits <= h + margin, 1))
    a: IntArray = axes[i, j]
    values: FloatArray = limits[i, j]
    # Twelve nudged copies of each pair: each of the six unknowns, + and -.
    steps: FloatArray = np.array([STEP_M, STEP_M, STEP_RAD] * 2)
    vi: FloatArray = np.repeat(layout[i][None], 12, axis=0)
    vj: FloatArray = np.repeat(layout[j][None], 12, axis=0)
    for col in range(6):
        for half, sign in enumerate((1.0, -1.0)):
            (vi if col < 3 else vj)[2 * col + half, :, col % 3] += sign * steps[col]
    nudged: FloatArray = _pair_limit(vi[..., 0], vi[..., 1], vi[..., 2], vj[..., 0], vj[..., 1],
                                     vj[..., 2], k[i], k[j], a)
    grads: FloatArray = ((nudged[0::2] - nudged[1::2]) / (2 * steps[:, None])).T
    return i, j, values, grads


@functools.cache
def _solver() -> highspy.Highs:
    """This process's linear-program solver (HiGHS), set up once and reused."""
    solver: highspy.Highs = highspy.Highs()  # type: ignore[no-untyped-call]
    solver.setOptionValue("output_flag", False)
    # These programs are small; skipping presolve makes them 2-3 times faster.
    solver.setOptionValue("presolve", "off")
    return solver


def _best_step(n_cols: int, starts: IntArray, index: IntArray, value: FloatArray,
               limit: FloatArray, bound: float) -> FloatArray | None:
    """Solve the polishing step's linear program: maximize the last unknown, t,
    subject to rows . z <= limit (rows given in compressed sparse row form)
    and every other unknown between -bound and bound.  Returns z, or None if
    the solver fails."""
    lp: highspy.HighsLp = highspy.HighsLp()
    lp.num_col_ = n_cols
    lp.num_row_ = len(limit)
    cost: FloatArray = np.zeros(n_cols)
    cost[-1] = -1.0
    lp.col_cost_ = cost
    lp.col_lower_ = np.append(np.full(n_cols - 1, -bound), -highspy.kHighsInf)
    lp.col_upper_ = np.append(np.full(n_cols - 1, bound), highspy.kHighsInf)
    lp.row_lower_ = np.full(len(limit), -highspy.kHighsInf)
    lp.row_upper_ = limit
    lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
    lp.a_matrix_.num_col_ = n_cols
    lp.a_matrix_.num_row_ = len(limit)
    lp.a_matrix_.start_ = starts
    lp.a_matrix_.index_ = index
    lp.a_matrix_.value_ = value
    solver: highspy.Highs = _solver()
    solver.passModel(lp)
    solver.run()
    if solver.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return None
    z: FloatArray = np.asarray(solver.getSolution().col_value)
    return z


def _polish(plane: _Plane, layout: Layout, cap: float, rho: float, rho_min: float = RHO_MIN,
            stop_gain: float = STOP_GAIN, abandon: tuple[int, float] | None = None,
            max_steps: int = 400) -> tuple[Layout, float]:
    """Improve a layout to a local best by sequential linear programming.

    Each step linearizes every limit within reach of the smallest and solves
    for the move -- each center by at most rho meters, each angle by at most
    about rho meters at the corners -- that raises the smallest linearized
    limit the most.  The move is kept only if the real size grows; the trust
    region rho grows after good steps and shrinks after bad ones.  Polishing
    stops when rho is below rho_min or the linear program foresees a gain
    below stop_gain.  With abandon = (steps, floor), it also gives up if the
    size is still below floor after that many steps.
    Returns (layout, common half-side)."""
    n: int = len(layout)
    h: float = _size(plane, layout, cap)
    rho_max: float = rho
    for step_no in range(max_steps):
        if rho < rho_min or (abandon and step_no == abandon[0] and h < abandon[1]):
            break
        hh: float = max(h, 1.0)
        # Within the trust region no limit moves by more than about 2.5 rho
        # (sliding moves a limit by up to rho, turning by up to sqrt(2) rho).
        margin: float = 4 * rho + 10
        own, r_val, r_grad = _room_rows(plane, layout, h, margin)
        pi, pj, p_val, p_grad = _pair_rows(plane, layout, h, margin)
        # Unknowns: dx, dy and corner movement h * dphi for each square, then
        # t.  Row for each limit: t - gradient . move <= value.
        scale: FloatArray = np.array([1.0, 1.0, 1 / hh])
        t_col: int = 3 * n
        r_index: IntArray = np.column_stack([3 * own, 3 * own + 1, 3 * own + 2,
                                             np.full(len(own), t_col)])
        r_value: FloatArray = np.column_stack([-r_grad * scale, np.ones(len(own))])
        p_index: IntArray = np.column_stack([3 * pi, 3 * pi + 1, 3 * pi + 2, 3 * pj, 3 * pj + 1,
                                             3 * pj + 2, np.full(len(pi), t_col)])
        p_value: FloatArray = np.column_stack([-p_grad[:, :3] * scale, -p_grad[:, 3:] * scale,
                                               np.ones(len(pi))])
        starts: IntArray = np.concatenate([np.arange(0, 4 * len(own), 4),
                                           4 * len(own) + np.arange(0, 7 * len(pi) + 1, 7)])
        z: FloatArray | None = _best_step(
            3 * n + 1, starts, np.concatenate([r_index.ravel(), p_index.ravel()]),
            np.concatenate([r_value.ravel(), p_value.ravel()]), np.concatenate([r_val, p_val]),
            rho)
        if z is None:
            rho /= 4
            continue
        predicted: float = float(z[-1]) - h
        if predicted < stop_gain:
            break
        trial: Layout = layout + z[:-1].reshape(n, 3) * scale
        # No limit can rise by more than about 2.5 rho, so the size can't
        # pass this; anything bigger needn't be measured.
        h_new: float = _size(plane, trial, min(cap, h + 4 * rho_max + 10))
        if h_new > h:
            layout, gain, h = trial, h_new - h, h_new
            if gain > 0.5 * predicted and np.abs(z[:-1]).max() > 0.9 * rho:
                rho = min(2 * rho, rho_max)
        else:
            rho /= 4
    return layout, h


def _greedy_layout(plane: _Plane, n: int, rng: np.random.Generator, pool: Layout,
                   pool_room: FloatArray) -> Layout:
    """A starting layout: place squares one at a time, each at a roomy spot
    (picked at random from the few roomiest) given the squares already placed."""
    k: FloatArray = plane.scale(pool[:, 0], pool[:, 1])
    score: FloatArray = pool_room.copy()
    chosen: list[int] = []
    for _ in range(n):
        best: IntArray = np.argsort(score)[-5:]
        pick: int = int(rng.choice(best))
        chosen.append(pick)
        limit: FloatArray = _pair_limit(pool[pick, 0], pool[pick, 1], pool[pick, 2], pool[:, 0],
                                        pool[:, 1], pool[:, 2], k[pick], k,
                                        np.arange(4)[:, None]).max(axis=0)
        score = np.minimum(score, limit)
        score[pick] = -np.inf
    return pool[chosen].copy()


def _drop_in(plane: _Plane, layout: Layout, h: float, movers: IntArray,
             rng: np.random.Generator, pool: Layout, pool_room: FloatArray) -> Layout:
    """Pick up the squares `movers` and put each back down in the roomiest
    spot (one of the few roomiest, at random), given the others.  The spots
    tried are a random sample of the pool's spots with room for nearly a
    full-size square (half-side h), or of the whole pool if there are none."""
    out: Layout = layout.copy()
    roomy: IntArray = np.flatnonzero(pool_room >= 0.9 * h)
    if not len(roomy):
        roomy = np.arange(len(pool))
    pick: IntArray = rng.choice(roomy, size=min(DROP_SPOTS, len(roomy)), replace=False)
    spots: Layout = pool[pick]
    spot_room: FloatArray = pool_room[pick]
    k_spot: FloatArray = plane.scale(spots[:, 0], spots[:, 1])
    for m in movers:
        others: Layout = np.delete(out, m, axis=0)
        k_o: FloatArray = plane.scale(others[:, 0], others[:, 1])
        # Limits against every other square at once: shape (others, spots).
        limit: FloatArray = _pair_limit(others[:, 0, None], others[:, 1, None],
                                        others[:, 2, None], spots[:, 0], spots[:, 1],
                                        spots[:, 2], k_o[:, None], k_spot,
                                        np.arange(4)[:, None, None]).max(axis=0)
        score: FloatArray = np.minimum(spot_room, limit.min(axis=0))
        out[m] = spots[int(rng.choice(np.argsort(score)[-3:]))]
    return out


def _shake(plane: _Plane, layout: Layout, h: float, cap: float, rng: np.random.Generator,
           pool: Layout, pool_room: FloatArray) -> Layout:
    """A random change to a layout, big enough to leave its local best:
    jiggle everything, pick up one or two squares and drop them in the
    roomiest spots, or turn one.  Mostly it picks squares that are wedged
    in, since moving one with room to spare changes nothing."""
    n: int = len(layout)
    tight: IntArray = _tight(plane, layout, h, cap, slack=max(30.0, 1e-3 * h))
    movable: IntArray = tight if len(tight) and rng.random() < 0.8 else np.arange(n)
    out: Layout = layout.copy()
    kind: float = rng.random()
    if kind < 0.3:
        sigma: float = h * float(rng.choice([0.03, 0.1, 0.2]))
        out[:, :2] += rng.normal(0, sigma, (n, 2))
        out[:, 2] += rng.normal(0, sigma / h, n)
    elif kind < 0.8:
        movers: IntArray = rng.choice(movable, size=min(len(movable), 1 if kind < 0.6 else 2),
                                      replace=False)
        out = _drop_in(plane, layout, h, movers, rng, pool, pool_room)
    else:
        out[int(rng.choice(movable)), 2] = rng.uniform(0, np.pi / 2)
    return out


def _kick(plane: _Plane, layout: Layout, h: float, rng: np.random.Generator, pool: Layout,
          pool_room: FloatArray) -> Layout:
    """A hard shake, for starting a new run from a good layout: nudge every
    square a little and re-drop three to five of them."""
    n: int = len(layout)
    out: Layout = layout.copy()
    out[:, :2] += rng.normal(0, 0.05 * h, (n, 2))
    out[:, 2] += rng.normal(0, 0.05, n)
    movers: IntArray = rng.choice(n, size=min(n, int(rng.integers(3, 6))), replace=False)
    return _drop_in(plane, out, h, movers, rng, pool, pool_room)


# A worker's report at the end of a run: a line of text, and the best layout
# so far with its common half-side.
Note = tuple[str, Layout, float]


class _Outbox(Protocol):
    """Somewhere to send reports (a queue, across processes)."""

    def put(self, item: Note) -> None:
        """Send one report."""


class _Direct:
    """An outbox that hands reports straight to a function, in this process."""

    def __init__(self, handle: Callable[[Note], None]) -> None:
        """Remember the function."""
        self._handle = handle

    def put(self, item: Note) -> None:
        """Pass one report on."""
        self._handle(item)


def _search(geom: BaseGeometry, count: int, seconds: float, seed: np.random.SeedSequence,
            worker: int, outbox: _Outbox) -> tuple[Layout, float, int]:
    """One worker's share of the search: runs of basin hopping, each from a
    fresh greedy layout, until time is up.  Returns (best layout, its common
    half-side, number of hops)."""
    start: float = time.perf_counter()
    rng: np.random.Generator = np.random.default_rng(seed)
    plane: _Plane = _Plane(geom)
    upper: float = float(np.sqrt(plane.area / count))
    cap: float = 0.55 * upper  # half-sides above this never matter
    px, py = plane.sample(rng, POOL_SIZE)
    pool: Layout = np.column_stack([px, py, rng.uniform(0, np.pi / 2, POOL_SIZE)])
    pool_room: FloatArray = plane.room(pool[:, 0], pool[:, 1], pool[:, 2], cap)

    best: Layout = _greedy_layout(plane, count, rng, pool, pool_room)
    best_h: float = _size(plane, best, cap)
    hops: int = 0
    run: int = 0
    while time.perf_counter() - start < seconds:
        run += 1
        kicked: bool = run > 1 and rng.random() < KICK_SHARE
        begin: Layout = (_kick(plane, best, best_h, rng, pool, pool_room) if kicked
                         else _greedy_layout(plane, count, rng, pool, pool_room))
        layout, h = _polish(plane, begin, cap, rho=0.1 * upper, rho_min=COARSE_RHO_MIN,
                            stop_gain=COARSE_STOP_GAIN)
        run_best: Layout = layout
        run_h: float = h
        stale: int = 0
        temperature: float = 2e-3 * h
        while stale < STALE_HOPS and time.perf_counter() - start < seconds:
            hops += 1
            trial, h_trial = _polish(plane, _shake(plane, layout, h, cap, rng, pool, pool_room),
                                     cap, rho=0.02 * upper, rho_min=COARSE_RHO_MIN,
                                     stop_gain=COARSE_STOP_GAIN,
                                     abandon=(ABANDON_STEPS, (1 - ABANDON_SHORTFALL) * h))
            if h_trial > h or rng.random() < np.exp((h_trial - h) / temperature):
                layout, h = trial, h_trial
            if h > run_h + 2.0:
                stale = 0
            else:
                stale += 1
            if h > run_h:
                run_best, run_h = layout, h
            temperature *= 0.98
            if h < run_h - 20 * temperature:  # wandered too far: go back
                layout, h = run_best, run_h
        run_best, run_h = _polish(plane, run_best, cap, rho=0.002 * upper)
        if run_h > best_h:
            best, best_h = run_best, run_h
        outbox.put((f"worker {worker + 1}, run {run} ({'kicked' if kicked else 'fresh'}): "
                    f"{2 * run_h / geodesy.METERS_PER_MILE:.3f} mi; "
                    f"worker best {2 * best_h / geodesy.METERS_PER_MILE:.3f} mi; "
                    f"{hops} hops, {time.perf_counter() - start:.0f}s", best, best_h))
    return best, best_h, hops


def _true_squares(plane: _Plane, layout: Layout, half_side: float) -> list[Square]:
    """Turn a map layout into true squares on the ellipsoid."""
    lon, lat = plane.lonlat(layout[:, 0], layout[:, 1])
    # Conformal map: angles match the ground, measured from true east.
    east: FloatArray = plane.north(layout[:, 0], layout[:, 1]) - np.pi / 2
    theta: FloatArray = np.mod(layout[:, 2] - east, np.pi / 2)
    return [Square(float(lo), float(la), float(t), half_side)
            for lo, la, t in zip(lon, lat, theta)]


def fits(geom: BaseGeometry, squares: list[Square]) -> bool:
    """Whether every square lies inside geom and no two overlap (touching is
    fine), checked with shapely on finely drawn outlines."""
    outlines: list[shapely.Polygon] = [shapely.Polygon(sq.ring(CHECK_POINTS)) for sq in squares]
    shapely.prepare(geom)
    if not all(geom.contains(p) for p in outlines):
        return False
    return not any(shapely.relate_pattern(a, b, "T********")  # interiors meet
                   for a, b in itertools.combinations(outlines, 2))


def exact_half_side(geom: BaseGeometry, squares: list[Square], guess: float) -> float:
    """The largest common half-side at which these squares (centers and
    rotations fixed) fit, to within a few centimeters, found by bisection
    with `fits`."""
    lo: float = guess * 0.999
    while not fits(geom, [replace(sq, half_side=lo) for sq in squares]):
        lo *= 0.99
    hi: float = guess * 1.001
    while fits(geom, [replace(sq, half_side=hi) for sq in squares]):
        lo, hi = hi, hi * 1.01
    while hi - lo > 0.02:
        mid: float = (lo + hi) / 2
        if fits(geom, [replace(sq, half_side=mid) for sq in squares]):
            lo = mid
        else:
            hi = mid
    return lo


def contacts(geom: BaseGeometry, squares: list[Square]) -> tuple[FloatArray, list[BaseGeometry]]:
    """Where a packing is wedged tight, for drawing.

    Returns the boundary points within CONTACT_GAP_M of some square, as a
    (k, 2) lon/lat array, and for each pair of squares that close together,
    the thin sliver (in lon/lat) where their slightly grown outlines meet:
    a line where sides lie flush, a dot where a corner touches a side."""
    pieces: list[shapely.Polygon] = polygons(geom)
    edge: list[FloatArray] = [np.empty((0, 2))]
    for sq in squares:
        center: shapely.Point = shapely.Point(sq.lon, sq.lat)
        piece: shapely.Polygon = next(p for p in pieces if p.contains(center))
        bnd: _Boundary = _Boundary(piece)
        projected: list[FloatArray] = [
            x[:, 0] for x in bnd.project(np.arange(len(bnd)), np.array([sq.lon]),
                                         np.array([sq.lat]), np.array([sq.theta]))]
        dist, t = closest_on_segments(*projected)
        near: BoolArray = dist <= sq.half_side + CONTACT_GAP_M
        lon0, lat0, lon1, lat1 = (x[near] for x in bnd.lonlat)
        edge.append(np.column_stack([lon0 + t[near] * (lon1 - lon0),
                                     lat0 + t[near] * (lat1 - lat0)]))
    # Grow each outline by the contact gap (in degrees; a degree of latitude
    # is about 111 km, and longitude degrees are shorter, so this is generous).
    grow: float = CONTACT_GAP_M / 111_000 / np.cos(np.radians(np.mean([s.lat for s in squares])))
    grown: list[BaseGeometry] = [shapely.Polygon(sq.ring(CHECK_POINTS)).buffer(grow / 2)
                                 for sq in squares]
    slivers: list[BaseGeometry] = [a.intersection(b) for a, b in itertools.combinations(grown, 2)
                                   if a.intersects(b)]
    return np.unique(np.round(np.concatenate(edge), 7), axis=0), slivers


def pack_squares(geom: BaseGeometry, count: int = 17, seconds: float = 300.0, seed: int = 0,
                 workers: int = 1, log: Logger | None = None,
                 on_best: Callable[[list[Square]], None] | None = None) -> Packing:
    """Search for the biggest `count` equal true squares that fit in geom
    (a state outline in lon/lat) without overlapping, for about `seconds`,
    using `workers` processes searching independently.

    `log` gets a line of progress at the end of every run.  `on_best` gets
    the squares whenever the best layout so far improves (as found on the
    map, before the exact check), so a caller can save a checkpoint.

    Returns the best packing found, checked on the ellipsoid.  The search is
    random but seeded; the time limit decides when it stops, so the same seed
    gives the same answer only on an equally fast computer."""
    start: float = time.perf_counter()
    plane: _Plane = _Plane(geom)
    upper: float = float(np.sqrt(plane.area / count))
    best: Layout | None = None
    best_h: float = 0.0

    def handle(note: Note) -> None:
        """Log a worker's report, and pass on a new best layout."""
        nonlocal best, best_h
        text, layout, h = note
        if log:
            log(text)
        if h > best_h:
            best, best_h = layout, h
            if on_best:
                on_best(_true_squares(plane, layout, h))

    seeds: list[np.random.SeedSequence] = np.random.SeedSequence(seed).spawn(workers)
    results: list[tuple[Layout, float, int]]
    if workers == 1:
        results = [_search(geom, count, seconds, seeds[0], 0, _Direct(handle))]
    else:
        with Manager() as manager, ProcessPoolExecutor(
                workers, mp_context=get_context("spawn")) as pool:
            outbox: queue.Queue[Note] = manager.Queue()
            futures: list[Future[tuple[Layout, float, int]]] = [
                pool.submit(_search, geom, count, seconds, s, w, outbox)
                for w, s in enumerate(seeds)]
            while not all(f.done() for f in futures) or not outbox.empty():
                try:
                    handle(outbox.get(timeout=0.5))
                except queue.Empty:
                    continue
            results = [f.result() for f in futures]

    layout, h, _ = max(results, key=lambda r: r[1])
    squares: list[Square] = _true_squares(plane, layout, h)
    half: float = exact_half_side(geom, squares, h)
    return Packing([replace(sq, half_side=half) for sq in squares],
                   upper / geodesy.METERS_PER_MILE, 2 * h / geodesy.METERS_PER_MILE,
                   time.perf_counter() - start, sum(r[2] for r in results), workers)

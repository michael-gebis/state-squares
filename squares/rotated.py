"""Largest true square inside a state, at any rotation (or facing north).

A square here is a quadrilateral whose sides are great-ellipse arcs (where a
plane through the Earth's center cuts the WGS84 ellipsoid -- the ellipsoidal
version of a great circle) and which projects to an exact square under the
gnomonic projection centered on it: central projection from the Earth's center
onto the plane tangent to the ellipsoid at the square's center.  On a sphere
that is exactly a regular spherical quadrilateral, with four equal sides and
four equal corner angles slightly over 90 degrees.  On the ellipsoid the four
sides, measured as geodesics, agree to a few feet, and a great-ellipse side
runs within about 25 m of the geodesic even on Alaska's square.

For a fixed center and rotation the largest square is found exactly: in the
rotated tangent plane its half-side is the L-infinity distance from the center
to the projected boundary.  The best center and rotation come from a branch and
bound over cells of (lon, lat, rotation), which comes within `tol` meters of
the optimal half-side (the cell bounds include generous allowances for the
small distortion between neighboring tangent planes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

from . import geodesy
from .arrays import BoolArray, FloatArray, IntArray, Segments
from .data import polygons, segments
from .linf import closest_on_segments, segment_distance

# Densify the boundary (whose segments are straight in lon/lat) so each piece
# stays straight to within centimeters in the tangent plane.
MAX_SEG_DEG: float = 0.02
# Cap on cells * segments per vectorized evaluation (limits memory).
CHUNK: int = 2_000_000
# Boundary points this close to the square count as touching it.
CONTACT_GAP_M: float = 15.0  # about 50 feet
# Smallest radius of curvature on the ellipsoid (meridian, at the equator).
R_MIN: float = geodesy.A * (1 - geodesy.E2)
SQRT2: float = float(np.sqrt(2.0))
# Safety margin on the search radius for boundary segments.
REACH_PAD: float = 1.02
DEG: float = np.pi / 180  # radians per degree

# Tangent-plane frames for n centers: center (ECEF), unit normal, and the
# square's u and v axes, each of shape (n, 3).
Frames: TypeAlias = tuple[FloatArray, FloatArray, FloatArray, FloatArray]


@dataclass(frozen=True)
class Square:
    """A true square on the ellipsoid, given by its center, its tilt and its
    half-side measured in the plane that touches the Earth at its center."""

    lon: float  # center
    lat: float
    theta: float  # radians counterclockwise from east/north, in [0, pi/2)
    half_side: float  # meters in the tangent plane

    def _frame(self) -> Frames:
        """The tangent-plane frame at this square's center, tilted to match it."""
        return _frames(np.array([self.lon]), np.array([self.lat]), np.array([self.theta]))

    def unproject(self, u: FloatArray, v: FloatArray) -> tuple[FloatArray, FloatArray]:
        """Turn positions measured along the square's own axes (meters from its
        center, in the tangent plane) into (longitude, latitude)."""
        frame: Frames = self._frame()
        center: FloatArray = frame[0][0]
        ax_u: FloatArray = frame[2][0]
        ax_v: FloatArray = frame[3][0]
        q: FloatArray = center + np.multiply.outer(u, ax_u) + np.multiply.outer(v, ax_v)
        return geodesy.to_surface(q)

    def corners(self) -> FloatArray:
        """The four corners as a (4, 2) array of (lon, lat), going counterclockwise."""
        h: float = self.half_side
        return np.column_stack(self.unproject(np.array([-h, h, h, -h]), np.array([-h, -h, h, h])))

    def ring(self, n: int = 64) -> FloatArray:
        """The outline as a closed list of (lon, lat) points, with `n` points per
        side so it still looks right after being drawn in a map projection."""
        h: float = self.half_side
        t: FloatArray = np.linspace(-h, h, n, endpoint=False)
        u: FloatArray = np.concatenate([t, np.full(n, h), -t, np.full(n, -h), [-h]])
        v: FloatArray = np.concatenate([np.full(n, -h), t, np.full(n, h), -t, [-h]])
        return np.column_stack(self.unproject(u, v))

    def guides(self, extend: float = 3.0) -> list[FloatArray]:
        """The four sides carried on past the corners (by `extend` times the
        half-side), as (lon, lat) polylines for drawing as guide lines."""
        h: float = self.half_side
        t: FloatArray = np.linspace(-extend * h, extend * h, 100)
        lines: list[tuple[FloatArray, FloatArray]] = (
            [(t, np.full_like(t, s * h)) for s in (-1, 1)]
            + [(np.full_like(t, s * h), t) for s in (-1, 1)])
        return [np.column_stack(self.unproject(u, v)) for u, v in lines]

    def sides_mi(self) -> FloatArray:
        """Lengths of the four sides in miles, each measured as the shortest path
        (geodesic) between neighboring corners."""
        c: FloatArray = self.corners()
        nxt: FloatArray = np.roll(c, -1, axis=0)
        dist: FloatArray = geodesy.GEOD.inv(c[:, 0], c[:, 1], nxt[:, 0], nxt[:, 1])[2]
        return dist / geodesy.METERS_PER_MILE

    def corner_angles(self) -> FloatArray:
        """The four interior angles in degrees.  On a curved surface they come
        out slightly over 90."""
        c: FloatArray = self.corners()
        nxt: FloatArray = np.roll(c, -1, axis=0)
        prv: FloatArray = np.roll(c, 1, axis=0)
        az_next: FloatArray = geodesy.GEOD.inv(c[:, 0], c[:, 1], nxt[:, 0], nxt[:, 1])[0]
        az_prev: FloatArray = geodesy.GEOD.inv(c[:, 0], c[:, 1], prv[:, 0], prv[:, 1])[0]
        d: FloatArray = np.mod(az_prev - az_next, 360)
        return np.minimum(d, 360 - d)

    def rotation_deg(self) -> float:
        """How far the sides are turned from north/east, in degrees between -45
        and 45 (positive is counterclockwise)."""
        d: float = float(np.degrees(self.theta) % 90)
        return d - 90 if d > 45 else d


@dataclass(frozen=True)
class Result:
    """The best square for a state, and the boundary points that hem it in."""

    square: Square
    contacts: FloatArray  # (k, 2) lon/lat boundary points touching the square


def _frames(lon: FloatArray, lat: FloatArray, theta: FloatArray) -> Frames:
    """For each center, the plane touching the ellipsoid there: the center
    point, the plane's normal (straight up), and two axes in the plane --
    east and north, turned counterclockwise by theta."""
    lam: FloatArray = lon * DEG
    phi: FloatArray = lat * DEG
    cl: FloatArray = np.cos(lam)
    sl: FloatArray = np.sin(lam)
    cp: FloatArray = np.cos(phi)
    sp: FloatArray = np.sin(phi)
    normal: FloatArray = np.stack([cp * cl, cp * sl, sp], axis=-1)
    east: FloatArray = np.stack([-sl, cl, np.zeros_like(sl)], axis=-1)
    north: FloatArray = np.stack([-sp * cl, -sp * sl, cp], axis=-1)
    c: FloatArray = np.cos(theta)[:, None]
    s: FloatArray = np.sin(theta)[:, None]
    return geodesy.ecef(lon, lat), normal, c * east + s * north, c * north - s * east


class _Boundary:
    """A polygon's outline, chopped into short segments and stored in 3-D so it
    can be projected quickly onto any tangent plane."""

    lonlat: Segments  # segment ends in lon/lat
    p0: FloatArray  # segment starts, Earth-centered x/y/z, shape (n, 3)
    p1: FloatArray  # segment ends
    box: tuple[FloatArray, FloatArray, FloatArray, FloatArray]  # each segment's lon/lat bounds

    def __init__(self, poly: shapely.Polygon) -> None:
        """Densify the polygon's outline and precompute what projection needs."""
        lon0, lat0, lon1, lat1 = segments(shapely.segmentize(poly, MAX_SEG_DEG))
        self.lonlat = (lon0, lat0, lon1, lat1)
        self.p0 = geodesy.ecef(lon0, lat0)
        self.p1 = geodesy.ecef(lon1, lat1)
        self.box = (np.minimum(lon0, lon1), np.minimum(lat0, lat1),
                    np.maximum(lon0, lon1), np.maximum(lat0, lat1))

    def __len__(self) -> int:
        """Number of segments."""
        return len(self.p0)

    def project(self, idx: IntArray, lon: FloatArray, lat: FloatArray,
                theta: FloatArray) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
        """Project the chosen segments into each cell's tilted tangent plane.

        Returns (u0, v0, du, dv), each of shape (len(idx), number of cells): the
        segment starts and directions, in meters, along the square's axes.
        """
        center, normal, ax_u, ax_v = _frames(lon, lat, theta)
        height: FloatArray = np.einsum("ij,ij->i", center, normal)
        cu: FloatArray = np.einsum("ij,ij->i", center, ax_u)
        cv: FloatArray = np.einsum("ij,ij->i", center, ax_v)
        out: list[FloatArray] = []
        for p in (self.p0[idx], self.p1[idx]):
            stretch: FloatArray = height / (p @ normal.T)  # slide along the ray onto the plane
            out += [stretch * (p @ ax_u.T) - cu, stretch * (p @ ax_v.T) - cv]
        u0, v0, u1, v1 = out
        return u0, v0, u1 - u0, v1 - v0

    def near(self, idx: IntArray, lon_lo: float, lat_lo: float, lon_hi: float,
             lat_hi: float) -> IntArray:
        """Which of the segments `idx` come anywhere near a lon/lat box."""
        bx0, by0, bx1, by1 = (b[idx] for b in self.box)
        overlaps: BoolArray = (bx1 >= lon_lo) & (bx0 <= lon_hi) & (by1 >= lat_lo) & (by0 <= lat_hi)
        return idx[overlaps]


def _evaluate(bnd: _Boundary, cand: IntArray, lon: FloatArray, lat: FloatArray,
              theta: FloatArray, inside: BoolArray, reach: FloatArray, t: FloatArray,
              x: FloatArray, floor: float) -> tuple[FloatArray, FloatArray, BoolArray]:
    """Score one round of search cells.

    Each cell has center (lon, lat), rotation theta, position slack t (m: how
    far any center in the cell is from this one) and rotation slack x (rad).
    Returns
      h       largest half-side (m) at each cell center, 0 outside the polygon;
      bound   upper limit on the half-side anywhere in the cell;
      needed  which segments could still be closest for some square in a cell
              whose bound exceeds `floor`, so later rounds can skip the rest.
    Only segments in `cand` within reach + t meters of a cell center are used;
    `reach` (inf for all) must cover any segment closest to a square in the cell.
    """
    h: FloatArray = np.zeros(len(lon))
    bound: FloatArray = np.empty(len(lon))
    needed: BoolArray = np.zeros(len(bnd), dtype=bool)
    radius: FloatArray = reach + t
    lat_r: FloatArray = radius / (R_MIN * DEG)
    far_lat: FloatArray = np.minimum(np.abs(lat) + lat_r, 89.0)
    lon_r: FloatArray = radius / (geodesy.prime_vertical_radius(far_lat) * np.cos(far_lat * DEG) * DEG)
    # Group nearby cells so each chunk needs few segments.
    finite: BoolArray = np.isfinite(lat_r)
    band: float = max(float(np.median(lat_r[finite])), 1e-4) if finite.any() else 1.0
    order: IntArray = np.lexsort((lon, np.floor(lat / band)))
    i: int = 0
    n: int = 256
    while i < len(order):
        c: IntArray = order[i:i + n]
        box: tuple[float, float, float, float] = (
            float((lon[c] - lon_r[c]).min()), float((lat[c] - lat_r[c]).min()),
            float((lon[c] + lon_r[c]).max()), float((lat[c] + lat_r[c]).max()))
        idx: IntArray = bnd.near(cand, *box) if np.isfinite(box).all() else cand
        if len(idx) == 0:
            idx = np.arange(len(bnd))  # only if a bound failed; stay safe
        if len(idx) * len(c) > CHUNK and len(c) > 1:
            n = max(1, len(c) // 2)
            continue
        dist: FloatArray = segment_distance(*bnd.project(idx, lon[c], lat[c], theta[c]))
        hc: FloatArray = np.where(inside[c], dist.min(axis=0), 0.0)
        tc: FloatArray = t[c]
        xc: FloatArray = x[c]
        # A square centered anywhere in the cell, at any rotation in it, is (in
        # this center's tangent plane) nearly a square offset by at most t and
        # turned by at most x, distorted by a relative eps.  Turning by x
        # changes an L-infinity distance d by at most sqrt(2) x d; offsetting
        # changes it by at most t.  The parent's bound still applies too.
        eps: FloatArray = 5 * tc * (hc + tc) / R_MIN**2
        bc: FloatArray = np.minimum((hc * (1 + SQRT2 * xc) + tc) / (1 - eps),
                                    reach[c] / (SQRT2 * REACH_PAD))
        h[c] = hc
        bound[c] = bc
        live: BoolArray = bc > floor
        if live.any():
            # The smallest distance each segment could have from a square in the cell.
            d: FloatArray = dist[:, live]
            tl: FloatArray = tc[live]
            lower: FloatArray = ((d * np.maximum(0.0, 1 - SQRT2 * xc[live]) - tl)
                                 * (1 - 5 * tl * (SQRT2 * d + tl) / R_MIN**2))
            needed[idx[(lower <= bc[live]).any(axis=1)]] = True
        i += len(c)
        n = min(2 * n, 4096)
    return h, bound, needed


def _center_slack(lat: FloatArray, a: float, b: float) -> FloatArray:
    """How far (in meters, in the tangent plane) any point of a cell can be from
    the cell's center, for cells of half-size a by b degrees."""
    lo: FloatArray = np.maximum(np.abs(lat) - b, 0.0)
    hi: FloatArray = np.minimum(np.abs(lat) + b, 90.0)
    m_lon: FloatArray = geodesy.prime_vertical_radius(lo) * np.cos(lo * DEG) * DEG
    m_lat: FloatArray = geodesy.meridional_radius(hi) * DEG
    t: FloatArray = np.hypot(a * m_lon, b * m_lat)
    return t * (1.01 + (t / R_MIN) ** 2)


def _solve_polygon(poly: shapely.Polygon, tol: float, best_h: float,
                   rotate: bool = True) -> tuple[float, tuple[float, float, float] | None]:
    """Search one polygon for the center and rotation giving the biggest square.

    The same cell-splitting idea as the aligned solver, but in three
    dimensions: each round splits cells either in position (into quarters) or
    in rotation (into halves), whichever is contributing more uncertainty.
    Without `rotate`, the rotation is fixed so the sides face north and east.

    Returns (half_side, (lon, lat, theta)) if it beats best_h, else (best_h, None).
    """
    bnd: _Boundary = _Boundary(poly)
    shapely.prepare(poly)
    minx, miny, maxx, maxy = poly.bounds
    mid: float = (miny + maxy) / 2
    m_lat: float = float(geodesy.meridional_radius(mid)) * DEG
    m_lon: float = float(geodesy.prime_vertical_radius(mid)) * np.cos(mid * DEG) * DEG
    cell: float = min((maxy - miny) * m_lat, (maxx - minx) * m_lon) / 16

    # All cells in a round share half-sizes a (deg lon), b (deg lat), d (rad).
    a: float = cell / m_lon
    b: float = cell / m_lat
    d: float = np.pi / 24 if rotate else 0.0
    thetas: FloatArray = np.arange(d, np.pi / 2, 2 * d) if rotate else np.array([0.0])
    mesh: tuple[FloatArray, ...] = np.meshgrid(np.arange(minx + a, maxx + a, 2 * a),
                                         np.arange(miny + b, maxy + b, 2 * b), thetas)
    cx: FloatArray = mesh[0].ravel()
    cy: FloatArray = mesh[1].ravel()
    ct: FloatArray = mesh[2].ravel()
    reach: FloatArray = np.full(cx.size, np.inf)
    cand: IntArray = np.arange(len(bnd))

    best: tuple[float, float, float] | None = None
    while cx.size:
        inside: BoolArray = shapely.contains_xy(poly, cx, cy)
        t: FloatArray = _center_slack(cy, a, b)
        # Meridian convergence: north at another center in the cell is turned
        # by up to g relative to north here.
        g: FloatArray = (1.2 * a * DEG * np.sin(np.minimum(np.abs(cy) + b, 90.0) * DEG)
                         + 2 * (t / R_MIN) ** 2)
        h: FloatArray
        bound: FloatArray
        needed: BoolArray
        h, bound, needed = _evaluate(bnd, cand, cx, cy, ct, inside, reach, t, d + g, best_h + tol)
        i: int = int(np.argmax(h))
        if h[i] > best_h:
            best_h, best = float(h[i]), (float(cx[i]), float(cy[i]), float(ct[i]))

        keep: BoolArray = bound > best_h + tol
        cx, cy, ct, bound = cx[keep], cy[keep], ct[keep], bound[keep]
        if not cx.size:
            break
        cand = np.flatnonzero(needed)
        # Split whichever of position or rotation is contributing more slack.
        if np.median(t) + SQRT2 * best_h * np.median(g) >= SQRT2 * best_h * d:
            a, b = a / 2, b / 2
            cx = np.concatenate([cx - a, cx + a, cx - a, cx + a])
            cy = np.concatenate([cy - b, cy - b, cy + b, cy + b])
            ct = np.tile(ct, 4)
            bound = np.tile(bound, 4)
        else:
            d = d / 2
            cx, cy = np.tile(cx, 2), np.tile(cy, 2)
            ct = np.concatenate([ct - d, ct + d])
            bound = np.tile(bound, 2)
        # A segment closest to a square of half-side at most `bound` lies within
        # L-infinity distance `bound`, so within Euclidean sqrt(2) * bound.
        reach = SQRT2 * REACH_PAD * bound
    return best_h, best


def _finish(poly: shapely.Polygon, lon: float, lat: float,
            theta: float) -> tuple[float, FloatArray]:
    """Recompute the half-side at the chosen center and rotation against every
    boundary segment, and find the boundary points touching the square."""
    bnd: _Boundary = _Boundary(poly)
    idx: IntArray = np.arange(len(bnd))
    projected: list[FloatArray] = [
        x[:, 0] for x in bnd.project(idx, np.array([lon]), np.array([lat]), np.array([theta]))]
    dist, t = closest_on_segments(*projected)
    h: float = float(dist.min())
    near: BoolArray = dist <= h + CONTACT_GAP_M
    lon0, lat0, lon1, lat1 = (x[near] for x in bnd.lonlat)
    t_near: FloatArray = t[near]
    contacts: FloatArray = np.column_stack([lon0 + t_near * (lon1 - lon0),
                                            lat0 + t_near * (lat1 - lat0)])
    return h, np.unique(np.round(contacts, 7), axis=0)


def largest_square(geom: BaseGeometry, tol: float = 5.0, rotate: bool = True) -> Result:
    """Find the biggest true square that fits inside one piece of `geom` (a
    state outline in lon/lat).  Without `rotate`, only squares whose sides face
    north, south, east and west are considered.

    `tol` is in meters of half-side: the result is within 2 * tol of the best
    possible side length (5 m is about 0.006 mile).
    """
    best_h: float = 0.0
    best: tuple[float, float, float] | None = None
    best_poly: shapely.Polygon | None = None
    for poly in sorted(polygons(geom), key=lambda p: p.area, reverse=True):
        # Skip pieces too small to beat the best so far: a square holds a
        # circle of radius equal to its half-side.
        minx, miny, maxx, maxy = poly.bounds
        near_eq: float = 0.0 if miny <= 0 <= maxy else min(abs(miny), abs(maxy))
        height: float = (maxy - miny) * float(geodesy.meridional_radius(max(abs(miny), abs(maxy)))) * DEG
        width: float = ((maxx - minx) * float(geodesy.prime_vertical_radius(near_eq))
                        * np.cos(near_eq * DEG) * DEG)
        if 1.01 * min(height, width) / 2 <= best_h:
            continue
        h, c = _solve_polygon(poly, tol, best_h, rotate)
        if c is not None:
            best_h, best, best_poly = h, c, poly
    if best is None or best_poly is None:
        raise ValueError("geometry contains no polygon")
    lon, lat, theta = best
    half_side, contacts = _finish(best_poly, lon, lat, theta)
    return Result(Square(lon, lat, theta % (np.pi / 2), half_side), contacts)

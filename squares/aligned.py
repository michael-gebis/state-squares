"""Largest latitude/longitude-aligned "square" inside a state.

The box is bounded by two parallels and two meridians, so on the ellipsoid its
north edge is shorter than its south edge.  We call it square when its
north-south side (a meridian arc) equals its east-west width measured along the
parallel through its middle latitude.  To first order this is the same as being
square in the Mercator projection.

With center latitude phi and half-height h (degrees of latitude) the half-width
is h * aspect(phi) degrees of longitude.  Dividing longitude offsets by
aspect(phi) turns the box into a true square, so for a fixed center the largest
box is given exactly by the L-infinity distance from the center to the polygon
boundary.  The best center is found with a polylabel-style branch and bound
over cells of candidate centers, which is guaranteed to come within `tol`
degrees of the optimal half-height.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import shapely
from shapely.geometry.base import BaseGeometry

from . import geodesy
from .arrays import BoolArray, FloatArray, Segments
from .data import polygons, segments
from .linf import closest_on_segments, segment_distance

# Cap on centers * segments per vectorized distance evaluation (limits memory).
CHUNK: int = 1_000_000

# Boundary points this close to the box count as touching it.  The optimum is
# tight in size, but where sliding the box barely changes its size (e.g. along a
# nearly vertical border) its position can be a few tens of feet off.
CONTACT_GAP_DEG: float = 1.45e-4  # about 50 feet of latitude


@dataclass(frozen=True)
class AlignedBox:
    """A box bounded by two parallels and two meridians, given by its center
    and half-sizes in degrees."""

    lon: float  # center
    lat: float
    half_height: float  # degrees of latitude
    half_width: float  # degrees of longitude

    @property
    def south(self) -> float:
        """Latitude of the bottom edge."""
        return self.lat - self.half_height

    @property
    def north(self) -> float:
        """Latitude of the top edge."""
        return self.lat + self.half_height

    @property
    def west(self) -> float:
        """Longitude of the left edge."""
        return self.lon - self.half_width

    @property
    def east(self) -> float:
        """Longitude of the right edge."""
        return self.lon + self.half_width

    def side_mi(self) -> float:
        """Length of the north-south sides in miles."""
        return geodesy.meridian_arc(self.south, self.north) / geodesy.METERS_PER_MILE

    def width_mi(self, lat: float) -> float:
        """East-west width in miles, measured along the parallel at `lat`."""
        return geodesy.parallel_arc(lat, 2 * self.half_width) / geodesy.METERS_PER_MILE

    def ring(self, n: int = 64) -> FloatArray:
        """The outline as a closed list of (lon, lat) points, with `n` points per
        side so it still looks right after being drawn in a map projection."""
        t: FloatArray = np.linspace(0, 1, n, endpoint=False)
        w: float = self.west
        e: float = self.east
        s: float = self.south
        nn: float = self.north
        pts: FloatArray = np.vstack([
            np.column_stack([w + (e - w) * t, np.full(n, s)]),
            np.column_stack([np.full(n, e), s + (nn - s) * t]),
            np.column_stack([e - (e - w) * t, np.full(n, nn)]),
            np.column_stack([np.full(n, w), nn - (nn - s) * t]),
        ])
        return np.vstack([pts, pts[:1]])

    def guides(self, extend: float = 3.0) -> list[FloatArray]:
        """The box's two parallels and two meridians, carried on past its corners
        (by `extend` times its size), for drawing as guide lines."""
        dlon: float = extend * (self.east - self.west)
        dlat: float = extend * (self.north - self.south)
        lons: FloatArray = np.linspace(self.west - dlon, self.east + dlon, 50)
        lats: FloatArray = np.linspace(self.south - dlat, self.north + dlat, 50)
        meridians: list[FloatArray] = [np.column_stack([np.full(50, x), lats])
                                       for x in (self.west, self.east)]
        parallels: list[FloatArray] = [np.column_stack([lons, np.full(50, y)])
                                       for y in (self.south, self.north)]
        return meridians + parallels


@dataclass(frozen=True)
class Result:
    """The best box for a state, and the boundary points that hem it in."""

    box: AlignedBox
    contacts: FloatArray  # (k, 2) lon/lat boundary points touching the box


def _scaled(cx: FloatArray, cy: FloatArray, k: FloatArray,
            seg: Segments) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    """Shift the segments so each center (cx, cy) is at the origin and shrink
    longitudes by that center's aspect k, which turns its box into a square.
    Returns arrays of shape (centers, segments) ready for the linf functions."""
    x0, y0, x1, y1 = seg
    kc: FloatArray = k[:, None]
    return (x0 - cx[:, None]) / kc, y0 - cy[:, None], (x1 - x0) / kc, y1 - y0


def _max_half_height(cx: FloatArray, cy: FloatArray, seg: Segments) -> FloatArray:
    """Half-height (degrees of latitude) of the biggest box centered at each
    point.  The points must be inside the polygon."""
    k: FloatArray = geodesy.aspect(cy)
    out: FloatArray = np.empty(len(cx))
    step: int = max(1, CHUNK // len(seg[0]))
    for i in range(0, len(cx), step):
        s: slice = slice(i, i + step)
        out[s] = segment_distance(*_scaled(cx[s], cy[s], k[s], seg)).min(axis=1)
    return out


def _min_aspect(lat_lo: npt.ArrayLike, lat_hi: npt.ArrayLike) -> FloatArray:
    """Smallest aspect() anywhere between two latitudes (it is smallest at the
    latitude nearest the equator)."""
    lo: FloatArray = np.asarray(lat_lo, dtype=float)
    hi: FloatArray = np.asarray(lat_hi, dtype=float)
    straddles: BoolArray = (lo <= 0) & (hi >= 0)
    nearest: FloatArray = np.where(straddles, 0.0, np.minimum(np.abs(lo), np.abs(hi)))
    return geodesy.aspect(nearest)


def _solve_polygon(poly: shapely.Polygon, tol: float,
                   best_h: float) -> tuple[float, tuple[float, float] | None]:
    """Search one polygon for the box center giving the biggest box.

    Works like Mapbox's polylabel: cover the polygon with cells, work out for
    each cell the biggest box its center allows and an upper limit for any
    center in the cell, throw away cells that cannot beat the best so far, and
    split the rest into quarters until none are left.

    Returns (half_height, (lon, lat)) if it beats best_h, else (best_h, None).
    """
    seg: Segments = segments(poly)
    shapely.prepare(poly)
    minx, miny, maxx, maxy = poly.bounds
    k_ref: float = float(geodesy.aspect((miny + maxy) / 2))

    # Cells have half-sizes (a, b) in (lon, lat); all cells in a round share them.
    b: float = min(maxy - miny, (maxx - minx) / k_ref) / 16
    a: float = b * k_ref
    mesh: tuple[FloatArray, ...] = np.meshgrid(np.arange(minx + a, maxx + a, 2 * a),
                                         np.arange(miny + b, maxy + b, 2 * b))
    cx: FloatArray = mesh[0].ravel()
    cy: FloatArray = mesh[1].ravel()

    best: tuple[float, float] | None = None
    while cx.size:
        h: FloatArray = np.zeros(cx.size)
        inside: BoolArray = shapely.contains_xy(poly, cx, cy)
        h[inside] = _max_half_height(cx[inside], cy[inside], seg)
        i: int = int(np.argmax(h))
        if h[i] > best_h:
            best_h, best = float(h[i]), (float(cx[i]), float(cy[i]))

        # Any box centered in the cell with half-height H contains a box centered
        # at the cell center with half-height H - b and half-width k'H - a,
        # which bounds H from above (see module docstring for the setup).
        k: FloatArray = geodesy.aspect(cy)
        k_min: FloatArray = _min_aspect(cy - b, cy + b)
        bound: FloatArray = np.maximum(h + b, (k * h + a) / k_min)
        keep: BoolArray = bound > best_h + tol

        cx, cy = cx[keep], cy[keep]
        a, b = a / 2, b / 2
        cx = np.concatenate([cx - a, cx + a, cx - a, cx + a])
        cy = np.concatenate([cy - b, cy - b, cy + b, cy + b])
    return best_h, best


def _contacts(poly: shapely.Polygon, box: AlignedBox, tol: float) -> FloatArray:
    """The boundary points touching the box (to within CONTACT_GAP_DEG): the
    places that stop it from growing."""
    seg: Segments = segments(poly)
    center_lon: FloatArray = np.array([box.lon])
    center_lat: FloatArray = np.array([box.lat])
    dist, t = closest_on_segments(*_scaled(center_lon, center_lat,
                                           geodesy.aspect(center_lat), seg))
    near: BoolArray = dist[0] <= box.half_height + max(2 * tol, CONTACT_GAP_DEG)
    x0, y0, x1, y1 = (s[near] for s in seg)
    t_near: FloatArray = t[0][near]
    pts: FloatArray = np.column_stack([x0 + t_near * (x1 - x0), y0 + t_near * (y1 - y0)])
    return np.unique(np.round(pts, 7), axis=0)


def largest_aligned_box(geom: BaseGeometry, tol: float = 1e-5) -> Result:
    """Find the biggest aligned square that fits inside one piece of `geom`
    (a state outline in lon/lat).

    `tol` is in degrees of latitude of half-height; the default 1e-5 pins the
    side length down to about 7 feet.
    """
    best_h: float = 0.0
    best_c: tuple[float, float] | None = None
    best_poly: shapely.Polygon | None = None
    for poly in sorted(polygons(geom), key=lambda p: p.area, reverse=True):
        # Skip pieces (e.g. small islands) too small to beat the best so far.
        minx, miny, maxx, maxy = poly.bounds
        cap: float = min(maxy - miny, (maxx - minx) / float(_min_aspect(miny, maxy))) / 2
        if cap <= best_h:
            continue
        h, c = _solve_polygon(poly, tol, best_h)
        if c is not None:
            best_h, best_c, best_poly = h, c, poly
    if best_c is None or best_poly is None:
        raise ValueError("geometry contains no polygon")
    lon, lat = best_c
    box: AlignedBox = AlignedBox(lon, lat, best_h, best_h * float(geodesy.aspect(lat)))
    return Result(box, _contacts(best_poly, box, tol))

"""Fit one state inside another: which other state is the biggest that fits?

A guest state fits in a host state if it can be slid and turned -- not
flipped, not stretched -- so that all of it, every island included, lies on
the host's land.

Moving a state.  The ellipsoid curves a little differently at different
latitudes, so no shape can slide across it perfectly rigidly.  Here a
state's shape is how it looks in the plane touching the Earth at its center
(the central projection that also defines the true squares).  Moving it
means laying that same flat shape, turned, in the plane touching the Earth
at the new center and projecting it back down.  Distances within the moved
state change by less than 0.05% (about 60 m for Florida moved to
California, 130 m for Texas moved to Alaska).

How close a guest comes to fitting is its scale: the biggest copy of the
guest, enlarged or shrunk about its center, that fits somewhere in the host.
A scale of 1 or more means the real guest fits.

How the search works:

* Sweep.  On a grid about 600 pixels across the host, for each degree of
  turn, one FFT cross-correlation counts how many of the guest's pixels
  would land off the host, for every possible slide at once.  The turns
  with the fewest stray pixels are the starting points, and at each one the
  guest is shrunk or grown, still on the grid, to the biggest size that
  fits.  If that size is far from 1, the sweep is repeated at that size, so
  the turns are ranked at a size where the choice matters.
* Polish.  From each starting point, a Nelder-Mead search hunts for the
  slide and turn that allow the biggest scale.  Each try is measured
  exactly: bisection on the scale, with shapely checking whether the
  densified outlines really lie inside the host.

This is a search, not a proof.  A guest reported as fitting really does (its
placement is checked exactly), but one reported as not fitting might still
fit some cleverer way.  Nor is a bigger copy always worse: along a ragged
coast, a copy can fit where a slightly smaller one doesn't, so the scale
found is a size that fits, not a promise about every smaller one.

The least spill asks a different question: where the guest, at its real
size, spills off the host least.  It uses the same sweep at full size, on a
grid with room for the guest to hang off, then Nelder-Mead on the exact
spilled area.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, replace
from multiprocessing import get_context

import numpy as np
import numpy.typing as npt
import shapely
from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely.geometry.base import BaseGeometry

from . import geodesy
from .arrays import FloatArray, IntArray
from .data import State, polygons
from .rotated import MAX_SEG_DEG, _frames

LONLAT: str = "+proj=longlat +ellps=WGS84 +no_defs"
# The sweep's grid has this many pixels across the host's longer side.
GRID_PIXELS: int = 600
# The sweep tries every turn this many degrees apart.
SWEEP_STEP_DEG: float = 1.0
# Starting points: up to this many turns, at least this many degrees apart...
STARTS: int = 8
START_GAP_DEG: float = 5.0
# ...and only those whose size on the grid is within this fraction of the best.
START_SPREAD: float = 0.03
# Sweep again at the size found on the grid if it is farther than this from 1.
RESWEEP: float = 0.02
# Sizes tried on the grid, and how many halvings of that range to make.
GRID_SCALES: tuple[float, float] = (0.2, 2.0)
GRID_HALVINGS: int = 12
# The exact scale is found to this relative precision (half a meter in 500 km).
SCALE_PRECISION: float = 1e-6
# Nelder-Mead stops when its points are this close, in units of its first
# step (a grid pixel, and the turn that moves the guest's far edge a pixel).
POLISH_TOL: float = 1e-3
POLISH_EVALS: int = 300

# Called with one line of progress.
Logger = Callable[[str], None]


@dataclass(frozen=True)
class Placement:
    """Where a guest state goes: the new position of its center, how far it
    is turned (radians counterclockwise; 0 keeps north up), and its scale (1
    is its real size)."""

    lon: float
    lat: float
    theta: float
    scale: float = 1.0

    def rotation_deg(self) -> float:
        """The turn in degrees, between -180 and 180 (positive is counterclockwise)."""
        d: float = float(np.degrees(self.theta) % 360)
        return d - 360 if d > 180 else d


@dataclass(frozen=True)
class Fit:
    """The best placement found for one guest in one host."""

    guest: State
    placement: Placement  # at the biggest scale found
    fits: bool  # whether the guest at its real size fits there (checked exactly)
    area: float  # the guest's area, square meters on the ellipsoid
    seconds: float


class _Tangent:
    """The plane touching the ellipsoid at one point, with axes pointing east
    and north.  Points go between the plane and the ellipsoid along lines
    through the Earth's center, as for the true squares."""

    basis: FloatArray  # rows: east, north, up (unit vectors, 3-D)
    center: FloatArray  # the touching point, Earth-centered x/y/z
    height: float  # how far the plane is from the Earth's center
    offset: FloatArray  # the touching point's (east, north) components

    def __init__(self, lon: float, lat: float) -> None:
        """Set up the plane touching the ellipsoid at (lon, lat)."""
        center, up, east, north = (a[0] for a in _frames(np.array([lon]), np.array([lat]),
                                                         np.array([0.0])))
        self.basis = np.stack([east, north, up])
        self.center = center
        self.height = float(center @ up)
        self.offset = np.array([center @ east, center @ north])

    def from_xyz(self, p: FloatArray) -> FloatArray:
        """Project 3-D points (n, 3), seen from the Earth's center, onto the
        plane; returns (n, 2) meters east and north of the touching point."""
        q: FloatArray = p @ self.basis.T
        return (self.height / q[:, 2:]) * q[:, :2] - self.offset

    def project(self, lonlat: FloatArray) -> FloatArray:
        """Project (n, 2) lon/lat points onto the plane."""
        return self.from_xyz(geodesy.ecef(lonlat[:, 0], lonlat[:, 1]))

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        """The point of the ellipsoid under (x, y) on the plane."""
        q: FloatArray = self.center + x * self.basis[0] + y * self.basis[1]
        lon, lat = geodesy.to_surface(q)
        return float(lon), float(lat)


def _carrier(place: Placement) -> FloatArray:
    """The 3x3 matrix taking a guest's flat coordinates (u, v, 1) -- meters
    east and north of its center, in the plane touching the Earth there --
    to 3-D points on the plane touching the Earth at the placement, turned
    and scaled to match."""
    center, _, ax_u, ax_v = (a[0] for a in _frames(np.array([place.lon]), np.array([place.lat]),
                                                   np.array([place.theta])))
    return np.column_stack([place.scale * ax_u, place.scale * ax_v, center])


class Outline:
    """A state ready to be moved and measured: its outline in the plane
    touching the Earth at its center, and on a stereographic map centered
    there (for the sweep's grid)."""

    state: State
    lon: float  # the center: the outline's centroid in its own plane
    lat: float
    area: float  # square meters, on the ellipsoid
    flat: BaseGeometry  # densified outline in its own plane, meters
    rings: list[FloatArray]  # each polygon's outer ring on the stereographic map
    reach: float  # how far the outline gets from the center, meters
    plane: _Tangent
    to_map: Transformer  # lon/lat -> stereographic map
    from_map: Transformer

    def __init__(self, state: State) -> None:
        """Find the state's center and set up its plane and map."""
        self.state = state
        geom: BaseGeometry = state.geom
        pts: FloatArray = shapely.get_coordinates(geom)
        # First guess: the average of the outline's points, seen from the
        # Earth's center (fine even for Alaska, which crosses 180 degrees).
        guess_lon, guess_lat = geodesy.to_surface(geodesy.ecef(pts[:, 0], pts[:, 1]).mean(axis=0))
        guess: _Tangent = _Tangent(float(guess_lon), float(guess_lat))
        middle: shapely.Point = shapely.transform(geom, guess.project).centroid
        self.lon, self.lat = guess.lonlat(middle.x, middle.y)
        self.plane = _Tangent(self.lon, self.lat)
        self.flat = shapely.transform(shapely.segmentize(geom, MAX_SEG_DEG), self.plane.project)
        shapely.prepare(self.flat)
        self.area = abs(float(geodesy.GEOD.geometry_area_perimeter(geom)[0]))
        flat_pts: FloatArray = shapely.get_coordinates(self.flat)
        self.reach = float(np.hypot(flat_pts[:, 0], flat_pts[:, 1]).max())
        crs: str = (f"+proj=stere +lat_0={self.lat} +lon_0={self.lon} +k_0=1 +ellps=WGS84 "
                    "+units=m +no_defs")
        self.to_map = Transformer.from_crs(LONLAT, crs, always_xy=True)
        self.from_map = Transformer.from_crs(crs, LONLAT, always_xy=True)
        self.rings = []
        for poly in polygons(geom):
            ring: FloatArray = np.asarray(poly.exterior.coords)
            self.rings.append(np.column_stack(self.to_map.transform(ring[:, 0], ring[:, 1])))

    def into(self, host: Outline, place: Placement,
             shape: BaseGeometry | None = None) -> BaseGeometry:
        """This state moved to `place`, in the host's plane (meters).  With
        `shape` (in this state's plane), moves that instead, as if it were
        part of this state: e.g. a smaller state already placed inside it."""
        to_host: FloatArray = host.plane.basis @ _carrier(place)

        def carry(uv: FloatArray) -> FloatArray:
            """Carry (n, 2) flat coordinates into the host's plane."""
            w: FloatArray = np.column_stack([uv, np.ones(len(uv))]) @ to_host.T
            return (host.plane.height / w[:, 2:]) * w[:, :2] - host.plane.offset

        return shapely.transform(self.flat if shape is None else shape, carry)

    def moved(self, place: Placement, shape: BaseGeometry | None = None) -> BaseGeometry:
        """This state moved to `place`, in lon/lat (for drawing).  With
        `shape` (in this state's plane), moves that instead, as in `into`."""
        carrier: FloatArray = _carrier(place)

        def carry(uv: FloatArray) -> FloatArray:
            """Carry (n, 2) flat coordinates down to lon/lat."""
            lon, lat = geodesy.to_surface(np.column_stack([uv, np.ones(len(uv))]) @ carrier.T)
            return np.column_stack([lon, lat])

        return shapely.transform(self.flat if shape is None else shape, carry)

    def home(self, place: Placement, shape: BaseGeometry) -> BaseGeometry:
        """Undo `moved`: where a lon/lat shape on this state, moved to
        `place`, sits in the state's own plane.  E.g. the part of it that
        spills off a host, traced back to where it is at home."""
        center, up, ax_u, ax_v = (a[0] for a in _frames(np.array([place.lon]),
                                                        np.array([place.lat]),
                                                        np.array([place.theta])))
        height: float = float(center @ up)

        def carry(lonlat: FloatArray) -> FloatArray:
            """Carry (n, 2) lon/lat points up to the plane and into flat coordinates."""
            p: FloatArray = geodesy.ecef(lonlat[:, 0], lonlat[:, 1])
            q: FloatArray = (height / (p @ up))[:, None] * p - center
            return np.column_stack([q @ ax_u, q @ ax_v]) / place.scale

        return shapely.transform(shape, carry)


def spilled(host: Outline, guest: Outline, place: Placement) -> float:
    """How much of the guest, moved to `place`, lies off the host: square
    meters in the host's plane (close to, but not exactly, the area on the
    ellipsoid)."""
    return float(guest.into(host, place).difference(host.flat).area)


def least_spill(host: Outline, guest: Outline) -> Placement:
    """Search for where the guest, at its real size, spills least off the
    host: the sweep's stray-pixel counts at every turn and slide (on a grid
    with room for the guest to hang off), then Nelder-Mead on the exact
    spilled area from the best few turns.  Stops early at a placement that
    doesn't spill at all.  A search, not a proof."""
    grid: _Grid = _Grid(host, margin=guest.reach)
    angles: FloatArray = _sweep_angles()
    steps: FloatArray = np.array([grid.pixel, grid.pixel, grid.pixel / guest.reach])

    def at(p: FloatArray) -> Placement:
        """The placement for Nelder-Mead's point (x, y in the host's plane, turn)."""
        lon, lat = host.plane.lonlat(float(p[0]), float(p[1]))
        return Placement(lon, lat, float(p[2]))

    def score(p: FloatArray) -> float:
        """Less spill scores higher."""
        return -spilled(host, guest, at(p))

    best: Placement = Placement(host.lon, host.lat, 0.0)
    least: float = np.inf
    for i in _starts(grid.sweep(guest, 1.0)):
        start: Placement = grid.placement(guest, float(angles[i]), 1.0)
        x, y = host.plane.project(np.array([[start.lon, start.lat]]))[0]
        p, v = _nelder_mead(score, np.array([x, y, start.theta]), steps, POLISH_TOL,
                            POLISH_EVALS)
        if -v < least:
            best, least = at(p), -v
        if least == 0:
            break
    return best


def nested(dolls: list[Outline], places: list[Placement]) -> list[BaseGeometry]:
    """States stacked like Russian dolls: dolls[0] moved to places[0] in some
    host, then each later doll moved to its place inside the doll before it
    (where that doll sits at home).  Returns every doll in lon/lat, with each
    one carried along with the dolls around it.  Carrying from one plane to
    the next keeps straight edges straight, so a doll that fits inside the
    one before at home still fits after the move."""
    shapes: list[BaseGeometry] = []
    for k, doll in enumerate(dolls):
        flat: BaseGeometry = doll.flat
        for j in range(k, 0, -1):  # out through the dolls around it, in their planes
            flat = dolls[j].into(dolls[j - 1], places[j], flat)
        shapes.append(dolls[0].moved(places[0], flat))
    return shapes


def fits(host: Outline, guest: Outline, place: Placement) -> bool:
    """Whether the guest, moved to `place`, lies inside the host (touching
    the border is fine), checked with shapely on the densified outlines."""
    return bool(host.flat.contains(guest.into(host, place)))


def scale_limit(host: Outline, guest: Outline, place: Placement) -> float:
    """The biggest scale at which the guest, centered and turned as in
    `place`, fits in the host (0 if none above a twentieth does), found by
    bisection starting from place.scale."""
    lo: float = place.scale
    hi: float
    if fits(host, guest, place):
        hi = lo * 1.005
        while fits(host, guest, replace(place, scale=hi)):
            lo, hi = hi, hi * 1.005
    else:
        hi, lo = lo, lo * 0.97
        while not fits(host, guest, replace(place, scale=lo)):
            if lo < 0.05:
                return 0.0
            hi, lo = lo, lo * 0.9
    while hi - lo > SCALE_PRECISION * lo:
        mid: float = (lo + hi) / 2
        if fits(host, guest, replace(place, scale=mid)):
            lo = mid
        else:
            hi = mid
    return lo


def _good_size(n: int) -> int:
    """The smallest whole number at least n with no prime factors but 2, 3
    and 5 (sizes the FFT handles fastest)."""
    while True:
        m: int = n
        for p in (2, 3, 5):
            while m % p == 0:
                m //= p
        if m == 1:
            return n
        n += 1


def _paint(rings: list[FloatArray], x0: float, y0: float, pixel: float,
           width: int, height: int) -> FloatArray:
    """A (height, width) array: 1 on pixels the rings' polygons cover (their
    outlines included), 0 elsewhere.  Pixel (i, j) is at map point
    (x0 + j pixel, y0 + i pixel)."""
    img: Image.Image = Image.new("L", (width, height), 0)
    pen: ImageDraw.ImageDraw = ImageDraw.Draw(img)
    for ring in rings:
        px: FloatArray = (ring - (x0, y0)) / pixel
        pen.polygon(px.ravel().tolist(), fill=1, outline=1)
    return np.asarray(img, dtype=np.float64)


class _Grid:
    """The host painted on a grid of pixels, ready to count, for every slide
    of a guest at once, how many of the guest's pixels land off the host."""

    host: Outline
    pixel: float  # meters
    x0: float  # map position of pixel (0, 0)
    y0: float
    shape: tuple[int, int]  # rows, columns
    _off: npt.NDArray[np.complex128]  # FFT of the off-host mask

    def __init__(self, host: Outline, pixels: int | None = None, margin: float = 0.0) -> None:
        """Paint the host on its map (GRID_PIXELS across unless `pixels` is
        given), with a border of off-host pixels: two pixels, plus `margin`
        meters for guests that may hang off the host."""
        self.host = host
        pts: FloatArray = np.concatenate(host.rings)
        lo: FloatArray = pts.min(axis=0) - margin
        hi: FloatArray = pts.max(axis=0) + margin
        self.pixel = float((hi - lo).max() - 2 * margin) / (pixels or GRID_PIXELS)
        self.x0, self.y0 = (float(v) for v in lo - 2 * self.pixel)
        cols: int = _good_size(int(np.ceil((hi[0] - self.x0) / self.pixel)) + 3)
        rows: int = _good_size(int(np.ceil((hi[1] - self.y0) / self.pixel)) + 3)
        self.shape = (rows, cols)
        on: FloatArray = _paint(host.rings, self.x0, self.y0, self.pixel, cols, rows)
        self._off = np.fft.rfft2(1.0 - on)

    def stray(self, guest: Outline, phi: float, scale: float) -> tuple[float, float, float]:
        """The guest turned by phi (radians counterclockwise on the maps) and
        scaled: the fewest of its pixels that land off the host at any slide,
        and where its center is for that slide (host map meters).  The count
        is inf if the guest doesn't fit on the grid at all."""
        c: float = scale * np.cos(phi)
        s: float = scale * np.sin(phi)
        turn: FloatArray = np.array([[c, s], [-s, c]])  # row vectors times this turn them
        rings: list[FloatArray] = [r @ turn for r in guest.rings]
        pts: FloatArray = np.concatenate(rings)
        lo: FloatArray = pts.min(axis=0) - self.pixel
        cols: int = int(np.ceil((pts[:, 0].max() - lo[0]) / self.pixel)) + 2
        rows: int = int(np.ceil((pts[:, 1].max() - lo[1]) / self.pixel)) + 2
        if rows > self.shape[0] or cols > self.shape[1]:
            return np.inf, 0.0, 0.0
        mask: FloatArray = np.zeros(self.shape)
        mask[:rows, :cols] = _paint(rings, float(lo[0]), float(lo[1]), self.pixel, cols, rows)
        # count[i, j] = how many guest pixels are off the host when the
        # guest's pixel (0, 0) sits on the host's pixel (i, j).
        count: FloatArray = np.fft.irfft2(self._off * np.conj(np.fft.rfft2(mask)), s=self.shape)
        valid: FloatArray = count[:self.shape[0] - rows + 1, :self.shape[1] - cols + 1]
        i, j = np.unravel_index(int(np.argmin(valid)), valid.shape)
        return (max(0.0, float(np.round(valid[i, j]))), self.x0 + int(j) * self.pixel - float(lo[0]),
                self.y0 + int(i) * self.pixel - float(lo[1]))

    def sweep(self, guest: Outline, scale: float) -> FloatArray:
        """The fewest stray pixels at each turn of the sweep."""
        return np.array([self.stray(guest, phi, scale)[0] for phi in _sweep_angles()])

    def biggest(self, guest: Outline, phi: float) -> float:
        """The biggest scale at which the guest, turned by phi, fits on the
        grid (no stray pixels), by bisection."""
        lo, hi = GRID_SCALES
        for _ in range(GRID_HALVINGS):
            mid: float = (lo + hi) / 2
            if self.stray(guest, phi, mid)[0] == 0:
                lo = mid
            else:
                hi = mid
        return lo

    def placement(self, guest: Outline, phi: float, scale: float) -> Placement:
        """The best slide on the grid for the guest turned by phi and scaled,
        as a placement on the ellipsoid."""
        _, x, y = self.stray(guest, phi, scale)
        to_map: Transformer = self.host.to_map
        lon, lat = self.host.from_map.transform(x, y)
        # The map is conformal, so the guest's turn on the ground is its turn
        # on the map less the turn of the map's east at that spot.
        nx, ny = to_map.transform(lon, lat + 1e-4)
        east: float = float(np.arctan2(ny - y, nx - x)) - np.pi / 2
        return Placement(float(lon), float(lat), phi - east, scale)


def _sweep_angles() -> FloatArray:
    """The turns the sweep tries, radians."""
    return np.radians(np.arange(0.0, 360.0, SWEEP_STEP_DEG))


def _starts(counts: FloatArray) -> IntArray:
    """Positions of up to STARTS of the smallest counts (in increasing order),
    at least START_GAP_DEG apart around the circle."""
    gap: int = max(1, round(START_GAP_DEG / SWEEP_STEP_DEG))
    n: int = len(counts)
    picked: list[int] = []
    for i in np.argsort(counts, kind="stable"):
        if not np.isfinite(counts[i]):
            break
        if all(min(abs(int(i) - p), n - abs(int(i) - p)) >= gap for p in picked):
            picked.append(int(i))
            if len(picked) == STARTS:
                break
    return np.array(picked, dtype=np.intp)


def _nelder_mead(f: Callable[[FloatArray], float], x0: FloatArray, steps: FloatArray,
                 tol: float, max_evals: int) -> tuple[FloatArray, float]:
    """Maximize f by the Nelder-Mead simplex method, starting from x0 with
    first steps `steps`; stop when every point is within tol steps of the
    best, or after max_evals evaluations.  Returns (best x, f there)."""
    n: int = len(x0)
    pts: list[FloatArray] = [x0.copy()] + [x0 + steps * e for e in np.eye(n)]
    vals: list[float] = [f(p) for p in pts]
    evals: int = n + 1
    while evals < max_evals:
        order: IntArray = np.argsort(vals)[::-1]
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        if max(float(np.abs((p - pts[0]) / steps).max()) for p in pts[1:]) < tol:
            break
        middle: FloatArray = np.mean(pts[:-1], axis=0)
        reflected: FloatArray = 2 * middle - pts[-1]
        f_r: float = f(reflected)
        evals += 1
        if f_r > vals[0]:
            expanded: FloatArray = 3 * middle - 2 * pts[-1]
            f_e: float = f(expanded)
            evals += 1
            pts[-1], vals[-1] = (expanded, f_e) if f_e > f_r else (reflected, f_r)
        elif f_r > vals[-2]:
            pts[-1], vals[-1] = reflected, f_r
        else:
            inner: bool = f_r <= vals[-1]
            contracted: FloatArray = (middle + pts[-1]) / 2 if inner else (middle + reflected) / 2
            f_c: float = f(contracted)
            evals += 1
            if f_c > (vals[-1] if inner else f_r):
                pts[-1], vals[-1] = contracted, f_c
            else:
                for k in range(1, n + 1):
                    pts[k] = (pts[0] + pts[k]) / 2
                    vals[k] = f(pts[k])
                evals += n
    best: int = int(np.argmax(vals))
    return pts[best], vals[best]


def _polish(host: Outline, guest: Outline, start: Placement, pixel: float) -> Placement:
    """Improve a starting placement to the biggest scale nearby: Nelder-Mead
    over the center's position (in the host's plane) and the turn, each try
    measured exactly by `scale_limit`."""
    x, y = host.plane.project(np.array([[start.lon, start.lat]]))[0]
    best: list[float] = [start.scale]  # warm start for the next bisection

    def at(p: FloatArray) -> Placement:
        """The placement for Nelder-Mead's point (x, y, turn)."""
        lon, lat = host.plane.lonlat(float(p[0]), float(p[1]))
        return Placement(lon, lat, float(p[2]), best[0])

    def score(p: FloatArray) -> float:
        """The biggest scale at Nelder-Mead's point."""
        s: float = scale_limit(host, guest, at(p))
        best[0] = max(best[0], s)
        return s

    steps: FloatArray = np.array([pixel, pixel, pixel / guest.reach])
    p, s = _nelder_mead(score, np.array([x, y, start.theta]), steps, POLISH_TOL, POLISH_EVALS)
    return replace(at(p), scale=s)


def best_fit(host: Outline, guest: Outline, grid: _Grid | None = None) -> Fit:
    """Search for the biggest scale at which the guest fits in the host, and
    check whether the guest at its real size fits there."""
    start: float = time.perf_counter()
    grid = grid or _Grid(host)
    angles: FloatArray = _sweep_angles()
    scale: float = 1.0
    picks: IntArray = _starts(grid.sweep(guest, scale))
    while len(picks) == 0 and scale > GRID_SCALES[0]:
        scale /= 2  # too big for the grid at every turn
        picks = _starts(grid.sweep(guest, scale))
    sizes: FloatArray = np.array([grid.biggest(guest, angles[i]) for i in picks])
    if len(sizes) == 0:
        return Fit(guest.state, Placement(guest.lon, guest.lat, 0.0, 0.0), False, guest.area,
                   time.perf_counter() - start)
    if abs(np.log(sizes.max() / scale)) > RESWEEP:
        # Rank the turns again near the size that matters.
        picks = _starts(grid.sweep(guest, float(sizes.max()) * 1.005))
        sizes = np.array([grid.biggest(guest, angles[i]) for i in picks])
    best: Placement = Placement(guest.lon, guest.lat, 0.0, 0.0)
    for i, size in zip(picks, sizes):
        if size < (1 - START_SPREAD) * sizes.max():
            continue
        found: Placement = _polish(host, guest, grid.placement(guest, angles[i], size), grid.pixel)
        if found.scale > best.scale:
            best = found
    real: bool = best.scale >= 1 and fits(host, guest, replace(best, scale=1.0))
    return Fit(guest.state, best, real, guest.area, time.perf_counter() - start)


# Each worker process keeps the host it is working on, keyed by its
# abbreviation, its bounds and the grid size.
_HOST: dict[tuple[str, tuple[float, ...], int], tuple[Outline, _Grid]] = {}


def _fit_job(host: State, guest: State) -> Fit:
    """One guest's search, in a worker process."""
    key: tuple[str, tuple[float, ...], int] = (host.abbr, tuple(host.geom.bounds), GRID_PIXELS)
    if key not in _HOST:
        _HOST.clear()
        outline: Outline = Outline(host)
        _HOST[key] = (outline, _Grid(outline))
    h, grid = _HOST[key]
    return best_fit(h, Outline(guest), grid)


def area(state: State) -> float:
    """A state's area in square meters, on the ellipsoid."""
    return abs(float(geodesy.GEOD.geometry_area_perimeter(state.geom)[0]))


def nest(host: State, guests: list[State], workers: int = 1, every: bool = False,
         log: Logger | None = None) -> list[Fit]:
    """Search for each guest's best fit in the host, biggest guest (by area)
    first, using `workers` processes.  Unless `every` is set, guests smaller
    than the biggest one found to fit are skipped.  Guests at least as big
    as the host can't fit and are left out.  Returns the fits in order of
    guest area, biggest first."""
    host_area: float = area(host)
    sized: list[tuple[float, State]] = sorted(
        ((area(g), g) for g in guests if g.abbr != host.abbr), key=lambda t: -t[0])
    todo: list[State] = [g for a, g in sized if a < host_area]
    rank: dict[str, int] = {g.abbr: i for i, g in enumerate(todo)}
    done: dict[str, Fit] = {}

    def record(fit: Fit) -> None:
        """Keep one guest's result and report it."""
        done[fit.guest.abbr] = fit
        if log:
            log(f"{fit.guest.abbr}: scale {fit.placement.scale:.4f}"
                f"{'  fits' if fit.fits else ''}  ({fit.seconds:.0f}s)")

    def winner() -> int:
        """Rank of the biggest guest known to fit (len(todo) if none)."""
        return min((rank[a] for a, f in done.items() if f.fits), default=len(todo))

    if workers == 1:
        for g in todo:
            if not every and rank[g.abbr] > winner():
                break
            record(_fit_job(host, g))
    else:
        with ProcessPoolExecutor(workers, mp_context=get_context("spawn")) as pool:
            futures: dict[str, Future[Fit]] = {g.abbr: pool.submit(_fit_job, host, g) for g in todo}
            for abbr, fut in futures.items():  # in order of size, biggest first
                if not every and rank[abbr] > winner():
                    fut.cancel()
                    continue
                record(fut.result())
    return [done[g.abbr] for g in todo if g.abbr in done]

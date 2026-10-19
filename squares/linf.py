"""The "square distance" from a point to line segments.

For a square centered on the origin with sides along the u and v axes, the
largest one that avoids a point (u, v) has half-side max(|u|, |v|) -- the
L-infinity distance.  So the biggest square that avoids a whole boundary is
set by the boundary point with the smallest L-infinity distance.

Both functions take the segments as start points (u0, v0) and direction
vectors (du, dv); a point on a segment is (u0 + t du, v0 + t dv) for t in
[0, 1].  Arguments broadcast together, so one call can handle many squares
and many segments at once.
"""

import numpy as np

from .arrays import BoolArray, FloatArray


def _candidates(u0: FloatArray, v0: FloatArray, du: FloatArray,
                dv: FloatArray) -> list[float | FloatArray]:
    """Where along each segment its closest point can be, apart from t = 0.

    Along a segment the distance is a convex, piecewise-linear function of t,
    so its minimum is at an end (t = 0 or 1) or where |u| = |v|.  Segments
    parallel to a diagonal give NaN here, which callers ignore.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        return [1.0, (v0 - u0) / (du - dv), -(u0 + v0) / (du + dv)]


def _distance_at(u0: FloatArray, v0: FloatArray, du: FloatArray, dv: FloatArray,
                 t: float | FloatArray) -> FloatArray:
    """L-infinity distance of the point a fraction t along each segment."""
    return np.maximum(np.abs(u0 + t * du), np.abs(v0 + t * dv))


def segment_distance(u0: FloatArray, v0: FloatArray, du: FloatArray,
                     dv: FloatArray) -> FloatArray:
    """Smallest L-infinity distance from the origin to each segment."""
    dist: FloatArray = np.maximum(np.abs(u0), np.abs(v0))
    for t in _candidates(u0, v0, du, dv):
        # fmin skips the NaNs from diagonal-parallel segments.
        dist = np.fmin(dist, _distance_at(u0, v0, du, dv, np.clip(t, 0.0, 1.0)))
    return dist


def closest_on_segments(u0: FloatArray, v0: FloatArray, du: FloatArray,
                        dv: FloatArray) -> tuple[FloatArray, FloatArray]:
    """Like segment_distance, but also says where the closest point is.

    Returns (dist, t): the distances and how far along each segment (0 to 1)
    the closest point lies.
    """
    dist: FloatArray = np.maximum(np.abs(u0), np.abs(v0))
    t_best: FloatArray = np.zeros_like(dist)
    for t in _candidates(u0, v0, du, dv):
        t_clipped: float | FloatArray = np.clip(t, 0.0, 1.0)
        d: FloatArray = _distance_at(u0, v0, du, dv, t_clipped)
        better: BoolArray = d < dist  # NaN never compares as better
        dist = np.where(better, d, dist)
        t_best = np.where(better, t_clipped, t_best)
    return dist, t_best

"""WGS84 ellipsoid helpers.

Angles are in degrees and lengths in meters.  Functions whose arguments are
typed `npt.ArrayLike` accept a single number or a whole NumPy array and work
element by element.
"""

import numpy as np
import numpy.typing as npt
from pyproj import Geod

from .arrays import FloatArray

A: float = 6378137.0  # equatorial radius
F: float = 1 / 298.257223563  # flattening
E2: float = F * (2 - F)  # first eccentricity squared
METERS_PER_MILE: float = 1609.344
GEOD: Geod = Geod(ellps="WGS84")


def _w(lat: npt.ArrayLike) -> FloatArray:
    """The factor sqrt(1 - e^2 sin^2 lat) that shows up in the ellipsoid's
    radii of curvature."""
    sin_lat: FloatArray = np.sin(np.radians(lat))
    return np.sqrt(1 - E2 * sin_lat**2)


def prime_vertical_radius(lat: npt.ArrayLike) -> FloatArray:
    """How sharply the ellipsoid curves in the east-west direction at a latitude.

    Multiplying by cos(lat) gives the radius of that latitude's parallel.
    """
    return A / _w(lat)


def meridional_radius(lat: npt.ArrayLike) -> FloatArray:
    """How sharply the ellipsoid curves in the north-south direction at a latitude."""
    return A * (1 - E2) / _w(lat) ** 3


def aspect(lat: npt.ArrayLike) -> FloatArray:
    """How many degrees of longitude cover the same ground distance as one
    degree of latitude, at the given latitude.

    It is about 1 at the equator and grows toward the poles, where meridians
    crowd together; it depends only on the size of the latitude.
    """
    return meridional_radius(lat) / (prime_vertical_radius(lat) * np.cos(np.radians(lat)))


def meridian_arc(lat1: float, lat2: float) -> float:
    """Distance along a meridian (a north-south line) between two latitudes."""
    return float(GEOD.inv(0.0, lat1, 0.0, lat2)[2])


def parallel_arc(lat: float, dlon: float) -> float:
    """Distance along a parallel (an east-west line of constant latitude)
    spanning `dlon` degrees of longitude.  Parallels are not shortest paths, so
    this is longer than the geodesic between the same two points."""
    return float(prime_vertical_radius(lat) * np.cos(np.radians(lat)) * np.radians(dlon))


def ecef(lon: npt.ArrayLike, lat: npt.ArrayLike) -> FloatArray:
    """3-D Earth-centered coordinates (x, y, z) of points on the ellipsoid's
    surface, as an array whose last axis has length 3."""
    lam: FloatArray = np.radians(lon)
    phi: FloatArray = np.radians(lat)
    n: FloatArray = prime_vertical_radius(lat)
    return np.stack([n * np.cos(phi) * np.cos(lam), n * np.cos(phi) * np.sin(lam),
                     n * (1 - E2) * np.sin(phi)], axis=-1)


def to_surface(p: FloatArray) -> tuple[FloatArray, FloatArray]:
    """Slide 3-D points along their lines of sight from the Earth's center until
    they land on the ellipsoid, and return their (longitude, latitude)."""
    b2: float = (A * A) * (1 - E2)
    s: FloatArray = 1 / np.sqrt((p[..., 0] ** 2 + p[..., 1] ** 2) / (A * A) + p[..., 2] ** 2 / b2)
    x: FloatArray = s * p[..., 0]
    y: FloatArray = s * p[..., 1]
    z: FloatArray = s * p[..., 2]
    lon: FloatArray = np.degrees(np.arctan2(y, x))
    # On the surface, tan(lat) = z / ((1 - e^2) * distance from the axis).
    lat: FloatArray = np.degrees(np.arctan2(z, (1 - E2) * np.hypot(x, y)))
    return lon, lat

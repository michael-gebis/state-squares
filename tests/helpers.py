"""Small shapes and checks shared by several test modules."""

import shapely
from shapely.geometry.base import BaseGeometry

from squares.arrays import FloatArray
from squares.data import State


def rectangle(west: float, south: float, east: float, north: float) -> shapely.Polygon:
    """A polygon bounded by two meridians and two parallels (straight edges in lon/lat)."""
    return shapely.box(west, south, east, north)


def state(abbr: str, geom: BaseGeometry, name: str | None = None) -> State:
    """A made-up State for tests that shouldn't depend on the Census download."""
    return State(abbr, name or abbr, geom)


def contains_ring(geom: BaseGeometry, ring: FloatArray) -> bool:
    """Whether the polygon traced by a closed lon/lat ring lies inside `geom`,
    judged independently by shapely."""
    return bool(geom.contains(shapely.Polygon(ring)))


# Made-up states in roughly the right places, so the map's projections and
# insets behave as they would with real data.
SYNTHETIC_STATES: list[State] = [
    state("WI", rectangle(-92.0, 43.0, -88.0, 46.0), "Wisconsin"),
    state("RI", rectangle(-71.8, 41.4, -71.5, 41.7), "Rhode Island"),
    state("AK", rectangle(-160.0, 60.0, -145.0, 68.0), "Alaska"),
    state("HI", rectangle(-156.0, 19.0, -155.0, 20.0), "Hawaii"),
]

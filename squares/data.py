"""US state boundaries from the Census Bureau's cartographic boundary files.

The cartographic boundary ("cb") files are clipped to the shoreline, so the
Great Lakes and coastal waters are not part of any state.  Coordinates are
NAD83 longitude/latitude, which matches WGS84 to about a meter.
"""

import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapefile  # type: ignore[import-untyped]  # pyshp ships no type hints
import shapely
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from .arrays import FloatArray, Segments

URL: str = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_500k.zip"
# Records in the Census file that are not states: DC and the territories.
NOT_STATES: frozenset[str] = frozenset({"DC", "PR", "AS", "GU", "MP", "VI"})


@dataclass(frozen=True)
class State:
    """One US state: postal abbreviation, name, and outline in lon/lat degrees."""

    abbr: str
    name: str
    geom: BaseGeometry  # a Polygon or MultiPolygon


def fetch(data_dir: Path) -> Path:
    """Download the Census boundary file into `data_dir` (only the first time)
    and return where it is."""
    path: Path = data_dir / URL.rsplit("/", 1)[1]
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        part: Path = path.with_suffix(".part")
        urllib.request.urlretrieve(URL, part)
        part.replace(path)
    return path


def load_states(path: Path) -> list[State]:
    """Read the 50 states from the downloaded zip file, sorted by name."""
    states: list[State] = []
    with shapefile.Reader(str(path)) as reader:
        for shape_record in reader.iterShapeRecords():
            record: dict[str, Any] = shape_record.record.as_dict()
            if record["STUSPS"] in NOT_STATES:
                continue
            geom: BaseGeometry = shape(shape_record.shape.__geo_interface__)
            if not geom.is_valid:
                geom = shapely.make_valid(geom)
            states.append(State(record["STUSPS"], record["NAME"], geom))
    states.sort(key=lambda s: s.name)
    return states


def polygons(geom: BaseGeometry) -> list[shapely.Polygon]:
    """Split a geometry into its separate polygons, e.g. a state's mainland and
    islands."""
    parts: list[BaseGeometry] = list(getattr(geom, "geoms", [geom]))
    return [p for p in parts if isinstance(p, shapely.Polygon) and not p.is_empty]


def segments(poly: shapely.Polygon) -> Segments:
    """Every straight edge of a polygon's outline (including the outlines of
    any holes), as four arrays (x0, y0, x1, y1)."""
    rings: list[FloatArray] = [np.asarray(r.coords) for r in [poly.exterior, *poly.interiors]]
    start: FloatArray = np.concatenate([c[:-1] for c in rings])
    end: FloatArray = np.concatenate([c[1:] for c in rings])
    return start[:, 0], start[:, 1], end[:, 0], end[:, 1]

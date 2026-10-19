"""Regression tests against the real Census boundaries.

These pin down the published numbers (see README.md).  They download the
boundary file on first use and take a little while, so they are marked slow:
skip them with `pytest -m "not slow"`.
"""

import pytest

from squares import aligned, data, rotated

pytestmark: pytest.MarkDecorator = pytest.mark.slow


def test_the_fifty_states_load(census_states: dict[str, data.State]) -> None:
    """Exactly the 50 states, with DC and the territories left out."""
    assert len(census_states) == 50
    assert not {"DC", "PR", "GU", "VI", "AS", "MP"} & set(census_states)
    assert len(data.polygons(census_states["AK"].geom)) == 549
    assert all(not p.interiors for s in census_states.values() for p in data.polygons(s.geom))


@pytest.mark.parametrize(("abbr", "side_mi"), [("WI", 156.299), ("CO", 275.703), ("RI", 20.504)])
def test_aligned_boxes(census_states: dict[str, data.State], abbr: str, side_mi: float) -> None:
    """Latitude/longitude-aligned boxes."""
    box: aligned.AlignedBox = aligned.largest_aligned_box(census_states[abbr].geom).box
    assert box.side_mi() == pytest.approx(side_mi, abs=0.01)


@pytest.mark.parametrize(("abbr", "side_mi"), [("WI", 157.716), ("CO", 273.894)])
def test_north_facing_squares(census_states: dict[str, data.State], abbr: str,
                              side_mi: float) -> None:
    """True squares facing north (Wisconsin's is the likely source of "158")."""
    sq: rotated.Square = rotated.largest_square(census_states[abbr].geom, rotate=False).square
    assert float(sq.sides_mi().mean()) == pytest.approx(side_mi, abs=0.01)


@pytest.mark.parametrize(("abbr", "side_mi", "rotation"),
                         [("WI", 167.468, 15.41), ("TX", 341.828, 43.63), ("RI", 20.641, 1.41)])
def test_rotated_squares(census_states: dict[str, data.State], abbr: str, side_mi: float,
                         rotation: float) -> None:
    """True squares at any rotation."""
    sq: rotated.Square = rotated.largest_square(census_states[abbr].geom).square
    assert float(sq.sides_mi().mean()) == pytest.approx(side_mi, abs=0.01)
    assert sq.rotation_deg() == pytest.approx(rotation, abs=0.05)

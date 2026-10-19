"""Shared pytest fixtures."""

from pathlib import Path

import matplotlib
import pytest

from squares import data

matplotlib.use("Agg")

ROOT: Path = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def census_states() -> dict[str, data.State]:
    """The real 50 states, keyed by postal abbreviation.  Downloads the Census
    file into data/ on first use; skips the test if that isn't possible."""
    try:
        path: Path = data.fetch(ROOT / "data")
    except OSError as exc:
        pytest.skip(f"Census boundary file unavailable: {exc}")
    return {s.abbr: s for s in data.load_states(path)}

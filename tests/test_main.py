"""Tests for the command-line program, using made-up states (no download)."""

import csv
import shutil
import sys
from pathlib import Path

import pytest

import main
from squares import data, draw, nesting, packing, rotated
from tests.helpers import SYNTHETIC_STATES


@pytest.fixture
def fake_states(monkeypatch: pytest.MonkeyPatch) -> list[data.State]:
    """Make the program use the made-up states instead of the Census file."""

    def fake_fetch(data_dir: Path) -> Path:
        """Pretend the download already happened."""
        return data_dir / "unused.zip"

    def fake_load(path: Path) -> list[data.State]:
        """Return the made-up states, sorted by name like the real loader."""
        return sorted(SYNTHETIC_STATES, key=lambda s: s.name)

    monkeypatch.setattr(data, "fetch", fake_fetch)
    monkeypatch.setattr(data, "load_states", fake_load)
    return SYNTHETIC_STATES


def run(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    """Run main.py as if from the command line."""
    monkeypatch.setattr(sys, "argv", ["main.py", *args])
    main.main()


def read_csv(path: Path) -> list[dict[str, str]]:
    """The rows of a results CSV."""
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize(("value", "expected"),
                         [(44.6481, "44.6481°N"), (-3.0, "3.0000°S"), (0.0, "0.0000°N")])
def test_fmt_lat(value: float, expected: str) -> None:
    """Latitudes get N or S, never a minus sign."""
    assert main.fmt_lat(value) == expected


@pytest.mark.parametrize(("value", "expected"),
                         [(-89.6353, "89.6353°W"), (10.0, "10.0000°E")])
def test_fmt_lon(value: float, expected: str) -> None:
    """Longitudes get E or W, never a minus sign."""
    assert main.fmt_lon(value) == expected


@pytest.mark.parametrize(("deg", "expected"), [
    (0.01, "none (sides run north–south and east–west)"),
    (15.41, "15.4° counterclockwise from north"),
    (-3.47, "3.5° clockwise from north"),
])
def test_describe_rotation(deg: float, expected: str) -> None:
    """Tilts are put into words, and tiny ones count as none."""
    assert main.describe_rotation(deg) == expected


def test_captions_are_filled_in() -> None:
    """Every command has all its titles and notes."""
    for captions in (main.ALIGNED_CAPTIONS, main.ROTATED_CAPTIONS, main.NORTH_CAPTIONS):
        assert all(vars(captions).values())


def test_aligned_command_writes_a_sorted_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             fake_states: list[data.State]) -> None:
    """One row per state, biggest first, with the box's edges."""
    run(monkeypatch, "aligned", "--no-draw", "--out", str(tmp_path))
    rows: list[dict[str, str]] = read_csv(tmp_path / "aligned_squares.csv")
    assert len(rows) == len(fake_states)
    sides: list[float] = [float(r["side_mi"]) for r in rows]
    assert sides == sorted(sides, reverse=True)
    assert {"north_edge_mi", "south_edge_mi", "west_lon", "east_lon"} <= set(rows[0])


def test_north_command_for_chosen_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         fake_states: list[data.State]) -> None:
    """Only the named states are computed, case-insensitively, with no rotation."""
    run(monkeypatch, "north", "wi", "RI", "--no-draw", "--out", str(tmp_path))
    rows: list[dict[str, str]] = read_csv(tmp_path / "north_squares.csv")
    assert sorted(r["abbr"] for r in rows) == ["RI", "WI"]
    assert all(float(r["rotation_deg"]) == 0.0 for r in rows)
    assert {f"corner{i}_lat" for i in range(1, 5)} <= set(rows[0])


def test_rotated_command_draws_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          fake_states: list[data.State]) -> None:
    """Without --no-draw, all five output files appear."""
    run(monkeypatch, "rotated", "--out", str(tmp_path))
    expected: set[str] = {"rotated_squares.csv", "rotated_squares.svg", "rotated_squares.pdf",
                          "rotated_social.png", "rotated_states.pdf"}
    assert expected <= {p.name for p in tmp_path.iterdir()}


def test_compare_command_writes_gains_and_a_gif(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               fake_states: list[data.State]) -> None:
    """The comparison lists each state's gain from rotating, biggest first, and
    draws the flip-book GIF."""
    run(monkeypatch, "compare", "--out", str(tmp_path))
    rows: list[dict[str, str]] = read_csv(tmp_path / "north_vs_rotated.csv")
    assert len(rows) == len(fake_states)
    gains: list[float] = [float(r["gain_mi"]) for r in rows]
    assert gains == sorted(gains, reverse=True)
    assert all(g >= -0.02 for g in gains)  # rotating never hurts (beyond tolerance)
    assert (tmp_path / "north_vs_rotated.gif").stat().st_size > 10_000
    # The video needs the ffmpeg program, which is optional.
    assert (tmp_path / "north_vs_rotated.mp4").exists() == (shutil.which("ffmpeg") is not None)


def test_compare_close_up_makes_one_flip_book_per_state(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_states: list[data.State]) -> None:
    """With --close-up, each named state gets its own GIF (and MP4, given
    ffmpeg), and the combined CSV and US map are left alone."""
    run(monkeypatch, "compare", "WI", "--close-up", "--out", str(tmp_path))
    names: set[str] = {p.name for p in tmp_path.iterdir()}
    want: set[str] = {"WI_north_vs_rotated.gif"}
    if shutil.which("ffmpeg") is not None:
        want.add("WI_north_vs_rotated.mp4")
    assert names == want


def test_unknown_state_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                   fake_states: list[data.State]) -> None:
    """A typo in a state abbreviation stops the program with a message."""
    with pytest.raises(SystemExit, match="unknown state abbreviation"):
        run(monkeypatch, "aligned", "ZZ", "--no-draw", "--out", str(tmp_path))


def test_pack_command_writes_results_and_merges_the_summary(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_states: list[data.State]) -> None:
    """Each state gets a CSV, PDF, PNG and checkpoint; the summary keeps rows
    from earlier runs; and the CSV holds enough to rebuild the packing."""
    quick: list[str] = ["--count", "3", "--minutes", "0.03", "--workers", "1",
                        "--out", str(tmp_path)]
    run(monkeypatch, "pack", "WI", *quick)
    run(monkeypatch, "pack", "RI", "--no-draw", *quick)
    names: set[str] = {p.name for p in tmp_path.iterdir()}
    assert {"WI_3_squares.csv", "WI_3_squares.pdf", "WI_3_squares.png",
            "WI_3_squares_checkpoint.csv", "RI_3_squares.csv", "pack_3_summary.csv"} <= names
    assert "RI_3_squares.pdf" not in names
    summary: list[dict[str, str]] = read_csv(tmp_path / "pack_3_summary.csv")
    assert [r["abbr"] for r in summary] == ["RI", "WI"]  # Rhode Island, Wisconsin
    assert all(0 < float(r["side_mi"]) < float(r["area_limit_mi"]) for r in summary)
    rows: list[dict[str, str]] = read_csv(tmp_path / "WI_3_squares.csv")
    rebuilt: list[rotated.Square] = [
        rotated.Square(float(r["center_lon"]), float(r["center_lat"]), float(r["theta_rad"]),
                       float(r["half_side_m"])) for r in rows]
    wi: data.State = next(s for s in fake_states if s.abbr == "WI")
    assert len(rebuilt) == 3 and packing.fits(wi.geom, rebuilt)


def test_nest_command_finds_the_biggest_state_that_fits(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_states: list[data.State]) -> None:
    """Hawaii, the biggest state smaller than Wisconsin, fits in it, so
    Rhode Island isn't tried; a state with nothing smaller is skipped."""
    monkeypatch.setattr(nesting, "GRID_PIXELS", 150)
    monkeypatch.setattr(nesting, "SWEEP_STEP_DEG", 3.0)
    run(monkeypatch, "nest", "WI", "RI", "--workers", "1", "--out", str(tmp_path))
    maps: set[str] = {"nest_map.svg", "nest_map.pdf", "nest_social.png"}
    assert {p.name for p in tmp_path.iterdir()} == {
        "WI_nest.csv", "WI_nest.pdf", "WI_nest.png", "nest_summary.csv", *maps}
    rows: list[dict[str, str]] = read_csv(tmp_path / "WI_nest.csv")
    assert [(r["guest"], r["fits"]) for r in rows] == [("HI", "yes")]
    assert float(rows[0]["best_scale_pct"]) > 100
    summary: list[dict[str, str]] = read_csv(tmp_path / "nest_summary.csv")
    assert [(r["abbr"], r["biggest_fit"]) for r in summary] == [("RI", ""), ("WI", "Hawaii")]


def test_nest_redraw_works_from_saved_results(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_states: list[data.State]) -> None:
    """--redraw searches nothing: with no saved results it draws nothing,
    and afterwards it rebuilds the page and the map from the CSVs, with
    Hawaii back in Wisconsin exactly where the search put it."""
    monkeypatch.setattr(nesting, "GRID_PIXELS", 150)
    monkeypatch.setattr(nesting, "SWEEP_STEP_DEG", 3.0)
    run(monkeypatch, "nest", "--redraw", "--out", str(tmp_path))
    assert not list(tmp_path.iterdir())
    run(monkeypatch, "nest", "WI", "--workers", "1", "--no-draw", "--out", str(tmp_path))
    assert {p.name for p in tmp_path.iterdir()} == {"WI_nest.csv", "nest_summary.csv"}
    run(monkeypatch, "nest", "--redraw", "--out", str(tmp_path))
    assert {"WI_nest.pdf", "WI_nest.png", "nest_map.svg", "nest_map.pdf",
            "nest_social.png"} <= {p.name for p in tmp_path.iterdir()}
    fits: list[nesting.Fit] = main.load_fits(tmp_path, fake_states[0], fake_states)
    assert [(f.guest.abbr, f.fits) for f in fits] == [("HI", True)]
    items: list[draw.Nested] = main.load_nested(tmp_path, fake_states)
    assert [(it.host.abbr, it.guest.abbr if it.guest else None) for it in items] == [("WI", "HI")]
    wi: data.State = next(s for s in fake_states if s.abbr == "WI")
    assert items[0].shape is not None and wi.geom.contains(items[0].shape)


@pytest.mark.parametrize(("deg", "expected"), [(0.01, "not turned"),
                                               (45.47, "turned 45.5° counterclockwise"),
                                               (-178.0, "turned 178.0° clockwise")])
def test_describe_turn(deg: float, expected: str) -> None:
    """A state's turn is put into words."""
    assert main.describe_turn(deg) == expected

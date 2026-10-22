"""Tests for the command-line program, using made-up states (no download)."""

import csv
import shutil
import sys
from pathlib import Path

import pytest
import shapely
from shapely.geometry.base import BaseGeometry

import main
from squares import data, draw, geodesy, nesting, packing, rotated
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
    maps: set[str] = {"nest_map.svg", "nest_map.pdf", "nest_social.png", "nest_chain_map.svg",
                      "nest_chain_map.pdf", "nest_chain_social.png"}
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
    and afterwards it rebuilds the pages and the maps from the CSVs, with
    Hawaii back in Wisconsin exactly where the search put it, and Rhode
    Island inside Hawaii inside Wisconsin on the chain map."""
    monkeypatch.setattr(nesting, "GRID_PIXELS", 150)
    monkeypatch.setattr(nesting, "SWEEP_STEP_DEG", 3.0)
    run(monkeypatch, "nest", "--redraw", "--out", str(tmp_path))
    assert not list(tmp_path.iterdir())
    run(monkeypatch, "nest", "WI", "HI", "--workers", "1", "--no-draw", "--out", str(tmp_path))
    assert {p.name for p in tmp_path.iterdir()} == {"WI_nest.csv", "HI_nest.csv",
                                                    "nest_summary.csv"}
    run(monkeypatch, "nest", "--redraw", "--out", str(tmp_path))
    assert {"WI_nest.pdf", "WI_nest.png", "HI_nest.pdf", "nest_map.svg", "nest_map.pdf",
            "nest_social.png", "nest_chain_map.svg", "nest_chain_map.pdf",
            "nest_chain_social.png"} <= {p.name for p in tmp_path.iterdir()}
    wi: data.State = next(s for s in fake_states if s.abbr == "WI")
    fits: list[nesting.Fit] = main.load_fits(tmp_path, wi, fake_states)
    assert [(f.guest.abbr, f.fits) for f in fits] == [("HI", True)]
    items: list[draw.Nested] = main.load_nested(tmp_path, fake_states)
    assert [(it.host.abbr, it.guest.abbr if it.guest else None) for it in items] == [
        ("HI", "RI"), ("WI", "HI")]
    assert items[1].shape is not None and wi.geom.contains(items[1].shape)
    chains: list[draw.Chain] = main.load_chains(tmp_path, fake_states)
    assert [(c.host.abbr, len(c.shapes)) for c in chains] == [("HI", 1), ("WI", 2)]
    hawaii, rhode_island = chains[1].shapes
    assert hawaii.equals(items[1].shape)
    assert wi.geom.contains(hawaii) and hawaii.contains(rhode_island)
    ri: data.State = next(s for s in fake_states if s.abbr == "RI")
    assert abs(geodesy.GEOD.geometry_area_perimeter(rhode_island)[0]) == pytest.approx(
        nesting.area(ri), rel=1e-3)


@pytest.mark.parametrize(("deg", "expected"), [(0.01, "not turned"),
                                               (45.47, "turned 45.5° counterclockwise"),
                                               (-178.0, "turned 178.0° clockwise")])
def test_describe_turn(deg: float, expected: str) -> None:
    """A state's turn is put into words."""
    assert main.describe_turn(deg) == expected


@pytest.mark.parametrize(("dx", "dy", "expected"), [
    (0.0, 1.0, "northern"), (1.0, 1.0, "northeastern"), (-1.0, 0.1, "western"),
    (0.1, -1.0, "southern"), (-1.0, -1.0, "southwestern"), (1.0, -0.2, "eastern")])
def test_compass(dx: float, dy: float, expected: str) -> None:
    """Directions become words for part of a state."""
    assert main.compass(dx, dy) == expected


@pytest.mark.parametrize(("area", "expected"), [
    (0.4, "under 1 sq mi"), (69.4, "69 sq mi"), (679.0, "680 sq mi"), (2737.0, "2,700 sq mi")])
def test_describe_area(area: float, expected: str) -> None:
    """Areas are given to two significant figures."""
    assert main.describe_area(area) == expected


def test_spill_captions_say_how_close() -> None:
    """A guest that fits says so; one that spills a little "almost fits",
    and its share is rounded down, never up to 100%; more is "doesn't fit"."""
    tx: data.State = data.State("TX", "Texas", shapely.Point(0, 0))
    ak: data.State = data.State("AK", "Alaska", shapely.Point(0, 0))
    title, subtitle, legend = main.spill_captions(tx, ak, True, 100.0, 0.0)
    assert title == "Texas fits inside Alaska" and len(legend) == 1
    title, subtitle, legend = main.spill_captions(tx, ak, False, 99.97, 80.0)
    assert title == "Texas almost fits inside Alaska" and "99.9% of it" in subtitle
    assert legend[1] == "Spills off Alaska: 80 sq mi (under 0.1%)"
    title, _, legend = main.spill_captions(tx, ak, False, 93.0, 77.0)
    assert title == "Texas doesn't fit inside Alaska"
    assert legend[1] == "Spills off Alaska: 77 sq mi (7.0%)"


def test_spill_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                       fake_states: list[data.State]) -> None:
    """Hawaii fits on Wisconsin, spilling nothing.  Wisconsin on Hawaii
    spills all but the part as big as Hawaii, and the spill gets a callout."""
    monkeypatch.setattr(nesting, "GRID_PIXELS", 150)
    monkeypatch.setattr(nesting, "SWEEP_STEP_DEG", 3.0)
    run(monkeypatch, "spill", "hi", "WI", "--out", str(tmp_path))
    run(monkeypatch, "spill", "WI", "HI", "--out", str(tmp_path))
    assert {p.name for p in tmp_path.iterdir()} == {
        "HI_in_WI.csv", "HI_in_WI_social.png", "WI_in_HI.csv", "WI_in_HI_social.png"}
    fits: dict[str, str] = read_csv(tmp_path / "HI_in_WI.csv")[0]
    assert (fits["fits"], float(fits["spill_sq_mi"]), fits["pct_of_guest_on_host"]) == (
        "yes", 0.0, "100.0")
    spills: dict[str, str] = read_csv(tmp_path / "WI_in_HI.csv")[0]
    by_abbr: dict[str, data.State] = {s.abbr: s for s in fake_states}
    share: float = 100 * nesting.area(by_abbr["HI"]) / nesting.area(by_abbr["WI"])
    assert spills["fits"] == "no"
    assert float(spills["pct_of_guest_on_host"]) == pytest.approx(share, abs=0.1)


def test_spill_notes_name_where_the_spill_comes_from(fake_states: list[data.State]) -> None:
    """Wisconsin slid half a degree west of its home spills a strip off its
    western edge, which the callout calls western Wisconsin."""
    by_abbr: dict[str, data.State] = {s.abbr: s for s in fake_states}
    host: nesting.Outline = nesting.Outline(by_abbr["WI"])
    place: nesting.Placement = nesting.Placement(host.lon - 0.5, host.lat, 0.0)
    off: BaseGeometry = host.moved(place).difference(by_abbr["WI"].geom)
    notes: list[tuple[float, float, str]] = main.spill_notes(host, host, place, off)
    assert len(notes) == 1
    lon, lat, text = notes[0]
    assert text.endswith("of\nwestern Wisconsin") and -92.5 < lon < -92.0


@pytest.mark.parametrize(("meters", "expected"), [
    (762.0, "760 m (2,500 ft)"), (378.0, "380 m (1,200 ft)"), (1500.0, "1.5 km (0.93 mi)"),
    (10496.0, "10 km (6.5 mi)")])
def test_describe_length(meters: float, expected: str) -> None:
    """Lengths are given to two significant figures, in both units."""
    assert main.describe_length(meters) == expected


def test_spill_closeups_only_for_spills_too_thin_to_see(fake_states: list[data.State]) -> None:
    """Wisconsin nudged a hair north spills a strip about 110 m wide along
    its northern edge, too thin to see, so it gets one close-up there; nudged
    half a degree, the spill is plain to see and gets none; staying put
    spills nothing."""
    wi: data.State = next(s for s in fake_states if s.abbr == "WI")
    host: nesting.Outline = nesting.Outline(wi)

    def closeups(north: float) -> list[draw.Closeup]:
        """The close-ups for Wisconsin moved `north` degrees."""
        place: nesting.Placement = nesting.Placement(host.lon, host.lat + north, 0.0)
        return main.spill_closeups(host, host, host.moved(place).difference(wi.geom))

    thin: list[draw.Closeup] = closeups(0.001)
    assert len(thin) == 1
    assert thin[0].overhang == pytest.approx(111, abs=5)
    assert thin[0].where.startswith("Wisconsin's north") and thin[0].lat > 45.9
    assert thin[0].how_much == "Wisconsin hangs over by up to 110 m (360 ft)"
    assert closeups(0.5) == [] and closeups(0.0) == []

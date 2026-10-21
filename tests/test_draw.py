"""Tests for the map, social-media PNG and detail-page output."""

import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import numpy.typing as npt
import pytest
import shapely
from matplotlib.axes import Axes
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.text import Text
from PIL import Image
from shapely.geometry.base import BaseGeometry

from squares import aligned, draw, rotated
from squares.data import State
from tests.helpers import SYNTHETIC_STATES, rectangle, state


def placed(st: State) -> draw.Placed:
    """Solve a made-up state with the (fast) aligned solver and package it for drawing."""
    result: aligned.Result = aligned.largest_aligned_box(st.geom)
    box: aligned.AlignedBox = result.box
    return draw.Placed(st, box.ring(), (box.lon, box.lat), box.side_mi(), result.contacts,
                       ["line one", "line two"], box.guides())


@pytest.fixture(scope="module")
def items() -> list[draw.Placed]:
    """Drawable results for the made-up states (lower 48, Alaska and Hawaii)."""
    return [placed(st) for st in SYNTHETIC_STATES]


def all_text(fig: Figure) -> list[str]:
    """Every piece of text drawn anywhere on a figure."""
    return [t.get_text() for t in fig.findobj(Text)]


def test_draw_map_writes_svg_and_pdf(tmp_path: Path, items: list[draw.Placed]) -> None:
    """Both vector files are written, and the SVG keeps its labels as real text."""
    paths: list[Path] = draw.draw_map(items, SYNTHETIC_STATES, tmp_path / "map", "Title", "Note")
    assert [p.name for p in paths] == ["map.svg", "map.pdf"]
    assert all(p.stat().st_size > 1000 for p in paths)
    svg: str = paths[0].read_text(encoding="utf-8")
    assert "Title" in svg and "WI" in svg


def test_draw_social_is_a_16_by_9_png(tmp_path: Path, items: list[draw.Placed]) -> None:
    """The social image is 2400 x 1350 pixels."""
    path: Path = draw.draw_social(items, SYNTHETIC_STATES, tmp_path / "social.png", "Title",
                                  "Subtitle", "Credit")
    with Image.open(path) as im:
        assert im.size == (2400, 1350)


def test_draw_details_has_a_page_per_state_plus_a_table(tmp_path: Path,
                                                        items: list[draw.Placed]) -> None:
    """One ranked-table page, then one page per state."""
    path: Path = draw.draw_details(items, SYNTHETIC_STATES, tmp_path / "details.pdf", "Title",
                                   "Note", "Legend")
    pages: int = len(re.findall(rb"/Type\s*/Page\b", path.read_bytes()))
    assert pages == 1 + len(items)


def grown(items: list[draw.Placed], abbr: str, miles: float) -> list[draw.Placed]:
    """A copy of the results with one state's square `miles` bigger."""
    return [replace(it, side_mi=it.side_mi + miles) if it.state.abbr == abbr else it
            for it in items]


FLIP_TEXT: draw.FlipText = draw.FlipText("Title", "Before", "After", "Legend")


def test_flip_gif_has_two_one_second_frames(tmp_path: Path, items: list[draw.Placed]) -> None:
    """The comparison GIF loops forever over two 1600x900 frames, one second each,
    on a pure white background."""
    path: Path = draw.draw_flip_gif(items, grown(items, "WI", 9.0), SYNTHETIC_STATES,
                                    tmp_path / "flip.gif", FLIP_TEXT)
    with Image.open(path) as im:
        assert im.size == (1600, 900)
        assert getattr(im, "n_frames", 1) == 2  # only animated formats have n_frames
        assert im.info["duration"] == 1000
        assert im.info["loop"] == 0
        for frame in range(2):
            im.seek(frame)
            assert im.convert("RGB").getpixel((3, 3)) == (255, 255, 255)


FFMPEG: str | None = shutil.which("ffmpeg")
FFPROBE: str | None = shutil.which("ffprobe")


@pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="needs ffmpeg and ffprobe")
def test_flip_video_meets_the_threads_requirements(tmp_path: Path,
                                                   items: list[draw.Placed]) -> None:
    """The MP4 is H.264 in 4:2:0 color at 1920x1080 and 30 fps, lasts one second
    per frame per cycle, has an AAC track, and has its index at the front.

    (The duration is checked loosely: the AAC encoder's 21 ms lead-in makes the
    first frame show slightly longer; see _save_video.)"""
    assert FFMPEG is not None and FFPROBE is not None
    path: Path = draw.draw_flip_video(items, grown(items, "WI", 9.0), SYNTHETIC_STATES,
                                      tmp_path / "flip.mp4", FLIP_TEXT, cycles=2, ffmpeg=FFMPEG)
    probe: subprocess.CompletedProcess[str] = subprocess.run(
        [FFPROBE, "-v", "error", "-count_frames", "-show_streams", "-show_format",
         "-of", "json", str(path)], capture_output=True, text=True, check=True)
    info: dict[str, Any] = json.loads(probe.stdout)
    video: dict[str, Any] = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio: dict[str, Any] = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert (video["width"], video["height"]) == (1920, 1080)
    assert video["r_frame_rate"] == "30/1"
    assert int(video["nb_read_frames"]) == 2 * 2 * 30  # 2 cycles x 2 frames x 1 s x 30 fps
    assert audio["codec_name"] == "aac"
    assert float(info["format"]["duration"]) == pytest.approx(4.0, abs=0.05)
    data: bytes = path.read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")  # "fast start"
    assert b"elst" not in data  # no edit lists


def test_flip_frames_note_the_gains(items: list[draw.Placed]) -> None:
    """Only changed squares get a "+N" note, and the other version is drawn as
    a dashed outline."""
    after: list[draw.Placed] = grown(items, "WI", 9.0)
    overlay: draw._Overlay = draw._Overlay(
        ghosts={it.state.abbr: it for it in items}, notes={"WI": "+9"},
        faces={"WI": draw._gain_color(9)}, anchors={it.state.abbr: it.center for it in items})
    fig: Figure = draw._map_figure(after, SYNTHETIC_STATES, draw.SOCIAL_STYLE, overlay)
    texts: list[str] = all_text(fig)
    assert "+9" in texts
    assert sum(t.startswith("+") for t in texts) == 1
    dashed: int = sum(1 for line in fig.findobj(Line2D) if line.get_linestyle() == "--")
    assert dashed == len(items)


def square_placed(st: State, theta: float, half_side: float) -> draw.Placed:
    """A true square in the middle of a made-up state, packaged for drawing,
    with one made-up border contact."""
    sq: rotated.Square = rotated.Square(-90.0, 44.5, theta, half_side)
    return draw.Placed(st, sq.ring(), (sq.lon, sq.lat), float(sq.sides_mi().mean()),
                       np.array([[-90.0, 46.0]]), [], [])


@pytest.fixture(scope="module")
def versions() -> tuple[draw.Placed, draw.Placed]:
    """A north-facing square in Wisconsin and a slightly bigger, turned one."""
    wi: State = SYNTHETIC_STATES[0]
    return square_placed(wi, 0.0, 150_000.0), square_placed(wi, 0.05, 152_000.0)


CLOSEUP_TEXT: draw.FlipText = draw.FlipText("Title", "Before", "After", "Legend",
                                            "First note", "Second note")


def test_closeup_frames_highlight_the_version_shown(
        versions: tuple[draw.Placed, draw.Placed]) -> None:
    """Each frame has both versions' numbers, the shown one in full color and
    the other faded, plus the other square as a dashed outline; the gain
    appears in both frames (alongside the numbers), and in the square only
    on the second."""
    first, second = versions
    gain: str = f"{round(second.side_mi, 1) - round(first.side_mi, 1):+.1f}"
    for shown in (0, 1):
        fig: Figure = draw._closeup_figure(first, second, shown, SYNTHETIC_STATES, CLOSEUP_TEXT)
        panel: dict[str, Text] = {t.get_text(): t for t in fig.texts}  # not the map's
        for i, it in enumerate(versions):
            color: str = draw.SQUARE_EDGE if i == shown else draw.FADED
            assert to_rgb(panel[f"{it.side_mi:.1f} mi"].get_color()) == to_rgb(color)
        assert {"First note", "Second note", "Legend", "Title"} <= set(panel)
        texts: list[str] = [t.strip() for t in all_text(fig)]
        assert f"{gain} mi" in texts
        assert (gain in texts) == (shown == 1)
        dashed: int = sum(1 for line in fig.findobj(Line2D) if line.get_linestyle() == "--")
        assert dashed == 1
        plt.close(fig)


def test_closeup_gif_has_two_frames(tmp_path: Path,
                                    versions: tuple[draw.Placed, draw.Placed]) -> None:
    """The close-up GIF is 1600x900 and flips between two different frames."""
    path: Path = draw.draw_closeup_gif(*versions, SYNTHETIC_STATES, tmp_path / "closeup.gif",
                                       CLOSEUP_TEXT)
    with Image.open(path) as im:
        assert im.size == (1600, 900)
        assert getattr(im, "n_frames", 1) == 2
        frames: list[npt.NDArray[np.uint8]] = []
        for frame in range(2):
            im.seek(frame)
            frames.append(np.asarray(im.convert("RGB")))
    assert not np.array_equal(frames[0], frames[1])


def test_gain_colors_deepen_with_the_gain() -> None:
    """No change is pale; bigger gains are darker, up to a cap."""
    assert draw._gain_color(0) == draw.SQUARE_PALE

    def lightness(gain: int) -> float:
        """Average of the color's red, green and blue."""
        return sum(to_rgb(draw._gain_color(gain))) / 3

    shades: list[float] = [lightness(g) for g in (0, 1, 3, 6, 12)]
    assert shades == sorted(shades, reverse=True)
    assert len(set(shades)) == len(shades)
    assert lightness(30) == pytest.approx(lightness(12))


def test_small_squares_get_callouts_on_the_social_map(items: list[draw.Placed]) -> None:
    """Rhode Island's square is too small for an inline label, so it gets a
    callout; Wisconsin's number goes inside its square without "mi"."""
    fig: Figure = draw._map_figure(items, SYNTHETIC_STATES, draw.SOCIAL_STYLE)
    texts: list[str] = all_text(fig)
    ri: draw.Placed = next(it for it in items if it.state.abbr == "RI")
    wi: draw.Placed = next(it for it in items if it.state.abbr == "WI")
    assert f"RI  {ri.side_mi:.0f}" in texts
    assert f"{wi.side_mi:.0f}" in texts
    assert not any(t.endswith(" mi") for t in texts)


def test_print_map_labels_include_units(items: list[draw.Placed]) -> None:
    """The detailed map writes "WI" and "### mi" inside big squares."""
    fig: Figure = draw._map_figure(items, SYNTHETIC_STATES, draw.PRINT_STYLE)
    wi: draw.Placed = next(it for it in items if it.state.abbr == "WI")
    assert f"WI\n{wi.side_mi:.0f} mi" in all_text(fig)


def test_map_with_only_some_states_still_draws(tmp_path: Path) -> None:
    """Leaving out Alaska and Hawaii (empty insets) is fine."""
    wi: State = SYNTHETIC_STATES[0]
    paths: list[Path] = draw.draw_map([placed(wi)], [wi], tmp_path / "wi", "Title", "Note")
    assert all(p.exists() for p in paths)


def test_hawaii_is_trimmed_to_the_main_islands() -> None:
    """The far-off Northwestern Hawaiian Islands are left off the drawing."""
    far_island: shapely.Polygon = rectangle(-170.0, 25.0, -169.9, 25.1)
    big_island: shapely.Polygon = rectangle(-156.0, 19.0, -155.0, 20.0)
    hawaii: State = state("HI", shapely.MultiPolygon([far_island, big_island]))
    shown: BaseGeometry = draw._display_geom(hawaii)
    assert shown.bounds == pytest.approx(big_island.bounds)
    other: State = state("WI", far_island)
    assert draw._display_geom(other) is other.geom


@pytest.mark.parametrize(("target", "expected"),
                         [(0.3, 1), (3.0, 5), (20.0, 20), (21.0, 25), (180.0, 200), (900.0, 1000)])
def test_scale_bar_lengths_are_round_numbers(target: float, expected: int) -> None:
    """The scale bar uses the first round length at least as long as asked."""
    assert draw._nice_length(target) == expected


def test_add_draws_collections_as_polygons() -> None:
    """A geometry collection's polygons are drawn, its other parts ignored."""
    fig: Figure = Figure()
    ax: Axes = fig.add_subplot()
    mixed: shapely.GeometryCollection = shapely.GeometryCollection(
        [shapely.box(0, 0, 1, 1), shapely.LineString([(0, 0), (2, 2)])])
    draw._add(ax, mixed, facecolor="red")
    draw._add(ax, shapely.Polygon(), facecolor="red")
    assert len(ax.patches) == 1


def test_draw_packing_writes_a_pdf_and_a_png(tmp_path: Path) -> None:
    """The packing page is saved both ways; the PNG is 2200 x 1700."""
    wi: State = SYNTHETIC_STATES[0]
    rings: list[npt.NDArray[np.float64]] = [
        rotated.Square(lon, 44.5, 0.1, 20_000.0).ring() for lon in (-91.0, -89.0)]
    paths: list[Path] = draw.draw_packing(
        wi, rings, np.array([[-90.0, 46.0]]), [shapely.box(-90.0, 44.0, -89.99, 44.01)],
        SYNTHETIC_STATES, tmp_path / "pack", "Header", "Headline", ["one", "two"], "Legend")
    assert [p.name for p in paths] == ["pack.pdf", "pack.png"]
    with Image.open(paths[1]) as im:
        assert im.size == (2200, 1700)


def test_draw_nesting_writes_a_pdf_and_a_png(tmp_path: Path) -> None:
    """The nesting page is saved both ways, with the guest labeled, and the
    dashed bigger copy drawn only when there is one."""
    wi: State = SYNTHETIC_STATES[0]
    guest: shapely.Polygon = rectangle(-91.0, 44.0, -89.0, 45.0)
    for bigger, dashed in [(rectangle(-91.2, 43.9, -88.8, 45.1), 1), (None, 0)]:
        stem: Path = tmp_path / f"nest{dashed}"
        paths: list[Path] = draw.draw_nesting(wi, guest, bigger, "Guest", SYNTHETIC_STATES, stem,
                                              "Header", "Headline", ["one"], "Legend")
        assert [p.name for p in paths] == [f"{stem.name}.pdf", f"{stem.name}.png"]
        with Image.open(paths[1]) as im:
            assert im.size == (2200, 1700)


def test_state_map_centers_alaska_across_180_degrees() -> None:
    """A state whose islands reach past 180 degrees (as Alaska's Aleutians
    do) gets a map centered on it, not on the Greenwich meridian."""
    west: shapely.Polygon = rectangle(-179.5, 51.0, -178.5, 52.0)
    east: shapely.Polygon = rectangle(178.5, 51.0, 179.5, 52.0)
    proj: draw._Proj = draw._state_proj(state("AK", shapely.MultiPolygon([west, east])))
    x0, _, x1, _ = proj.geom(shapely.MultiPolygon([west, east])).bounds
    assert -150_000 < x0 < x1 < 150_000


def test_nest_maps_label_big_guests_and_call_out_small_ones(tmp_path: Path) -> None:
    """A guest with room gets its abbreviation inside it; a tiny guest in a
    roomy host gets it on a white badge; a tiny guest in a tiny host gets a
    callout like "DE in RI"; a state with nothing inside says "none"."""
    wi, ri, ak, hi = SYNTHETIC_STATES
    delaware: State = state("DE", rectangle(-71.66, 41.54, -71.64, 41.56))
    items: list[draw.Nested] = [
        draw.Nested(wi, hi, rectangle(-91.0, 44.0, -89.0, 45.0)),
        draw.Nested(hi, ri, rectangle(-155.6, 19.4, -155.55, 19.45)),
        draw.Nested(ri, delaware, delaware.geom),
        draw.Nested(ak, None, None),
    ]
    fig: Figure = draw._nest_figure(items, SYNTHETIC_STATES, draw.SOCIAL_STYLE)
    texts: dict[str, Text] = {t.get_text(): t for t in fig.findobj(Text)}
    assert {"HI", "RI", "DE in RI", "AK  none"} <= set(texts)
    assert texts["HI"].get_bbox_patch() is None
    assert texts["RI"].get_bbox_patch() is not None
    plt.close(fig)
    paths: list[Path] = draw.draw_nest_map(items, SYNTHETIC_STATES, tmp_path / "map", "T", "N")
    assert [p.name for p in paths] == ["map.svg", "map.pdf"]
    social: Path = draw.draw_nest_social(items, SYNTHETIC_STATES, tmp_path / "s.png", "T", "S",
                                         "C")
    with Image.open(social) as im:
        assert im.size == (2400, 1350)


def test_deepest_point_of_a_rectangle() -> None:
    """The deepest point of a rectangle is on its midline, half its width
    from the long sides."""
    spot, room = draw._deepest(shapely.MultiPolygon([shapely.box(0, 0, 10, 4),
                                                     shapely.box(20, 0, 21, 1)]))
    assert spot.y == pytest.approx(2.0, abs=0.05) and 2 <= spot.x <= 8
    assert room == pytest.approx(2.0, abs=0.05)

"""SVG/PDF output: a US map of the squares and per-state detail pages."""

from __future__ import annotations

import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import numpy.typing as npt  # noqa: E402
import shapely  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.colors import to_hex, to_rgb  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.text import Text  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely.geometry.base import BaseGeometry  # noqa: E402
from shapely.plotting import patch_from_polygon  # noqa: E402

from .arrays import FloatArray  # noqa: E402
from .data import State, polygons  # noqa: E402

# Keep text as text in SVG and embed TrueType fonts in PDF, so both stay editable.
plt.rcParams.update({"svg.fonttype": "none", "pdf.fonttype": 42})

GEO: str = "EPSG:4269"  # NAD83 lon/lat, as in the Census files
CONUS: str = "EPSG:5070"  # Albers equal-area conic for the lower 48
ALASKA: str = "EPSG:3338"  # Alaska Albers
HAWAII: str = "+proj=aea +lat_0=13 +lon_0=-157 +lat_1=8 +lat_2=18 +datum=NAD83 +units=m +no_defs"
# Leaves out the Northwestern Hawaiian Islands, which would dwarf the rest.
HAWAII_MAIN: shapely.Polygon = shapely.box(-161, 18, -154, 23)

LAND: str = "#ebe8e1"
LAND_EDGE: str = "#8a8a8a"
NEIGHBOR: str = "#f7f6f3"
NEIGHBOR_EDGE: str = "#c4c4c4"
SQUARE: str = "#e8743b"
SQUARE_EDGE: str = "#9c3d12"
CONTACT: str = "#1d4f91"
INK: str = "#222222"
MUTED: str = "#666666"
SQUARE_PALE: str = "#f3cdb2"  # comparison GIF: squares that don't change...
SQUARE_DEEP: str = "#d9480f"  # ...through those that gain the most
GAIN: str = "#1b7a3a"  # comparison GIF: the "+9" notes...
BADGE: dict[str, Any] = {"boxstyle": "round,pad=0.12", "fc": "white", "ec": "none"}  # ...behind
METERS_PER_MILE: float = 1609.344
# The comparison GIF is 1600x900: GIFs are bulkier than PNGs, and 1600 px wide
# is still sharp when expanded on a phone or in a timeline.
GIF_DPI: float = 1600 / 12
# The comparison video is 1920x1080, the widest Threads accepts.
VIDEO_DPI: float = 1920 / 12


@dataclass(frozen=True)
class Placed:
    """Everything needed to draw one state's square."""

    state: State
    outline: FloatArray  # (n, 2) closed lon/lat ring
    center: tuple[float, float]  # lon, lat
    side_mi: float
    contacts: FloatArray  # (k, 2) lon/lat boundary points touching the square
    details: list[str]  # text lines for the detail page
    guides: list[FloatArray]  # lon/lat polylines drawn dashed on the detail page


class _Proj:
    """Converts lon/lat points and shapes into one map projection's x/y meters."""

    _tr: Transformer

    def __init__(self, crs: str) -> None:
        """Set up the conversion into `crs` (an EPSG code or PROJ string)."""
        self._tr = Transformer.from_crs(GEO, crs, always_xy=True)

    def xy(self, lonlat: npt.ArrayLike) -> FloatArray:
        """Project an (n, 2) array of lon/lat points; returns (n, 2) of x/y."""
        pts: FloatArray = np.asarray(lonlat, dtype=float).reshape(-1, 2)
        return np.column_stack(self._tr.transform(pts[:, 0], pts[:, 1]))

    def geom(self, g: BaseGeometry) -> BaseGeometry:
        """Project a whole shapely geometry."""
        return shapely.transform(g, self.xy)


def _display_geom(st: State) -> BaseGeometry:
    """The state outline to draw.  Same as the real one except for Hawaii,
    which is trimmed to the main islands."""
    if st.abbr == "HI":
        return shapely.MultiPolygon(polygons(st.geom.intersection(HAWAII_MAIN)))
    return st.geom


def _add(ax: Axes, geom: BaseGeometry, **style: Any) -> None:
    """Draw a geometry's polygons, holes and all, on the axes."""
    area: shapely.Polygon | shapely.MultiPolygon = (
        geom if isinstance(geom, (shapely.Polygon, shapely.MultiPolygon))
        else shapely.MultiPolygon(polygons(geom)))  # e.g. a GeometryCollection
    if not area.is_empty:
        ax.add_patch(patch_from_polygon(area, **style))


def _pts_per_meter(ax: Axes) -> float:
    """How many typographic points one map meter takes up on the page."""
    ax.apply_aspect()
    x0, x1 = ax.get_xlim()
    return float(ax.get_window_extent().width * 72 / ax.figure.dpi / (x1 - x0))


# Axes position as (left, bottom, width, height), in fractions of the figure.
Rect: TypeAlias = tuple[float, float, float, float]


@dataclass(frozen=True)
class _MapStyle:
    """Sizes and layout for one kind of US map image."""

    figsize: tuple[float, float]  # inches
    dpi: int
    conus: Rect  # where the lower-48, Alaska and Hawaii panels go
    alaska: Rect
    hawaii: Rect
    border_width: float  # line widths, in points
    square_width: float
    label_fit: float  # label font size as a fraction of the square's width
    label_min: float  # font sizes in points; a square whose label would be
    label_max: float  # smaller than label_min gets a callout instead
    callout_size: float
    compact: bool  # bold number plus small abbreviation, instead of "WI / 158 mi"
    callout_room: float  # space right of each panel for callouts, as a fraction of its width


# The detailed vector map (SVG/PDF), meant to be zoomed into.
PRINT_STYLE: _MapStyle = _MapStyle(
    figsize=(17, 10), dpi=150, conus=(0.01, 0.02, 0.98, 0.86), alaska=(0.0, 0.01, 0.30, 0.31),
    hawaii=(0.25, 0.02, 0.15, 0.14), border_width=0.4, square_width=0.5, label_fit=0.2,
    label_min=5, label_max=12, callout_size=7, compact=False, callout_room=0.2)

# A 2400x1350 PNG for social media: 16:9 shows uncropped in feeds, and text is
# sized to stay legible when the image is shrunk to a phone-width timeline.
SOCIAL_STYLE: _MapStyle = _MapStyle(
    figsize=(12, 6.75), dpi=200, conus=(0.0, 0.03, 1.0, 0.81), alaska=(0.0, 0.02, 0.27, 0.30),
    hawaii=(0.215, 0.03, 0.12, 0.12), border_width=0.6, square_width=0.9, label_fit=0.42,
    label_min=7, label_max=34, callout_size=12, compact=True, callout_room=0.15)


def _label_squares(ax: Axes, proj: _Proj, items: list[Placed], callout_x: float,
                   style: _MapStyle, overlay: _Overlay | None = None) -> None:
    """Write each square's side length (and state) inside it, or, when the
    square is too small for readable text, in a column at callout_x with a
    leader line.  An overlay can add a note under each number and pin where
    the callouts go."""
    notes: dict[str, str] = overlay.notes if overlay else {}
    scale: float = _pts_per_meter(ax)
    small: list[tuple[float, float, float, Placed]] = []  # (label y, x, y, item)
    for it in items:
        x, y = proj.xy(it.center)[0]
        size: float = min(style.label_fit * it.side_mi * METERS_PER_MILE * scale, style.label_max)
        note: str = notes.get(it.state.abbr, "")
        if size < style.label_min:
            anchor: tuple[float, float] = overlay.anchors[it.state.abbr] if overlay else it.center
            small.append((float(proj.xy(anchor)[0][1]), x, y, it))
        elif not style.compact:
            ax.text(x, y, f"{it.state.abbr}\n{it.side_mi:.0f} mi", ha="center", va="center",
                    fontsize=size, color=INK, linespacing=1.1)
        elif size < 15:  # too small for the abbreviation: just the number (and note)
            ax.annotate(f"{it.side_mi:.0f}", (x, y), xytext=(0, 0.35 * size if note else 0),
                        textcoords="offset points", ha="center", va="center", fontsize=size,
                        weight="bold", color=INK)
            if note:
                ax.annotate(note, (x, y), xytext=(0, -0.65 * size), textcoords="offset points",
                            ha="center", va="center", fontsize=max(0.6 * size, 6.5),
                            weight="bold", color=GAIN, bbox=BADGE)
        else:
            shift: float = 0.3 * size if note else 0.0  # make room for the note below
            ax.annotate(f"{it.side_mi:.0f}", (x, y), xytext=(0, -0.22 * size + shift),
                        textcoords="offset points", ha="center", va="center", fontsize=size,
                        weight="bold", color=INK)
            ax.annotate(it.state.abbr, (x, y), xytext=(0, 0.62 * size + shift),
                        textcoords="offset points", ha="center", va="center",
                        fontsize=0.45 * size, color=INK)
            if note:
                ax.annotate(note, (x, y), xytext=(0, -0.95 * size + shift),
                            textcoords="offset points", ha="center", va="center",
                            fontsize=0.45 * size, weight="bold", color=GAIN, bbox=BADGE)

    # Stack the callouts top to bottom without overlapping.
    spacing: float = 1.6 * style.callout_size / scale
    prev: float = np.inf
    for anchor_y, x, y, it in sorted(small, key=lambda s: -s[0]):
        label_y: float = min(anchor_y, prev - spacing)
        prev = label_y
        ax.plot([x, callout_x - 0.3 * spacing], [y, label_y], color=MUTED,
                lw=style.border_width)
        label: str = (f"{it.state.abbr}  {it.side_mi:.0f}" if style.compact
                      else f"{it.state.abbr}  {it.side_mi:.0f} mi")
        text: Text = ax.text(callout_x, label_y, label, ha="left", va="center",
                             fontsize=style.callout_size, color=INK)
        note = notes.get(it.state.abbr, "")
        if note:
            ax.annotate(f"  {note}", (1, 0.5), xycoords=text, ha="left", va="center",
                        fontsize=style.callout_size, weight="bold", color=GAIN)


@dataclass(frozen=True)
class _Overlay:
    """Extras for comparing two versions of the squares on one map."""

    ghosts: dict[str, Placed]  # the other version's square, drawn as a dashed outline
    notes: dict[str, str]  # small text under each state's number, e.g. "+9"
    faces: dict[str, str]  # fill color for each state's square
    anchors: dict[str, tuple[float, float]]  # lon/lat that fixes each callout's height


def _map_figure(items: list[Placed], states: list[State], style: _MapStyle,
                overlay: _Overlay | None = None) -> Figure:
    """A figure with the lower 48 and the Alaska and Hawaii insets, each state's
    square drawn and labeled.  Titles are left to the caller."""
    by_abbr: dict[str, Placed] = {it.state.abbr: it for it in items}
    # (projection, where its axes go, states drawn)
    panels: list[tuple[str, Rect, list[State]]] = [
        (CONUS, style.conus, [s for s in states if s.abbr not in ("AK", "HI")]),
        (ALASKA, style.alaska, [s for s in states if s.abbr == "AK"]),
        (HAWAII, style.hawaii, [s for s in states if s.abbr == "HI"]),
    ]
    fig: Figure = plt.figure(figsize=style.figsize, dpi=style.dpi)
    for crs, rect, panel_states in panels:
        if not panel_states:  # e.g. drawing only a few states
            continue
        proj: _Proj = _Proj(crs)
        ax: Axes = fig.add_axes(rect)
        ax.set_axis_off()
        geoms: list[BaseGeometry] = [proj.geom(_display_geom(s)) for s in panel_states]
        for g in geoms:
            _add(ax, g, facecolor=LAND, edgecolor=LAND_EDGE, linewidth=style.border_width)
        panel_items: list[Placed] = [by_abbr[s.abbr] for s in panel_states if s.abbr in by_abbr]
        for it in panel_items:
            face: str = overlay.faces.get(it.state.abbr, SQUARE) if overlay else SQUARE
            _add(ax, shapely.Polygon(proj.xy(it.outline)), facecolor=face,
                 edgecolor=SQUARE_EDGE, linewidth=style.square_width, alpha=0.85)
            ghost: Placed | None = overlay.ghosts.get(it.state.abbr) if overlay else None
            if ghost is not None:
                line: FloatArray = proj.xy(ghost.outline)
                ax.plot(line[:, 0], line[:, 1], color=INK, lw=style.square_width,
                        ls=(0, (3, 2)), alpha=0.8)
        minx, miny, maxx, maxy = shapely.union_all(geoms).bounds
        pad: float = 0.02 * (maxx - minx)
        ax.set_xlim(minx - pad, maxx + style.callout_room * (maxx - minx))
        ax.set_ylim(miny - pad, maxy + pad)
        ax.set_aspect("equal")
        _label_squares(ax, proj, panel_items, maxx + 2 * pad, style, overlay)
    return fig


def draw_map(items: list[Placed], states: list[State], stem: Path, title: str, note: str,
             formats: tuple[str, ...] = ("svg", "pdf")) -> list[Path]:
    """Draw the detailed US map with every state's square and save it as
    stem.svg, stem.pdf, etc.  Returns the paths written."""
    fig: Figure = _map_figure(items, states, PRINT_STYLE)
    fig.text(0.5, 0.955, title, ha="center", fontsize=22, weight="bold", color=INK)
    fig.text(0.5, 0.935, note, ha="center", va="top", fontsize=10, color=MUTED, wrap=True)
    paths: list[Path] = []
    for fmt in formats:
        path: Path = stem.with_suffix(f".{fmt}")
        fig.savefig(path, dpi=PRINT_STYLE.dpi)
        paths.append(path)
    plt.close(fig)
    return paths


def draw_social(items: list[Placed], states: list[State], path: Path, title: str,
                subtitle: str, credit: str) -> Path:
    """Draw a simplified 16:9 PNG of the map for posting on social media: big
    title, one bold number per square, callouts for the small states."""
    fig: Figure = _map_figure(items, states, SOCIAL_STYLE)
    fig.text(0.025, 0.955, title, ha="left", va="top", fontsize=27, weight="bold", color=INK)
    fig.text(0.025, 0.875, subtitle, ha="left", va="top", fontsize=14, color=MUTED)
    fig.text(0.985, 0.015, credit, ha="right", va="bottom", fontsize=8, color=MUTED)
    fig.savefig(path, dpi=SOCIAL_STYLE.dpi, facecolor="white")
    plt.close(fig)
    return path


def _gain_color(gain: int) -> str:
    """Fill color for a square that grows by `gain` (whole) miles: pale if it
    doesn't change, then deeper orange the more it grows (full depth at 12+)."""
    if gain <= 0:
        return SQUARE_PALE
    pale: FloatArray = np.array(to_rgb(SQUARE_PALE))
    deep: FloatArray = np.array(to_rgb(SQUARE_DEEP))
    # Start partway along, so even +1 stands apart from "no change".
    mix: float = 0.35 + 0.65 * min(gain / 12, 1.0) ** 0.6
    r, g, b = (float(c) for c in pale + (deep - pale) * mix)
    return str(to_hex((r, g, b)))


@dataclass(frozen=True)
class FlipText:
    """The words on a flip-book comparison (GIF or video)."""

    title: str  # headline, the same on both frames
    first_label: str  # banner naming each frame, e.g. "Facing north"
    second_label: str
    legend: str  # small print explaining the shading and outlines


def _flip_frames(first: list[Placed], second: list[Placed], states: list[State],
                 text: FlipText, dpi: float) -> list[Image.Image]:
    """Render the two frames of a flip-book comparison between two versions of
    the map (e.g. north-facing and rotated squares), built so the changes jump
    out.

    Both frames show the other version's square as a dashed outline.  Each
    square is shaded by how much it changes (pale for no change after
    rounding, deeper for bigger gains) in both frames, and the second frame
    notes each change ("+9") under the number.  The callouts are pinned to the
    first version's positions so they don't jitter.
    """
    by_first: dict[str, Placed] = {it.state.abbr: it for it in first}
    by_second: dict[str, Placed] = {it.state.abbr: it for it in second}
    change: dict[str, int] = {abbr: round(by_second[abbr].side_mi) - round(it.side_mi)
                              for abbr, it in by_first.items() if abbr in by_second}
    faces: dict[str, str] = {abbr: _gain_color(d) for abbr, d in change.items()}
    anchors: dict[str, tuple[float, float]] = {abbr: it.center for abbr, it in by_first.items()}
    notes: dict[str, str] = {abbr: f"{d:+d}" for abbr, d in change.items() if d}

    frames: list[Image.Image] = []
    for items, ghosts, label, pill, frame_notes in [
        (first, by_second, text.first_label, INK, {}),
        (second, by_first, text.second_label, GAIN, notes),
    ]:
        overlay: _Overlay = _Overlay(ghosts, frame_notes, faces, anchors)
        fig: Figure = _map_figure(items, states, SOCIAL_STYLE, overlay)
        fig.text(0.025, 0.955, text.title, ha="left", va="top", fontsize=27, weight="bold",
                 color=INK)
        fig.text(0.03, 0.865, label, ha="left", va="top", fontsize=19, weight="bold",
                 color="white", bbox={"boxstyle": "round,pad=0.35", "fc": pill, "ec": "none"})
        fig.text(0.985, 0.015, text.legend, ha="right", va="bottom", fontsize=9.5, color=MUTED)
        buffer: io.BytesIO = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=dpi, facecolor="white")
        plt.close(fig)
        buffer.seek(0)
        frames.append(Image.open(buffer).convert("RGB"))
    return frames


def draw_flip_gif(first: list[Placed], second: list[Placed], states: list[State], path: Path,
                  text: FlipText, frame_seconds: float = 1.0) -> Path:
    """Write an animated GIF (1600x900, looping forever) that flips between two
    versions of the map; see _flip_frames for what each frame shows."""
    frames: list[Image.Image] = _flip_frames(first, second, states, text, GIF_DPI)

    # One shared palette (GIFs allow 256 colors) so colors don't shimmer between frames.
    width, height = frames[0].size
    both: Image.Image = Image.new("RGB", (width, height * len(frames)))
    for i, frame in enumerate(frames):
        both.paste(frame, (0, i * height))
    palette: Image.Image = both.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                                         dither=Image.Dither.NONE)
    entries: list[int] = palette.getpalette() or []
    # Make sure pure white is in the palette, and map the white background to
    # it exactly: Pillow's color matching is approximate and turns it light gray.
    white: int = max(range(len(entries) // 3), key=lambda i: sum(entries[3 * i:3 * i + 3]))
    entries[3 * white:3 * white + 3] = [255, 255, 255]
    images: list[Image.Image] = []
    for frame in frames:
        indices: npt.NDArray[np.uint8] = np.array(
            frame.quantize(palette=palette, dither=Image.Dither.NONE))
        indices[(np.asarray(frame) == 255).all(axis=2)] = white
        image: Image.Image = Image.fromarray(indices)
        image.putpalette(entries)
        images.append(image)
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=int(frame_seconds * 1000), loop=0)
    return path


def draw_flip_video(first: list[Placed], second: list[Placed], states: list[State], path: Path,
                    text: FlipText, cycles: int = 8, ffmpeg: str = "ffmpeg") -> Path:
    """Write the flip-book comparison as an MP4 video, for sites that take video
    rather than GIFs (such as Threads), using the ffmpeg program.

    Each frame shows for one second, and the pair repeats `cycles` times,
    since videos don't loop on their own the way GIFs do.  The encoding follows
    Meta's published requirements for Threads: H.264 video, 4:2:0 color, at
    most 1920 pixels wide, 23-60 frames per second, the index ("moov atom") at
    the front of the file and no edit lists.  It also carries a silent AAC
    audio track, because some upload pipelines reject videos with none.
    """
    frames: list[Image.Image] = _flip_frames(first, second, states, text, VIDEO_DPI)
    seconds: int = cycles * len(frames)
    with tempfile.TemporaryDirectory() as tmp:
        for i, frame in enumerate(frames):
            frame.save(Path(tmp) / f"frame_{i}.png")
        command: list[str] = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            # The frames, one per second, looping...
            "-framerate", "1", "-loop", "1", "-i", str(Path(tmp) / "frame_%d.png"),
            # ...and silence for the soundtrack.  The AAC encoder's ~21 ms
            # lead-in starts before time zero, so everything is shifted by that
            # much; with no edit list to record the shift, the first frame
            # simply shows 21 ms longer (all 30 fps, but the *average* rate in
            # the metadata reads 29.8).
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v", "-map", "1:a", "-t", str(seconds),
            # 30 fps, converted to video color the way HD players expect (BT.709).
            "-vf", "fps=30,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            # H.264 tuned for still pictures, with a keyframe at every flip.
            "-c:v", "libx264", "-preset", "slow", "-tune", "stillimage", "-crf", "18",
            "-profile:v", "high", "-g", "30", "-bf", "0",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-use_editlist", "0",
            str(path),
        ]
        result: subprocess.CompletedProcess[str] = subprocess.run(
            command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")
    return path


def _nice_length(target_mi: float) -> int:
    """A round number of miles, at least `target_mi`, for a scale bar."""
    for v in [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500]:
        if v >= target_mi:
            return v
    return 1000


def _detail_page(it: Placed, states: list[State], header: str, legend: str) -> Figure:
    """One page zoomed in on a state: its square, neighboring states, guide
    lines, touching points, a scale bar, and the numbers alongside."""
    lon, lat = it.center
    # Azimuthal equidistant centered on the square: true distances from its center.
    proj: _Proj = _Proj(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")
    fig: Figure = plt.figure(figsize=(11, 8.5))
    fig.text(0.03, 0.965, header, fontsize=9, color=MUTED)
    ax: Axes = fig.add_axes((0.02, 0.04, 0.63, 0.88))
    ax.set_axis_off()

    geom: BaseGeometry = _display_geom(it.state)
    sminx, sminy, smaxx, smaxy = geom.bounds
    for other in states:
        if other.abbr == it.state.abbr:
            continue
        ominx, ominy, omaxx, omaxy = other.geom.bounds
        if ominx <= smaxx + 2 and omaxx >= sminx - 2 and ominy <= smaxy + 2 and omaxy >= sminy - 2:
            _add(ax, proj.geom(other.geom), facecolor=NEIGHBOR, edgecolor=NEIGHBOR_EDGE,
                 linewidth=0.5)
    projected: BaseGeometry = proj.geom(geom)
    _add(ax, projected, facecolor=LAND, edgecolor=LAND_EDGE, linewidth=0.7)

    for guide in it.guides:
        line: FloatArray = proj.xy(guide)
        ax.plot(line[:, 0], line[:, 1], color=SQUARE_EDGE, lw=0.5, ls=(0, (4, 4)), alpha=0.6)

    _add(ax, shapely.Polygon(proj.xy(it.outline)), facecolor=SQUARE, edgecolor=SQUARE_EDGE,
         linewidth=1.0, alpha=0.8)
    if len(it.contacts):
        c: FloatArray = proj.xy(it.contacts)
        ax.scatter(c[:, 0], c[:, 1], s=22, color=CONTACT, zorder=5, edgecolor="white", lw=0.6)
    ax.text(0, 0, f"{it.side_mi:.1f} mi", ha="center", va="center", fontsize=14,
            weight="bold", color=INK)

    minx, miny, maxx, maxy = projected.bounds
    pad: float = 0.04 * max(maxx - minx, maxy - miny)
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal")

    # Scale bar, bottom left.
    x0, x1 = ax.get_xlim()
    y0, _ = ax.get_ylim()
    bar_mi: int = _nice_length((x1 - x0) / 5 / METERS_PER_MILE)
    bar_x: float = x0 + pad
    bar_y: float = y0 + pad / 2
    ax.plot([bar_x, bar_x + bar_mi * METERS_PER_MILE], [bar_y, bar_y], color=INK, lw=2,
            solid_capstyle="butt")
    ax.text(bar_x + bar_mi * METERS_PER_MILE / 2, bar_y + pad / 4, f"{bar_mi} mi", ha="center",
            va="bottom", fontsize=9, color=INK)

    fig.text(0.67, 0.86, f"{it.state.name}", fontsize=24, weight="bold", color=INK)
    fig.text(0.67, 0.81, f"{it.side_mi:.1f} miles", fontsize=16, color=SQUARE_EDGE)
    fig.text(0.67, 0.77, "\n".join(it.details), fontsize=9.5, family="monospace", color=INK,
             va="top", linespacing=1.6)
    fig.text(0.67, 0.12, "Blue dots: boundary points within 50 ft of the\n"
                         "square, which stop it from growing.\n" + legend,
             fontsize=8.5, color=MUTED, va="bottom", linespacing=1.5)
    return fig


def _table_page(items: list[Placed], title: str, note: str) -> Figure:
    """A page listing every state's square from biggest to smallest."""
    fig: Figure = plt.figure(figsize=(8.5, 11))
    fig.text(0.07, 0.95, title, fontsize=14, weight="bold", color=INK)
    fig.text(0.07, 0.935, note, fontsize=7.5, color=MUTED, va="top", wrap=True)
    lines: list[str] = [f"{'':>3}  {'State':<16}{'Side (mi)':>10}{'Side (km)':>11}"]
    for rank, it in enumerate(sorted(items, key=lambda i: -i.side_mi), 1):
        lines.append(f"{rank:>3}  {it.state.name:<16}{it.side_mi:>10.1f}"
                     f"{it.side_mi * METERS_PER_MILE / 1000:>11.1f}")
    fig.text(0.07, 0.87, "\n".join(lines), fontsize=8.5, family="monospace", color=INK,
             va="top", linespacing=1.45)
    return fig


def draw_details(items: list[Placed], states: list[State], path: Path, title: str, note: str,
                 legend: str) -> Path:
    """Write a multi-page PDF: a ranked table, then one page per state.
    `legend` explains what the dashed guide lines are."""
    with PdfPages(path) as pdf:
        fig: Figure = _table_page(items, title, note)
        pdf.savefig(fig)
        plt.close(fig)
        for it in items:
            fig = _detail_page(it, states, title, legend)
            pdf.savefig(fig)
            plt.close(fig)
    return path

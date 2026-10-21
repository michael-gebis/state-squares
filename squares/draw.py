"""SVG/PDF output: a US map of the squares and per-state detail pages."""

from __future__ import annotations

import io
import subprocess
import tempfile
from dataclasses import dataclass, replace
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
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.text import Annotation, Text  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely.geometry.base import BaseGeometry  # noqa: E402
from shapely.ops import polylabel  # noqa: E402
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


@dataclass(frozen=True)
class _Panel:
    """One part of a US map: the lower 48, or the Alaska or Hawaii inset."""

    proj: _Proj
    ax: Axes
    states: list[State]
    callout_x: float  # where callout labels start, map meters


def _panels(fig: Figure, states: list[State], style: _MapStyle) -> list[_Panel]:
    """Lay out the lower 48 and the Alaska and Hawaii insets on a figure, with
    the states drawn as plain land and room on the right for callouts.
    Panels with no states (e.g. when drawing only a few) are left out."""
    # (projection, where its axes go, states drawn)
    layout: list[tuple[str, Rect, list[State]]] = [
        (CONUS, style.conus, [s for s in states if s.abbr not in ("AK", "HI")]),
        (ALASKA, style.alaska, [s for s in states if s.abbr == "AK"]),
        (HAWAII, style.hawaii, [s for s in states if s.abbr == "HI"]),
    ]
    panels: list[_Panel] = []
    for crs, rect, panel_states in layout:
        if not panel_states:
            continue
        proj: _Proj = _Proj(crs)
        ax: Axes = fig.add_axes(rect)
        ax.set_axis_off()
        geoms: list[BaseGeometry] = [proj.geom(_display_geom(s)) for s in panel_states]
        for g in geoms:
            _add(ax, g, facecolor=LAND, edgecolor=LAND_EDGE, linewidth=style.border_width)
        minx, miny, maxx, maxy = shapely.union_all(geoms).bounds
        pad: float = 0.02 * (maxx - minx)
        ax.set_xlim(minx - pad, maxx + style.callout_room * (maxx - minx))
        ax.set_ylim(miny - pad, maxy + pad)
        ax.set_aspect("equal")
        panels.append(_Panel(proj, ax, panel_states, maxx + 2 * pad))
    return panels


def _map_figure(items: list[Placed], states: list[State], style: _MapStyle,
                overlay: _Overlay | None = None) -> Figure:
    """A figure with the lower 48 and the Alaska and Hawaii insets, each state's
    square drawn and labeled.  Titles are left to the caller."""
    by_abbr: dict[str, Placed] = {it.state.abbr: it for it in items}
    fig: Figure = plt.figure(figsize=style.figsize, dpi=style.dpi)
    for panel in _panels(fig, states, style):
        proj: _Proj = panel.proj
        ax: Axes = panel.ax
        panel_items: list[Placed] = [by_abbr[s.abbr] for s in panel.states if s.abbr in by_abbr]
        for it in panel_items:
            face: str = overlay.faces.get(it.state.abbr, SQUARE) if overlay else SQUARE
            _add(ax, shapely.Polygon(proj.xy(it.outline)), facecolor=face,
                 edgecolor=SQUARE_EDGE, linewidth=style.square_width, alpha=0.85)
            ghost: Placed | None = overlay.ghosts.get(it.state.abbr) if overlay else None
            if ghost is not None:
                line: FloatArray = proj.xy(ghost.outline)
                ax.plot(line[:, 0], line[:, 1], color=INK, lw=style.square_width,
                        ls=(0, (3, 2)), alpha=0.8)
        _label_squares(ax, proj, panel_items, panel.callout_x, style, overlay)
    return fig


def draw_map(items: list[Placed], states: list[State], stem: Path, title: str, note: str,
             formats: tuple[str, ...] = ("svg", "pdf")) -> list[Path]:
    """Draw the detailed US map with every state's square and save it as
    stem.svg, stem.pdf, etc.  Returns the paths written."""
    return _save_map(_map_figure(items, states, PRINT_STYLE), stem, title, note, formats)


def _save_map(fig: Figure, stem: Path, title: str, note: str,
              formats: tuple[str, ...]) -> list[Path]:
    """Title a detailed US map and save it as stem.svg, stem.pdf, etc.
    Returns the paths written."""
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
    return _save_social(_map_figure(items, states, SOCIAL_STYLE), path, title, subtitle, credit)


def _save_social(fig: Figure, path: Path, title: str, subtitle: str, credit: str) -> Path:
    """Title a social-media map and save it as a PNG."""
    fig.text(0.025, 0.955, title, ha="left", va="top", fontsize=27, weight="bold", color=INK)
    fig.text(0.025, 0.875, subtitle, ha="left", va="top", fontsize=14, color=MUTED)
    fig.text(0.985, 0.015, credit, ha="right", va="bottom", fontsize=8, color=MUTED)
    fig.savefig(path, dpi=SOCIAL_STYLE.dpi, facecolor="white")
    plt.close(fig)
    return path


@dataclass(frozen=True)
class Nested:
    """One state with the biggest other state that fits inside it, for the
    US map."""

    host: State
    guest: State | None  # None when no other state fits
    shape: BaseGeometry | None  # the guest moved into the host, lon/lat


def _deepest(geom: BaseGeometry) -> tuple[shapely.Point, float]:
    """The point deepest inside a (projected) geometry's biggest piece, and
    how far it is from that piece's edge."""
    piece: shapely.Polygon = max(polygons(geom), key=lambda p: p.area)
    spot: shapely.Point = polylabel(piece, tolerance=piece.length / 1000)
    return spot, float(piece.exterior.distance(spot))


def _callouts(ax: Axes, entries: list[tuple[float, float, float, str]], callout_x: float,
              style: _MapStyle) -> None:
    """Write labels in a column at callout_x, stacked top to bottom without
    overlapping, each with a leader line back to its spot.  Each entry is
    (preferred label y, spot x, spot y, text), in map meters."""
    spacing: float = 1.6 * style.callout_size / _pts_per_meter(ax)
    prev: float = np.inf
    for anchor_y, x, y, text in sorted(entries, key=lambda e: -e[0]):
        label_y: float = min(anchor_y, prev - spacing)
        prev = label_y
        ax.plot([x, callout_x - 0.3 * spacing], [y, label_y], color=MUTED,
                lw=style.border_width)
        ax.text(callout_x, label_y, text, ha="left", va="center", fontsize=style.callout_size,
                color=INK)


def _label_size(room: float, scale: float, style: _MapStyle) -> float:
    """The font size (points) for a two-letter label that fits in a circle of
    radius `room` (map meters), capped at the style's biggest label.  Two
    capital letters take about 1.3 times the font size across."""
    return min(2 * room * scale / 1.3 * 0.6, style.label_max)


def _nest_figure(items: list[Nested], states: list[State], style: _MapStyle) -> Figure:
    """A US map with, inside each state, the biggest other state that fits,
    drawn at its real size and labeled with its abbreviation.  A guest too
    narrow for its label gets it on a white badge, sized to the host; when
    even the host is too small, the label becomes a callout such as "RI in
    MD".  Titles are left to the caller."""
    by_host: dict[str, Nested] = {it.host.abbr: it for it in items}
    fig: Figure = plt.figure(figsize=style.figsize, dpi=style.dpi)
    for panel in _panels(fig, states, style):
        ax: Axes = panel.ax
        scale: float = _pts_per_meter(ax)
        small: list[tuple[float, float, float, str]] = []
        for st in panel.states:
            it: Nested | None = by_host.get(st.abbr)
            if it is None:
                continue
            if it.guest is None or it.shape is None:
                spot, _ = _deepest(panel.proj.geom(_display_geom(st)))
                small.append((spot.y, spot.x, spot.y, f"{st.abbr}  none"))
                continue
            moved: BaseGeometry = panel.proj.geom(it.shape)
            _add(ax, moved, facecolor=SQUARE, edgecolor=SQUARE_EDGE,
                 linewidth=style.square_width, alpha=0.85)
            spot, room = _deepest(moved)
            size: float = _label_size(room, scale, style)
            badge: dict[str, Any] | None = None
            if size < style.label_min:
                _, host_room = _deepest(panel.proj.geom(_display_geom(st)))
                size = 0.8 * _label_size(host_room, scale, style)
                badge = {**BADGE, "alpha": 0.8}
            if size < style.label_min:
                small.append((spot.y, spot.x, spot.y, f"{it.guest.abbr} in {st.abbr}"))
            else:
                ax.text(spot.x, spot.y, it.guest.abbr, ha="center", va="center", fontsize=size,
                        weight="bold", color=INK, bbox=badge)
        _callouts(ax, small, panel.callout_x, style)
    return fig


def draw_nest_map(items: list[Nested], states: list[State], stem: Path, title: str, note: str,
                  formats: tuple[str, ...] = ("svg", "pdf")) -> list[Path]:
    """Draw the detailed US map of the biggest state that fits inside each
    state, and save it as stem.svg, stem.pdf, etc.  Returns the paths
    written."""
    return _save_map(_nest_figure(items, states, PRINT_STYLE), stem, title, note, formats)


def draw_nest_social(items: list[Nested], states: list[State], path: Path, title: str,
                     subtitle: str, credit: str) -> Path:
    """Draw a 16:9 PNG of the map of the biggest state that fits inside each
    state, for posting on social media."""
    return _save_social(_nest_figure(items, states, SOCIAL_STYLE), path, title, subtitle, credit)


@dataclass(frozen=True)
class Chain:
    """One state with the biggest other state that fits inside it, the
    biggest that fits inside that one, and so on, for the US map."""

    host: State
    shapes: list[BaseGeometry]  # lon/lat, biggest first, each inside the one before


# Fills for each step down a chain: pale orange, darkening to nearly black.
DEPTH_RAMP: tuple[str, ...] = (SQUARE_PALE, SQUARE, SQUARE_EDGE, "#2e0f03")


def _depth_color(depth: int, deepest: int) -> str:
    """Fill color for the state `depth` steps inside its host (1 is the
    biggest state that fits), in a map whose longest chain is `deepest`
    steps: the same shade for the same step everywhere, darker each step
    (and no darker past `deepest`)."""
    stops: FloatArray = np.array([to_rgb(c) for c in DEPTH_RAMP])
    at: float = (len(stops) - 1) * min((depth - 1) / max(deepest - 1, 1), 1.0)
    i: int = min(int(at), len(stops) - 2)
    r, g, b = (float(c) for c in stops[i] + (stops[i + 1] - stops[i]) * (at - i))
    return str(to_hex((r, g, b)))


def _chain_figure(chains: list[Chain], states: list[State], style: _MapStyle) -> Figure:
    """A US map with each state's whole chain drawn inside it, one state
    inside the next, darker at each step.  No labels, so no room is kept for
    callouts.  Titles are left to the caller."""
    by_host: dict[str, Chain] = {c.host.abbr: c for c in chains}
    deepest: int = max((len(c.shapes) for c in chains), default=1)
    fig: Figure = plt.figure(figsize=style.figsize, dpi=style.dpi)
    for panel in _panels(fig, states, replace(style, callout_room=0.0)):
        for st in panel.states:
            chain: Chain | None = by_host.get(st.abbr)
            for depth, shape in enumerate(chain.shapes if chain else [], start=1):
                # Outlined in the next shade down, so even a ragged coast stays quiet.
                _add(panel.ax, panel.proj.geom(shape), facecolor=_depth_color(depth, deepest),
                     edgecolor=_depth_color(depth + 1, deepest),
                     linewidth=0.5 * style.border_width)
    return fig


def draw_chain_map(chains: list[Chain], states: list[State], stem: Path, title: str, note: str,
                   formats: tuple[str, ...] = ("svg", "pdf")) -> list[Path]:
    """Draw the detailed US map of each state's chain of states nested inside
    it, and save it as stem.svg, stem.pdf, etc.  Returns the paths written."""
    return _save_map(_chain_figure(chains, states, PRINT_STYLE), stem, title, note, formats)


def draw_chain_social(chains: list[Chain], states: list[State], path: Path, title: str,
                      subtitle: str, credit: str) -> Path:
    """Draw a 16:9 PNG of the map of each state's chain of states nested
    inside it, for posting on social media."""
    return _save_social(_chain_figure(chains, states, SOCIAL_STYLE), path, title, subtitle,
                        credit)


SPILL: str = "#c62828"  # the parts of a state that spill off another
SPILL_EDGE: str = "#7f1010"


@dataclass(frozen=True)
class Spill:
    """One state at its real size on another, and the parts of it that
    spill off, for a social-media image."""

    host: State
    guest: State
    shape: BaseGeometry  # the guest moved into place, lon/lat
    off: BaseGeometry  # the parts of it that spill off the host, lon/lat (maybe empty)
    notes: list[tuple[float, float, str]]  # callouts: (lon, lat of a spill, text)


def _name_size(name: str, room: float, scale: float, biggest: float) -> float:
    """The font size (points) for a name in capitals that fits in a circle of
    radius `room` (map meters), capped at `biggest`.  Capitals take about
    0.75 times the font size each."""
    return min(2 * room * scale / (0.75 * max(len(name), 2)), biggest)


def _spill_figure(sp: Spill, states: list[State], legend: list[str]) -> Figure:
    """The host and its neighbors, the guest at its real size filled in where
    it lands on the host and in red where it spills off, both named, with a
    callout for each note and, in a row under the map, a legend (an entry for
    the guest, and one for the spill if any).  Titles are left to the caller."""
    proj: _Proj = _state_proj(sp.host)
    fig: Figure = plt.figure(figsize=SOCIAL_STYLE.figsize, dpi=SOCIAL_STYLE.dpi)
    ax: Axes = fig.add_axes((0.0, 0.11, 1.0, 0.72))  # the legend goes below
    ax.set_axis_off()
    for other in _nearby(sp.host, states):
        _add(ax, proj.geom(other.geom), facecolor=NEIGHBOR, edgecolor=NEIGHBOR_EDGE,
             linewidth=0.5)
    host: BaseGeometry = proj.geom(_display_geom(sp.host))
    _add(ax, host, facecolor=LAND, edgecolor=LAND_EDGE, linewidth=0.7)
    guest: BaseGeometry = proj.geom(sp.shape)
    _add(ax, proj.geom(sp.shape.difference(sp.off)), facecolor=SQUARE, edgecolor="none",
         alpha=0.85)
    _add(ax, proj.geom(sp.off), facecolor=SPILL, edgecolor=SPILL_EDGE, linewidth=0.6)
    for poly in polygons(guest):
        ring: FloatArray = np.asarray(poly.exterior.coords)
        ax.plot(ring[:, 0], ring[:, 1], color=SQUARE_EDGE, lw=0.9)

    # Each callout's text goes out from the host's middle, past its spill,
    # far enough to be clear of the host's land.
    middle, _ = _deepest(host)
    bounds: tuple[float, float, float, float] = shapely.union_all([host, guest]).bounds
    extent: float = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    # (spill, text tip, text alignment left/right, bottom/center/top, text)
    callouts: list[tuple[FloatArray, FloatArray, str, str, str]] = []
    for lon, lat, text in sp.notes:
        spot: FloatArray = proj.xy([lon, lat])[0]
        out: FloatArray = spot - np.array([middle.x, middle.y])
        out = out / max(float(np.hypot(*out)), 1.0)
        tip: FloatArray = spot + 0.06 * extent * out
        for _ in range(15):
            if host.distance(shapely.Point(tip)) >= 0.03 * extent:
                break
            tip = tip + 0.02 * extent * out
        # The text reaches away from the map: sideways, and up or down too
        # when the way out is mostly up or down.
        va: str = "center" if abs(out[1]) < 0.7 else "bottom" if out[1] > 0 else "top"
        callouts.append((spot, tip, "left" if out[0] >= 0 else "right", va, text))

    def frame(room: float) -> float:
        """Set the map's limits to hold the host, the guest and the callouts,
        with `room` map meters for each callout's text, above or below its
        tip or centered on it, and below everything for the scale bar.
        Returns the padding around it all."""
        x0, y0, x1, y1 = bounds
        for _, tip, _, va, _ in callouts:
            below: float = {"bottom": 0.0, "center": 0.5, "top": 1.0}[va] * room
            x0, x1 = min(x0, float(tip[0])), max(x1, float(tip[0]))
            y0, y1 = min(y0, float(tip[1]) - below), max(y1, float(tip[1]) - below + room)
        y0 -= room
        pad: float = 0.03 * max(x1 - x0, y1 - y0)
        ax.set_xlim(x0 - pad, x1 + pad)
        ax.set_ylim(y0 - pad, y1 + pad)
        ax.set_aspect("equal")
        return pad

    # Two lines of 12 pt text are a fixed height on the page, so measure the
    # map's scale first, then make room.
    frame(0.0)
    pad: float = frame(36 / _pts_per_meter(ax))
    _scale_bar(ax, pad, fontsize=11)
    scale: float = _pts_per_meter(ax)

    for spot, tip, ha, va, text in callouts:
        ax.annotate(text, (float(spot[0]), float(spot[1])),
                    xytext=(float(tip[0]), float(tip[1])), fontsize=12, color=INK,
                    linespacing=1.3, ha=ha, va=va,
                    arrowprops={"arrowstyle": "-", "color": INK, "lw": 0.8, "shrinkA": 4,
                                "shrinkB": 2})
    for geom, name, biggest, color in [(guest, sp.guest.name, 22.0, INK),
                                       (host.difference(guest), sp.host.name, 16.0, MUTED)]:
        if geom.is_empty:
            continue
        where, room = _deepest(geom)
        size: float = _name_size(name, room, scale, biggest)
        if size >= 9:
            ax.text(where.x, where.y, name.upper(), ha="center", va="center", fontsize=size,
                    weight="bold", color=color, alpha=0.8)

    handles: list[Patch] = [Patch(facecolor=SQUARE, edgecolor=SQUARE_EDGE, alpha=0.85,
                                  label=legend[0])]
    if len(legend) > 1:
        handles.append(Patch(facecolor=SPILL, edgecolor=SPILL_EDGE, label=legend[1]))
    fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.02, 0.035),
               ncol=len(handles), fontsize=12, frameon=False, handlelength=1.6,
               handleheight=1.1, columnspacing=2.5)
    return fig


def draw_spill(sp: Spill, states: list[State], path: Path, title: str, subtitle: str,
               legend: list[str], credit: str) -> Path:
    """Draw a 16:9 PNG for social media of one state at its real size on
    another, showing what spills off (see `_spill_figure`)."""
    return _save_social(_spill_figure(sp, states, legend), path, title, subtitle, credit)


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
    first_note: str = ""  # close-ups only: a line under each version's number,
    second_note: str = ""  # e.g. "Turned 1.7° clockwise from north"


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
        frames.append(_render(fig, dpi))
    return frames


def _render(fig: Figure, dpi: float) -> Image.Image:
    """Turn a figure into an RGB image on a white background, and close it."""
    buffer: io.BytesIO = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def draw_flip_gif(first: list[Placed], second: list[Placed], states: list[State], path: Path,
                  text: FlipText, frame_seconds: float = 1.0) -> Path:
    """Write an animated GIF (1600x900, looping forever) that flips between two
    versions of the map; see _flip_frames for what each frame shows."""
    return _save_gif(_flip_frames(first, second, states, text, GIF_DPI), path, frame_seconds)


def draw_flip_video(first: list[Placed], second: list[Placed], states: list[State], path: Path,
                    text: FlipText, cycles: int = 8, ffmpeg: str = "ffmpeg") -> Path:
    """Write the flip-book comparison as a 1920x1080 MP4 video, for sites that
    take video rather than GIFs (such as Threads); see _save_video."""
    return _save_video(_flip_frames(first, second, states, text, VIDEO_DPI), path, cycles,
                       ffmpeg)


def _save_gif(frames: list[Image.Image], path: Path, frame_seconds: float) -> Path:
    """Write frames as an animated GIF that loops forever, showing each for
    `frame_seconds`."""
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


def _save_video(frames: list[Image.Image], path: Path, cycles: int, ffmpeg: str) -> Path:
    """Write frames as an MP4 video using the ffmpeg program.

    Each frame shows for one second, and the set repeats `cycles` times,
    since videos don't loop on their own the way GIFs do.  The encoding follows
    Meta's published requirements for Threads: H.264 video, 4:2:0 color, at
    most 1920 pixels wide, 23-60 frames per second, the index ("moov atom") at
    the front of the file and no edit lists.  It also carries a silent AAC
    audio track, because some upload pipelines reject videos with none.
    """
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


# Close-up flip-books (16:9): where the map goes, and a color for the
# numbers of the version not being shown.
CLOSEUP_MAP: Rect = (0.01, 0.03, 0.58, 0.78)
FADED: str = "#c9c9c9"


def _closeup_figure(first: Placed, second: Placed, shown: int, states: list[State],
                    text: FlipText) -> Figure:
    """One frame of a close-up flip-book comparing two versions of one state's
    square (e.g. facing north and free to rotate); `shown` is 0 for the first
    version, 1 for the second.

    The map shows that version's square filled, with its side length and the
    border points that stop it from growing, and the other version as a
    dashed outline.  The numbers for both versions sit alongside, the shown
    one highlighted.  Everything else is the same in both frames, so flipping
    between them shows only what changes.
    """
    st: State = first.state
    proj: _Proj = _state_proj(st)
    land: BaseGeometry = proj.geom(_display_geom(st))
    versions: list[Placed] = [first, second]
    sides: list[float] = [round(v.side_mi, 1) for v in versions]
    gain: float = round(sides[1] - sides[0], 1)  # as the reader would subtract them
    it: Placed = versions[shown]

    fig: Figure = plt.figure(figsize=(12, 6.75))
    ax: Axes = fig.add_axes(CLOSEUP_MAP)
    ax.set_axis_off()
    for other in _nearby(st, states):
        _add(ax, proj.geom(other.geom), facecolor=NEIGHBOR, edgecolor=NEIGHBOR_EDGE,
             linewidth=0.6)
    _add(ax, land, facecolor=LAND, edgecolor=LAND_EDGE, linewidth=0.8)
    _add(ax, shapely.Polygon(proj.xy(it.outline)), facecolor=SQUARE, edgecolor=SQUARE_EDGE,
         linewidth=1.0, alpha=0.85)
    ghost: FloatArray = proj.xy(versions[1 - shown].outline)
    ax.plot(ghost[:, 0], ghost[:, 1], color=INK, lw=1.0, ls=(0, (3, 2)), alpha=0.8)
    if len(it.contacts):
        c: FloatArray = proj.xy(it.contacts)
        ax.scatter(c[:, 0], c[:, 1], s=20, color=CONTACT, zorder=5, edgecolor="white", lw=0.5)
    x, y = proj.xy(it.center)[0]
    ax.annotate(f"{sides[shown]:.1f} mi", (x, y), ha="center", va="center", fontsize=24,
                weight="bold", color=INK)
    if shown == 1 and gain:
        ax.annotate(f"{gain:+.1f}", (x, y), xytext=(0, -30), textcoords="offset points",
                    ha="center", va="center", fontsize=16, weight="bold", color=GAIN, bbox=BADGE)
    sx0, sy0, sx1, sy1 = land.bounds
    pad: float = 0.04 * max(sx1 - sx0, sy1 - sy0)
    ax.set_xlim(sx0 - pad, sx1 + pad)
    ax.set_ylim(sy0 - pad, sy1 + pad)
    ax.set_aspect("equal")
    _scale_bar(ax, pad, fontsize=11)

    fig.text(0.025, 0.955, text.title, ha="left", va="top", fontsize=27, weight="bold", color=INK)
    fig.text(0.03, 0.865, [text.first_label, text.second_label][shown], ha="left", va="top",
             fontsize=19, weight="bold", color="white",
             bbox={"boxstyle": "round,pad=0.35", "fc": [INK, GAIN][shown], "ec": "none"})
    for i, (label, note) in enumerate([(text.first_label, text.first_note),
                                       (text.second_label, text.second_note)]):
        on: bool = i == shown
        top: float = 0.78 - 0.2 * i
        fig.text(0.63, top, label, fontsize=15, weight="bold", color=INK if on else FADED,
                 va="top")
        value: Text = fig.text(0.63, top - 0.045, f"{sides[i]:.1f} mi", fontsize=30,
                               weight="bold", color=SQUARE_EDGE if on else FADED, va="top")
        if i == 1 and gain:
            fig.add_artist(Annotation(f"  {gain:+.1f} mi", (1.0, 0.5), xycoords=value,
                                      ha="left", va="center", fontsize=18, weight="bold",
                                      color=GAIN if on else FADED))
        fig.text(0.63, top - 0.12, note, fontsize=11, color=MUTED if on else FADED, va="top")
    fig.text(0.63, 0.06, text.legend, ha="left", va="bottom", fontsize=11, color=MUTED,
             linespacing=1.5)
    return fig


def _closeup_frames(first: Placed, second: Placed, states: list[State], text: FlipText,
                    dpi: float) -> list[Image.Image]:
    """Render both frames of a close-up flip-book; see _closeup_figure."""
    return [_render(_closeup_figure(first, second, shown, states, text), dpi) for shown in (0, 1)]


def draw_closeup_gif(first: Placed, second: Placed, states: list[State], path: Path,
                     text: FlipText, frame_seconds: float = 1.0) -> Path:
    """Write an animated GIF (1600x900, looping forever) that flips between two
    versions of one state's square, zoomed in on the state; see
    _closeup_figure for what each frame shows."""
    return _save_gif(_closeup_frames(first, second, states, text, GIF_DPI), path, frame_seconds)


def draw_closeup_video(first: Placed, second: Placed, states: list[State], path: Path,
                       text: FlipText, cycles: int = 8, ffmpeg: str = "ffmpeg") -> Path:
    """Write the close-up flip-book as a 1920x1080 MP4 video; see _save_video."""
    return _save_video(_closeup_frames(first, second, states, text, VIDEO_DPI), path, cycles,
                       ffmpeg)


def _nice_length(target_mi: float) -> int:
    """A round number of miles, at least `target_mi`, for a scale bar."""
    for v in [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500]:
        if v >= target_mi:
            return v
    return 1000


def _scale_bar(ax: Axes, pad: float, fontsize: float = 9) -> None:
    """Draw a scale bar of a round number of miles about a fifth of the way
    across, in the bottom left corner of a zoomed-in map (projected in
    meters, limits already set), `pad` meters in from the edges."""
    x0, x1 = ax.get_xlim()
    y0, _ = ax.get_ylim()
    bar_mi: int = _nice_length((x1 - x0) / 5 / METERS_PER_MILE)
    bar_x: float = x0 + pad
    bar_y: float = y0 + pad / 2
    ax.plot([bar_x, bar_x + bar_mi * METERS_PER_MILE], [bar_y, bar_y], color=INK, lw=2,
            solid_capstyle="butt")
    ax.text(bar_x + bar_mi * METERS_PER_MILE / 2, bar_y + pad / 4, f"{bar_mi} mi", ha="center",
            va="bottom", fontsize=fontsize, color=INK)


def _nearby(st: State, states: list[State]) -> list[State]:
    """The other states close enough to show around `st` on a zoomed-in map."""
    minx, miny, maxx, maxy = _display_geom(st).bounds
    near: list[State] = []
    for other in states:
        if other.abbr == st.abbr:
            continue
        ominx, ominy, omaxx, omaxy = other.geom.bounds
        if ominx <= maxx + 2 and omaxx >= minx - 2 and ominy <= maxy + 2 and omaxy >= miny - 2:
            near.append(other)
    return near


def _state_proj(st: State) -> _Proj:
    """A stereographic map centered on a state.  It is conformal, so squares
    look square everywhere on it."""
    geom: BaseGeometry = _display_geom(st)
    minx, miny, maxx, maxy = geom.bounds
    if maxx - minx > 180:  # Alaska's Aleutians reach across 180 degrees
        lon: FloatArray = shapely.get_coordinates(geom)[:, 0] % 360
        minx, maxx = float(lon.min()), float(lon.max())
    lon0: float = ((minx + maxx) / 2 + 180) % 360 - 180
    return _Proj(f"+proj=stere +lat_0={(miny + maxy) / 2} +lon_0={lon0} "
                 "+datum=WGS84 +units=m +no_defs")


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

    for other in _nearby(it.state, states):
        _add(ax, proj.geom(other.geom), facecolor=NEIGHBOR, edgecolor=NEIGHBOR_EDGE,
             linewidth=0.5)
    projected: BaseGeometry = proj.geom(_display_geom(it.state))
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
    _scale_bar(ax, pad)

    fig.text(0.67, 0.86, f"{it.state.name}", fontsize=24, weight="bold", color=INK)
    fig.text(0.67, 0.81, f"{it.side_mi:.1f} miles", fontsize=16, color=SQUARE_EDGE)
    fig.text(0.67, 0.77, "\n".join(it.details), fontsize=9.5, family="monospace", color=INK,
             va="top", linespacing=1.6)
    fig.text(0.67, 0.12, "Blue dots: boundary points within 50 ft of the\n"
                         "square, which stop it from growing.\n" + legend,
             fontsize=8.5, color=MUTED, va="bottom", linespacing=1.5)
    return fig


def draw_packing(st: State, outlines: list[FloatArray], edge_contacts: FloatArray,
                 pair_contacts: list[BaseGeometry], states: list[State], stem: Path,
                 header: str, headline: str, details: list[str], legend: str) -> list[Path]:
    """A page showing several squares packed into one state: the squares
    (lon/lat rings), the boundary points and square-to-square slivers where
    they are wedged tight, neighboring states, a scale bar, and the numbers
    alongside.  Saved as stem.pdf and as stem.png (2200 x 1700 pixels);
    returns both paths."""
    proj: _Proj = _state_proj(st)
    fig: Figure = plt.figure(figsize=(11, 8.5))
    fig.text(0.03, 0.965, header, fontsize=9, color=MUTED)
    ax: Axes = fig.add_axes((0.02, 0.04, 0.63, 0.88))
    ax.set_axis_off()
    for other in _nearby(st, states):
        _add(ax, proj.geom(other.geom), facecolor=NEIGHBOR, edgecolor=NEIGHBOR_EDGE,
             linewidth=0.5)
    projected: BaseGeometry = proj.geom(_display_geom(st))
    _add(ax, projected, facecolor=LAND, edgecolor=LAND_EDGE, linewidth=0.7)
    for ring in outlines:
        _add(ax, shapely.Polygon(proj.xy(ring)), facecolor=SQUARE, edgecolor=SQUARE_EDGE,
             linewidth=0.9, alpha=0.85)
    for sliver in pair_contacts:
        _add(ax, proj.geom(sliver), facecolor=INK, edgecolor=INK, linewidth=2.2, zorder=4)
    if len(edge_contacts):
        c: FloatArray = proj.xy(edge_contacts)
        ax.scatter(c[:, 0], c[:, 1], s=14, color=CONTACT, zorder=5, edgecolor="white", lw=0.4)

    sx0, sy0, sx1, sy1 = projected.bounds
    pad: float = 0.04 * max(sx1 - sx0, sy1 - sy0)
    ax.set_xlim(sx0 - pad, sx1 + pad)
    ax.set_ylim(sy0 - pad, sy1 + pad)
    ax.set_aspect("equal")
    _scale_bar(ax, pad)

    fig.text(0.67, 0.86, st.name, fontsize=24, weight="bold", color=INK)
    fig.text(0.67, 0.81, headline, fontsize=14, color=SQUARE_EDGE)  # 16 clipped "104.2 miles"
    fig.text(0.67, 0.77, "\n".join(details), fontsize=9.5, family="monospace", color=INK,
             va="top", linespacing=1.6)
    fig.text(0.67, 0.12, legend, fontsize=8.5, color=MUTED, va="bottom", linespacing=1.5)
    paths: list[Path] = [stem.with_suffix(".pdf"), stem.with_suffix(".png")]
    fig.savefig(paths[0])
    fig.savefig(paths[1], dpi=200)
    plt.close(fig)
    return paths


def draw_nesting(host: State, guest: BaseGeometry, bigger: BaseGeometry | None, label: str,
                 states: list[State], stem: Path, header: str, headline: str,
                 details: list[str], legend: str) -> list[Path]:
    """A page showing one state moved inside another: the host and its
    neighbors, the guest (a lon/lat geometry) filled in and labeled, an
    optional bigger copy of it drawn dashed, a scale bar, and the numbers
    alongside.  Saved as stem.pdf and as stem.png (2200 x 1700 pixels);
    returns both paths."""
    proj: _Proj = _state_proj(host)
    fig: Figure = plt.figure(figsize=(11, 8.5))
    fig.text(0.03, 0.965, header, fontsize=9, color=MUTED)
    ax: Axes = fig.add_axes((0.02, 0.04, 0.63, 0.88))
    ax.set_axis_off()
    for other in _nearby(host, states):
        _add(ax, proj.geom(other.geom), facecolor=NEIGHBOR, edgecolor=NEIGHBOR_EDGE,
             linewidth=0.5)
    projected: BaseGeometry = proj.geom(_display_geom(host))
    _add(ax, projected, facecolor=LAND, edgecolor=LAND_EDGE, linewidth=0.7)
    moved: BaseGeometry = proj.geom(guest)
    _add(ax, moved, facecolor=SQUARE, edgecolor=SQUARE_EDGE, linewidth=0.8, alpha=0.85)
    if bigger is not None:
        for poly in polygons(proj.geom(bigger)):
            ring: FloatArray = np.asarray(poly.exterior.coords)
            ax.plot(ring[:, 0], ring[:, 1], color=SQUARE_EDGE, lw=0.8, ls=(0, (3, 2)))
    # Label the guest's biggest piece at the point deepest inside it.
    piece: shapely.Polygon = max(polygons(moved), key=lambda p: p.area)
    spot: shapely.Point = polylabel(piece, tolerance=piece.length / 1000)
    ax.text(spot.x, spot.y, label, ha="center", va="center", fontsize=13, weight="bold",
            color=INK, bbox={**BADGE, "alpha": 0.75})

    sx0, sy0, sx1, sy1 = projected.bounds
    pad: float = 0.04 * max(sx1 - sx0, sy1 - sy0)
    ax.set_xlim(sx0 - pad, sx1 + pad)
    ax.set_ylim(sy0 - pad, sy1 + pad)
    ax.set_aspect("equal")
    _scale_bar(ax, pad)

    fig.text(0.67, 0.86, host.name, fontsize=24, weight="bold", color=INK)
    fig.text(0.67, 0.81, headline, fontsize=14, color=SQUARE_EDGE)
    fig.text(0.67, 0.77, "\n".join(details), fontsize=9.5, family="monospace", color=INK,
             va="top", linespacing=1.6)
    fig.text(0.67, 0.06, legend, fontsize=8.5, color=MUTED, va="bottom", linespacing=1.5)
    paths: list[Path] = [stem.with_suffix(".pdf"), stem.with_suffix(".png")]
    fig.savefig(paths[0])
    fig.savefig(paths[1], dpi=200)
    plt.close(fig)
    return paths


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

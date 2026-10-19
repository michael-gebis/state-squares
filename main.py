# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "matplotlib>=3.9",
#     "numpy>=2.0",
#     "pillow>=10",
#     "pyproj>=3.6",
#     "pyshp>=2.3",
#     "shapely>=2.0",
# ]
# ///
"""Largest square that fits in each US state.

    uv run main.py aligned              # lat/long-aligned box, all 50 states
    uv run main.py rotated              # true square at any rotation
    uv run main.py north                # true square with sides facing north/east
    uv run main.py compare              # GIF flipping between north and rotated
    uv run main.py rotated WI CO WY     # just these
"""

import argparse
import csv
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import numpy as np

ROOT: Path = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from squares import aligned, data, draw, rotated  # noqa: E402
from squares.arrays import FloatArray  # noqa: E402

KM_PER_MILE: float = 1.609344

SOCIAL_CREDIT: str = "Land area · US Census Bureau 2024 boundaries · lengths on the WGS84 ellipsoid"

# One line of a results CSV, column name -> value.
Row: TypeAlias = dict[str, str | float]


@dataclass(frozen=True)
class Captions:
    """The words that go on one command's maps and PDFs."""

    title: str  # headline of the detailed map and the PDF
    note: str  # the definition, in a sentence or two, under the title
    legend: str  # explains the dashed lines on the detail pages
    social_title: str  # short headline for the social-media PNG
    social_subtitle: str


SOURCE_NOTE: str = ("Land area only (US Census 2024 cartographic boundaries, 1:500,000). "
                    "Lengths on the WGS84 ellipsoid.")
TRUE_SQUARE_NOTE: str = ("True squares: four equal sides (great-circle arcs on the ellipsoid) "
                         "and four equal corner angles")
ALIGNED_CAPTIONS: Captions = Captions(
    title="Largest latitude/longitude-aligned square in each state",
    note=("Box bounded by two parallels and two meridians; its north–south side equals its "
          f"east–west width at the box's middle latitude. {SOURCE_NOTE}"),
    legend="Dashed lines: the square's parallels and meridians.",
    social_title="The biggest latitude/longitude box in each state",
    social_subtitle="Bounded by parallels and meridians, as tall as it is wide at mid-latitude · miles")
ROTATED_CAPTIONS: Captions = Captions(
    title="Largest square in each state, at any rotation",
    note=f"{TRUE_SQUARE_NOTE}, free to rotate. {SOURCE_NOTE}",
    legend="Dashed lines: the square's sides, extended.",
    social_title="The biggest square that fits in each state",
    social_subtitle="Free to rotate · side length in miles")
COMPARE_TEXT: draw.FlipText = draw.FlipText(
    title="The biggest square in each state",
    first_label="Facing north",
    second_label="Free to rotate",
    legend="Side length in miles · dashed: the other version · darker: bigger gain")
NORTH_CAPTIONS: Captions = Captions(
    title="Largest north-facing square in each state",
    note=f"{TRUE_SQUARE_NOTE}, with sides facing north, south, east and west. {SOURCE_NOTE}",
    legend="Dashed lines: the square's sides, extended.",
    social_title="The biggest north-facing square in each state",
    social_subtitle="Sides facing north, south, east and west · side length in miles")


def fmt_lat(v: float) -> str:
    """Format a latitude like 44.6481°N."""
    return f"{abs(v):.4f}°{'N' if v >= 0 else 'S'}"


def fmt_lon(v: float) -> str:
    """Format a longitude like 89.6353°W."""
    return f"{abs(v):.4f}°{'E' if v >= 0 else 'W'}"


def selected_states(args: argparse.Namespace) -> tuple[list[data.State], list[data.State]]:
    """Load all 50 states and pick out the ones named on the command line
    (all of them if none were named).  Returns (all states, chosen states)."""
    states: list[data.State] = data.load_states(data.fetch(ROOT / "data"))
    wanted: set[str] = {s.upper() for s in args.states}
    unknown: set[str] = wanted - {s.abbr for s in states}
    if unknown:
        sys.exit(f"unknown state abbreviation(s): {', '.join(sorted(unknown))}")
    return states, [s for s in states if not wanted or s.abbr in wanted]


def write_outputs(args: argparse.Namespace, prefix: str, rows: list[Row],
                  items: list[draw.Placed], all_states: list[data.State],
                  text: Captions) -> None:
    """Save the CSV of results and, unless --no-draw was given, the maps (SVG,
    PDF and social-media PNG) and the per-state detail PDF, all named after
    `prefix`."""
    out: Path = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path: Path = out / f"{prefix}_squares.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer: csv.DictWriter[str] = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: -float(r["side_mi"])))
    print(f"wrote {csv_path}")
    if args.no_draw:
        return
    for path in draw.draw_map(items, all_states, out / f"{prefix}_squares", text.title, text.note):
        print(f"wrote {path}")
    social: Path = draw.draw_social(items, all_states, out / f"{prefix}_social.png",
                                    text.social_title, text.social_subtitle, SOCIAL_CREDIT)
    print(f"wrote {social}")
    details: Path = draw.draw_details(items, all_states, out / f"{prefix}_states.pdf",
                                      text.title, text.note, text.legend)
    print(f"wrote {details}")


def cmd_aligned(args: argparse.Namespace) -> None:
    """The `aligned` command: largest box bounded by parallels and meridians."""
    all_states, states = selected_states(args)
    rows: list[Row] = []
    items: list[draw.Placed] = []
    for st in states:
        t0: float = time.perf_counter()
        res: aligned.Result = aligned.largest_aligned_box(st.geom, tol=args.tol)
        box: aligned.AlignedBox = res.box
        side: float = box.side_mi()
        widths: dict[str, float] = {name: box.width_mi(lat) for name, lat in
                                    [("north", box.north), ("mid", box.lat), ("south", box.south)]}
        print(f"{st.abbr}  {st.name:<15} {side:8.2f} mi   "
              f"(edges N {widths['north']:.2f} / S {widths['south']:.2f})   "
              f"{time.perf_counter() - t0:5.1f}s", flush=True)
        rows.append({
            "state": st.name, "abbr": st.abbr,
            "side_mi": round(side, 3), "side_km": round(side * KM_PER_MILE, 3),
            "north_edge_mi": round(widths["north"], 3),
            "mid_width_mi": round(widths["mid"], 3),
            "south_edge_mi": round(widths["south"], 3),
            "south_lat": round(box.south, 6), "north_lat": round(box.north, 6),
            "west_lon": round(box.west, 6), "east_lon": round(box.east, 6),
        })
        items.append(draw.Placed(st, box.ring(), (box.lon, box.lat), side, res.contacts, [
            f"Side (north–south): {side:.1f} mi  ({side * KM_PER_MILE:.1f} km)",
            "East–west width:",
            f"    north edge   {widths['north']:.1f} mi",
            f"    middle       {widths['mid']:.1f} mi",
            f"    south edge   {widths['south']:.1f} mi",
            f"Latitude    {fmt_lat(box.south)} to {fmt_lat(box.north)}",
            f"Longitude   {fmt_lon(box.west)} to {fmt_lon(box.east)}",
            f"Boundary points within 50 ft: {len(res.contacts)}",
        ], box.guides()))

    write_outputs(args, "aligned", rows, items, all_states, ALIGNED_CAPTIONS)


def describe_rotation(deg: float) -> str:
    """Put a square's tilt into words for the detail page."""
    if abs(deg) < 0.05:
        return "none (sides run north–south and east–west)"
    return f"{abs(deg):.1f}° {'counterclockwise' if deg > 0 else 'clockwise'} from north"


def solve_true_squares(states: list[data.State], tol: float,
                       rotate: bool) -> tuple[list[Row], list[draw.Placed]]:
    """Find each state's largest true square, free to rotate or facing north,
    and return the CSV rows and the drawable results."""
    rows: list[Row] = []
    items: list[draw.Placed] = []
    for st in states:
        t0: float = time.perf_counter()
        res: rotated.Result = rotated.largest_square(st.geom, tol=tol, rotate=rotate)
        sq: rotated.Square = res.square
        sides: FloatArray = sq.sides_mi()
        side: float = float(sides.mean())
        angle: float = float(sq.corner_angles().mean())
        rot: float = sq.rotation_deg()
        corners: FloatArray = sq.corners()
        print(f"{st.abbr}  {st.name:<15} {side:8.2f} mi   rotation {rot:6.2f}°   "
              f"{time.perf_counter() - t0:5.1f}s", flush=True)
        row: Row = {
            "state": st.name, "abbr": st.abbr,
            "side_mi": round(side, 3), "side_km": round(side * KM_PER_MILE, 3),
            "rotation_deg": round(rot, 3), "corner_angle_deg": round(angle, 4),
            "center_lat": round(sq.lat, 6), "center_lon": round(sq.lon, 6),
        }
        for i, (lon, lat) in enumerate(corners, 1):
            row[f"corner{i}_lat"] = round(float(lat), 6)
            row[f"corner{i}_lon"] = round(float(lon), 6)
        rows.append(row)
        corner_text: str = "\n         ".join(f"{fmt_lat(la)}, {fmt_lon(lo)}" for lo, la in corners)
        items.append(draw.Placed(st, sq.ring(), (sq.lon, sq.lat), side, res.contacts, [
            f"Side: {side:.1f} mi  ({side * KM_PER_MILE:.1f} km)",
            f"    four sides agree within {np.ptp(sides) * 5280:.0f} ft",
            f"Rotation: {describe_rotation(rot)}",
            f"Corner angles: {angle:.3f}°",
            "    (over 90° because the Earth is curved)",
            f"Center   {fmt_lat(sq.lat)}, {fmt_lon(sq.lon)}",
            f"Corners  {corner_text}",
            f"Boundary points within 50 ft: {len(res.contacts)}",
        ], sq.guides()))
    return rows, items


def cmd_rotated(args: argparse.Namespace) -> None:
    """The `rotated` command: largest true square at any rotation."""
    all_states, states = selected_states(args)
    rows, items = solve_true_squares(states, args.tol, rotate=True)
    write_outputs(args, "rotated", rows, items, all_states, ROTATED_CAPTIONS)


def cmd_north(args: argparse.Namespace) -> None:
    """The `north` command: largest true square with sides facing north and east."""
    all_states, states = selected_states(args)
    rows, items = solve_true_squares(states, args.tol, rotate=False)
    write_outputs(args, "north", rows, items, all_states, NORTH_CAPTIONS)


def cmd_compare(args: argparse.Namespace) -> None:
    """The `compare` command: how much each state gains by rotating its square,
    as a CSV, an animated GIF that flips between the two maps, and (if ffmpeg
    is installed) the same flip-book as an MP4 video."""
    all_states, states = selected_states(args)
    print("north-facing squares:")
    north_rows, north_items = solve_true_squares(states, args.tol, rotate=False)
    print("rotated squares:")
    rotated_rows, rotated_items = solve_true_squares(states, args.tol, rotate=True)
    rows: list[Row] = []
    for north, turned in zip(north_rows, rotated_rows):
        rows.append({
            "state": north["state"], "abbr": north["abbr"],
            "north_mi": north["side_mi"], "rotated_mi": turned["side_mi"],
            "gain_mi": round(float(turned["side_mi"]) - float(north["side_mi"]), 3),
            "rotation_deg": turned["rotation_deg"],
        })
    out: Path = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path: Path = out / "north_vs_rotated.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer: csv.DictWriter[str] = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: -float(r["gain_mi"])))
    print(f"wrote {csv_path}")
    if args.no_draw:
        return
    gif: Path = draw.draw_flip_gif(north_items, rotated_items, all_states,
                                   out / "north_vs_rotated.gif", COMPARE_TEXT)
    print(f"wrote {gif}")
    ffmpeg: str | None = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg not found, so no MP4 version (install ffmpeg to get one)")
        return
    video: Path = draw.draw_flip_video(north_items, rotated_items, all_states,
                                       out / "north_vs_rotated.mp4", COMPARE_TEXT, ffmpeg=ffmpeg)
    print(f"wrote {video}")


def main() -> None:
    """Parse the command line and run the chosen command."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub: argparse._SubParsersAction[argparse.ArgumentParser] = parser.add_subparsers(required=True)
    for name, func, help_, tol, tol_help in [
        ("aligned", cmd_aligned, "largest latitude/longitude-aligned square", 1e-5,
         "degrees of half-height (default 1e-5, about 7 ft of side)"),
        ("rotated", cmd_rotated, "largest true square at any rotation", 5.0,
         "meters of half-side (default 5, about 30 ft of side)"),
        ("north", cmd_north, "largest true square with sides facing north/east", 5.0,
         "meters of half-side (default 5, about 30 ft of side)"),
        ("compare", cmd_compare, "animated GIF comparing north-facing and rotated squares", 5.0,
         "meters of half-side (default 5, about 30 ft of side)"),
    ]:
        p: argparse.ArgumentParser = sub.add_parser(name, help=help_)
        p.add_argument("states", nargs="*", help="state abbreviations (default: all 50)")
        p.add_argument("--tol", type=float, default=tol, help=f"optimality tolerance in {tol_help}")
        p.add_argument("--out", default=str(ROOT / "out"), help="output directory")
        p.add_argument("--no-draw", action="store_true", help="write only the CSV, no maps or images")
        p.set_defaults(func=func)
    args: argparse.Namespace = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

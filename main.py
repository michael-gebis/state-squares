# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "highspy>=1.7",
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
    uv run main.py compare NM --close-up  # flip-book zoomed in on New Mexico
    uv run main.py pack WI --minutes 60 # 17 equal squares packed into Wisconsin
"""

import argparse
import csv
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import numpy as np
import shapely

ROOT: Path = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from squares import aligned, data, draw, geodesy, packing, rotated  # noqa: E402
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
CLOSEUP_LEGEND: str = ("Dashed: the other square\n"
                       "Blue dots: border points within 50 ft of the square,\n"
                       "which stop it from growing\n"
                       "Land area only (US Census 2024 boundaries)\n"
                       "Lengths on the WGS84 ellipsoid")
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


def write_closeups(out: Path, north_items: list[draw.Placed], rotated_items: list[draw.Placed],
                   rotated_rows: list[Row], all_states: list[data.State]) -> None:
    """For each state, a zoomed-in flip-book between its north-facing and
    rotated squares: an animated GIF and, if ffmpeg is installed, an MP4."""
    ffmpeg: str | None = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg not found, so no MP4 versions (install ffmpeg to get them)")
    for north, turned, row in zip(north_items, rotated_items, rotated_rows):
        rot: float = float(row["rotation_deg"])
        text: draw.FlipText = draw.FlipText(
            title=f"The biggest square in {north.state.name}",
            first_label=COMPARE_TEXT.first_label, second_label=COMPARE_TEXT.second_label,
            legend=CLOSEUP_LEGEND,
            first_note="Sides face north, south, east and west",
            second_note=("Best with no turn" if abs(rot) < 0.05
                         else f"Turned {describe_rotation(rot)}"))
        stem: Path = out / f"{north.state.abbr}_north_vs_rotated"
        gif: Path = draw.draw_closeup_gif(north, turned, all_states, stem.with_suffix(".gif"),
                                          text)
        print(f"wrote {gif}")
        if ffmpeg is not None:
            video: Path = draw.draw_closeup_video(north, turned, all_states,
                                                  stem.with_suffix(".mp4"), text, ffmpeg=ffmpeg)
            print(f"wrote {video}")


def cmd_compare(args: argparse.Namespace) -> None:
    """The `compare` command: how much each state gains by rotating its square,
    as a CSV, an animated GIF that flips between the two maps, and (if ffmpeg
    is installed) the same flip-book as an MP4 video.  With --close-up, one
    zoomed-in flip-book per state instead (and no CSV, so a quick look at a
    few states doesn't replace the full results)."""
    all_states, states = selected_states(args)
    print("north-facing squares:")
    north_rows, north_items = solve_true_squares(states, args.tol, rotate=False)
    print("rotated squares:")
    rotated_rows, rotated_items = solve_true_squares(states, args.tol, rotate=True)
    if args.close_up:
        if not args.no_draw:
            out_dir: Path = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            write_closeups(out_dir, north_items, rotated_items, rotated_rows, all_states)
        return
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


def keep_awake() -> None:
    """Ask Windows not to fall asleep while this program runs, so a long
    search isn't paused overnight.  The request ends when the program does;
    on other systems this does nothing."""
    if sys.platform == "win32":
        import ctypes
        es_continuous: int = 0x80000000
        es_system_required: int = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)


def square_rows(squares: list[rotated.Square]) -> list[Row]:
    """CSV rows for the squares of a packing, one per square, with enough
    precision to rebuild them exactly."""
    rows: list[Row] = []
    for i, sq in enumerate(squares, 1):
        row: Row = {
            "square": i, "side_mi": round(float(sq.sides_mi().mean()), 4),
            "rotation_deg": round(sq.rotation_deg(), 4),
            "center_lat": sq.lat, "center_lon": sq.lon,  # full precision
            "theta_rad": sq.theta, "half_side_m": sq.half_side,
        }
        for j, (lon, lat) in enumerate(sq.corners(), 1):
            row[f"corner{j}_lat"] = round(float(lat), 7)
            row[f"corner{j}_lon"] = round(float(lon), 7)
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[Row]) -> None:
    """Save rows (all with the same columns) as a CSV file."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer: csv.DictWriter[str] = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def cmd_pack(args: argparse.Namespace) -> None:
    """The `pack` command: the biggest `--count` equal squares that fit, all
    together, in each chosen state.  This is a timed random search, so it
    saves a checkpoint whenever it improves, then the final packing (checked
    on the ellipsoid) as a CSV, a PDF page and a PNG, and a summary CSV."""
    all_states, states = selected_states(args)
    out: Path = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    keep_awake()
    n: int = args.count
    summary_path: Path = out / f"pack_{n}_summary.csv"
    summary: dict[str, Row] = {}
    if summary_path.exists():
        with open(summary_path, encoding="utf-8") as f:
            summary = {r["abbr"]: dict(r) for r in csv.DictReader(f)}

    for st in states:
        stem: Path = out / f"{st.abbr}_{n}_squares"
        print(f"{st.abbr}  {st.name}: {n} squares, searching for {args.minutes:g} min "
              f"on {args.workers} workers (seed {args.seed})", flush=True)
        t0: float = time.perf_counter()
        last_report: list[float] = [t0]

        def checkpoint(squares: list[rotated.Square]) -> None:
            """Save the best layout so far (not yet checked on the ellipsoid)."""
            side: float = 2 * squares[0].half_side / geodesy.METERS_PER_MILE
            print(f"    {time.perf_counter() - t0:6.0f}s  new best {side:.3f} mi", flush=True)
            try:
                write_csv(stem.with_name(stem.name + "_checkpoint.csv"), square_rows(squares))
            except OSError as err:  # e.g. the file is open in a spreadsheet
                print(f"    (couldn't save the checkpoint: {err})", flush=True)

        def log(line: str) -> None:
            """Show worker reports in verbose mode, else a heartbeat every five minutes."""
            now: float = time.perf_counter()
            if args.verbose:
                print(f"    {line}", flush=True)
            elif now - last_report[0] >= 300:
                last_report[0] = now
                print(f"    {now - t0:6.0f}s  still searching ({line})", flush=True)

        result: packing.Packing = packing.pack_squares(
            st.geom, n, seconds=60 * args.minutes, seed=args.seed, workers=args.workers,
            log=log, on_best=checkpoint)
        side: float = result.side_mi()
        sides: FloatArray = result.sides_mi()
        area: float = sum(abs(float(geodesy.GEOD.geometry_area_perimeter(
            shapely.Polygon(sq.ring()))[0])) for sq in result.squares)
        state_area: float = abs(float(geodesy.GEOD.geometry_area_perimeter(st.geom)[0]))
        print(f"{st.abbr}  {st.name:<15} {side:8.3f} mi   (map search {result.search_mi:.3f}, "
              f"area limit {result.upper_mi:.2f})   {result.hops:,} hops   "
              f"{result.seconds:5.0f}s", flush=True)
        write_csv(stem.with_suffix(".csv"), square_rows(result.squares))
        print(f"wrote {stem.with_suffix('.csv')}")
        summary[st.abbr] = {
            "state": st.name, "abbr": st.abbr, "count": n,
            "side_mi": round(side, 4), "side_km": round(side * KM_PER_MILE, 4),
            "area_limit_mi": round(result.upper_mi, 4),
            "coverage_pct": round(100 * area / state_area, 2),
            "minutes": args.minutes, "workers": result.workers, "hops": result.hops,
            "seed": args.seed,
        }
        write_csv(summary_path, sorted(summary.values(), key=lambda r: str(r["state"])))
        print(f"wrote {summary_path}")
        if args.no_draw:
            continue
        edge, touching = packing.contacts(st.geom, result.squares)
        details: list[str] = [
            f"Side: {side:.2f} mi  ({side * KM_PER_MILE:.2f} km)",
            f"    all {4 * n} sides agree within {max(1, round(np.ptp(sides) * 5280))} ft",
            f"Area limit: {result.upper_mi:.2f} mi",
            f"    {n} squares that big would cover",
            "    the whole state, so none can beat it",
            f"Squares cover {100 * area / state_area:.1f}% of the state",
            f"Search: {result.seconds / 60:.0f} min on {result.workers} cores, "
            f"{result.hops:,} hops",
            "    best found, not proven best",
            f"Edge contacts: {len(edge)} boundary points",
            f"Square-to-square contacts: {len(touching)}",
        ]
        for path in draw.draw_packing(
                st, [sq.ring() for sq in result.squares], edge, touching, all_states, stem,
                f"{n} equal squares packed into {st.name} · {SOURCE_NOTE}",
                f"{n} squares, {side:.1f} miles on a side", details,
                "Blue dots: state boundary points within 100 ft of a square.\n"
                "Black marks: where two squares touch (within 100 ft).\n"
                "Each is a true square (great-circle sides, equal corner\n"
                "angles), free to rotate."):
            print(f"wrote {path}")


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
        if name == "compare":
            p.add_argument("--close-up", action="store_true",
                           help="instead of the US map and CSV, a zoomed-in flip-book of each "
                                "state (ABBR_north_vs_rotated.gif and .mp4)")
        p.set_defaults(func=func)
    pack: argparse.ArgumentParser = sub.add_parser(
        "pack", help="biggest COUNT equal squares that fit together in each state")
    pack.add_argument("states", nargs="*", help="state abbreviations (default: all 50)")
    pack.add_argument("--count", type=int, default=17, help="how many squares (default 17)")
    pack.add_argument("--minutes", type=float, default=5.0,
                      help="search time per state (default 5)")
    pack.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                      help="processes searching in parallel (default: one per CPU)")
    pack.add_argument("--seed", type=int, default=0, help="random seed (default 0)")
    pack.add_argument("--verbose", action="store_true", help="report every finished run")
    pack.add_argument("--out", default=str(ROOT / "out" / "pack"), help="output directory")
    pack.add_argument("--no-draw", action="store_true", help="write only the CSVs, no PDF or PNG")
    pack.set_defaults(func=cmd_pack)
    args: argparse.Namespace = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

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
    uv run main.py nest CA              # the biggest other state that fits in California
"""

import argparse
import csv
import os
import shutil
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeAlias

import numpy as np
import shapely

ROOT: Path = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from squares import aligned, data, draw, geodesy, nesting, packing, rotated  # noqa: E402
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


SQ_METERS_PER_SQ_MILE: float = geodesy.METERS_PER_MILE**2
NEST_LEGEND: str = ("Each state is slid and turned, never flipped or stretched,\n"
                    "and every island has to land on the host's land.\n"
                    "Areas are of the outlines: land plus inland lakes\n"
                    "and rivers (US Census 2024 boundaries).")
NEST_SOURCE: str = ("US Census 2024 cartographic boundaries (1:500,000), "
                    "shapes moved on the WGS84 ellipsoid")
# How many of the bigger states that don't fit to list on the page.
NEST_MISSES_SHOWN: int = 8
NEST_MAP_TITLE: str = "The biggest state that fits inside each state"
NEST_MAP_NOTE: str = ("Each one drawn at its real size, slid and turned (never flipped or "
                      "stretched), with every island on the host's land. Biggest by the area of "
                      "its outline (land plus inland lakes and rivers). Found by a search, not "
                      "proven best. US Census 2024 cartographic boundaries (1:500,000); shapes "
                      "moved on the WGS84 ellipsoid.")
NEST_SOCIAL_SUBTITLE: str = "Drawn at real size · slid and turned, never flipped or stretched"
NEST_SOCIAL_CREDIT: str = "US Census Bureau 2024 boundaries · shapes moved on the WGS84 ellipsoid"


def describe_turn(deg: float) -> str:
    """Put a state's turn into words, e.g. "turned 45.5° counterclockwise"."""
    if abs(deg) < 0.05:
        return "not turned"
    return f"turned {abs(deg):.1f}° {'counterclockwise' if deg > 0 else 'clockwise'}"


def sq_mi(area_m2: float) -> float:
    """Square meters to square miles."""
    return area_m2 / SQ_METERS_PER_SQ_MILE


def nest_rows(host: data.State, host_area: float, fits: list[nesting.Fit]) -> list[Row]:
    """CSV rows for every state tried inside a host, biggest first, with
    enough precision to rebuild each placement."""
    rows: list[Row] = []
    for fit in fits:
        p: nesting.Placement = fit.placement
        rows.append({
            "host": host.abbr, "guest": fit.guest.abbr, "guest_name": fit.guest.name,
            "guest_area_sq_mi": round(sq_mi(fit.area)),
            "pct_of_host_area": round(100 * fit.area / host_area, 2),
            "best_scale_pct": round(100 * p.scale, 2), "fits": "yes" if fit.fits else "no",
            "center_lat": p.lat, "center_lon": p.lon,  # full precision
            "theta_rad": p.theta, "rotation_deg": round(p.rotation_deg(), 2),
            "seconds": round(fit.seconds, 1),
        })
    return rows


def describe_time(seconds: float) -> str:
    """A search time in words: seconds if under a minute and a half, else
    minutes."""
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min"


def nest_outcome(fits: list[nesting.Fit]) -> tuple[nesting.Fit | None, list[nesting.Fit]]:
    """From the guests tried in a host (biggest first): the biggest one that
    fits (None if none does), and the bigger ones that don't, closest call
    first."""
    winner: nesting.Fit | None = next((f for f in fits if f.fits), None)
    misses: list[nesting.Fit] = sorted(
        (f for f in fits if not f.fits and (winner is None or f.area > winner.area)),
        key=lambda f: -f.placement.scale)
    return winner, misses


def nest_details(host: data.State, host_area: float, winner: nesting.Fit | None,
                 misses: list[nesting.Fit], seconds: float, workers: int) -> list[str]:
    """The text beside the map: the winner's numbers, then the bigger states
    that don't fit and how close they come."""
    lines: list[str] = []
    if winner:
        g: data.State = winner.guest
        room: float = 100 * (winner.placement.scale - 1)
        lines += [f"{g.name}: {sq_mi(winner.area):,.0f} sq mi,",
                  f"    {100 * winner.area / host_area:.0f}% of {host.name}'s "
                  f"{sq_mi(host_area):,.0f}",
                  describe_turn(winner.placement.rotation_deg()).capitalize(),
                  f"Room to spare: it fits even {room:.1f}% bigger" if room >= 0.05
                  else "A tight fit: under 0.05% to spare",
                  "    (dashed outline)", ""]
    if misses:
        lines += ["Bigger states that don't fit, and the" if winner
                  else "Smaller states, and the biggest",
                  "biggest copy of each that does:" if winner else "copy of each that fits:"]
        lines += [f"    {f.guest.name:<16}{100 * f.placement.scale:5.1f}%"
                  for f in misses[:NEST_MISSES_SHOWN]]
        if len(misses) > NEST_MISSES_SHOWN:
            lines.append(f"    and {len(misses) - NEST_MISSES_SHOWN} more")
        lines.append("")
    lines += [f"Search: {describe_time(seconds)} on {workers} cores",
              "    best found, not proven best"]
    return lines


def nest_summary_row(host: data.State, host_area: float, winner: nesting.Fit | None,
                     closest: nesting.Fit | None, tried: int, seconds: float,
                     workers: int) -> Row:
    """One host's row in nest_summary.csv (blank where there is no winner
    or no miss)."""
    return {
        "state": host.name, "abbr": host.abbr,
        "biggest_fit": winner.guest.name if winner else "",
        "biggest_fit_abbr": winner.guest.abbr if winner else "",
        "fit_area_sq_mi": round(sq_mi(winner.area)) if winner else "",
        "fit_pct_of_area": round(100 * winner.area / host_area, 2) if winner else "",
        "fit_scale_pct": round(100 * winner.placement.scale, 2) if winner else "",
        "rotation_deg": round(winner.placement.rotation_deg(), 2) if winner else "",
        "closest_miss": closest.guest.name if closest else "",
        "closest_miss_scale_pct": round(100 * closest.placement.scale, 2) if closest else "",
        "tried": tried, "minutes": round(seconds / 60, 1), "workers": workers,
    }


def load_fits(out: Path, host: data.State, states: list[data.State]) -> list[nesting.Fit]:
    """The guests tried in a host, biggest first, rebuilt from its saved CSV
    (empty if it has none).  Scales come back rounded to 0.01%."""
    path: Path = out / f"{host.abbr}_nest.csv"
    if not path.exists():
        return []
    by_abbr: dict[str, data.State] = {s.abbr: s for s in states}
    fits: list[nesting.Fit] = []
    for r in read_rows(path):
        guest: data.State = by_abbr[r["guest"]]
        place: nesting.Placement = nesting.Placement(
            float(r["center_lon"]), float(r["center_lat"]), float(r["theta_rad"]),
            float(r["best_scale_pct"]) / 100)
        fits.append(nesting.Fit(guest, place, r["fits"] == "yes", nesting.area(guest),
                                float(r["seconds"])))
    return fits


def draw_nest_page(out: Path, host: data.State, fits: list[nesting.Fit], seconds: float,
                   workers: int, states: list[data.State]) -> list[Path]:
    """Draw one host's page (ABBR_nest.pdf and .png) from its search results:
    the biggest state that fits, with the biggest copy of it that fits
    dashed; or, if none fits, the closest call at the biggest size that
    fits.  Returns the paths written (none if no state was tried)."""
    winner, misses = nest_outcome(fits)
    shown: nesting.Fit | None = winner or (misses[0] if misses else None)
    if shown is None:
        return []
    outline: nesting.Outline = nesting.Outline(shown.guest)
    place: nesting.Placement = shown.placement
    scale: float = 1.0 if winner else place.scale
    label: str = shown.guest.name if winner else f"{shown.guest.name}\nat {100 * scale:.1f}%"
    host_area: float = nesting.area(host)
    return draw.draw_nesting(
        host, outline.moved(replace(place, scale=scale)), outline.moved(place) if winner else None,
        label, states, out / f"{host.abbr}_nest",
        f"The biggest state that fits inside {host.name} · {NEST_SOURCE}",
        f"Biggest state that fits: {shown.guest.name}" if winner else "No other state fits",
        nest_details(host, host_area, winner, misses, seconds, workers), NEST_LEGEND)


def cmd_nest(args: argparse.Namespace) -> None:
    """The `nest` command: for each chosen state, the biggest other state
    that fits inside it, slid and turned but never flipped or stretched.
    Bigger states are tried first.  Saves a CSV of every state tried, a
    summary CSV, and a PDF page and PNG showing the winner in place; then
    redraws the US map from every state searched so far.  With --redraw,
    searches nothing and redraws the pages and the map from saved results."""
    all_states, states = selected_states(args)
    out: Path = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    keep_awake()
    summary_path: Path = out / "nest_summary.csv"
    summary: dict[str, Row] = {}
    if summary_path.exists():
        summary = {r["abbr"]: dict(r) for r in read_rows(summary_path)}

    for host in [] if args.redraw else states:
        print(f"{host.abbr}  {host.name}: trying the bigger states first, "
              f"on {args.workers} workers", flush=True)
        t0: float = time.perf_counter()
        fits: list[nesting.Fit] = nesting.nest(
            host, all_states, args.workers, every=args.all,
            log=lambda line: print(f"    {line}", flush=True))
        seconds: float = time.perf_counter() - t0
        host_area: float = nesting.area(host)
        winner, misses = nest_outcome(fits)
        closest: nesting.Fit | None = misses[0] if misses else None
        summary[host.abbr] = nest_summary_row(host, host_area, winner, closest, len(fits),
                                              seconds, args.workers)
        write_csv(summary_path, sorted(summary.values(), key=lambda r: str(r["state"])))
        if not fits:
            print(f"{host.abbr}  {host.name}: no smaller state to try")
            continue
        csv_path: Path = out / f"{host.abbr}_nest.csv"
        write_csv(csv_path, nest_rows(host, host_area, fits))
        print(f"{host.abbr}  {host.name:<15} biggest that fits: "
              f"{winner.guest.name if winner else 'none'}"
              + (f" ({100 * winner.placement.scale:.1f}%)" if winner else "")
              + (f"; closest miss: {closest.guest.name} ({100 * closest.placement.scale:.1f}%)"
                 if closest else "") + f"   {seconds:5.0f}s", flush=True)
        print(f"wrote {csv_path}")
        print(f"wrote {summary_path}")
        if not args.no_draw:
            for path in draw_nest_page(out, host, fits, seconds, args.workers, all_states):
                print(f"wrote {path}")
    if args.redraw:
        for host in states:
            row: Row | None = summary.get(host.abbr)
            if row is None:
                continue
            for path in draw_nest_page(out, host, load_fits(out, host, all_states),
                                       60 * float(row["minutes"]), int(row["workers"]),
                                       all_states):
                print(f"wrote {path}")
    if not args.no_draw:
        for path in draw_nest_maps(out, all_states):
            print(f"wrote {path}")


def load_nested(out: Path, states: list[data.State]) -> list[draw.Nested]:
    """Every state searched so far (from nest_summary.csv), with the biggest
    state that fits moved into place, rebuilt from its saved placement."""
    summary_path: Path = out / "nest_summary.csv"
    if not summary_path.exists():
        return []
    by_abbr: dict[str, data.State] = {s.abbr: s for s in states}
    items: list[draw.Nested] = []
    for row in read_rows(summary_path):
        host: data.State = by_abbr[row["abbr"]]
        guest_abbr: str = row.get("biggest_fit_abbr") or ""
        if not guest_abbr:
            items.append(draw.Nested(host, None, None))
            continue
        saved: dict[str, str] = next(r for r in read_rows(out / f"{host.abbr}_nest.csv")
                                     if r["guest"] == guest_abbr)
        place: nesting.Placement = nesting.Placement(
            float(saved["center_lon"]), float(saved["center_lat"]), float(saved["theta_rad"]))
        guest: data.State = by_abbr[guest_abbr]
        items.append(draw.Nested(host, guest, nesting.Outline(guest).moved(place)))
    return items


def read_rows(path: Path) -> list[dict[str, str]]:
    """The rows of a CSV file, as dictionaries."""
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def draw_nest_maps(out: Path, states: list[data.State]) -> list[Path]:
    """Draw the US map of the biggest state that fits inside each state
    searched so far: nest_map.svg and .pdf, and nest_social.png.  Returns the
    paths written (none if nothing has been searched)."""
    items: list[draw.Nested] = load_nested(out, states)
    if not items:
        return []
    return [*draw.draw_nest_map(items, states, out / "nest_map", NEST_MAP_TITLE, NEST_MAP_NOTE),
            draw.draw_nest_social(items, states, out / "nest_social.png", NEST_MAP_TITLE,
                                  NEST_SOCIAL_SUBTITLE, NEST_SOCIAL_CREDIT)]


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
    nest: argparse.ArgumentParser = sub.add_parser(
        "nest", help="biggest other state that fits inside each state")
    nest.add_argument("states", nargs="*", help="host state abbreviations (default: all 50)")
    nest.add_argument("--all", action="store_true",
                      help="try every smaller state, not just down to the biggest that fits")
    nest.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                      help="processes searching in parallel (default: one per CPU)")
    nest.add_argument("--out", default=str(ROOT / "out" / "nest"), help="output directory")
    nest.add_argument("--no-draw", action="store_true",
                      help="write only the CSVs, no pages or maps")
    nest.add_argument("--redraw", action="store_true",
                      help="don't search; redraw the pages and the US map from saved results")
    nest.set_defaults(func=cmd_nest)
    args: argparse.Namespace = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

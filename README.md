# Largest squares in US states

What is the largest square that fits inside each US state? It sounds like a
simple question, but on a curved Earth "square" can mean several different
things, and the answers differ by as much as 46 miles (in Alaska). This
project computes three versions for all 50 states and draws them as maps:

![The biggest square that fits in each state, free to rotate, with its side length in miles](docs/rotated_social.png)

| Command | What it finds |
|---|---|
| `aligned` | The largest box bounded by two **parallels and two meridians** (lines of latitude and longitude), sized so its height equals its width at its middle latitude. Not truly square: its north edge is shorter than its south edge. |
| `north` | The largest **true square** whose sides face north, south, east and west. |
| `rotated` | The largest **true square at any rotation**. |
| `compare` | An **animated GIF** (and MP4 video) flipping between the `north` and `rotated` maps, showing which squares grow by turning and by how much. |

Each search is a branch-and-bound that finds the *best possible* square, not
just a good one, to within a stated tolerance of a few feet to a few tens of
feet (subject to the caveats under [Guarantees](#guarantees-and-their-limits)).

Facing north versus free to rotate (darker squares gain more by turning):

![Animation flipping between the biggest north-facing square and the biggest rotated square in each state](docs/north_vs_rotated.gif)

## Quick start

You need [uv](https://docs.astral.sh/uv/). `main.py` lists its own dependencies
([PEP 723](https://peps.python.org/pep-0723/) inline script metadata), so uv
builds a matching environment in its cache automatically, with no install
step. (For running the tests, see [Development](#development).)

```sh
git clone https://github.com/michael-gebis/state-squares.git
cd state-squares
uv run main.py aligned              # all 50 states
uv run main.py north
uv run main.py rotated
uv run main.py compare              # north vs rotated, as a GIF
uv run main.py rotated WI CO TX     # just these states
uv run main.py rotated --help       # all options
```

The first run downloads the Census boundary file (about 3 MB) into `data/`.
Options:

- `STATES...`: postal abbreviations to compute (default: all 50)
- `--out DIR`: where to write results (default `out/`)
- `--no-draw`: write only the CSV, skip the maps
- `--tol X`: how close to optimal the search must get (see [Accuracy](#accuracy))

Each command writes, with `PREFIX` being `aligned`, `north` or `rotated`:

| File | Contents |
|---|---|
| `PREFIX_squares.svg`, `.pdf` | Detailed US map with every state's square and its side length; Alaska and Hawaii as insets |
| `PREFIX_social.png` | A simplified 2400×1350 version of the map for social media (see [below](#social-media-images)) |
| `PREFIX_states.pdf` | A ranked table, then one zoomed page per state with the square, the boundary points that stop it growing, guide lines, a scale bar, and the numbers |
| `PREFIX_squares.csv` | All the numbers: side length, center, corners, rotation, edge widths |

`compare` instead writes `north_vs_rotated.gif` (see [below](#comparison-gif)),
the same flip-book as `north_vs_rotated.mp4` for sites that take video rather
than GIFs, such as Threads (only if [ffmpeg](https://ffmpeg.org/) is
installed), and `north_vs_rotated.csv`, which lists each state's north-facing
and rotated side lengths and the gain, biggest gain first.

On a typical desktop the searches take about 1½ minutes for `aligned`, 15
seconds for `north` and 4–5 minutes for `rotated` (so about 5 for `compare`),
plus about a minute to draw the outputs.

## Results

Side lengths in miles for a selection of states (the CSVs have all 50).
Rotation is counterclockwise from north.

| State | `aligned` (north / south edge) | `north` | `rotated` (tilt) |
|---|---|---|---|
| Alaska | 552.2 (466.7 / 635.0) | 505.8 | 521.8 (8.2°) |
| New Mexico | 341.4 (331.0 / 351.1) | 333.4 | 333.9 (−1.7°) |
| Texas | 322.0 (313.8 / 329.7) | 324.8 | **341.8** (43.6°) |
| Montana | 275.8 (265.4 / 285.9) | 274.2 | 275.3 (0.9°) |
| Colorado | 275.7 (267.8 / 283.3) | 273.9 | 274.1 (−0.1°) |
| California | 196.3 (192.1 / 200.4) | 196.0 | 210.4 (−42.0°) |
| Wisconsin | 156.3 (153.2 / 159.3) | 157.7 | 167.5 (15.4°) |
| Florida | 109.0 (108.2 / 109.8) | 108.7 | 113.6 (20.9°) |
| West Virginia | 91.6 (90.7 / 92.4) | 91.5 | 105.3 (−43.4°) |
| Rhode Island | 20.5 (20.5 / 20.6) | 20.5 | 20.6 (1.4°) |

### Why the aligned box can beat a true square

Freedom to rotate can only help, so `rotated` is always at least as big as
`north`. But Alaska's aligned box (552 mi) is bigger than its rotated true
square (522 mi). That is because the aligned box isn't really a square:
meridians converge toward the pole, so at Alaska's latitude its north edge is
467 miles and its south edge 635. The 552 is only its height. A trapezoid
like that has more room than any true square of the same height. The effect
grows with latitude, which is why the tall, rectangular western states
(Colorado, Wyoming, Utah, Montana, New Mexico) also come out slightly smaller
as true squares.

### The Wisconsin "158 miles" puzzle

This project started from someone else's informal claim that the largest
aligned square in Wisconsin is about 158 miles. The aligned box here is 156.3,
and no reasonable variant of the aligned box reproduces 158:

| Variant | Wisconsin (mi) |
|---|---|
| Aligned box, middle-latitude definition (the default) | 156.3 |
| ... with 1:5,000,000 or 1:20,000,000 Census boundaries | 156.6 / 156.8 |
| ... measured on a sphere instead of the WGS84 ellipsoid | 156.4 |
| ... defining "square" by the north edge / the south edge | ≈153.8 / ≈159.2 |
| Flat square in Wisconsin Transverse Mercator (EPSG:3071) | 157.4 |
| Flat square in UTM zone 16N | 159.4 |
| Flat square in Web Mercator, measured in map units | 219.8 |
| **True square facing north (`north`)** | **157.7** |

The north-facing true square rounds to 158, so that is most likely what the
original computation measured, perhaps without its author realizing it differs
from a latitude/longitude box.

## Design decisions

### Boundary data

- **Source:** the US Census Bureau's 2024
  [cartographic boundary file](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html)
  for states at 1:500,000 (`cb_2024_us_state_500k`), the most detailed
  generalized version.
- **Land only.** The cartographic files are clipped to the shoreline. The
  alternative, legal boundaries (TIGER/Line), run out to the middle of the
  Great Lakes and into coastal waters; a Wisconsin square could then extend
  into Lake Michigan. Land seemed closer to what people mean.
- **Inland lakes count as land.** The file has no holes, so lakes that don't
  connect to the sea (the Great Salt Lake, Lake Winnebago) are part of their
  state.
- **One piece at a time.** A square must fit inside a single polygon; 28
  states have several (Michigan's peninsulas, islands; Alaska has 549).
- **Resolution matters a little.** Coarser outlines smooth away small inlets,
  so squares grow slightly: Wisconsin is 156.3 at 1:500k and 156.8 at 1:20M.
- **Boundary segments are straight in latitude/longitude**, which is how the
  file defines them (borders along parallels, like 49°N, are straight lines in
  latitude/longitude).
- **Only the 50 states**: DC and the territories are skipped.
- The maps leave out the Northwestern Hawaiian Islands for legibility. That
  affects only the drawing; the computation uses all of Hawaii.

### Earth model

- All lengths are on the **WGS84 ellipsoid**. A sphere would change lengths
  by up to a few tenths of a percent, depending on latitude and direction (0.1
  mile for Wisconsin's box): small, but enough to tip some results across a
  rounding boundary. The Census data uses NAD83, which matches WGS84 to about
  a meter.
- **Aligned box:** the north–south side is a meridian arc. Its east–west
  extent is measured along parallels, so the north edge, middle and south edge
  all differ. The box counts as square when its height equals its width at
  its middle latitude. To first order that is the same as being square on a
  Mercator map. Defining it by the north or south edge instead changes
  Wisconsin by about ±3 miles.
- **True square:** a "square" with straight sides doesn't exist on a curved
  surface. The definition used here: four sides along **great ellipses** (the
  ellipsoid's version of great circles: where a plane through the Earth's
  center cuts its surface), arranged so the
  [gnomonic projection](https://en.wikipedia.org/wiki/Gnomonic_projection)
  centered on the square shows it as an exact square. On a sphere this is
  precisely a regular spherical quadrilateral: four equal sides and four equal
  angles, each slightly over 90° (90.07° for Colorado's square). On the
  ellipsoid the four sides, measured as true shortest paths (geodesics) between
  the corners, agree to within about 5 feet even for Alaska.
- **Great ellipses vs geodesics:** a great-ellipse side strays up to about 25 m
  from the geodesic between the same corners on Alaska's 520-mile square, and
  about 5 m on Wisconsin's. That is far below the 0.1-mile precision of the
  results, and great ellipses have a decisive advantage: they appear as
  straight lines in the gnomonic projection, which makes the search exact.

### Social-media images

A map with 50 numbers on it is hard to read once a timeline shrinks it to
phone width, so the `_social.png` version is drawn separately rather than
being a scaled-down copy of the detailed map:

- **16:9 at 2400×1350.** X/Twitter shows that shape uncropped, and the
  resolution stays sharp when someone taps to expand it.
- **One bold number per square**, sized to fill the square (up to 34 pt). The
  unit appears once, in the subtitle, instead of "mi" on every label. State
  abbreviations appear only in squares big enough to hold a second line.
- **Callouts only for squares too small for a 7 pt number** (the small
  northeastern states and Hawaii). An earlier version that moved anything
  under 9.5 pt to callouts needed 17 of them, and their leader lines crossed
  half the map.
- **A short, large title** and heavier borders, so the image still reads at a
  glance at 600 px wide.

At 600 px wide (a desktop timeline) nearly every number is readable; at phone
width the title and the larger states read, and the small eastern ones need a
tap to expand.

### Comparison GIF

`compare` makes a two-frame GIF that flips once a second between the
north-facing and rotated squares, built so the changes are easy to spot:

- Everything except the squares stays put between frames: same map, same
  layout, and callouts pinned in place (rather than following the squares) so
  they don't jitter.
- Each frame shows the *other* version's square as a **dashed outline**, so
  even a paused frame shows where each square moves.
- Squares are **shaded by how much they gain** in both frames: pale if
  rotating doesn't change the rounded length, deepening to strong orange for
  gains of 12 miles or more.
- The rotated frame labels each gain (**+17**) in green on a small white badge,
  readable on any shade, and a colored banner names the current frame.
- At 1600×900 it's about 260 KB. Both frames share one 256-color palette, so
  colors don't shimmer as it flips, and the background is mapped to pure white
  explicitly, because Pillow's palette matching would otherwise turn it light
  gray.

The **MP4 version** follows Meta's published video requirements for Threads:

- H.264 (High profile) in 4:2:0 color at 1920×1080, the widest Threads takes,
  and 30 fps, with a keyframe at every flip;
- the index ("moov atom") at the front of the file and no edit lists;
- a silent AAC track, since some upload paths reject videos with no audio;
- color tagged as BT.709, so players don't shift the oranges and greens.

Videos don't loop on their own the way GIFs do, so it repeats the flip eight
times (16 seconds, about 4 MB). One quirk: the AAC encoder's 21 ms lead-in
makes the first frame show 21 ms longer, because without an edit list
there's nowhere to record the offset. It's invisible, and the stream is
still exactly 30 fps.

## How the search works

### The key trick: square distance

For a square centered at the origin with sides along the axes, the largest one
that avoids a point (u, v) has half-side max(|u|, |v|), the
[L∞ or Chebyshev distance](https://en.wikipedia.org/wiki/Chebyshev_distance).
So for a *given* center (and rotation), the biggest square is set exactly by
the boundary point with the smallest L∞ distance. That is a cheap, vectorized
calculation over every boundary segment (`squares/linf.py`). What remains is
to search over centers (and rotations) for the best one.

### Aligned boxes (`squares/aligned.py`)

Dividing longitude offsets by the local "aspect" (how many degrees of
longitude match one degree of latitude in ground distance) turns each aligned
box into a square in a stretched latitude/longitude plane, so the trick above
applies directly.

The best center comes from a branch-and-bound in the style of Mapbox's
[polylabel](https://github.com/mapbox/polylabel):

1. Cover the polygon with a grid of cells.
2. For each cell, compute the best box at its center, plus a provable upper
   limit for any center within the cell.
3. Drop cells whose limit can't beat the best found so far.
4. Split the survivors into quarters and repeat until nothing is left.

The upper limit accounts for the aspect changing with latitude, so the result
is guaranteed to be within the tolerance of the true optimum.

### True squares (`squares/rotated.py`)

For each candidate center, the boundary is projected onto the plane touching
the ellipsoid there, using central projection from the Earth's center (the
gnomonic projection). Great ellipses become straight lines, so the square is an
exact square in this plane. Rotating the plane's axes by the candidate
rotation, the L∞ trick gives the largest square exactly.

The branch-and-bound now runs over three dimensions (longitude, latitude,
rotation). Each round splits cells either by position (into quarters) or by
rotation (into halves), whichever contributes more uncertainty. The upper
limit for a cell allows for four effects:

- moving the center within the cell (at most the cell's size, `t`);
- turning the square, which changes an L∞ distance `d` by at most √2·d per
  radian;
- meridian convergence: "north" points in slightly different directions at
  different centers in a cell;
- the small distortion between neighboring tangent planes.

**Speed:** each round also works out which boundary segments could still be
nearest to *any* square in a surviving cell, using a lower bound on each
segment's distance, and later rounds look only at those. After a few rounds
that is a handful of segments near the touching points instead of tens of
thousands of coastline segments. This took Alaska (about 50,000 boundary
vertices) from not finishing after several minutes to about 20 seconds.

**Detail:** the boundary is densified to segments of at most 0.02° before
projecting. Segments that are straight in latitude/longitude are slightly
curved in the tangent plane, and short pieces keep that error to centimeters.

The final square is recomputed against every boundary segment, as a
safeguard against a bug in the pruning.

### Guarantees and their limits

- **Aligned:** the bound is derived exactly, so the result is provably within
  the tolerance of the best possible.
- **True squares:** the bounds for the second-order effects (tangent-plane
  distortion, convergence) use generous safety factors (5×, 1.2×, ...) rather
  than tight derivations. They are conservative by a wide margin at state
  scale, but they are not formal proofs. Two empirical checks back them up:
  - for every state, the `north` square is no bigger than the `rotated` one,
    so the two searches agree with each other;
  - during development, all 150 results (50 states × 3 kinds), shrunk by one
    part in 100,000, were confirmed to lie inside their states using shapely,
    which is entirely independent of the search code;
  - the test suite checks the solvers against shapes with known answers and
    against brute-force searches (see [Development](#development)).
- **Position vs size:** the side length is tight, but where sliding the square
  barely changes its size (along a nearly straight border, for example) its
  position can be off by a few tens of feet. The detail pages therefore mark
  boundary points within 50 feet of the square as "touching".

### Accuracy

| Mode | Default `--tol` | Side length within |
|---|---|---|
| `aligned` | 1e-5 degrees of half-height | about 7 ft |
| `north`, `rotated` | 5 m of half-side | about 33 ft |

Results are reported to 0.1 mile, so these tolerances don't affect the
published numbers. Differences in boundary data, such as which file or what
resolution, matter far more than the tolerance.

## Project layout

```
main.py              command-line entry point (uv script with inline dependencies)
squares/
  data.py            download and load the Census state boundaries
  geodesy.py         WGS84 ellipsoid formulas
  linf.py            L-infinity distance from a point to line segments
  aligned.py         largest latitude/longitude-aligned box
  rotated.py         largest true square, rotated or facing north
  draw.py            maps, social PNGs, comparison GIF and MP4, detail pages
  arrays.py          names for the NumPy array types used in type hints
tests/               pytest suite (fully type-annotated too)
docs/                images shown in this README
pyproject.toml       project metadata, development tools, pytest and mypy settings
```

## Development

`pyproject.toml` declares the same dependencies as `main.py`, plus pytest and
mypy, so `uv run` sets up a development environment (in `.venv/`) the first
time:

```sh
uv run pytest                  # the whole suite, about 40 seconds
uv run pytest -m "not slow"    # skip the tests that use the real Census data
uv run mypy                    # strict type check of main.py, squares/ and tests/
```

The suite has 123 tests:

- **Formulas:** the ellipsoid formulas are checked against published WGS84
  values (quarter meridian, equator length, polar radius).
- **Distance routine:** checked against hand-worked cases, including the
  awkward ones (zero-length segments, segments parallel to a diagonal), and
  against brute-force sampling.
- **Solvers on shapes with known answers:** a polygon that is itself a true
  square gives back that square; a diamond gives a north-facing square with a
  side of half its diagonal; narrow strips give boxes limited by their width.
- **Solvers against independent checks:** brute-force grid searches, random
  squares, and shapely containment.
- **Output:** the drawing and command-line code is tested end to end on
  made-up states, so those tests need no download.
- **Real data:** tests marked `slow` pin down the published numbers for
  Wisconsin, Colorado, Texas and Rhode Island using the real Census
  boundaries, downloading the file on first use.

The code is fully type-annotated, including local variables. The exceptions
are tuple unpacking and loop variables, which Python can't annotate inline;
their types follow from the annotated functions that produce them, and mypy's
strict mode checks them.

## License

[MIT](LICENSE). Everything this project depends on is compatible with that.
None of it is copied into this repository; it is all installed separately:

| Package | License |
|---|---|
| numpy | BSD-3-Clause |
| shapely | BSD-3-Clause (its wheels include the GEOS library: LGPL-2.1) |
| pyproj | MIT (its wheels include the PROJ library: MIT) |
| pyshp | MIT |
| matplotlib | Matplotlib license (PSF-based, BSD-compatible) |
| pillow | MIT-CMU |
| installed by the above | fonttools (MIT), pyparsing (MIT), six (MIT), contourpy (BSD-3-Clause), kiwisolver (BSD), cycler (BSD), packaging (Apache-2.0 or BSD-2-Clause), python-dateutil (Apache-2.0 or BSD), certifi (MPL-2.0) |
| development only | pytest (MIT), mypy (MIT), types-shapely (Apache-2.0), typing_extensions (PSF-2.0), pathspec (MPL-2.0) |

GEOS, certifi and pathspec use "weak copyleft" licenses (LGPL and MPL). Their
conditions cover distributing modified copies of those libraries themselves,
not programs that merely use them, so they place no restrictions on this
code. The optional MP4 output runs the separately installed `ffmpeg` program
(typically a GPL build) as an external command; the code never links to
ffmpeg or includes any of it, so this doesn't affect the license either. The Census boundary data is a US government work in the public domain,
and the branch-and-bound borrows only polylabel's *idea*, not its code.

## Credits

- Boundaries: US Census Bureau, cartographic boundary files (public domain).
- Search strategy inspired by Mapbox's
  [polylabel](https://github.com/mapbox/polylabel).
- Built on [NumPy](https://numpy.org/), [Shapely](https://shapely.readthedocs.io/),
  [pyproj](https://pyproj4.github.io/pyproj/), [pyshp](https://github.com/GeospatialPython/pyshp)
  and [Matplotlib](https://matplotlib.org/).

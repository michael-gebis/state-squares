"""Names for the kinds of NumPy arrays passed around the package."""

from typing import TypeAlias

import numpy as np
import numpy.typing as npt

# Coordinates, distances, angles.
FloatArray: TypeAlias = npt.NDArray[np.float64]
# Positions in other arrays, e.g. which boundary segments to look at.
IntArray: TypeAlias = npt.NDArray[np.intp]
# Yes/no flags, e.g. which search cells to keep.
BoolArray: TypeAlias = npt.NDArray[np.bool_]
# Four parallel arrays describing line segments: (x0, y0, x1, y1).
Segments: TypeAlias = tuple[FloatArray, FloatArray, FloatArray, FloatArray]

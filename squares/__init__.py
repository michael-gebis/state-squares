"""Largest squares that fit inside US states.

Modules:
    data     download and load state boundaries
    geodesy  WGS84 ellipsoid formulas
    linf     the distance calculation at the heart of both solvers
    aligned  largest box bounded by parallels and meridians
    rotated  largest true square, at any rotation or facing north
    draw     SVG/PDF maps and per-state detail pages
"""

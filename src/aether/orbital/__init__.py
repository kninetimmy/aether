"""Orbital propagation + coordinate transforms for the CelesTrak adapter (M6.5).

Pure-Python, deterministically testable helpers that turn an SGP4 TEME state vector
into the observer-relative geometry the COP needs (azimuth / elevation / slant range)
and the sub-satellite point (lat / lon / altitude) for the map. The SGP4 propagation
itself lives in :mod:`aether.orbital.sgp4_propagate`, behind the optional ``[orbital]``
capability gate; the transforms here import nothing optional (PRD §11.14, §18.12).
"""

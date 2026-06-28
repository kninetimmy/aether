"""Canonical watchlist identity key derivation (PRD §24.6, §21.5).

The watchlist key is the stable, deterministic identity string that the client mints
via ``watchlistKey()`` in ``selectors.ts`` (lines ~307-329) and the backend
recomputes via :func:`watchlist_key` from a Pydantic record.  Both sides MUST agree
for a key the client PUTs to match a record the engine evaluates.

Branch-for-branch parity with the TypeScript implementation:

1. Prefer truthy ``correlation_key`` (empty-string falsy on both sides).
2. Else dispatch on ``track_type``:
   - ``aircraft`` → ``aircraft:icao:{lower(icao || hex)}``
   - ``vessel`` → ``mmsi:{mmsi}``
   - ``aprs_station`` / ``aprs_object`` → ``aprs:{label}``
3. Else raw ``record.id`` (last-resort; no stable identity).
4. Non-track records → ``None`` (the TS ``watchlistKey`` is only called on TrackRecords;
   the backend returns None so a ``watchlist:true`` leaf is simply False for a
   geo-feature/event/alert/status record — consistent, never crashes).

``orbital_object`` has no explicit branch on EITHER side because the CelesTrak adapter
always sets ``correlation_key=\"orbital:celestrak:{norad_id}\"``; both sides resolve via
the PRIMARY branch and agree.

In production EVERY track that reaches the alert engine is the FUSED record from
``LiveState._apply_track`` which sets the track id to the correlation_key and carries
``correlation_key``, so both sides always take the PRIMARY branch on the same bytes.
The fallback branches handle demo/keyless tracks and must still produce the same key.
"""

from __future__ import annotations

from aether.schema.records import Record, TrackRecord


def _str_attr(record: TrackRecord, key: str) -> str | None:
    """Return ``record.attributes[key]`` as a non-empty string, or ``None``.

    Mirrors the TypeScript ``strAttr(track, key)`` which returns the attribute only
    when it is a non-empty string — the same guard ensures a spurious ``""`` in the
    attributes dict does not resolve as a key component.
    """
    raw = record.attributes.get(key)
    return raw if isinstance(raw, str) and len(raw) > 0 else None


def watchlist_key(record: Record) -> str | None:
    """Derive the canonical watchlist membership key from a record.

    Returns ``None`` for any non-track record (only :class:`~aether.schema.records.
    TrackRecord` instances are ever watchlistable). Returns a non-empty string key
    for every track — either via the primary ``correlation_key`` branch, one of the
    fallback track-type branches, or the last-resort raw id.

    Parity contract: this function is branch-for-branch identical to the TypeScript
    ``watchlistKey()`` in ``frontend/src/state/selectors.ts`` (lines ~307-329).  Any
    divergence breaks the client–server contract (a key the client PUTs would never
    match the record the engine evaluates). See ``tests/unit/test_watchlist_identity_parity.py``
    for the shared 9-case table that pins both ends.
    """
    if not isinstance(record, TrackRecord):
        return None  # only tracks are watchlistable (non-track → never a member)

    # PRIMARY branch: production fused tracks always carry correlation_key; both sides agree.
    if record.correlation_key:  # empty-string falsy, just like JS `if (track.correlation_key)`
        return record.correlation_key

    # Fallback branches — mirror TS dispatch on track_type.
    tt = record.track_type
    if tt == "aircraft":
        icao = _str_attr(record, "icao") or _str_attr(record, "hex")
        if icao:
            return f"aircraft:icao:{icao.lower()}"  # .lower() matches JS .toLowerCase()
    elif tt == "vessel":
        mmsi = _str_attr(record, "mmsi")
        if mmsi:
            return f"mmsi:{mmsi}"
    elif tt in ("aprs_station", "aprs_object"):
        if record.label:  # empty-string falsy like JS `if (track.label)`
            return f"aprs:{record.label}"

    # Last resort: raw record id (no stable identity; same as TS `track.id` fallback).
    return record.id

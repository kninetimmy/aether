"""Operator tracks-of-interest (TOI) watchlist entries (PRD §24.6, §21.5).

A watchlist entry is *operator configuration*, not an observed record: the operator
marks a canonical identity key (aircraft, vessel, APRS station, orbital object) as
a track of interest so the alert engine can evaluate a ``watchlist`` condition
(PRD §20.2). It is persisted in the ``watchlist`` table and CRUD-managed via
``/api/v2/watchlist``.

Unlike geofences this has no geometry and projects no live-map overlay — membership
is purely an identity set that the alert engine holds in memory. The identity key is
CLIENT-MINTED by :func:`aether.alerts.identity.watchlist_key` (or its TypeScript
equivalent ``watchlistKey()`` in ``selectors.ts``); both sides derive the same
deterministic string from the same record, so the key is stable, meaningful, and
usable as the REST path segment.

Shape mirrors :mod:`aether.schema.geofence` as a standalone Pydantic store (NOT part
of the record union → NO ``schema_version`` bump).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aether.schema.common import UtcDatetime


class WatchlistEntryCreate(BaseModel):
    """Request body for ``PUT /api/v2/watchlist/{key}`` — operator meta only.

    The canonical identity *key* comes from the URL path; only decoration lives here.
    All fields are optional: a bare PUT with an empty body upserts a key with no
    human label (the minimal idempotent toggle-on).
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=200)
    priority: int | None = Field(default=None, ge=0, le=9)  # PRD §24.6
    notes: str | None = Field(default=None, max_length=2000)


class WatchlistEntryUpdate(BaseModel):
    """Request body for ``PATCH /api/v2/watchlist/{key}`` — every field optional.

    A field left unset (``None``/absent) keeps its stored value; the patch is applied
    field-by-field by :meth:`WatchlistEntry.with_update`.  Same shape as
    :class:`WatchlistEntryCreate` by design — the only difference is semantics.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=200)
    priority: int | None = Field(default=None, ge=0, le=9)
    notes: str | None = Field(default=None, max_length=2000)


class WatchlistEntry(BaseModel):
    """A stored watchlist entry — operator config with a stable key and timestamps."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=256)  # canonical watchlist_key
    label: str | None = Field(default=None, max_length=200)
    priority: int | None = Field(default=None, ge=0, le=9)
    notes: str | None = Field(default=None, max_length=2000)
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def create(
        cls,
        body: WatchlistEntryCreate,
        *,
        key: str,
        now: UtcDatetime,
    ) -> WatchlistEntry:
        """Build a stored entry from a create request at time ``now``."""
        return cls(
            key=key,
            label=body.label,
            priority=body.priority,
            notes=body.notes,
            created_at=now,
            updated_at=now,
        )

    def with_update(self, patch: WatchlistEntryUpdate, *, now: UtcDatetime) -> WatchlistEntry:
        """Return a copy with ``patch``'s set fields applied and ``updated_at=now``.

        ``created_at`` is preserved; only fields the patch actually sets (present and
        non-``None``) change. The merged copy is re-validated so max_length/bounds stay
        enforced, mirroring :meth:`~aether.schema.geofence.Geofence.with_update`.
        """
        changes: dict[str, Any] = {
            name: getattr(patch, name)
            for name in patch.model_fields_set
            if getattr(patch, name) is not None
        }
        merged = self.model_copy(update={**changes, "updated_at": now})
        return WatchlistEntry.model_validate(merged.model_dump())

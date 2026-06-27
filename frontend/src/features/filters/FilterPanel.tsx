// Display-filter panel (PRD §16.5 + COP-FR-009 + AIS-FR-005 + APRSIS-FR-006).
//
// DISPLAY ONLY — every control here writes the client-side DisplayFilters object;
// nothing touches ingestion. Because the filters are reactive Zustand state, the
// filtered set updates with no page reload (the hard acceptance gate, PRD §33.1).
// The panel stays dumb: it edits state and reuses centralized labels
// (MIL_BASIS_LABEL); the actual filtering lives in the pure selectors.

import { useMemo } from "react";
import { MIL_BASIS_LABEL } from "../../map/presentationRegistry";
import { activeSources, activeTrackTypes } from "../../state/selectors";
import {
  useStore,
  type MilitaryBasis,
  type MilitaryFilter,
} from "../../state/store";
import type { TrackType } from "../../types/records";

const MIL_BASES: MilitaryBasis[] = ["provider", "address_block", "both", "unknown"];

const MILITARY_OPTIONS: { value: MilitaryFilter; label: string }[] = [
  { value: "any", label: "Any" },
  { value: "military", label: "Military" },
  { value: "civil", label: "Civil" },
];

/** Toggle a value in a nullable Set; null means "all" (no constraint). */
function toggleInSet<T>(current: Set<T> | null, value: T): Set<T> | null {
  const next = new Set(current ?? []);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next.size === 0 ? null : next;
}

/** Parse a numeric input into number|null (blank → null = inactive). */
function numOrNull(raw: string): number | null {
  if (raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/** Normalize a text input to string|null (blank → null = inactive). */
function strOrNull(raw: string): string | null {
  return raw === "" ? null : raw;
}

export function FilterPanel() {
  const tracks = useStore((s) => s.live.tracks);
  const filters = useStore((s) => s.filters);
  const setFilters = useStore((s) => s.setFilters);
  const resetFilters = useStore((s) => s.resetFilters);
  const stationCenter = useStore((s) => s.stationCenter);
  const orbitalConfig = useStore((s) => s.orbitalConfig);

  const sources = useMemo(() => activeSources(tracks), [tracks]);
  const trackTypes = useMemo(() => activeTrackTypes(tracks), [tracks]);

  const rangeDisabled = stationCenter === null;

  return (
    <section className="panel-section" aria-label="Display filters">
      <h2>
        Filters
        <button type="button" className="link" onClick={() => resetFilters()}>
          reset
        </button>
      </h2>

      <label className="filter-row">
        <input
          type="checkbox"
          checked={filters.liveLocalOnly}
          onChange={(e) => setFilters({ liveLocalOnly: e.target.checked })}
        />
        <span>Live LOCAL only</span>
      </label>

      <label className="filter-row">
        <input
          type="checkbox"
          checked={filters.watchlistOnly}
          onChange={(e) => setFilters({ watchlistOnly: e.target.checked })}
        />
        <span>Watchlist only</span>
      </label>

      <fieldset className="filter-group" aria-label="Track types">
        <legend>Track type</legend>
        {trackTypes.length === 0 && <p className="muted">none yet</p>}
        {trackTypes.map((tt: TrackType) => (
          <label key={tt} className="filter-chip">
            <input
              type="checkbox"
              checked={filters.trackTypes?.has(tt) ?? false}
              onChange={() =>
                setFilters({ trackTypes: toggleInSet(filters.trackTypes, tt) })
              }
            />
            <span>{tt}</span>
          </label>
        ))}
      </fieldset>

      <fieldset className="filter-group" aria-label="Sources">
        <legend>Source</legend>
        {sources.length === 0 && <p className="muted">none yet</p>}
        {sources.map((src: string) => (
          <label key={src} className="filter-chip">
            <input
              type="checkbox"
              checked={filters.sources?.has(src) ?? false}
              onChange={() =>
                setFilters({ sources: toggleInSet(filters.sources, src) })
              }
            />
            <span>{src}</span>
          </label>
        ))}
      </fieldset>

      <fieldset className="filter-group" aria-label="Range from station">
        <legend>Range (NM){rangeDisabled && " — station unset"}</legend>
        <input
          type="number"
          min={0}
          placeholder="max NM"
          disabled={rangeDisabled}
          value={filters.rangeNmMax ?? ""}
          onChange={(e) => setFilters({ rangeNmMax: numOrNull(e.target.value) })}
        />
      </fieldset>

      <fieldset className="filter-group" aria-label="Altitude band (m)">
        <legend>Altitude (m)</legend>
        <input
          type="number"
          placeholder="min"
          value={filters.altitudeMinM ?? ""}
          onChange={(e) => setFilters({ altitudeMinM: numOrNull(e.target.value) })}
        />
        <input
          type="number"
          placeholder="max"
          value={filters.altitudeMaxM ?? ""}
          onChange={(e) => setFilters({ altitudeMaxM: numOrNull(e.target.value) })}
        />
      </fieldset>

      <fieldset className="filter-group" aria-label="Speed band (m/s)">
        <legend>Speed (m/s)</legend>
        <input
          type="number"
          placeholder="min"
          value={filters.speedMinMps ?? ""}
          onChange={(e) => setFilters({ speedMinMps: numOrNull(e.target.value) })}
        />
        <input
          type="number"
          placeholder="max"
          value={filters.speedMaxMps ?? ""}
          onChange={(e) => setFilters({ speedMaxMps: numOrNull(e.target.value) })}
        />
      </fieldset>

      <fieldset className="filter-group" aria-label="Max age (s)">
        <legend>Max age (s)</legend>
        <input
          type="number"
          min={0}
          placeholder="seconds"
          value={filters.ageMaxS ?? ""}
          onChange={(e) => setFilters({ ageMaxS: numOrNull(e.target.value) })}
        />
      </fieldset>

      <fieldset
        className="filter-group"
        role="radiogroup"
        aria-label="Military classification"
      >
        <legend>Military</legend>
        {MILITARY_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={filters.military === opt.value}
            className={filters.military === opt.value ? "active" : ""}
            onClick={() => setFilters({ military: opt.value })}
          >
            {opt.label}
          </button>
        ))}
      </fieldset>

      <fieldset className="filter-group" aria-label="Military classification basis">
        <legend>Military basis</legend>
        {MIL_BASES.map((basis) => (
          <label key={basis} className="filter-chip" title={MIL_BASIS_LABEL[basis]}>
            <input
              type="checkbox"
              checked={filters.militaryBasis?.has(basis) ?? false}
              onChange={() =>
                setFilters({
                  militaryBasis: toggleInSet(filters.militaryBasis, basis),
                })
              }
            />
            <span>{MIL_BASIS_LABEL[basis]}</span>
          </label>
        ))}
      </fieldset>

      <fieldset className="filter-group" aria-label="AIS vessel filters">
        <legend>AIS</legend>
        <input
          type="text"
          placeholder="name contains"
          value={filters.ais.nameLike ?? ""}
          onChange={(e) =>
            setFilters({ ais: { ...filters.ais, nameLike: strOrNull(e.target.value) } })
          }
        />
        <input
          type="text"
          placeholder="MMSI contains"
          value={filters.ais.mmsiLike ?? ""}
          onChange={(e) =>
            setFilters({ ais: { ...filters.ais, mmsiLike: strOrNull(e.target.value) } })
          }
        />
        <input
          type="text"
          placeholder="destination contains"
          value={filters.ais.destinationLike ?? ""}
          onChange={(e) =>
            setFilters({
              ais: { ...filters.ais, destinationLike: strOrNull(e.target.value) },
            })
          }
        />
        <input
          type="text"
          placeholder="vessel-type codes (e.g. 70,80)"
          value={filters.ais.vesselTypes ? [...filters.ais.vesselTypes].join(",") : ""}
          onChange={(e) =>
            setFilters({ ais: { ...filters.ais, vesselTypes: parseIntSet(e.target.value) } })
          }
        />
        <input
          type="text"
          placeholder="nav-status codes (e.g. 0,1)"
          value={filters.ais.navStatuses ? [...filters.ais.navStatuses].join(",") : ""}
          onChange={(e) =>
            setFilters({ ais: { ...filters.ais, navStatuses: parseIntSet(e.target.value) } })
          }
        />
      </fieldset>

      <fieldset className="filter-group" aria-label="APRS callsign">
        <legend>APRS callsign</legend>
        <input
          type="text"
          placeholder="callsign contains"
          value={filters.aprsCallsignLike ?? ""}
          onChange={(e) => setFilters({ aprsCallsignLike: strOrNull(e.target.value) })}
        />
      </fieldset>

      {/* Orbital controls render ONLY when the backend orbital adapter is on
          (from /api/config). They narrow WITHIN the transmitted set; they can
          never reveal objects below the station's configured emission floor. */}
      {orbitalConfig?.enabled && (
        <fieldset className="filter-group" aria-label="Orbital">
          <legend>Orbital</legend>
          {orbitalConfig.groups.length === 0 && <p className="muted">no groups</p>}
          {orbitalConfig.groups.map((group) => (
            <label key={group} className="filter-chip">
              <input
                type="checkbox"
                checked={filters.orbitalCategory?.has(group) ?? false}
                onChange={() =>
                  setFilters({
                    orbitalCategory: toggleInSet(filters.orbitalCategory, group),
                  })
                }
              />
              <span>{group}</span>
            </label>
          ))}
          <label className="filter-row">
            <span>Min elevation (deg)</span>
            <input
              type="number"
              min={orbitalConfig.minElevationDeg}
              max={90}
              placeholder={`>= ${orbitalConfig.minElevationDeg}`}
              value={filters.orbitalMinElevationDeg ?? ""}
              onChange={(e) =>
                setFilters({ orbitalMinElevationDeg: numOrNull(e.target.value) })
              }
            />
          </label>
          <p className="muted">
            Narrows within the transmitted set; cannot reveal objects below the
            station floor ({orbitalConfig.minElevationDeg}&deg;).
          </p>
        </fieldset>
      )}
    </section>
  );
}

/** Parse a comma/space list of int codes into a Set, or null when empty. */
function parseIntSet(raw: string): Set<number> | null {
  const codes = raw
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter((s) => s !== "")
    .map((s) => Number(s))
    .filter((n) => Number.isInteger(n));
  return codes.length === 0 ? null : new Set(codes);
}

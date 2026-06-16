"""Source adapters: normalize a source into schema v2 records at the edge.

Each adapter turns one source's native shape into the discriminated record union
(PRD §17) so the backend stays generic. Adapters live here; the runner that owns
their lifecycle (connect, retry/backoff, source-status publication, shutdown)
arrives with the first wired adapter. M2 begins with the local ADS-B (`readsb`)
adapter in :mod:`aether.adapters.readsb`.
"""

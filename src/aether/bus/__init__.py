"""MQTT bus: the wiring between source adapters and the backend (PRD §13.3, §23).

Adapters normalize at the edge and publish records onto ``aether/v2/...`` topics;
the backend subscribes the source tree and feeds live state. The bus is dumb
transport — the backend stays the authoritative fused-state owner, and retained
MQTT messages are never treated as the source of truth (PRD §13.3).
"""

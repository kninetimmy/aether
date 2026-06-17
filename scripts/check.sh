#!/bin/sh
# Run the exact gate CI runs, locally: ruff lint + format check, mypy, pytest.
# Works in Git Bash on Windows and on Linux/Pi. Prefers the project venv.
set -e

if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"        # Windows venv layout
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"                # POSIX venv layout
else
  PY="python"                          # fall back to PATH
fi

echo "Using $PY"
"$PY" -m ruff check .
"$PY" -m ruff format --check .
"$PY" -m mypy
"$PY" -m pytest

# Receive-only tripwire: fail if any RF-transmit / beacon / digipeat /
# Internet-to-RF directive appears UNCOMMENTED in the sample Dire Wolf config.
# The receive-only guarantee (decision 1 / PRD §2.3 / §18.3) rests on these being
# absent; this makes that load-bearing property self-enforcing, matching the claim
# in config/direwolf.conf.example and docs/local-aprs-igate.md.
DIREWOLF_CONF="config/direwolf.conf.example"
TX_DIRECTIVES='IGTXVIA|IGTXLIMIT|PBEACON|OBEACON|TBEACON|CBEACON|IBEACON|SMARTBEACONING|DIGIPEAT|CDIGIPEAT|PTT'
if [ -f "$DIREWOLF_CONF" ] && \
   grep -nE "^[[:space:]]*($TX_DIRECTIVES)([[:space:]]|\$)" "$DIREWOLF_CONF"; then
  echo "ERROR: $DIREWOLF_CONF contains an uncommented transmit/beacon directive (above)." >&2
  echo "       aether is RECEIVE-ONLY (PRD §2.3/§18.3); remove or comment it out." >&2
  exit 1
fi

echo "All checks passed."

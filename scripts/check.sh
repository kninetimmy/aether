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
echo "All checks passed."

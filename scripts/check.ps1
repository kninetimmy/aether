# Run the exact gate CI runs, locally: ruff lint + format check, mypy, pytest.
# Native PowerShell entry point (the sh version is scripts/check.sh).
$ErrorActionPreference = "Stop"

if (Test-Path ".venv\Scripts\python.exe") {
    $py = ".venv\Scripts\python.exe"
} elseif (Test-Path ".venv/bin/python") {
    $py = ".venv/bin/python"
} else {
    $py = "python"
}

Write-Host "Using $py"
foreach ($step in @(
    @("ruff", "check", "."),
    @("ruff", "format", "--check", "."),
    @("mypy"),
    @("pytest")
)) {
    & $py -m @step
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Host "All checks passed."

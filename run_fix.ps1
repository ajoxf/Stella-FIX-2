$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$logDirectory = Join-Path $PSScriptRoot 'logs'
$runtimeDirectory = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Force -Path $logDirectory, $runtimeDirectory | Out-Null
$sessionLog = Join-Path $logDirectory ('fixtrader-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

$pythonCandidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe')
)
$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $python = $pythonCommand.Source
    } else {
        throw 'Python was not found. Create .venv\Scripts\python.exe and install requirements.txt.'
    }
}

# First run: the Stellar venue comes from the example, so everything after
# this is done on the Exchanges page - nothing to copy or edit by hand. An
# existing config is never overwritten.
$config = Join-Path $PSScriptRoot 'config.stellar-uat.json'
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $PSScriptRoot 'config.stellar-uat.example.json') $config
    Write-Host "[run_fix] created config.stellar-uat.json from the example - fill in the venue on the Exchanges page"
}

$env:PYTHONUNBUFFERED = '1'
# Python's logging writes to STDERR. Under Windows PowerShell 5.1, a native
# command's stderr merged with 2>&1 arrives as an ErrorRecord, and with
# $ErrorActionPreference = 'Stop' the FIRST such line - Flask's development
# server warning - terminates this script. The web process it had already
# started kept running with no engine behind it, and the launcher that
# should have started the engine was gone. So errors are not fatal from
# here on, and every line is passed on as plain text.
$ErrorActionPreference = 'Continue'
& $python start.py --fix --no-browser --config config.stellar-uat.json `
    --status (Join-Path $runtimeDirectory 'status.stellar-uat.json') `
    --commands (Join-Path $runtimeDirectory 'commands.stellar-uat.jsonl') `
    --results (Join-Path $runtimeDirectory 'results.stellar-uat.json') `
    --port 8000 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $sessionLog
exit $LASTEXITCODE

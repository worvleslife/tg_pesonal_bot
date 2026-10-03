$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$environmentFile = Join-Path $projectDirectory '.env'

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    Write-Host 'Python environment is missing. Open this folder in a terminal and run:'
    Write-Host '  python -m venv .venv'
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
    Write-Host 'Then run .\start.ps1 again. Python 3.11 or newer is required.'
    exit 1
}

if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    Write-Host 'Create the local configuration first:'
    Write-Host '  Copy-Item .env.example .env'
    Write-Host 'Add your BotFather token to .env. See README.md for the first start.'
    exit 1
}

Push-Location -LiteralPath $projectDirectory
try {
    & $pythonExecutable -m assistant_bot
    $processExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $processExitCode

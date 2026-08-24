param(
    [int]$Port = 8000,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".env") -and (-not $env:MIREYE_API_KEY -or -not $env:OPENAI_API_KEY)) {
    throw "Create .env with MIREYE_API_KEY and OPENAI_API_KEY, or set both environment variables."
}

$SessionRoot = Join-Path $env:TEMP ("mireye-run-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $SessionRoot -Force | Out-Null
$env:WORKSPACE_DB = Join-Path $SessionRoot "workspace.db"
$env:WORLD_ASSET_DIR = Join-Path $SessionRoot "world-assets"
$env:PYTHONPATH = $Root

Write-Host "MIREYE temporary data: $SessionRoot"
Write-Host "MIREYE URL: http://127.0.0.1:$Port/"
Write-Host "Press Ctrl+C to stop."

$Arguments = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $Port)
if ($Reload) {
    $Arguments += "--reload"
}

& python @Arguments
exit $LASTEXITCODE

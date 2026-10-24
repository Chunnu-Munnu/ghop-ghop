<#
.SYNOPSIS
  Make sure a Python environment can run the RescueSense backend.

.DESCRIPTION
  If any installed Python already has the packages (starlette, uvicorn,
  pyserial, numpy, scipy), nothing is downloaded. Otherwise a virtual
  environment is created in backend\.venv and requirements are installed
  with generous timeouts for slow connections.

.EXAMPLE
  .\tools\setup.ps1            # check, install only if needed
  .\tools\setup.ps1 -WithML    # also scikit-learn for the optional model
  .\tools\setup.ps1 -ForceVenv # always build backend\.venv
#>
param(
    [switch]$WithML,
    [switch]$ForceVenv
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-env.ps1')
$backend = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'

if (-not $ForceVenv) {
    $found = @(Find-BackendPython -Backend $backend)
    if ($found.Count -gt 0 -and -not $WithML) {
        Write-Host "Ready: '$($found -join ' ')' already has every backend package. Nothing to download." -ForegroundColor Green
        exit 0
    }
}

$base = $null
foreach ($version in '3.12', '3.11', '3.13', '3.14') {
    & py "-$version" -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) { $base = "-$version"; break }
}
if (-not $base) {
    Write-Host "Python 3.11+ was not found. Install it from https://www.python.org/downloads/ first." -ForegroundColor Red
    exit 1
}

$venvPython = Join-Path $backend '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating backend\.venv with py $base ..."
    & py $base -m venv (Join-Path $backend '.venv')
}
$requirements = if ($WithML) { 'requirements-ml.txt' } else { 'requirements.txt' }
Write-Host "Installing $requirements (slow connections are fine; retries are enabled) ..."
& $venvPython -m pip install --default-timeout=300 --retries 10 -r (Join-Path $backend $requirements)
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip failed. Check the internet connection and run this script again; finished packages are cached." -ForegroundColor Red
    exit 1
}
Write-Host "Backend environment ready: $venvPython" -ForegroundColor Green

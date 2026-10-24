<#
.SYNOPSIS
  Start the RescueSense backend and open the dashboard.

.EXAMPLE
  .\tools\start-demo.ps1                     # live ESP32, serial port auto-detected
  .\tools\start-demo.ps1 -SerialPort COM9    # explicit port
  .\tools\start-demo.ps1 -Simulate           # no hardware
  .\tools\start-demo.ps1 -AutoAlert          # buzzer follows detections

.NOTES
  The old script declared a parameter named $Host, which is a read-only
  PowerShell automatic variable, so it failed before doing anything.
#>
param(
    [string]$SerialPort = "auto",
    [switch]$Simulate,
    [switch]$AutoAlert,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-env.ps1')

$backend = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$python = @(Find-BackendPython -Backend $backend)
if ($python.Count -eq 0) {
    Write-Host "No Python with the backend packages was found. Run .\tools\setup.ps1 once." -ForegroundColor Red
    exit 1
}
$exe = $python[0]
$prefix = @($python | Select-Object -Skip 1)

$arguments = @('app.py', '--host', $BindHost, '--port', "$Port")
if ($Simulate) { $arguments += '--simulate' } else { $arguments += @('--serial-port', $SerialPort) }
if ($AutoAlert) { $arguments += '--auto-alert' }
if (-not $NoBrowser) { $arguments += '--open' }

Push-Location $backend
try {
    & $exe @prefix @arguments
} finally {
    Pop-Location
}

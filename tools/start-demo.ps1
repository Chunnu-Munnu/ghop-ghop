<#
Run the fast, hardware-free RescueSense demo. Use -SerialPort COM5 for the
live ESP32 stream. Execute from PowerShell after the normal venv setup.
#>
param(
    [string]$SerialPort,
    [int]$Baud = 921600,
    [switch]$AutoAlert,
    [string]$Host = "127.0.0.1",
    [int]$Port = 8000
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $projectRoot "backend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "py" }

Push-Location $backend
try {
    $arguments = @("app.py", "--host", $Host, "--port", $Port)
    if ($SerialPort) {
        $arguments += @("--serial-port", $SerialPort, "--baud", $Baud)
        if ($AutoAlert) { $arguments += "--auto-alert" }
    } else {
        $arguments += "--simulate"
    }
    Write-Host "RescueSense backend: http://$Host`:$Port  (Ctrl+C to stop)"
    & $python @arguments
} finally {
    Pop-Location
}

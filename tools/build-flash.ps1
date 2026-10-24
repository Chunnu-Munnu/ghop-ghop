<#
.SYNOPSIS
  Build the RescueSense ESP32-S3 firmware and flash it.

.DESCRIPTION
  - Loads ESP-IDF (export.ps1) if idf.py is not already available.
  - Copies firmware\ to a folder without spaces (ESP-IDF refuses paths with
    spaces, and this project lives under "HACK0WEEN 2.0").
  - Applies sdkconfig.defaults + your Wi-Fi file (+ the USB console overlay
    with -Console usb), regenerating sdkconfig whenever those files change.
  - Finds the board's COM port, skipping Bluetooth virtual ports.

.EXAMPLE
  .\tools\build-flash.ps1                  # build + flash (UART/COM connector)
  .\tools\build-flash.ps1 -Console usb     # board with only the native USB connector
  .\tools\build-flash.ps1 -BuildOnly
  .\tools\build-flash.ps1 -Port COM9 -Monitor
#>
param(
    [string]$Port = 'auto',
    [ValidateSet('uart', 'usb')][string]$Console = 'uart',
    [string]$IdfPath = $(if ($env:IDF_PATH) { $env:IDF_PATH } else { 'C:\esp\esp-idf-v5.5.6' }),
    [string]$WorkDir = 'C:\esp\rescuesense-fw',
    [switch]$BuildOnly,
    [switch]$Monitor
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'firmware'
$wifiFile = Join-Path $source 'sdkconfig.wifi'

function Find-EspPort([string]$Prefer) {
    $devices = Get-CimInstance Win32_PnPEntity |
        Where-Object { $_.Name -match '\(COM\d+\)' -and $_.PNPDeviceID -match 'VID_(10C4|1A86|0403|303A)' }
    $ranked = $devices | Sort-Object {
        $native = $_.PNPDeviceID -match 'VID_303A'
        if ($Prefer -eq 'usb') { -not $native } else { $native }
    }
    foreach ($device in $ranked) {
        if ($device.Name -match '\((COM\d+)\)') { return $Matches[1] }
    }
    return $null
}

# --- Wi-Fi credentials -------------------------------------------------------
if (-not (Test-Path $wifiFile)) {
    Write-Host "Missing firmware\sdkconfig.wifi." -ForegroundColor Red
    Write-Host "Copy firmware\sdkconfig.wifi.example to firmware\sdkconfig.wifi and enter your 2.4 GHz network name and password."
    exit 1
}
if (Select-String -Path $wifiFile -Pattern 'YourHotspotName' -Quiet) {
    Write-Host "firmware\sdkconfig.wifi still has the example SSID. Put your real network in it first." -ForegroundColor Red
    exit 1
}

# --- ESP-IDF -----------------------------------------------------------------
if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
    $export = Join-Path $IdfPath 'export.ps1'
    if (-not (Test-Path $export)) {
        Write-Host "ESP-IDF was not found at $IdfPath." -ForegroundColor Red
        Write-Host "Install ESP-IDF v5.5.x (docs\firmware-setup.md), or pass -IdfPath <folder>."
        exit 1
    }
    Write-Host "Loading ESP-IDF from $IdfPath ..."
    . $export
}

# --- Copy to a space-free build folder ----------------------------------------
New-Item -ItemType Directory -Force $WorkDir | Out-Null
robocopy $source $WorkDir /MIR /XD build managed_components /XF sdkconfig sdkconfig.old dependencies.lock .defaults-hash /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying firmware to $WorkDir failed (robocopy $LASTEXITCODE)" }

# --- Configuration -----------------------------------------------------------
$defaults = @('sdkconfig.defaults')
if ($Console -eq 'usb') { $defaults += 'sdkconfig.usb-console' }
$defaults += 'sdkconfig.wifi'
$fingerprint = ($defaults | ForEach-Object { "$_`n" + (Get-Content (Join-Path $WorkDir $_) -Raw) }) -join "`n"
$sha = [System.Security.Cryptography.SHA256]::Create()
$hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($fingerprint))) -replace '-', ''
$stamp = Join-Path $WorkDir '.defaults-hash'
$stale = -not (Test-Path (Join-Path $WorkDir 'sdkconfig')) -or -not (Test-Path $stamp) -or ((Get-Content $stamp -Raw).Trim() -ne $hash)

Push-Location $WorkDir
try {
    if ($stale) {
        Write-Host "Configuring for ESP32-S3 with: $($defaults -join ', ')"
        Remove-Item sdkconfig, sdkconfig.old -ErrorAction SilentlyContinue
        idf.py -D "SDKCONFIG_DEFAULTS=$($defaults -join ';')" set-target esp32s3
        if ($LASTEXITCODE -ne 0) { throw 'idf.py set-target failed' }
        Set-Content -Path $stamp -Value $hash -Encoding ascii
    }

    idf.py build
    if ($LASTEXITCODE -ne 0) { throw 'Build failed. Scroll up for the first error.' }
    if ($BuildOnly) {
        Write-Host "Build OK: $WorkDir\build\rescue_sense_csi.bin" -ForegroundColor Green
        return
    }

    if ($Port -eq 'auto') {
        $Port = Find-EspPort -Prefer $Console
        if (-not $Port) { throw 'No ESP32 serial port found. Plug the board in (UART/COM connector) and retry, or pass -Port COMx.' }
    }
    Write-Host "Flashing $Port ..."
    idf.py -p $Port -b 921600 flash
    if ($LASTEXITCODE -ne 0) {
        throw "Flash failed on $Port. Hold BOOT, tap RESET, release BOOT, then retry."
    }
    Write-Host "Flashed. Start the dashboard with .\tools\start-demo.ps1" -ForegroundColor Green
    if ($Monitor) {
        Write-Host 'Opening the serial monitor (Ctrl+] to exit). Close it before starting the backend.'
        idf.py -p $Port monitor
    }
} finally {
    Pop-Location
}

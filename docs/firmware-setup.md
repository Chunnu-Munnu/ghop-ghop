# Firmware setup — start here

The receiver firmware is derived from Espressif's official
[`esp-csi`](https://github.com/espressif/esp-csi) `csi_recv_router` and
`csi_recv` examples. It changes only the output/alert path: CSI is queued out
of the Wi-Fi callback, serialized as gain-compensated amplitudes, and it
accepts `ALERT,1` / `ALERT,0` over the console.

Use **ESP-IDF v5.5.x** (the official esp-csi CI covers ESP32-S3 on IDF 5.4/5.5).

## 1. Install ESP-IDF v5.5.x (once)

Open an **ESP-IDF PowerShell**, or install the toolchain once:

```powershell
New-Item -ItemType Directory -Force C:\esp | Out-Null
git clone -b v5.5.6 --depth 1 --recursive https://github.com/espressif/esp-idf.git C:\esp\esp-idf-v5.5.6
C:\esp\esp-idf-v5.5.6\install.ps1 esp32s3
C:\esp\esp-idf-v5.5.6\export.ps1
```

`tools\build-flash.ps1` will auto-load `export.ps1` from `C:\esp\esp-idf-v5.5.6`
if `idf.py` is not already on the path (override with `-IdfPath <folder>`).

## 2. Set the Wi-Fi name and password

Credentials live in a git-ignored file, not in source. Copy the example and
edit it:

```powershell
Copy-Item firmware\sdkconfig.wifi.example firmware\sdkconfig.wifi
notepad firmware\sdkconfig.wifi
```

```ini
CONFIG_RESCUE_WIFI_SSID="YourHotspotName"
CONFIG_RESCUE_WIFI_PASSWORD="your-wifi-password"
```

Use a **2.4 GHz** network — the ESP32-S3 is 2.4 GHz only, and a normal WPA2
router is far more reliable than a phone hotspot. On iPhone, enable Personal
Hotspot → **Maximize Compatibility**; on Android, choose the 2.4 GHz band.

## 3. Build and flash

```powershell
.\tools\build-flash.ps1
```

This copies `firmware\` to `C:\esp\rescuesense-fw` (ESP-IDF refuses to build
from a path with spaces, and this project lives under `HACK0WEEN 2.0`),
configures the ESP32-S3 target with `sdkconfig.defaults` + your
`sdkconfig.wifi`, builds, auto-detects the board's COM port (Bluetooth virtual
ports are skipped), and flashes at 921600 baud.

Useful switches:

```powershell
.\tools\build-flash.ps1 -Console usb   # board exposes only the native USB connector
.\tools\build-flash.ps1 -Port COM9     # pick the port yourself
.\tools\build-flash.ps1 -BuildOnly     # compile without flashing
.\tools\build-flash.ps1 -Monitor       # open the serial monitor after flashing
```

If flashing fails, hold **BOOT**, tap **RESET**, release **BOOT**, then retry.

Close the serial monitor (`Ctrl+]`) before starting the backend — only one
program can own the serial port.

## 4. What you should see

Status lines begin with `#`; data lines are the CSI rows:

```text
#RESCUESENSE,fw=2.0,mode=router,ping_hz=100,gain=compensate,...
#WIFI,connected,channel=6,bssid=...
#GAIN,mode=compensate,base_agc=...,base_fft=...
CSI,123456789,-47,31.0,35.2,42.1,...
```

The firmware waits for ~100 packets of AGC baseline before emitting CSI rows,
so the first second is status-only. The backend turns the `#WIFI` reason codes
into plain-language hints on the dashboard if the board cannot join Wi-Fi.

## Receiver options (menuconfig → RescueSense)

Build-time options, set in `firmware\main\Kconfig.projbuild` or
`idf.py menuconfig → RescueSense`:

- **Router vs. dedicated transmitter** — router mode (default) pings the AP at
  100 Hz. Disable it to use a second ESP32 running stock `csi_send`.
- **Request 20 MHz frames** (default on) — keeps every CSI packet at 64
  subcarriers so the host never sees mixed widths.
- **Gain handling** — *compensate* (default, Espressif's recommended AGC
  correction), *force* (lock gain at the baseline), or *off*.
- **Buzzer GPIO** (default 4) and **alert timeout** (default 20 s).

## Two-board fallback

If a hotspot will not sustain 100 Hz ICMP replies, flash stock `csi_send` to a
spare ESP32 and disable router mode on the receiver. Keep both on channel
11/HT40 (the official defaults) and at least a metre apart.

```powershell
cd C:\esp\esp-csi\examples\get-started\csi_send
idf.py set-target esp32s3
idf.py -p COM8 -b 921600 flash
```

## Sources

- [Espressif esp-csi](https://github.com/espressif/esp-csi)
- [esp-csi get-started examples](https://github.com/espressif/esp-csi/tree/master/examples/get-started)
- [ESP32-S3 Wi-Fi / CSI API guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.1/esp32s3/api-guides/wifi.html)

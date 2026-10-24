# Firmware setup — start here

## Exact official bases

Primary, one-board path: clone Espressif's official [`esp-csi`](https://github.com/espressif/esp-csi) repository and use `examples/get-started/csi_recv_router`. It joins an AP and repeatedly pings the gateway at 100 Hz, yielding CSI from the reply path.

Reliable two-board fallback: use the same repository's `examples/get-started/csi_send` (TX) and `examples/get-started/csi_recv` (RX). The official sender already transmits ESP-NOW packets at 100 Hz.

This firmware overlay is derived from both official receiver examples. It changes only the output/alert path: CSI is queued out of the Wi-Fi task, serialized as amplitudes, and accepts `ALERT,1` / `ALERT,0` over the console.

Use **ESP-IDF v5.5.6**. The official esp-csi CI covers ESP32-S3 on IDF 5.4/5.5; pinning v5.5.6 avoids current IDF 6 migration differences.

## Windows / PowerShell setup

Open an ESP-IDF PowerShell, or install this exact version once:

```powershell
New-Item -ItemType Directory -Force C:\esp | Out-Null
Set-Location C:\esp
git clone --branch v5.5.6 --recursive https://github.com/espressif/esp-idf.git esp-idf-v5.5.6
Set-Location C:\esp\esp-idf-v5.5.6
.\install.ps1 esp32s3
.\export.ps1
```

Clone the official CSI source for provenance and copy this ready-to-build overlay onto its router example:

```powershell
Set-Location C:\esp
git clone --depth 1 https://github.com/espressif/esp-csi.git
Set-Location C:\esp\esp-csi
git rev-parse HEAD

$overlay = 'C:\Users\Pratyush\Documents\Codex\2026-10-23\you-are-a-senior-embedded-dsp\outputs\rescue-sense\firmware'
$target = 'C:\esp\esp-csi\examples\get-started\csi_recv_router'
Copy-Item -Force "$overlay\CMakeLists.txt" $target
Copy-Item -Force "$overlay\sdkconfig.defaults" $target
Copy-Item -Recurse -Force "$overlay\main\*" "$target\main"
Set-Location $target

idf.py set-target esp32s3
idf.py menuconfig
```

## Set the Wi-Fi name and password — do this before building

With the `csi_recv_router` project open, `idf.py menuconfig` opens the blue text UI. Set the ESP32-S3's Wi-Fi credentials exactly here:

1. Use the arrow keys to highlight **Example Connection Configuration**, then press `Enter`.
2. Confirm **connect using WiFi interface** has `[*]` beside it.
3. Confirm **Get ssid and password from stdin** has `[ ]` beside it. If it has `[*]`, press `Space` to uncheck it; the SSID/password fields are otherwise hidden.
4. Highlight **WiFi SSID**, press `Enter`, type the exact 2.4 GHz router/hotspot name, then press `Enter` again.
5. Highlight **WiFi Password**, press `Enter`, type that Wi-Fi network's password, then press `Enter` again. This is **not** your Windows password and does not go in Python code.
6. Optionally set **Maximum retry** to `-1` so the receiver keeps retrying after a hotspot/router drop.
7. Press `S` to save. When prompted for the filename, accept the default `sdkconfig` by pressing `Enter`, then press `Q` until menuconfig exits.

Use a fixed **2.4 GHz** SSID — ESP32-S3 is 2.4 GHz only. A normal WPA2 2.4 GHz router is much more reliable than a phone hotspot. `sdkconfig` stores the password locally as configuration, so do not paste it into source files, commit it, or show it in a demo screenshot.

Now build and flash:

```powershell
idf.py build
idf.py -p COM7 -b 921600 flash
idf.py -p COM7 monitor
```

Replace `COM7` with the receiver's port. The first build resolves Espressif's `esp_csi_gain_ctrl` component declared in `main/idf_component.yml`. Keep `git rev-parse HEAD` in your demo notes so the exact Espressif base is reproducible.

Close `idf.py monitor` before starting Python: only one program can own the serial port.

## Remaining menuconfig choices

Confirm these values after `idf.py menuconfig` or in `sdkconfig`:

```ini
CONFIG_ESP_WIFI_CSI_ENABLED=y
CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=128
CONFIG_ESP_WIFI_AMPDU_TX_ENABLED=n
CONFIG_ESP_WIFI_AMPDU_RX_ENABLED=n
CONFIG_ESP_CONSOLE_UART_BAUDRATE=921600
CONFIG_FREERTOS_HZ=1000
CONFIG_ESP32S3_DEFAULT_CPU_FREQ_240=y
```

Under **RescueSense** leave **Use router / phone hotspot as CSI transmitter** enabled and `Router ping / CSI sample rate` at `100`. For the alert, set only **Buzzer GPIO (active high)** to match your board; the default is GPIO4.

No LEDs are used by this version. Drive the buzzer through a transistor/MOSFET or a compatible active-buzzer module — do not power a buzzer directly from a GPIO. Check the board schematic and avoid strapping pins, USB GPIO19/20, UART0 GPIO43/44, and flash/PSRAM pins.

## Expected output and alert command

The receiver prints status lines beginning with `#`, followed by data-only lines:

```text
CSI,123456789,-47,31,35,42,38,...
```

The fields are `timestamp_us`, RSSI in dBm, then one integer amplitude per CSI subcarrier. The backend removes first-word-invalid data, DC/null, and guard subcarriers. It sends `ALERT,1\n` on detection and `ALERT,0\n` when clear; firmware responds with `#ALERT,1` or `#ALERT,0` and turns the buzzer on or off.

## Two-board fallback

If your router/hotspot will not sustain ICMP replies at 100 Hz, flash stock official `csi_send` to the spare ESP32 and use this overlay with **Use router / phone hotspot as CSI transmitter** disabled. Both boards must use channel 11/HT40 (the official defaults). Build/flash the sender from:

```powershell
Set-Location C:\esp\esp-csi\examples\get-started\csi_send
idf.py set-target esp32s3
idf.py build
idf.py -p COM8 -b 921600 flash
```

Then set the receiver mode switch off in `menuconfig`, rebuild it, and keep the sender and receiver at least one metre apart. If you need to change channel, make the same change in both projects.

## Sources

- [Espressif ESP-CSI repository](https://github.com/espressif/esp-csi)
- [Official CSI example guide](https://github.com/espressif/esp-csi/tree/master/examples/get-started)
- [ESP-IDF v5.5.6 release](https://github.com/espressif/esp-idf/releases/tag/v5.5.6)
- [ESP32-S3 CSI API guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-guides/wifi.html)

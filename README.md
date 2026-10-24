# RescueSense — camera-free Wi-Fi CSI breathing demonstrator

RescueSense uses an ESP32-S3 receiver, ordinary 2.4 GHz Wi-Fi channel-state
information (CSI), and laptop-side DSP to surface a **breathing-like periodic
signal**, a BPM estimate, a confidence score, and a calibrated **estimated
proximity (0–5 m)** on a live dashboard. It is a hackathon demonstrator, not a
medically validated life detector or a through-wall rangefinder.

## What's in the box

| Part | Role |
| --- | --- |
| `firmware/` | ESP32-S3 receiver (derived from Espressif's official esp-csi examples) |
| `backend/` | Python DSP + WebSocket server; also serves the dashboard |
| `dashboard/` | Single-page web console (no build step) |
| `tools/` | PowerShell setup, build/flash, and run scripts |
| `docs/` | Firmware setup, calibration, demo script, failure modes, architecture |

## Quick start (laptop, no hardware)

You can run the whole UI and DSP against a built-in simulator before the board
is ready. From `RescueSense_VisionX/`:

```powershell
.\tools\setup.ps1          # one-time: prepares a Python environment if needed
.\tools\start-demo.ps1 -Simulate
```

A browser opens at `http://127.0.0.1:8000`. The stream is clearly badged
**SIMULATION · not real data** — it is a UI/DSP rehearsal aid, not a detection.

## Live hardware run

1. **Flash the receiver.** Copy `firmware\sdkconfig.wifi.example` to
   `firmware\sdkconfig.wifi`, put your **2.4 GHz** network name and password in
   it, then build and flash (needs ESP-IDF v5.5.x — see
   [docs/firmware-setup.md](docs/firmware-setup.md)):

   ```powershell
   .\tools\build-flash.ps1
   ```

   The script copies the firmware to a space-free path (ESP-IDF cannot build
   from `HACK0WEEN 2.0`), configures the target, builds, finds the board's COM
   port (Bluetooth ports are ignored), and flashes. Use `-Console usb` for
   boards that only expose the native USB connector.

2. **Start the backend.** It auto-detects the ESP32 serial port:

   ```powershell
   .\tools\start-demo.ps1
   ```

   Or run it directly, with options:

   ```powershell
   cd backend
   python app.py                     # auto-detect port, serve dashboard
   python app.py --serial-port COM9  # explicit port
   python app.py --auto-alert        # buzzer follows detections
   python app.py --list-ports        # show candidate ports and exit
   ```

3. **Open the dashboard** at `http://127.0.0.1:8000`. Within ~30 s the header
   should read **Live** with a packet rate near 100 pkt/s. If a browser on
   another machine needs it, start with `--host 0.0.0.0` and open
   `http://<laptop-ip>:8000`.

4. **Calibrate** from the dashboard: record a 30 s empty-room baseline, then
   (optionally) 1–5 m distance anchors to enable proximity. See
   [docs/calibration.md](docs/calibration.md).

## How it works

```
2.4 GHz AP  --Wi-Fi-->  ESP32-S3 CSI receiver  --USB serial-->  Python DSP  --WebSocket-->  Dashboard
(or a 2nd ESP32                                  CSI,ts,rssi,amp…   detection + calibration     live console
 running csi_send)                                                                              |
Dashboard  --alert command-->  backend  --ALERT,1/0 serial-->  ESP32  -->  buzzer
```

The receiver does RF acquisition, AGC-gain compensation, and alert I/O only.
The laptop owns all signal processing. Each second the newest 30 s of CSI are:

1. filtered for stray-width and corrupted packets (one odd packet no longer
   wipes the window), DC/guard subcarriers masked;
2. averaged to 10 Hz and whitened per subcarrier by its own noise level, with
   residual receiver-gain steps removed;
3. checked for gross body movement (0.8–4 Hz); breathing is measured only from
   the still signal after the last movement;
4. combined across subcarriers with PCA, keeping the **most periodic**
   component;
5. scored on **rhythm** (autocorrelation), **focus** (spectral concentration)
   and **strength** (vs. the empty-room background). Confidence is their
   weighted combination; a hysteresis state machine decides
   `breathing / clear / movement / warming up / no signal`.

See [docs/architecture.md](docs/architecture.md) for the full pipeline.

## Hardware wiring

| Item | Default | Notes |
| --- | --- | --- |
| Active buzzer | GPIO4 | Drive through a transistor/MOSFET — never straight off a GPIO |

Set the real pin in `firmware\main\Kconfig.projbuild` or via `idf.py menuconfig
→ RescueSense → Buzzer GPIO`. Avoid strapping pins, USB GPIO19/20, UART0
GPIO43/44, and flash/PSRAM pins. The buzzer also stops on its own if the laptop
stops sending its heartbeat (configurable alert timeout).

## Integrity of the claim

Say **"Breathing detected"** only for a calibrated respiration-band signature
that clears the empty-room baseline. Call the range output **"estimated
proximity"** and keep its uncertainty visible. Wi-Fi CSI varies with geometry,
antennas, metal, motion and channel; it cannot establish survival or an exact
distance. When the RF link is poor, use the clearly-labelled simulation mode
rather than presenting synthetic data as a live detection.

# RescueSense — camera-free Wi-Fi CSI breathing demonstrator

RescueSense uses an ESP32-S3 receiver, ordinary 2.4 GHz Wi-Fi CSI, and laptop-side DSP to surface a **breathing-like periodic signal**, BPM estimate, confidence, and calibrated **estimated proximity (0–5 m)** through a live 3D dashboard. It is designed for a 10-hour hackathon demo, not for real rescue deployment or medical confirmation.

## Live hardware launch — use this for the real-time demo

First flash the receiver and set its Wi-Fi credentials using [the firmware setup guide](docs/firmware-setup.md). The password goes in **`idf.py menuconfig → Example Connection Configuration → WiFi Password`** before you build and flash; it is the password for the router/hotspot the ESP32-S3 will join.

After flashing, close `idf.py monitor` so Python can own the COM port. Then run the actual hardware pipeline:

```powershell
Set-Location .\backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py --serial-port COM5 --baud 921600 --auto-alert
```

Replace `COM5` with the ESP32-S3 port from Device Manager. **Do not add `--simulate` for the real demo.** Open [dashboard/index.html](dashboard/index.html) in a browser; it normally connects to `ws://127.0.0.1:8000/ws` automatically. Wait about 30 seconds, then verify that the dashboard says **LIVE CSI**, not `DEMO STREAM`.

If the browser is on another machine, start the backend with `--host 0.0.0.0` and use `ws://<laptop-ip>:8000/ws` in the dashboard.

## Optional simulator fallback

Simulation is only a UI/DSP fallback when hardware is unavailable. It does not read the ESP32:

```powershell
python app.py --simulate
```

It warms a synthetic 30-second buffer at startup and displays a coherent 15 BPM / 2.5 m visual stream. Label it as a fallback if you ever use it.

## 4-hour working route

| Time | Outcome |
| --- | --- |
| 0:00–1:30 | Build/flash official ESP-CSI-derived receiver; set Wi-Fi credentials; see 80–120 `CSI,` lines per second |
| 1:30–2:15 | Run serial backend; get `LIVE CSI` into dashboard |
| 2:15–3:15 | Record empty room plus 1–5 m calibration anchors |
| 3:15–4:00 | Tune geometry/channel, wire/test the buzzer, rehearse 5-minute story |
| 4:00–10:00 | Optional RandomForest, second ESP32 TX fallback, polish/pitch prep |

## Start with these docs

- [Architecture and file tree](docs/architecture.md)
- [Firmware setup, exact Espressif example, ESP-IDF pin, flash commands](docs/firmware-setup.md)
- [Calibration procedure](docs/calibration.md)
- [Five-minute demo script](docs/demo-script.md)
- [Failure modes and venue triage](docs/failure-modes.md)
- [Backend API, DSP pipeline, labelled data collection, optional RandomForest](backend/README.md)

## Hardware wiring defaults

| Item | Default GPIO | Notes |
| --- | --- | --- |
| Active buzzer control | GPIO4 | Use an NPN/MOSFET driver; do not drive a buzzer from GPIO power |

No LED wiring is required. Leave the individual colour LEDs disconnected for now; the dashboard's red alert state remains visual, while the physical alert is the buzzer only.

Set the real buzzer pin in **`idf.py menuconfig → RescueSense → Buzzer GPIO (active high)`** after checking your exact board schematic. Avoid strapping pins, USB Serial/JTAG GPIO19/20, UART0 GPIO43/44, and flash/PSRAM pins.

## Core data contract

Firmware emits one compact line per CSI packet:

```text
CSI,<timestamp_us>,<rssi_dbm>,<amp_sc0>,<amp_sc1>,...
```

The backend broadcasts once per second at `/ws`:

```json
{
  "presence": true,
  "bpm": 15.0,
  "confidence": 0.66,
  "distance_m": 2.5,
  "distance_uncertainty_m": 0.5,
  "rssi": -51.0,
  "raw_waveform": [{"t": -29.9, "v": 0.14}],
  "filtered_waveform": [{"t": -29.9, "v": 0.04}],
  "spectrum": [{"hz": 0.25, "power": 1.2}]
}
```

Alert commands travel dashboard → WebSocket → backend → ESP32 serial as `ALERT,1\n` / `ALERT,0\n`.

## Integrity of the claim

Use the words **“Breathing detected”** only for a calibrated respiration-band signature that clears the empty-room baseline. Call the range output **“estimated proximity”** and leave its uncertainty visible. Wi-Fi CSI varies substantially with room geometry, antennas, metal, motion, and channel; it cannot establish a person’s survival or exact physical distance.

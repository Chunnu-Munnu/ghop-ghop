# RescueSense backend

This folder is the laptop-side CSI pipeline and WebSocket server. It accepts the compact ESP32-S3 stream

```text
CSI,<timestamp_us>,<rssi>,<amp_sc0>,<amp_sc1>,...
```

and also understands the stock ESP-IDF `CSI_DATA,...,"[imag,real,...]"` output from the S3 `csi_recv` examples. The compact format is the preferred 100 Hz path.

## Run it live — this reads the actual ESP32 serial stream

From this folder, create an environment and install the small, demo-critical dependency set. Make sure the ESP32 is already flashed, connected to its 2.4 GHz Wi-Fi network, and that `idf.py monitor` is closed:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py --serial-port COM5 --baud 921600 --auto-alert
```

Replace `COM5` with the port shown for the ESP32 in Device Manager. This is the real-time command; **do not include `--simulate`**. Open the dashboard against `ws://127.0.0.1:8000/ws` and wait about 30 seconds for a stable full analysis window. It must report `LIVE CSI`, not `DEMO STREAM`.

## Optional simulator fallback

The simulator is useful to rehearse UI behavior without the board; it is not real CSI:

```powershell
python app.py --simulate
```

Useful variations:

```powershell
# Test the no-presence state, then toggle it through POST /simulate.
python app.py --simulate --simulate-absent

# Make the ESP32's buzzer follow presence automatically.
python app.py --serial-port COM5 --baud 921600 --auto-alert

# Load an optional model trained below.
python app.py --serial-port COM5 --model presence_model.joblib
```

The HTTP health check is [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health), and the latest telemetry is at `/telemetry`.

## WebSocket and alert contract

Connect a browser to `ws://127.0.0.1:8000/ws`. It receives one telemetry object per second. The primary fields are:

```json
{
  "type": "telemetry",
  "presence": true,
  "bpm": 15.0,
  "confidence": 0.42,
  "distance_m": 2.5,
  "distance_uncertainty_m": 0.5,
  "rssi": -51.0,
  "raw_waveform": [{"t": -29.9, "v": 0.14}],
  "filtered_waveform": [{"t": -29.9, "v": 0.04}],
  "spectrum": [{"hz": 0.25, "power": 1.2}],
  "signal": {"band_energy": 0.12, "variance": 0.42, "snr_db": 11.0},
  "distance_calibrated": true
}
```

Use the dashboard alert button (or send this from browser JavaScript) to command the ESP32:

```js
socket.send(JSON.stringify({ type: "alert", active: true }));
```

`enabled` is also accepted, as are `ALERT,1` / `ALERT,0` commands. The backend writes exactly `ALERT,1\n` or `ALERT,0\n` to the ESP32 serial port. The equivalent REST command is:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/alert -ContentType application/json -Body '{"active":true}'
```

## Calibration (REST; no dashboard controls required)

Keep transmitter/channel/obstruction fixed throughout. Do this after the live graph has collected at least 30 seconds.

1. Clear the area and record the empty-room floor:

   ```powershell
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/calibration/start -ContentType application/json -Body '{"kind":"empty","duration_seconds":30}'
   ```

2. Place a normally breathing volunteer behind the obstruction at 1 m, stay still except for breathing, then run this command. Repeat after moving them to 2, 3, 4, and 5 m.

   ```powershell
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/calibration/start -ContentType application/json -Body '{"kind":"distance","distance_m":1,"duration_seconds":30}'
   ```

3. Confirm each recording auto-finishes and inspect state:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8000/calibration/status
   ```

The calibration is saved to `calibration.json` in the working directory. The mapping applies monotonic isotonic regression to a simple log breathing-band-energy + small SNR score, yielding **estimated proximity**. It never claims precise ranging; `distance_uncertainty_m` includes calibration residual/spacing uncertainty. Re-run calibration whenever the router channel, room, board position, or wall changes.

To abort/finalize an active sample early:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/calibration/stop
```

## Optional RandomForest presence model

The built-in heuristic is the reliable first-demo path: breathing spectral confidence plus variance and band energy above the empty-room baseline. If time remains, collect labelled one-second feature windows and train a model for this exact room:

```powershell
python collect.py --port COM5 --label absent  --duration 90 --output recordings\absent.csv
python collect.py --port COM5 --label present --duration 90 --output recordings\present.csv
pip install -r requirements-ml.txt
python train_presence_model.py "recordings\*.csv" --output presence_model.joblib
python app.py --serial-port COM5 --model presence_model.joblib
```

`collect.py` can preserve the wire stream for debugging with `--raw-output recordings\present_raw.txt`. The model uses `band_energy`, `peak_ratio`, `variance`, and `spectral_entropy`; it does not replace the empty-room variance safety gate.

## Signal pipeline

For each one-second update, the server uses up to the newest 30 seconds of CSI:

1. S3 LLTF bins retain `+1..+26` and `-26..-1` (DC and guards are removed; the suspect first-word carrier is also dropped by default).
2. A per-carrier Hampel filter removes packet spikes and all carriers are linearly resampled to 100 Hz.
3. PCA runs across subcarriers; among the first six components, the one with strongest 0.1–0.5 Hz energy is selected.
4. A 3rd-order Butterworth bandpass plus Welch PSD finds 6–30 BPM. Confidence is `peak PSD / total breathing-band PSD`.
5. Presence requires spectral confidence and signal variance/band energy above the empty-room baseline. The dashboard receives downsampled raw/filtered traces and the spectrum.

If data are missing for more than 0.5 seconds, the processor discards the stale segment rather than inventing samples across the gap.

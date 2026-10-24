# Five-minute demo script

## 0:00–0:35 — set the problem and prove this is live

> “RescueSense is a camera-free breathing-motion demonstrator. It uses ordinary 2.4 GHz Wi-Fi packets and the ESP32-S3's channel-state information, so it can keep watching when optical cameras are obstructed.”

Start the backend with `.\tools\start-demo.ps1 -AutoAlert` (it auto-detects the ESP32 port and serves the dashboard at `http://127.0.0.1:8000`). Show that the header reads **Live** with a packet rate near 100 pkt/s. Point to the breathing waveform, the rhythm/focus/strength scores, BPM, confidence, the spectrum peak, the subcarrier activity map, and the detection timeline.

Keep simulation (`-Simulate`) as a clearly labelled fallback only; it is badged **SIMULATION · not real data** and must never be presented as a live detection.

## 0:35–1:15 — show the acquisition path

> “The receiver starts from Espressif's official `csi_recv_router` example. It asks the router for a packet every 10 ms, reads the channel response, and sends only timestamp, RSSI, and CSI magnitudes to the laptop. The heavy DSP stays on the laptop.”

Show the ESP32 serial lines briefly (`.\tools\build-flash.ps1 -Monitor`), then close the monitor so the backend owns the port. Start the live backend. Let the dashboard header move from **Simulation** to **Live**.

## 1:15–2:05 — explain only the working signal chain

> “We drop invalid and guard carriers, reject spikes with a Hampel filter, align samples to 100 Hz, then use PCA to combine the subcarriers. A 0.1–0.5 Hz filter isolates 6–30 breaths per minute. The strongest spectral peak becomes BPM; peak power relative to the band becomes confidence.”

Point to the raw CSI trace, filtered trace, and spectrum peak. Avoid claiming it diagnoses life; call it a “breathing-like periodic signature.”

## 2:05–3:00 — show a live target

Place a still, normally breathing volunteer behind the wall/rubble. After the 30-second window warms up, show:

> “The signal clears the empty-room baseline. We estimate **[BPM] BPM** with **[confidence]% confidence**. The person marker is at **[estimated proximity]**, with the visible uncertainty band because this is calibrated signal inference, not exact ranging.”

Have the volunteer take one slow obvious breath cycle if the signal needs visual help; do not ask them to create body motion.

## 3:00–3:40 — alert actuation

Click **Send alert**. Say:

> “The dashboard sends a WebSocket command to the backend, which writes `ALERT,1` over the same USB link. The ESP32 turns on the locally wired buzzer, so a nearby responder gets a cue even if the browser is not in view.”

Click **Stop alert** after the buzzer response.

## 3:40–4:25 — calibration credibility

> “Before this pitch we recorded 30 seconds of an empty room and then 30 seconds at 1 through 5 metres. That fits a monotonic, environment-specific proximity curve. Moving the wall, AP, or antenna requires recalibration; we expose uncertainty rather than pretending this is an exact radar.”

Show the saved calibration file or the relevant console line.

## 4:25–5:00 — resilience and ask

> “If a phone hotspot rate-limits pings, we switch to Espressif's dedicated two-ESP32 `csi_send`/`csi_recv` path, still at 100 Hz. The next step is multi-link diversity and field validation with response teams. Today, RescueSense demonstrates a cheap, privacy-preserving supplemental sensing channel for obstructed environments.”

Keep the simulator tab open as the no-risk backup through questions.

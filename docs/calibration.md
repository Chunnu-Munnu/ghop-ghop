# Calibration procedure

Calibrate after every meaningful geometry change: AP/transmitter position, ESP32 position/orientation, wall/rubble material, Wi-Fi channel, or room. The distance output is deliberately named **estimated proximity**; it is a link-specific signal-strength/SNR mapping, not a time-of-flight measurement.

## Pre-flight (five minutes)

1. Lock the transmitter to 2.4 GHz and one fixed channel. Disable router auto-channel/band steering if possible.
2. Put the ESP32 receiver at the planned rescue-side location and leave its antenna/orientation unchanged. Keep the transmitter at least one metre away.
3. Place the same wall/rubble material between the link endpoints that you will use in the pitch.
4. Start the backend in `--simulate` first to verify the dashboard, then start it on the receiver serial port. Verify that telemetry reports roughly 80–120 CSI frames/s and a sensible RSSI.
5. Do not let spectators move through the link during the recordings. Keep phones and people away from the Fresnel zone except for the subject.

## Record the empty-room baseline — 30 seconds

With no person inside the sensing zone, start the backend's **empty** calibration recording and keep the scene still for 30 seconds:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/calibration/start -ContentType application/json -Body '{"kind":"empty","duration_seconds":30}'
```

The backend stores carrier variance and breathing-band energy. This establishes the adaptive variance floor and the maximum normal empty-room peak ratio.

Accept the baseline only if frame rate is stable and the dashboard does not show a persistent breathing peak. If it does, reduce environmental motion, choose a cleaner channel, check for a fan/curtain, and repeat.

## Record the five proximity anchors

At each mark below, use the same seated/lying posture and remain as still as possible except for normal breathing. Start a labelled 30-second recording; wait for its automatic completion before moving to the next mark.

| Label | Physical placement | Record |
| --- | --- | --- |
| `1m` | 1 m from receiver along the test path | 30 s |
| `2m` | 2 m | 30 s |
| `3m` | 3 m | 30 s |
| `4m` | 4 m | 30 s |
| `5m` | 5 m or the farthest usable mark | 30 s |

The server fits a monotonic relationship between breathing-band SNR/energy and proximity. It reports the local scatter/holdout residual as `distance_uncertainty_m`, clipped to the 0–5 m demo range. The calibrated mapping is only valid for the current channel and geometry.

For each row, change only `distance_m`:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/calibration/start -ContentType application/json -Body '{"kind":"distance","distance_m":1,"duration_seconds":30}'
Invoke-RestMethod http://127.0.0.1:8000/calibration/status
```

## Validate before the pitch

1. Move the subject to a randomly selected marked distance.
2. Wait for one full 30-second analysis window plus one-second hop.
3. Confirm BPM is physiologically plausible (6–30), confidence rises over baseline, and estimated proximity uncertainty covers the actual mark.
4. Save a screen capture of the dashboard's simulated and live modes. If live radio conditions change, repeat the empty baseline and shorten the claim: “breathing-like periodic CSI signature detected.”

## Suggested recording commands

The intended labels are exactly `empty`, `1m`, `2m`, `3m`, `4m`, and `5m`; avoid putting a person at another range during a labelled capture. Use `POST /calibration/stop` if you need to finalize a capture early. The profile saves automatically as `calibration.json` in the backend working directory.

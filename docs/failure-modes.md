# Failure modes and fast fixes

| Symptom | Likely cause | Fast fix | Preserve for demo |
| --- | --- | --- | --- |
| No `CSI,` lines | CSI not enabled, incorrect Wi-Fi SSID/password, AP unavailable, serial monitor owns port | Verify the router is 2.4 GHz; re-enter credentials in `idf.py menuconfig → Example Connection Configuration`; confirm `CONFIG_ESP_WIFI_CSI_ENABLED=y`; close `idf.py monitor`, then use backend as sole serial reader | Use `--simulate` only as a clearly labelled fallback |
| CSI rate below 80 Hz | Hotspot/router rate-limits ICMP, busy Wi-Fi, serial congestion | Use a fixed 2.4 GHz router; disable auto-channel; raise UART to 921600; switch to official second-board `csi_send` | Explain router vs dedicated-TX modes |
| No Wi-Fi association | Phone is 5 GHz-only, WPA mode mismatch, band steering | Enable 2.4 GHz compatibility SSID/WPA2; use a travel router | Bring the travel router / TX board |
| Garbled or dropped CSV | Baud mismatch, another program uses COM port, too much logging | Match 921600; close monitor; leave `#` logs filtered; inspect backend frame counter | Fix the serial stream before continuing the live demo |
| Constant false breathing | Fan, curtain, moving people, HVAC, periodic equipment | Remove motion; record a new empty baseline; change channel; shorten field of view via placement | Say `MOTION — VERIFY`, not survivor confirmed |
| BPM outside 6–30 | Gross body motion or spectral leakage | Ask subject to stay still; wait for window to refill; verify bandpass settings; lower confidence threshold only after baseline | Use slow breathing volunteer |
| BPM unstable/low confidence | Weak multipath modulation, person too far, bad antenna orientation | Move RX/TX for stronger multipath; use an external antenna; reduce range; add a second link | Show calibrated uncertainty / demo stream |
| Proximity wrong | RSSI/SNR is environment-dependent, wall/geometry changed | Re-run all six calibration labels after any geometry/channel change | Always call it “estimated proximity” |
| Alert does not fire | Wrong GPIO4 mapping, buzzer is passive/incompatible, or buzzer draws too much current | Check board schematic; use an active buzzer through a transistor driver; set `RescueSense → Buzzer GPIO (active high)`; test `ALERT,1` via serial | Keep browser alert state visible |
| Firmware build error | Wrong IDF major version or unresolved component | Use clean ESP-IDF v5.5.6 shell; run `idf.py fullclean`, then `idf.py build`; ensure network for component manager | Build before arrival |
| Person too far / behind dense metal | Link budget/penetration is insufficient | Shorten link, change placement, use more capable antenna or second link; do not increase claims | State physical limits honestly |
| Channel hops mid-demo | Router auto-channel/band steering | Lock 2.4 GHz channel; assign fixed channel to direct ESP-NOW pair | Use direct TX fallback |

## Triage order at the venue

1. Check the receiver serial port and confirm 80–120 `CSI,` rows per second.
2. Confirm the ESP32 joined the fixed 2.4 GHz router; recheck SSID/password in menuconfig if it did not.
3. Lock the router's 2.4 GHz channel and retest.
4. Re-run the empty-room baseline.
5. If sample rate remains poor, flash/switch to the official two-ESP32 sender path.
6. If RF is still poor, use simulated telemetry only with an explicit fallback label alongside any recorded live evidence; do not fabricate a live detection.

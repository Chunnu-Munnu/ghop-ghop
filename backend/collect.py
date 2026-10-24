"""Record labelled one-second CSI feature windows for optional ML training.

Run it once with the area empty and once (or more) with a still, breathing
person behind the obstruction.  It logs compact features, not raw CSI.

    python collect.py --label absent  --duration 120 --output recordings/absent.csv
    python collect.py --label present --duration 120 --output recordings/present.csv
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

from processing import CSIProcessor, ProcessorConfig
from serial_io import SerialCSIReader

FEATURE_COLUMNS = [
    "timestamp",
    "label",
    "label_present",
    "strength_db",
    "periodicity",
    "concentration",
    "motion_index",
    "confidence",
    "bpm",
    "rssi",
    "state",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect labelled RescueSense CSI windows")
    parser.add_argument("--port", default="auto", help="ESP32 port, e.g. COM9 (default: auto-detect)")
    parser.add_argument("--label", choices=("present", "absent"), required=True)
    parser.add_argument("--duration", type=float, default=120.0, help="recording duration in seconds")
    parser.add_argument("--output", required=True, help="feature CSV output path")
    parser.add_argument("--raw-output", help="optional raw serial log for offline debugging")
    parser.add_argument("--baud", type=int, default=921600)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.duration < 30.0:
        raise SystemExit("Use at least 30 seconds; 120 seconds is recommended.")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_handle = None
    if args.raw_output:
        raw_path = Path(args.raw_output)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_handle = raw_path.open("w", encoding="utf-8", newline="")

    # Use a throwaway calibration file so collection never changes the live one.
    processor = CSIProcessor(ProcessorConfig(), calibration_path=output_path.with_suffix(".calibration.json"))

    def on_frame(frame):
        processor.add_frame(frame)
        if raw_handle is not None:
            raw_handle.write(f"{frame.timestamp_s:.6f},{frame.rssi:.0f}," + ",".join(f"{v:.1f}" for v in frame.amplitudes) + "\n")

    reader = SerialCSIReader(args.port, args.baud, on_frame=on_frame)
    reader.start()
    label_value = 1 if args.label == "present" else 0
    rows_written = 0
    end_time = time.monotonic() + args.duration
    print(f"Recording '{args.label}' for {args.duration:.0f} s (port: {args.port}). Ctrl+C to stop early.")
    try:
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FEATURE_COLUMNS)
            writer.writeheader()
            while time.monotonic() < end_time:
                time.sleep(1.0)
                metrics = processor.telemetry()
                status = reader.status()
                if not metrics["valid"]:
                    print(f"  {metrics['state']}: {metrics['state_detail']}  (serial: {status['state']}, {status['packet_rate_hz']} pkt/s)")
                    continue
                writer.writerow(
                    {
                        "timestamp": round(time.time(), 2),
                        "label": args.label,
                        "label_present": label_value,
                        "strength_db": metrics["strength_db"],
                        "periodicity": metrics["periodicity"],
                        "concentration": metrics["concentration"],
                        "motion_index": metrics["motion_index"],
                        "confidence": metrics["confidence"],
                        "bpm": metrics["bpm_instant"],
                        "rssi": metrics["rssi"],
                        "state": metrics["state"],
                    }
                )
                handle.flush()
                rows_written += 1
                print(
                    f"{rows_written:03d} | {metrics['state']:<9} conf={metrics['confidence']:.2f} "
                    f"bpm~{metrics['bpm_instant']} strength={metrics['strength_db']:.1f} dB"
                )
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        reader.stop()
        if raw_handle is not None:
            raw_handle.close()
    print(f"Saved {rows_written} labelled windows to {output_path}")


if __name__ == "__main__":
    main()

"""Record labelled one-second CSI feature windows for optional ML training.

Run this once for an empty room and once (or more) with a person behind the
obstruction.  It logs compact features rather than raw camera-like data.
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import serial

from processing import CSIProcessor, ProcessorConfig
from serial_io import CSIStreamParser


FEATURE_COLUMNS = [
    "timestamp",
    "label",
    "label_present",
    "band_energy",
    "peak_ratio",
    "variance",
    "spectral_entropy",
    "snr_db",
    "bpm",
    "rssi",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect labelled RescueSense CSI windows")
    parser.add_argument("--port", required=True, help="ESP32 port, e.g. COM5")
    parser.add_argument("--label", choices=("present", "absent"), required=True)
    parser.add_argument("--duration", type=float, default=60.0, help="recording duration in seconds")
    parser.add_argument("--output", required=True, help="feature CSV output path")
    parser.add_argument("--raw-output", help="optional raw serial log for offline debugging")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--sample-rate", type=float, default=100.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.duration < 15.0:
        raise SystemExit("Use at least 15 seconds; 60 seconds is recommended.")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_handle = None
    if args.raw_output:
        raw_path = Path(args.raw_output)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_handle = raw_path.open("w", encoding="utf-8", newline="")

    processor = CSIProcessor(ProcessorConfig(sample_rate_hz=args.sample_rate))
    parser = CSIStreamParser()
    rows_written = 0
    end_time = time.monotonic() + args.duration
    next_feature_at = time.monotonic() + 1.0
    label_value = 1 if args.label == "present" else 0

    try:
        with serial.Serial(args.port, args.baud, timeout=0.25) as device, output_path.open(
            "w", newline="", encoding="utf-8"
        ) as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=FEATURE_COLUMNS)
            writer.writeheader()
            print(f"Recording {args.label} for {args.duration:.0f}s from {args.port}…")
            while time.monotonic() < end_time:
                raw = device.readline()
                if raw:
                    line = raw.decode("utf-8", errors="replace")
                    if raw_handle is not None:
                        raw_handle.write(line)
                    frame = parser.parse_line(line)
                    if frame is not None:
                        processor.add_frame(frame)
                now = time.monotonic()
                if now >= next_feature_at:
                    metrics = processor.telemetry()
                    next_feature_at = now + 1.0
                    if metrics["valid"]:
                        signal = metrics["signal"]
                        writer.writerow(
                            {
                                "timestamp": time.time(),
                                "label": args.label,
                                "label_present": label_value,
                                "band_energy": signal["band_energy"],
                                "peak_ratio": signal["peak_ratio"],
                                "variance": signal["variance"],
                                "spectral_entropy": signal["spectral_entropy"],
                                "snr_db": signal["snr_db"],
                                "bpm": metrics["bpm"],
                                "rssi": metrics["rssi"],
                            }
                        )
                        output_handle.flush()
                        rows_written += 1
                        print(
                            f"{rows_written:03d} windows | bpm={metrics['bpm']} "
                            f"conf={metrics['confidence']:.2f} present={metrics['presence']}"
                        )
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        if raw_handle is not None:
            raw_handle.close()
    print(f"Saved {rows_written} labelled windows to {output_path}")


if __name__ == "__main__":
    main()

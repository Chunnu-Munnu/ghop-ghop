"""Offline accuracy check of the detection pipeline against the simulator.

Runs faster than real time.  Reports, per scenario, how often the processor
claimed breathing, its BPM error, and the raw feature distributions used to
set the thresholds in ProcessorConfig.

    python tests/evaluate_pipeline.py            # full sweep
    python tests/evaluate_pipeline.py --quick    # smaller sweep
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processing import CSIProcessor, ProcessorConfig  # noqa: E402
from serial_io import SimulatedCSIProducer  # noqa: E402


def run(
    *,
    present: bool,
    bpm: float = 15.0,
    distance: float = 2.5,
    seconds: float = 90.0,
    seed: int = 1,
    motion_at: float | None = None,
    config: ProcessorConfig | None = None,
) -> list[dict]:
    calibration = Path(tempfile.gettempdir()) / f"rs-eval-{seed}.json"
    calibration.unlink(missing_ok=True)
    processor = CSIProcessor(config or ProcessorConfig(), calibration_path=calibration)
    sim = SimulatedCSIProducer(bpm=bpm, distance_m=distance, present=present, seed=seed)
    start = sim._clock
    next_update = start + 1.0
    results = []
    motion_done = False
    while sim._clock - start < seconds:
        if motion_at is not None and not motion_done and sim._clock - start >= motion_at:
            sim.configure(motion_seconds=4.0)
            motion_done = True
        frame = sim.next_frame()
        if frame is not None:
            processor.add_frame(frame)
        if sim._clock >= next_update:
            telemetry = processor.telemetry(now=sim._clock)
            telemetry["t"] = round(sim._clock - start, 1)
            results.append(telemetry)
            next_update += 1.0
    return results


def summarise(results: list[dict], after: float = 30.0) -> dict:
    tail = [r for r in results if r["t"] >= after]
    valid = [r for r in tail if r["valid"]]
    breathing = [r for r in tail if r["state"] == "breathing"]
    bpms = [r["bpm"] for r in breathing if r["bpm"] is not None]
    return {
        "updates": len(tail),
        "breathing_frac": len(breathing) / max(1, len(tail)),
        "motion_frac": sum(r["state"] == "motion" for r in tail) / max(1, len(tail)),
        "bpm_median": float(np.median(bpms)) if bpms else None,
        "conf_median": float(np.median([r["confidence"] for r in valid])) if valid else 0.0,
        "periodicity": float(np.median([r["periodicity"] for r in valid])) if valid else 0.0,
        "concentration": float(np.median([r["concentration"] for r in valid])) if valid else 0.0,
        "strength_db": float(np.median([r["strength_db"] for r in valid])) if valid else 0.0,
        "strength_p90": float(np.percentile([r["strength_db"] for r in valid], 90)) if valid else 0.0,
        "periodicity_p90": float(np.percentile([r["periodicity"] for r in valid], 90)) if valid else 0.0,
        "concentration_p90": float(np.percentile([r["concentration"] for r in valid], 90)) if valid else 0.0,
        "motion_p90": float(np.percentile([r["motion_index"] for r in valid], 90)) if valid else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    seeds = [1, 2] if args.quick else [1, 2, 3, 4]

    print("== Empty room (false alarms) ==")
    empty_rates = []
    for seed in seeds:
        s = summarise(run(present=False, seconds=150, seed=seed))
        empty_rates.append(s["breathing_frac"])
        print(
            f"seed {seed}: breathing {s['breathing_frac']:.0%}  conf~{s['conf_median']:.2f}  "
            f"period p90 {s['periodicity_p90']:.2f}  conc p90 {s['concentration_p90']:.2f}  "
            f"strength med {s['strength_db']:.1f} p90 {s['strength_p90']:.1f} dB  motion p90 {s['motion_p90']:.2f}"
        )

    print("\n== Person present (detection rate / BPM error) ==")
    distances = [1.0, 2.5, 4.0, 5.0] if args.quick else [1.0, 2.0, 3.0, 4.0, 5.0]
    rates = [8.0, 15.0, 26.0] if args.quick else [8.0, 12.0, 16.0, 22.0, 28.0]
    errors = []
    detections = []
    for distance in distances:
        row = []
        for bpm in rates:
            s = summarise(run(present=True, bpm=bpm, distance=distance, seconds=80, seed=int(distance * 10 + bpm)))
            detections.append(s["breathing_frac"])
            if s["bpm_median"] is not None:
                errors.append(abs(s["bpm_median"] - bpm))
            shown = f"{s['bpm_median']:.1f}" if s["bpm_median"] is not None else "  -- "
            row.append(
                f"{bpm:>4.0f}->{shown:>5} {s['breathing_frac']:>4.0%} m{s['motion_frac']:.0%} "
                f"c{s['conf_median']:.2f} {s['strength_db']:>4.1f}dB"
            )
        print(f"{distance:.1f} m | " + " | ".join(row))

    print("\n== Movement at t=45 s, person at 2 m ==")
    timeline = run(present=True, bpm=14, distance=2.0, seconds=100, seed=9, motion_at=45.0)
    print(" ".join(f"{r['t']:.0f}:{r['state'][0].upper()}" for r in timeline if r["t"] >= 38 and r["t"] <= 80))

    print("\n== Summary ==")
    print(f"empty-room false-alarm rate: {np.mean(empty_rates):.1%}")
    print(f"detection rate (all present cases, after 30 s): {np.mean(detections):.1%}")
    if errors:
        print(f"BPM abs error: median {np.median(errors):.2f}, max {np.max(errors):.2f}")


if __name__ == "__main__":
    main()

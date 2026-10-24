"""Regression tests for the serial parser and processor robustness fixes.

    python -m pytest tests          (if pytest is installed)
    python tests/test_pipeline.py   (no dependencies beyond the backend's)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processing import (  # noqa: E402
    CSIFrame,
    CSIProcessor,
    DistanceModel,
    DistanceObservation,
    _remove_gain_steps,
)
from serial_io import ClockAligner, CSIStreamParser, SimulatedCSIProducer  # noqa: E402


def _processor() -> CSIProcessor:
    path = Path(tempfile.mkdtemp()) / "calibration.json"
    return CSIProcessor(calibration_path=path)


def _frame(t: float, width: int = 64) -> CSIFrame:
    rng = np.random.default_rng(int(t * 1000) % 2**32)
    return CSIFrame(t, -50.0, np.abs(rng.normal(30.0, 5.0, width)))


def test_compact_line_parses() -> None:
    parser = CSIStreamParser()
    values = ",".join(f"{v:.1f}" for v in np.linspace(10, 40, 64))
    frame = parser.parse_line(f"CSI,1000000,-45,{values}\r\n", host_now=100.0)
    assert frame is not None
    assert frame.amplitudes.shape == (64,)
    assert frame.rssi == -45.0
    assert abs(frame.timestamp_s - 100.0) < 1e-6


def test_status_and_log_lines_are_not_frames() -> None:
    messages: list[str] = []
    parser = CSIStreamParser(on_message=messages.append)
    assert parser.parse_line("#WIFI,disconnected,reason=201") is None
    assert "not found" in parser.device["wifi"]["reason_text"]
    assert parser.parse_line("#STAT,pps=98.5,dropped=0,gain=compensate") is None
    assert parser.device["stats"]["pps"] == "98.5"
    assert parser.parse_line("I (1234) wifi:connected with hotspot") is None
    assert parser.dropped == 0 and len(messages) == 3


def test_stock_csi_data_line() -> None:
    parser = CSIStreamParser()
    header = ["CSI_DATA", "0", "aa:bb:cc:dd:ee:ff", "-52"] + ["0"] * 14 + ["123456"] + ["0"] * 5
    payload = '"[' + ",".join(["3", "4"] * 64) + ']"'
    frame = parser.parse_line(",".join(header) + "," + payload, host_now=50.0)
    assert frame is not None and frame.rssi == -52.0
    assert np.allclose(frame.amplitudes, 5.0)


def test_clock_resyncs_after_board_reboot() -> None:
    clock = ClockAligner()
    for i in range(1000):  # 10 s of packets
        clock.to_host(1_000_000 + i * 10_000, host_now=100.0 + i * 0.01)
    after_reboot = clock.to_host(500_000, host_now=112.0)  # device clock restarted
    assert clock.resyncs == 1
    assert abs(after_reboot - 112.0) < 0.05  # not squashed onto the old timeline


def test_stray_width_packets_do_not_reset_window() -> None:
    processor = _processor()
    t = 0.0
    for i in range(2000):
        t += 0.01
        processor.add_frame(_frame(t, 128 if i % 100 == 50 else 64))
    assert processor.dropped_width == 20
    assert processor.width_switches == 0
    assert len(processor._frames) == 1980


def test_width_switch_when_new_format_dominates() -> None:
    processor = _processor()
    t = 0.0
    for _ in range(500):
        t += 0.01
        processor.add_frame(_frame(t, 64))
    for _ in range(400):
        t += 0.01
        processor.add_frame(_frame(t, 128))
    assert processor.width_switches == 1
    assert processor._width == 128


def test_backwards_timestamp_starts_new_capture() -> None:
    processor = _processor()
    for i in range(300):
        processor.add_frame(_frame(10.0 + i * 0.01))
    processor.add_frame(_frame(2.0))
    assert len(processor._frames) == 1


def test_gain_step_is_removed() -> None:
    rng = np.random.default_rng(3)
    base = rng.uniform(20, 60, 40)
    data = base + rng.normal(0, 0.2, (300, 40))
    data[150:] *= 1.03  # a 3 % receiver gain step
    corrected, steps = _remove_gain_steps(data)
    assert steps >= 1
    before = np.median(corrected[140:150], axis=0)
    after = np.median(corrected[150:160], axis=0)
    assert np.median(np.abs(after / before - 1.0)) < 0.01


def test_distance_model_is_monotonic() -> None:
    observations = [
        DistanceObservation(1.0, 35.0, 30),
        DistanceObservation(2.0, 30.0, 30),
        DistanceObservation(3.0, 26.0, 30),
        DistanceObservation(4.0, 23.0, 30),
    ]
    model = DistanceModel.fit(observations)
    assert model is not None
    estimate, uncertainty = model.estimate(30.0)
    assert abs(estimate - 2.0) < 0.05 and uncertainty >= 0.35
    assert model.estimate(40.0)[0] <= model.estimate(20.0)[0]


def test_simulated_person_is_detected_with_correct_rate() -> None:
    processor = _processor()
    sim = SimulatedCSIProducer(bpm=18.0, distance_m=2.0, seed=11)
    start = sim._clock
    result = None
    while sim._clock - start < 50.0:
        frame = sim.next_frame()
        if frame is not None:
            processor.add_frame(frame)
        if sim._clock - start > 20.0 and int((sim._clock - start) * 100) % 100 == 0:
            result = processor.telemetry(now=sim._clock)
    assert result is not None and result["state"] == "breathing"
    assert abs(result["bpm"] - 18.0) < 1.0


def test_simulated_empty_room_stays_clear() -> None:
    processor = _processor()
    sim = SimulatedCSIProducer(present=False, seed=12)
    start = sim._clock
    states = []
    while sim._clock - start < 60.0:
        frame = sim.next_frame()
        if frame is not None:
            processor.add_frame(frame)
        if sim._clock - start > 20.0 and int((sim._clock - start) * 100) % 100 == 0:
            states.append(processor.telemetry(now=sim._clock)["state"])
    assert states and "breathing" not in states


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)

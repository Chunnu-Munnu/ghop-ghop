"""CSI breathing-detection pipeline and calibration for RescueSense.

The receiver sends per-subcarrier CSI *amplitudes* at roughly 100 Hz.  Every
second the newest 30 s are analysed:

1.  Packets of an unexpected width (e.g. a stray HT40 frame) are dropped
    instead of resetting the window; corrupted whole-frame outliers are
    removed; LLTF guard/DC bins are masked.
2.  Packets are averaged into 10 Hz bins.  Breathing lives below 0.6 Hz, so
    this costs nothing and cuts per-packet noise by ~3x.  It also keeps the
    band-pass filter well conditioned (the old 100 Hz design put the cutoff
    at 0.002 x Nyquist and its edge transients dominated the window).
3.  Every subcarrier is whitened by its own noise level (MAD of first
    differences), so quiet, informative subcarriers count as much as noisy
    ones in the combination.
4.  Gross body movement is detected from 0.8-4 Hz energy.  Breathing is only
    estimated from the data after the last movement.
5.  The band-passed subcarriers are combined with PCA; of the first few
    components, the most *periodic* one is kept (not the most energetic).
6.  Three scores describe it, each 0..1 and shown on the dashboard:
      rhythm   - autocorrelation at the breathing period (is it repeating?)
      focus    - spectral power concentrated at one rate (is it one rhythm?)
      strength - band power above the noise / empty-room reference
    Confidence is their weighted geometric mean, so all three must agree.
7.  A hysteresis state machine turns confidence into
    no_signal / warming_up / empty / breathing / motion, and BPM is the
    median of recent confident estimates.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
from scipy import ndimage, signal


EPS = 1.0e-12
CALIBRATION_VERSION = 2


@dataclass
class CSIFrame:
    """One CSI packet after the serial framing layer has decoded it."""

    timestamp_s: float
    rssi: float
    amplitudes: np.ndarray


@dataclass
class ProcessorConfig:
    sample_rate_hz: float = 100.0  # expected packet rate (diagnostics only)
    analysis_rate_hz: float = 10.0
    window_seconds: float = 30.0
    min_window_seconds: float = 15.0
    # Hotspots sometimes pause ICMP replies; tolerate ordinary jitter but
    # treat a genuine multi-second outage as the start of a new capture.
    max_gap_seconds: float = 2.0
    stale_after_seconds: float = 3.0
    breathing_low_hz: float = 0.10  # 6 BPM
    breathing_high_hz: float = 0.50  # 30 BPM
    filter_low_hz: float = 0.07
    filter_high_hz: float = 0.70
    motion_low_hz: float = 0.8
    motion_high_hz: float = 4.0
    motion_threshold: float = 2.5  # x noise-only energy in the motion band
    motion_settle_seconds: float = 2.0
    pca_candidates: int = 3
    drop_first_word: bool = True
    # Score ramps.  An empty-room calibration raises the floors to the
    # values that room actually produced.
    rhythm_floor: float = 0.35
    rhythm_full: float = 0.85
    focus_floor: float = 0.35
    focus_full: float = 0.80
    # Before an empty-room calibration exists: simulated empty rooms with
    # drift and gain steps sit around 14 dB (p90 ~18 dB); people at 1-5 m
    # land between 23 and 41 dB.  Calibration replaces this with the room's
    # own p90 + 1 dB.
    strength_ref_db: float = 13.0
    strength_span_db: float = 10.0
    enter_confidence: float = 0.55
    exit_confidence: float = 0.35
    enter_updates: int = 3
    exit_updates: int = 5
    bpm_min_confidence: float = 0.35
    history_seconds: int = 180


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


@dataclass
class Baseline:
    """Empty-room statistics that make the decision thresholds room-specific."""

    strength_db_median: float
    strength_db_p90: float
    periodicity_p90: float
    concentration_p90: float
    motion_p90: float
    samples: int
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_samples(cls, samples: Iterable[Mapping[str, float]]) -> "Baseline":
        rows = list(samples)
        if not rows:
            raise ValueError("No valid signal windows were captured for the baseline")

        def column(name: str) -> np.ndarray:
            return np.asarray([float(row[name]) for row in rows], dtype=float)

        strength = column("strength_db")
        return cls(
            strength_db_median=float(np.median(strength)),
            strength_db_p90=float(np.percentile(strength, 90)),
            periodicity_p90=float(np.percentile(column("periodicity"), 90)),
            concentration_p90=float(np.percentile(column("concentration"), 90)),
            motion_p90=float(np.percentile(column("motion_index"), 90)),
            samples=len(rows),
        )


@dataclass
class DistanceObservation:
    distance_m: float
    strength_db: float
    samples: int
    created_at: float = field(default_factory=time.time)


@dataclass
class DistanceModel:
    """Monotonic (isotonic) map: breathing strength above noise -> proximity."""

    strength_db: list[float]
    distance_m: list[float]
    uncertainty_m: float
    trained_observations: int

    @classmethod
    def fit(cls, observations: Iterable[DistanceObservation]) -> "DistanceModel | None":
        records = list(observations)
        if len(records) < 2:
            return None
        x = np.asarray([record.strength_db for record in records], dtype=float)
        y = np.asarray([record.distance_m for record in records], dtype=float)
        order = np.argsort(x)
        x, y = x[order], y[order]
        # Stronger breathing signal -> closer.  Pool-adjacent-violators keeps
        # the curve monotonic even when one anchor recording was noisy.
        fitted = _isotonic_decreasing(y)
        unique_x, inverse = np.unique(x, return_inverse=True)
        unique_y = np.asarray([float(np.mean(fitted[inverse == i])) for i in range(len(unique_x))])
        predicted = np.interp(x, unique_x, unique_y)
        rmse = float(np.sqrt(np.mean((predicted - y) ** 2)))
        spacing = float(np.median(np.abs(np.diff(np.sort(y))))) / 2.0 if len(y) > 1 else 0.5
        uncertainty = float(np.clip(max(rmse + 0.2, spacing, 0.35), 0.35, 2.5))
        return cls(unique_x.tolist(), unique_y.tolist(), uncertainty, len(records))

    def estimate(self, strength_db: float) -> tuple[float, float]:
        xs = np.asarray(self.strength_db)
        ys = np.asarray(self.distance_m)
        estimate = float(np.interp(strength_db, xs, ys))
        uncertainty = self.uncertainty_m
        # Outside the calibrated span the curve is flat: say so with width.
        if strength_db < xs[0] or strength_db > xs[-1]:
            uncertainty = min(2.5, uncertainty + 0.5)
        return float(np.clip(estimate, 0.0, 5.0)), uncertainty


def _isotonic_decreasing(values: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators regression with a non-increasing constraint."""

    blocks: list[list[float]] = []  # [first, last, count, mean]
    for index, value in enumerate(values.astype(float)):
        blocks.append([float(index), float(index), 1.0, float(value)])
        while len(blocks) >= 2 and blocks[-2][3] < blocks[-1][3]:
            right = blocks.pop()
            left = blocks.pop()
            count = left[2] + right[2]
            blocks.append([left[0], right[1], count, (left[2] * left[3] + right[2] * right[3]) / count])
    output = np.empty_like(values, dtype=float)
    for first, last, _count, mean in blocks:
        output[int(first) : int(last) + 1] = mean
    return output


class CalibrationManager:
    """Persistent empty-room baseline and multi-distance proximity anchors."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.baseline: Baseline | None = None
        self.observations: list[DistanceObservation] = []
        self.distance_model: DistanceModel | None = None
        self.active: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.load_note: str | None = None
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != CALIBRATION_VERSION:
                self.load_note = "Old calibration format ignored; please recalibrate."
                return
            if payload.get("baseline"):
                self.baseline = Baseline(**payload["baseline"])
            self.observations = [DistanceObservation(**item) for item in payload.get("observations", [])]
            self.distance_model = DistanceModel.fit(self.observations)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # A bad calibration file must never prevent a demo from starting.
            self.baseline, self.observations, self.distance_model = None, [], None
            self.load_note = f"Calibration file unreadable ({exc}); starting uncalibrated."

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CALIBRATION_VERSION,
            "baseline": asdict(self.baseline) if self.baseline else None,
            "observations": [asdict(item) for item in self.observations],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def start(self, kind: str, duration_seconds: float = 30.0, distance_m: float | None = None) -> dict[str, Any]:
        with self._lock:
            if kind not in {"empty", "distance"}:
                raise ValueError("Calibration kind must be 'empty' or 'distance'")
            if kind == "distance" and (distance_m is None or not 0.0 < distance_m <= 5.0):
                raise ValueError("Distance calibration needs distance_m between 0 and 5")
            if not 5.0 <= float(duration_seconds) <= 120.0:
                raise ValueError("duration_seconds must be between 5 and 120")
            self.active = {
                "kind": kind,
                "distance_m": float(distance_m) if distance_m is not None else None,
                "duration_seconds": float(duration_seconds),
                "started_monotonic": time.monotonic(),
                "samples": [],
                "skipped_motion": 0,
            }
            self.last_result = None
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.active:
                return self.status()
            return self._finish()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self.baseline = None
            self.observations = []
            self.distance_model = None
            self.active = None
            self.last_result = {"ok": True, "message": "Calibration cleared."}
            self.save()
            return self.status()

    def record(self, metrics: Mapping[str, Any]) -> None:
        with self._lock:
            if not self.active:
                return
            if metrics.get("valid") and metrics.get("state") != "motion":
                self.active["samples"].append(
                    {
                        "strength_db": float(metrics["strength_db"]),
                        "periodicity": float(metrics["periodicity"]),
                        "concentration": float(metrics["concentration"]),
                        "motion_index": float(metrics["motion_index"]),
                    }
                )
            elif metrics.get("state") == "motion":
                self.active["skipped_motion"] += 1
            if self._elapsed() >= self.active["duration_seconds"]:
                self._finish()

    def _elapsed(self) -> float:
        if not self.active:
            return 0.0
        return max(0.0, time.monotonic() - float(self.active["started_monotonic"]))

    def _finish(self) -> dict[str, Any]:
        assert self.active is not None
        active = self.active
        samples: list[dict[str, float]] = active["samples"]
        if len(samples) < 3:
            self.last_result = {
                "ok": False,
                "message": "Not enough clean signal captured (movement or no CSI). Keep still and retry.",
            }
        elif active["kind"] == "empty":
            self.baseline = Baseline.from_samples(samples)
            self.last_result = {
                "ok": True,
                "message": f"Empty-room baseline saved from {len(samples)} windows.",
                "samples": len(samples),
            }
        else:
            observation = DistanceObservation(
                distance_m=float(active["distance_m"]),
                strength_db=float(np.median([sample["strength_db"] for sample in samples])),
                samples=len(samples),
            )
            # Re-recording a distance replaces it instead of double-weighting it.
            self.observations = [
                item for item in self.observations if not math.isclose(item.distance_m, observation.distance_m, abs_tol=0.01)
            ]
            self.observations.append(observation)
            self.distance_model = DistanceModel.fit(self.observations)
            self.last_result = {
                "ok": True,
                "message": f"{observation.distance_m:g} m anchor saved ({observation.strength_db:.1f} dB).",
                "distance_m": observation.distance_m,
                "samples": observation.samples,
                "mapping_ready": self.distance_model is not None,
            }
        self.active = None
        self.save()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            active: dict[str, Any] | None = None
            if self.active:
                active = {
                    "kind": self.active["kind"],
                    "distance_m": self.active["distance_m"],
                    "duration_seconds": self.active["duration_seconds"],
                    "elapsed_seconds": round(self._elapsed(), 1),
                    "samples": len(self.active["samples"]),
                }
            baseline = None
            if self.baseline:
                baseline = {
                    "strength_db_p90": round(self.baseline.strength_db_p90, 2),
                    "periodicity_p90": round(self.baseline.periodicity_p90, 3),
                    "concentration_p90": round(self.baseline.concentration_p90, 3),
                    "samples": self.baseline.samples,
                    "created_at": self.baseline.created_at,
                }
            return {
                "active": active,
                "baseline_ready": self.baseline is not None,
                "baseline": baseline,
                "distance_points": sorted(
                    ({"distance_m": o.distance_m, "strength_db": round(o.strength_db, 2)} for o in self.observations),
                    key=lambda item: item["distance_m"],
                ),
                "mapping_ready": self.distance_model is not None,
                "uncertainty_m": round(self.distance_model.uncertainty_m, 2) if self.distance_model else None,
                "last_result": self.last_result,
                "note": self.load_note,
            }


# --------------------------------------------------------------------------
# Processor
# --------------------------------------------------------------------------


class CSIProcessor:
    """Stores recent frames and turns them into dashboard-ready telemetry."""

    def __init__(
        self,
        config: ProcessorConfig | None = None,
        calibration_path: str | Path = "calibration.json",
        simulated_distance_m: float | None = None,
    ):
        self.config = config or ProcessorConfig()
        self.calibration = CalibrationManager(calibration_path)
        self.presence_model: Any | None = None
        self.simulated_distance_m = simulated_distance_m
        self._frames: deque[tuple[float, float, np.ndarray]] = deque()
        self._width: int | None = None
        self._width_votes: deque[int] = deque(maxlen=400)
        self._lock = threading.RLock()
        self.dropped_width = 0
        self.width_switches = 0
        self.total_frames = 0
        self._state = "no_signal"
        self._above = 0
        self._below = 0
        self._bpm_estimates: deque[float] = deque(maxlen=7)
        self._prev_loading: np.ndarray | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=int(self.config.history_seconds))
        self._state_since = time.time()

    def set_presence_model(self, model: Any) -> None:
        self.presence_model = model

    # -- ingest ------------------------------------------------------------
    def add_frame(self, frame: CSIFrame) -> bool:
        amplitudes = np.asarray(frame.amplitudes, dtype=float).ravel()
        if amplitudes.size < 8 or not np.all(np.isfinite(amplitudes)):
            return False
        timestamp = float(frame.timestamp_s)
        if not math.isfinite(timestamp):
            return False
        width = int(amplitudes.size)
        with self._lock:
            self._width_votes.append(width)
            if self._width is None:
                self._width = width
            elif width != self._width:
                # One odd packet must not wipe 30 s of history (the old code
                # cleared the buffer on every width change).  Switch format
                # only when the new width clearly dominates recent traffic.
                votes = sum(1 for value in self._width_votes if value == width)
                if len(self._width_votes) >= 200 and votes > 0.6 * len(self._width_votes):
                    self._frames.clear()
                    self._width = width
                    self.width_switches += 1
                    self._reset_detection()
                else:
                    self.dropped_width += 1
                    return False
            if self._frames and timestamp <= self._frames[-1][0]:
                if timestamp < self._frames[-1][0] - 1.0:
                    # The source clock jumped backwards: a new capture.
                    self._frames.clear()
                    self._reset_detection()
                else:
                    timestamp = self._frames[-1][0] + 1.0e-4
            self._frames.append((timestamp, float(frame.rssi), amplitudes.astype(np.float32)))
            self.total_frames += 1
            cutoff = timestamp - (self.config.window_seconds + 5.0)
            while self._frames and self._frames[0][0] < cutoff:
                self._frames.popleft()
        return True

    def _reset_detection(self) -> None:
        self._above = 0
        self._below = 0
        self._bpm_estimates.clear()
        self._prev_loading = None

    # -- analysis ----------------------------------------------------------
    def telemetry(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            frames = list(self._frames)
            width = self._width
        result = self._analyse(frames, width, now)
        self.calibration.record(result)
        result["calibration"] = self.calibration.status()
        return result

    def _analyse(self, frames: list[tuple[float, float, np.ndarray]], width: int | None, now: float) -> dict[str, Any]:
        cfg = self.config
        link = self._link_stats(frames, width, now)
        if not frames:
            return self._idle("no_signal", "Waiting for CSI packets from the ESP32.", link)
        if now - frames[-1][0] > cfg.stale_after_seconds:
            return self._idle("no_signal", "The CSI stream stopped. Check the board and its Wi-Fi link.", link)

        times = np.fromiter((item[0] for item in frames), dtype=float, count=len(frames))
        gaps = np.flatnonzero(np.diff(times) > cfg.max_gap_seconds)
        start = int(gaps[-1] + 1) if gaps.size else 0
        start = max(start, int(np.searchsorted(times, times[-1] - cfg.window_seconds)))
        times = times[start:]
        segment = frames[start:]
        duration = float(times[-1] - times[0]) if times.size > 1 else 0.0
        link["window_seconds"] = round(duration, 1)
        if duration < cfg.min_window_seconds or times.size < 50:
            return self._idle(
                "warming_up",
                f"Collecting signal: {duration:.0f} of {cfg.min_window_seconds:.0f} s.",
                link,
                progress=duration / cfg.min_window_seconds,
            )

        rows = np.vstack([item[2] for item in segment]).astype(float)
        mask = self._carrier_mask(rows)
        link["subcarriers_used"] = int(mask.sum())
        if mask.sum() < 4:
            return self._idle("no_signal", "CSI rows contain too few usable subcarriers.", link)
        rows, times = _reject_bad_frames(rows[:, mask], times)
        fs = cfg.analysis_rate_hz
        grid, binned = _bin_average(times, rows, fs)
        binned = _hampel(binned, 7, 3.5)
        binned, gain_steps = _remove_gain_steps(binned)
        link["gain_steps_corrected"] = gain_steps
        sigma = _noise_sigma(binned)
        whitened = signal.detrend((binned - binned.mean(axis=0)) / sigma, axis=0, type="linear")

        # --- gross movement -------------------------------------------------
        motion_trace = _band_energy(whitened, fs, cfg.motion_low_hz, cfg.motion_high_hz)
        recent = max(1, int(round(2.0 * fs)))
        motion_index = float(np.mean(motion_trace[-recent:]))
        moving = np.flatnonzero(motion_trace > cfg.motion_threshold)
        heatmap = _heatmap(whitened, fs, cfg)
        common = {"link": link, "heatmap": heatmap, "motion_index": round(motion_index, 2)}
        if motion_index > cfg.motion_threshold:
            return self._motion_result("Large movement in the sensing area. Breathing cannot be measured while it lasts.", common, motion_index)
        analysis_start = 0
        if moving.size:
            analysis_start = int(moving[-1] + cfg.motion_settle_seconds * fs)
        usable_seconds = (whitened.shape[0] - analysis_start) / fs
        if usable_seconds < cfg.min_window_seconds:
            return self._motion_result(
                f"Settling after movement: {max(0.0, usable_seconds):.0f} of {cfg.min_window_seconds:.0f} s of still signal.",
                common,
                motion_index,
                progress=max(0.0, usable_seconds) / cfg.min_window_seconds,
            )
        still = whitened[analysis_start:]
        still_grid = grid[analysis_start:]

        # --- breathing -------------------------------------------------------
        filtered = _bandpass(still, fs, cfg.filter_low_hz, cfg.filter_high_hz)
        try:
            _u, _s, vt = np.linalg.svd(filtered, full_matrices=False)
        except np.linalg.LinAlgError:
            return self._idle("no_signal", "Signal decomposition failed; waiting for the next window.", link)
        candidates = []
        for index in range(min(cfg.pca_candidates, vt.shape[0])):
            loading = vt[index]
            metrics = self._breathing_metrics(filtered @ loading, fs)
            if metrics is not None:
                metrics["component"] = index
                metrics["loading"] = loading
                candidates.append(metrics)
        if not candidates:
            return self._idle("no_signal", "Window too short for breathing analysis.", link)
        best = max(candidates, key=lambda item: item["quality"])
        loading = best["loading"]
        component = best["filtered"]
        if self._prev_loading is not None and self._prev_loading.shape == loading.shape and float(np.dot(loading, self._prev_loading)) < 0:
            # Keep the waveform's sign stable between updates (PCA sign is arbitrary).
            loading, component = -loading, -component
        self._prev_loading = loading

        scores, confidence = self._score(best)
        model_probability = self._model_probability(best, motion_index)
        if model_probability is not None:
            confidence = 0.5 * confidence + 0.5 * model_probability
        state, detail = self._advance_state(confidence)

        bpm_instant = best["bpm"]
        if confidence >= cfg.bpm_min_confidence:
            self._bpm_estimates.append(bpm_instant)
        bpm = float(np.median(self._bpm_estimates)) if state == "breathing" and self._bpm_estimates else None

        distance_m = uncertainty_m = None
        distance_source = None
        if state == "breathing":
            if self.calibration.distance_model is not None:
                distance_m, uncertainty_m = self.calibration.distance_model.estimate(best["strength_db"])
                distance_source = "calibrated"
            elif self.simulated_distance_m is not None:
                distance_m, uncertainty_m = float(np.clip(self.simulated_distance_m, 0.0, 5.0)), 0.35
                distance_source = "simulated"

        raw_component = _lowpass(still, fs, 2.0) @ loading
        analytic = signal.hilbert(component)
        self._push_history(state, confidence, bpm if bpm is not None else (bpm_instant if confidence >= cfg.bpm_min_confidence else None))
        return {
            "valid": True,
            "state": state,
            "state_detail": detail,
            "presence": state == "breathing",
            "presence_source": "random_forest+heuristic" if model_probability is not None else "heuristic",
            "bpm": round(bpm, 1) if bpm is not None else None,
            "bpm_instant": round(bpm_instant, 1),
            "bpm_acf": round(best["bpm_acf"], 1) if best["bpm_acf"] else None,
            "rates_agree": bool(best["agree"]),
            "confidence": round(confidence, 3),
            "scores": {key: round(value, 3) for key, value in scores.items()},
            "periodicity": round(best["periodicity"], 3),
            "concentration": round(best["concentration"], 3),
            "strength_db": round(best["strength_db"], 2),
            "motion_index": round(motion_index, 2),
            "model_probability": round(model_probability, 3) if model_probability is not None else None,
            "distance_m": round(distance_m, 2) if distance_m is not None else None,
            "distance_uncertainty_m": round(uncertainty_m, 2) if uncertainty_m is not None else None,
            "distance_source": distance_source,
            "distance_calibrated": self.calibration.distance_model is not None,
            "breath_phase": round(float(np.angle(analytic[-1])), 3),
            "breath_rate_hz": round(bpm_instant / 60.0, 4),
            "pca_component": int(best["component"]),
            "analysis_seconds": round(float(still_grid[-1] - still_grid[0]), 1),
            "waveform": _waveform(still_grid, component, now),
            "raw_waveform": _waveform(still_grid, raw_component, now),
            "spectrum": _spectrum_points(best["frequencies"], best["psd"]),
            "spectrum_peak_hz": round(best["peak_hz"], 4),
            "heatmap": heatmap,
            "link": link,
            "history": list(self._history),
            "state_since": self._state_since,
            "rssi": link.get("rssi"),
        }

    # -- metrics -----------------------------------------------------------
    def _breathing_metrics(self, component: np.ndarray, fs: float) -> dict[str, Any] | None:
        cfg = self.config
        x = component - float(np.mean(component))
        n = x.size
        if n < int(cfg.min_window_seconds * fs * 0.8):
            return None
        band_fraction = (cfg.filter_high_hz - cfg.filter_low_hz) / (fs / 2.0)
        variance = float(np.mean(x**2))
        # The input was whitened, so a unit-norm projection of pure noise has
        # unit variance before filtering.  This makes strength dimensionless.
        strength_db = 10.0 * math.log10(max(variance, EPS) / band_fraction)

        window = signal.windows.hann(n)
        nfft = max(4096, 1 << (n - 1).bit_length())
        psd = np.abs(np.fft.rfft(x * window, nfft)) ** 2
        frequencies = np.fft.rfftfreq(nfft, 1.0 / fs)
        search = (frequencies >= cfg.breathing_low_hz) & (frequencies <= cfg.breathing_high_hz)
        passband = (frequencies >= cfg.filter_low_hz) & (frequencies <= cfg.filter_high_hz)
        if not search.any():
            return None
        search_idx = np.flatnonzero(search)
        peak_index = int(search_idx[np.argmax(psd[search])])
        peak_hz = float(frequencies[peak_index])
        near_peak = passband & (np.abs(frequencies - peak_hz) <= 0.05)
        concentration = float(np.sum(psd[near_peak]) / max(np.sum(psd[passband]), EPS))

        periodicity, acf_period = _autocorrelation_period(x, fs, cfg.breathing_low_hz, cfg.breathing_high_hz)
        bpm = peak_hz * 60.0
        bpm_acf = 60.0 / acf_period if acf_period else None
        agree = bool(bpm_acf and abs(bpm_acf - bpm) <= max(1.5, 0.12 * bpm))
        quality = max(periodicity, 0.0) * concentration * (1.0 + 0.02 * max(strength_db, 0.0))
        return {
            "filtered": x,
            "frequencies": frequencies,
            "psd": psd,
            "peak_hz": peak_hz,
            "bpm": bpm,
            "bpm_acf": bpm_acf,
            "agree": agree,
            "periodicity": float(max(periodicity, 0.0)),
            "concentration": concentration,
            "strength_db": strength_db,
            "quality": quality,
        }

    def _score(self, best: Mapping[str, Any]) -> tuple[dict[str, float], float]:
        cfg = self.config
        rhythm_floor, focus_floor, strength_ref = cfg.rhythm_floor, cfg.focus_floor, cfg.strength_ref_db
        baseline = self.calibration.baseline
        if baseline is not None:
            # What the empty room produced is, by definition, "nothing".
            rhythm_floor = float(np.clip(baseline.periodicity_p90, cfg.rhythm_floor, cfg.rhythm_full - 0.2))
            focus_floor = float(np.clip(baseline.concentration_p90, cfg.focus_floor, cfg.focus_full - 0.2))
            strength_ref = baseline.strength_db_p90 + 1.0
        rhythm = _ramp(best["periodicity"], rhythm_floor, cfg.rhythm_full)
        focus = _ramp(best["concentration"], focus_floor, cfg.focus_full)
        strength = _ramp(best["strength_db"], strength_ref, strength_ref + cfg.strength_span_db)
        confidence = (rhythm**0.4) * (focus**0.3) * (strength**0.3)
        if not best["agree"]:
            confidence *= 0.7
        return {"rhythm": rhythm, "focus": focus, "strength": strength}, float(confidence)

    def _model_probability(self, best: Mapping[str, Any], motion_index: float) -> float | None:
        if self.presence_model is None:
            return None
        features = np.asarray(
            [[best["strength_db"], best["periodicity"], best["concentration"], motion_index]], dtype=float
        )
        try:
            probabilities = self.presence_model.predict_proba(features)[0]
            classes = list(getattr(self.presence_model, "classes_", []))
            return float(probabilities[classes.index(1)]) if 1 in classes else None
        except Exception:
            return None

    def _advance_state(self, confidence: float) -> tuple[str, str]:
        cfg = self.config
        previous = self._state
        if previous != "breathing":
            self._above = self._above + 1 if confidence >= cfg.enter_confidence else 0
            self._below = 0
            state = "breathing" if self._above >= cfg.enter_updates else "empty"
        else:
            self._below = self._below + 1 if confidence < cfg.exit_confidence else 0
            self._above = 0
            state = "empty" if self._below >= cfg.exit_updates else "breathing"
        self._set_state(state)
        if state == "breathing":
            detail = "A steady breathing rhythm stands out above the background."
        elif self._above > 0:
            detail = f"Possible breathing rhythm; confirming ({self._above}/{cfg.enter_updates})."
        else:
            detail = "No breathing rhythm above the background level."
        return state, detail

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._state_since = time.time()

    def _motion_result(self, detail: str, common: dict[str, Any], motion_index: float, progress: float | None = None) -> dict[str, Any]:
        self._above = 0
        self._below = 0
        self._bpm_estimates.clear()
        self._set_state("motion")
        self._push_history("motion", 0.0, None)
        result = self._blank("motion", detail, common["link"], progress)
        result.update({"heatmap": common["heatmap"], "motion_index": round(motion_index, 2), "history": list(self._history)})
        return result

    def _idle(self, state: str, detail: str, link: dict[str, Any], progress: float | None = None) -> dict[str, Any]:
        if state in {"no_signal", "warming_up"}:
            self._reset_detection()
        self._set_state(state)
        return self._blank(state, detail, link, progress)

    def _blank(self, state: str, detail: str, link: dict[str, Any], progress: float | None) -> dict[str, Any]:
        return {
            "valid": False,
            "state": state,
            "state_detail": detail,
            "progress": round(float(np.clip(progress, 0.0, 1.0)), 3) if progress is not None else None,
            "presence": False,
            "presence_source": "waiting",
            "bpm": None,
            "bpm_instant": None,
            "confidence": 0.0,
            "scores": {"rhythm": 0.0, "focus": 0.0, "strength": 0.0},
            "periodicity": 0.0,
            "concentration": 0.0,
            "strength_db": 0.0,
            "motion_index": 0.0,
            "distance_m": None,
            "distance_uncertainty_m": None,
            "distance_source": None,
            "distance_calibrated": self.calibration.distance_model is not None,
            "waveform": [],
            "raw_waveform": [],
            "spectrum": [],
            "heatmap": None,
            "link": link,
            "history": list(self._history),
            "state_since": self._state_since,
            "rssi": link.get("rssi"),
        }

    def _push_history(self, state: str, confidence: float, bpm: float | None) -> None:
        self._history.append(
            {"t": round(time.time(), 1), "state": state, "c": round(confidence, 3), "bpm": round(bpm, 1) if bpm else None}
        )

    def _link_stats(self, frames: list[tuple[float, float, np.ndarray]], width: int | None, now: float) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "frames_buffered": len(frames),
            "frame_width": width,
            "dropped_width": self.dropped_width,
            "width_switches": self.width_switches,
            "total_frames": self.total_frames,
            "packet_rate_hz": 0.0,
            "rssi": None,
            "last_frame_age_s": None,
            "window_seconds": 0.0,
            "subcarriers_used": None,
        }
        if frames:
            last = frames[-1][0]
            stats["last_frame_age_s"] = round(max(0.0, now - last), 2)
            recent = [item for item in frames if item[0] >= last - 5.0]
            span = recent[-1][0] - recent[0][0] if len(recent) > 1 else 0.0
            if span > 0.5:
                stats["packet_rate_hz"] = round((len(recent) - 1) / span, 1)
            rssi = [item[1] for item in frames if item[0] >= last - 2.0]
            stats["rssi"] = round(float(np.median(rssi)), 1)
        return stats

    def _carrier_mask(self, rows: np.ndarray) -> np.ndarray:
        count = rows.shape[1]
        mask = np.zeros(count, dtype=bool)
        if count == 64:
            # ESP32-S3 20 MHz LLTF order is [0..31, -32..-1]; data tones are
            # +1..+26 and -26..-1.  Bin 1 shares the possibly-invalid first word.
            mask[1:27] = True
            mask[38:64] = True
            if self.config.drop_first_word:
                mask[1] = False
        elif count == 128:
            # 40 MHz LLTF order [0..63, -64..-1]: keep |k| in 2..58; the
            # near-zero test below drops the per-half DC nulls.
            mask[2:59] = True
            mask[70:127] = True
        else:
            guard = min(6, max(1, count // 8))
            mask[guard:-guard] = True
            mask[count // 2] = False
        median_amplitude = np.median(rows, axis=0)
        spread = np.ptp(rows, axis=0)
        mask &= np.isfinite(median_amplitude) & (median_amplitude > 0.5) & (spread > 0.0)
        return mask


# --------------------------------------------------------------------------
# Numerical helpers
# --------------------------------------------------------------------------


def _ramp(value: float, low: float, high: float) -> float:
    if high <= low:
        return 1.0 if value >= high else 0.0
    return float(np.clip((value - low) / (high - low), 0.0, 1.0))


def _reject_bad_frames(rows: np.ndarray, times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Drop packets whose overall level jumps away from their neighbours."""

    level = rows.mean(axis=1)
    if level.size < 30:
        return rows, times
    local = ndimage.median_filter(level, size=51, mode="nearest")
    deviation = np.abs(level - local)
    mad = float(np.median(deviation))
    threshold = 6.0 * 1.4826 * mad + 0.02 * float(np.median(local)) + 1.0e-6
    keep = deviation <= threshold
    if keep.mean() < 0.7:  # that much "outlier" is real movement, not glitches
        return rows, times
    return rows[keep], times[keep]


def _bin_average(times: np.ndarray, rows: np.ndarray, rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """Average irregular packets into uniform bins; interpolate short holes."""

    index = np.floor((times - times[0]) * rate_hz).astype(int)
    bins = int(index[-1]) + 1
    starts = np.flatnonzero(np.r_[True, np.diff(index) > 0])
    sums = np.add.reduceat(rows, starts, axis=0)
    counts = np.diff(np.r_[starts, index.size])
    filled = index[starts]
    means = sums / counts[:, None]
    grid = times[0] + (np.arange(bins) + 0.5) / rate_hz
    if filled.size == bins:
        return grid, means
    out = np.empty((bins, rows.shape[1]), dtype=float)
    centers = np.arange(bins, dtype=float)
    for column in range(rows.shape[1]):
        out[:, column] = np.interp(centers, filled.astype(float), means[:, column])
    return grid, out


def _hampel(values: np.ndarray, window: int, sigmas: float) -> np.ndarray:
    """Replace impulsive samples with their local median, per subcarrier."""

    window = max(3, int(window) | 1)
    median = ndimage.median_filter(values, size=(window, 1), mode="nearest")
    deviation = np.abs(values - median)
    mad = ndimage.median_filter(deviation, size=(window, 1), mode="nearest")
    threshold = sigmas * 1.4826 * mad
    return np.where((deviation > threshold) & (threshold > 1.0e-9), median, values)


def _remove_gain_steps(values: np.ndarray) -> tuple[np.ndarray, int]:
    """Undo residual receiver-gain steps.

    A gain change multiplies every subcarrier by the same factor, i.e. adds
    one constant to all log-amplitudes in a single bin.  Breathing and body
    motion move subcarriers in different directions, so the across-carrier
    median of the log change stays small for them.
    """

    logs = np.log(np.maximum(values, 1.0e-3))
    common = np.median(np.diff(logs, axis=0), axis=1)
    mad = float(np.median(np.abs(common - np.median(common)))) * 1.4826
    threshold = max(8.0 * mad, 0.004)
    jumps = np.flatnonzero(np.abs(common) > threshold)
    if not jumps.size:
        return values, 0
    correction = np.zeros(values.shape[0])
    for index in jumps:
        correction[index + 1 :] += common[index]
    return values * np.exp(-correction)[:, None], int(jumps.size)


def _noise_sigma(values: np.ndarray) -> np.ndarray:
    """Per-subcarrier white-noise level from the MAD of first differences.

    Breathing barely changes from one 0.1 s bin to the next, so differences
    are dominated by measurement noise; the median keeps brief movement out.
    """

    diffs = np.diff(values, axis=0)
    mad = np.median(np.abs(diffs - np.median(diffs, axis=0)), axis=0)
    sigma = 1.4826 * mad / math.sqrt(2.0)
    floor = np.maximum(0.02, 1.0e-3 * np.abs(np.median(values, axis=0)))
    return np.maximum(sigma, floor)


def _bandpass(values: np.ndarray, fs: float, low: float, high: float, order: int = 4) -> np.ndarray:
    sos = signal.butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
    padlen = min(values.shape[0] - 1, int(3.0 * fs / low))
    return signal.sosfiltfilt(sos, values, axis=0, padtype="odd", padlen=padlen)


def _lowpass(values: np.ndarray, fs: float, cutoff: float) -> np.ndarray:
    sos = signal.butter(4, cutoff, btype="lowpass", fs=fs, output="sos")
    padlen = min(values.shape[0] - 1, int(3.0 * fs))
    return signal.sosfiltfilt(sos, values, axis=0, padtype="odd", padlen=padlen)


def _band_energy(whitened: np.ndarray, fs: float, low: float, high: float) -> np.ndarray:
    """Movement trace: band energy per 0.1 s bin relative to pure noise (=1)."""

    high = min(high, 0.45 * fs)
    sos = signal.butter(4, [low, high], btype="bandpass", fs=fs, output="sos")
    banded = signal.sosfiltfilt(sos, whitened, axis=0, padtype="odd", padlen=min(whitened.shape[0] - 1, int(3 * fs)))
    expected = (high - low) / (fs / 2.0)
    energy = np.mean(banded**2, axis=1) / expected
    # A median over 1.5 s ignores sub-second glitches; real movement lasts.
    return ndimage.median_filter(energy, size=max(3, int(1.5 * fs) | 1), mode="nearest")


def _autocorrelation_period(x: np.ndarray, fs: float, low_hz: float, high_hz: float) -> tuple[float, float | None]:
    """Pearson autocorrelation peak within the breathing-period range.

    Returns (peak correlation, period in seconds).  A clean breathing trace
    scores 0.7-0.95; band-limited noise rarely exceeds 0.4.
    """

    n = x.size
    lag_min = max(2, int(round(fs / high_hz)))
    lag_max = int(min(round(fs / low_hz), 0.6 * n))
    if lag_max <= lag_min + 2:
        return 0.0, None
    lags = np.arange(lag_min - 1, lag_max + 2)
    values = np.empty(lags.size)
    for i, lag in enumerate(lags):
        a, b = x[:-lag], x[lag:]
        denominator = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
        values[i] = float(np.dot(a, b)) / denominator if denominator > EPS else 0.0
    inner = values[1:-1]
    peaks = np.flatnonzero((inner >= values[:-2]) & (inner >= values[2:]))
    if not peaks.size:
        return float(max(0.0, inner.max())), None
    best = float(inner[peaks].max())
    if best <= 0.0:
        return 0.0, None
    # Prefer the fundamental: the first peak that is nearly as high as the best.
    chosen = int(peaks[np.flatnonzero(inner[peaks] >= 0.85 * best)[0]])
    i = chosen + 1
    left, center, right = values[i - 1], values[i], values[i + 1]
    denominator = left - 2 * center + right
    offset = 0.5 * (left - right) / denominator if abs(denominator) > EPS else 0.0
    period = (lags[i] + float(np.clip(offset, -0.5, 0.5))) / fs
    return float(center), period


def _waveform(times: np.ndarray, values: np.ndarray, now: float, step: int = 2) -> list[list[float]]:
    if values.size == 0:
        return []
    picked = slice(None, None, step)
    return [[round(float(t - now), 2), round(float(v), 4)] for t, v in zip(times[picked], values[picked])]


def _spectrum_points(frequencies: np.ndarray, psd: np.ndarray) -> list[list[float]]:
    target = np.round(np.arange(0.02, 1.0001, 0.01), 3)
    values = np.interp(target, frequencies, psd)
    peak = float(values[target >= 0.05].max()) or 1.0
    return [[float(f), round(float(v / peak), 4)] for f, v in zip(target, values)]


def _heatmap(whitened: np.ndarray, fs: float, cfg: ProcessorConfig) -> dict[str, Any]:
    """Subcarrier x time picture (int -100..100) for the dashboard waterfall."""

    sos = signal.butter(3, [cfg.filter_low_hz, 1.5], btype="bandpass", fs=fs, output="sos")
    banded = signal.sosfiltfilt(sos, whitened, axis=0, padtype="odd", padlen=min(whitened.shape[0] - 1, int(3 * fs / cfg.filter_low_hz)))
    step = max(1, int(round(fs / 2.0)))  # 2 columns per second
    columns = ndimage.uniform_filter1d(banded, size=step, axis=0, mode="nearest")[::step]
    scale = float(np.percentile(np.abs(columns), 97)) or 1.0
    picture = np.clip(np.round(columns / scale * 100.0), -100, 100).astype(int)
    return {"seconds": round(whitened.shape[0] / fs, 1), "rows": picture.T.tolist()}

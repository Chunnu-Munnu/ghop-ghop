"""CSI signal processing and calibration primitives for RescueSense.

The receiver sends *amplitude* (not IQ) samples for every CSI subcarrier.  The
pipeline below is deliberately conservative: it throws away LLTF guard/DC
carriers, makes the time axis uniform, rejects impulsive samples, and chooses
the PCA component with the strongest respiration-band energy.
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


@dataclass
class CSIFrame:
    """One CSI callback after the serial framing layer has decoded it."""

    timestamp_s: float
    rssi: float
    amplitudes: np.ndarray


@dataclass
class ProcessorConfig:
    sample_rate_hz: float = 100.0
    window_seconds: float = 30.0
    min_analysis_seconds: float = 10.0
    breathing_low_hz: float = 0.10
    breathing_high_hz: float = 0.50
    hampel_window_samples: int = 9
    hampel_sigmas: float = 3.0
    # Router/phone hotspots sometimes pause ICMP replies or deliver CSI in
    # short bursts.  Half a second was too strict and repeatedly discarded a
    # perfectly usable 30 s window; tolerate ordinary Wi-Fi jitter while
    # still treating a genuine multi-second outage as a new capture.
    max_gap_seconds: float = 2.0
    drop_first_word: bool = True
    min_confidence: float = 0.10
    default_variance_threshold: float = 0.20
    default_band_energy_threshold: float = 0.002
    bpm_stability_windows: int = 4
    bpm_max_spread_bpm: float = 3.0


@dataclass
class Baseline:
    """Empty-room metrics used to make presence decisions channel-specific."""

    variance_mean: float
    variance_std: float
    band_energy_mean: float
    band_energy_std: float
    samples: int
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_samples(cls, samples: Iterable[Mapping[str, float]]) -> "Baseline":
        rows = list(samples)
        if not rows:
            raise ValueError("No valid signal windows were captured for the baseline")
        variance = np.asarray([float(row["variance"]) for row in rows], dtype=float)
        energy = np.asarray([float(row["band_energy"]) for row in rows], dtype=float)
        return cls(
            variance_mean=float(np.mean(variance)),
            variance_std=float(np.std(variance, ddof=0)),
            band_energy_mean=float(np.mean(energy)),
            band_energy_std=float(np.std(energy, ddof=0)),
            samples=len(rows),
        )


@dataclass
class DistanceObservation:
    distance_m: float
    band_energy: float
    snr_db: float
    samples: int
    created_at: float = field(default_factory=time.time)


@dataclass
class DistanceModel:
    """A one-dimensional isotonic calibration: energy/SNR score -> proximity."""

    feature_score: list[float]
    distance_m: list[float]
    uncertainty_m: float
    trained_observations: int

    @classmethod
    def fit(cls, observations: Iterable[DistanceObservation]) -> "DistanceModel | None":
        records = list(observations)
        if len(records) < 2:
            return None

        # Log energy reduces the near/far dynamic range. A deliberately small
        # SNR term makes the score more robust when an otherwise energetic
        # window is broad/noisy. The desired relation is decreasing distance
        # as this calibrated score grows; PAV preserves monotonicity.
        x = np.asarray(
            [_proximity_score(record.band_energy, record.snr_db) for record in records], dtype=float
        )
        y = np.asarray([record.distance_m for record in records], dtype=float)
        order = np.argsort(x)
        x, y = x[order], y[order]
        fitted_y = _isotonic_decreasing(y)

        # np.interp needs unique x coordinates.  Average any repeated-energy
        # calibration points (rare, but possible with quantized CSI amplitudes).
        unique_x, inverse = np.unique(x, return_inverse=True)
        unique_y = np.asarray(
            [float(np.mean(fitted_y[inverse == index])) for index in range(len(unique_x))]
        )
        predicted = np.interp(x, unique_x, unique_y)
        rmse = float(np.sqrt(np.mean((predicted - y) ** 2)))
        if len(unique_y) > 1:
            spacing = float(np.median(np.abs(np.diff(np.sort(unique_y))))) / 2.0
        else:
            spacing = 0.5
        uncertainty = float(np.clip(max(0.35, rmse + 0.20, spacing), 0.35, 2.5))
        return cls(
            feature_score=unique_x.tolist(),
            distance_m=unique_y.tolist(),
            uncertainty_m=uncertainty,
            trained_observations=len(records),
        )

    def estimate(self, band_energy: float, snr_db: float) -> tuple[float, float]:
        x = _proximity_score(band_energy, snr_db)
        estimated = float(
            np.interp(x, np.asarray(self.feature_score), np.asarray(self.distance_m))
        )
        return float(np.clip(estimated, 0.0, 5.0)), self.uncertainty_m


def _proximity_score(band_energy: float, snr_db: float) -> float:
    """One scalar permits a transparent monotonic 1–5 m calibration curve."""

    return math.log10(max(float(band_energy), EPS)) + 0.015 * float(
        np.clip(snr_db, -20.0, 40.0)
    )


def _isotonic_decreasing(values: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators regression with a non-increasing constraint."""

    # Each block is [first_index, last_index, count, average].
    blocks: list[list[float]] = []
    for index, value in enumerate(values.astype(float)):
        blocks.append([float(index), float(index), 1.0, float(value)])
        # For decreasing values, a preceding average must be >= the next one.
        while len(blocks) >= 2 and blocks[-2][3] < blocks[-1][3]:
            right = blocks.pop()
            left = blocks.pop()
            count = left[2] + right[2]
            average = (left[2] * left[3] + right[2] * right[3]) / count
            blocks.append([left[0], right[1], count, average])

    output = np.empty_like(values, dtype=float)
    for first, last, _count, average in blocks:
        output[int(first) : int(last) + 1] = average
    return output


class CalibrationManager:
    """Owns the persistent empty-room and multi-distance calibration profile."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.baseline: Baseline | None = None
        self.observations: list[DistanceObservation] = []
        self.distance_model: DistanceModel | None = None
        self.active: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("baseline"):
                self.baseline = Baseline(**payload["baseline"])
            self.observations = [
                DistanceObservation(**item) for item in payload.get("observations", [])
            ]
            if payload.get("distance_model"):
                self.distance_model = DistanceModel(**payload["distance_model"])
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A bad calibration file must never prevent a demo from starting.
            self.baseline = None
            self.observations = []
            self.distance_model = None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "baseline": asdict(self.baseline) if self.baseline else None,
            "observations": [asdict(item) for item in self.observations],
            "distance_model": asdict(self.distance_model) if self.distance_model else None,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def start(self, kind: str, duration_seconds: float = 30.0, distance_m: float | None = None) -> dict[str, Any]:
        if kind not in {"empty", "distance"}:
            raise ValueError("Calibration kind must be 'empty' or 'distance'")
        if kind == "distance" and (distance_m is None or not 0.0 < distance_m <= 5.0):
            raise ValueError("Distance calibration needs distance_m between 0 and 5")
        self.active = {
            "kind": kind,
            "distance_m": float(distance_m) if distance_m is not None else None,
            "duration_seconds": float(duration_seconds),
            "started_monotonic": time.monotonic(),
            "samples": [],
        }
        self.last_result = None
        return self.status()

    def stop(self) -> dict[str, Any]:
        if not self.active:
            return self.status()
        return self._finish()

    def record(self, metrics: Mapping[str, Any]) -> None:
        if not self.active:
            return
        if not metrics.get("valid"):
            if self._elapsed() >= self.active["duration_seconds"]:
                self._finish()
            return
        sample = {
            "variance": float(metrics["variance"]),
            "band_energy": float(metrics["band_energy"]),
            "snr_db": float(metrics["snr_db"]),
        }
        self.active["samples"].append(sample)
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
        if not samples:
            self.last_result = {
                "ok": False,
                "message": "No valid analysis windows captured; wait for CSI then try again.",
            }
        elif active["kind"] == "empty":
            self.baseline = Baseline.from_samples(samples)
            self.last_result = {
                "ok": True,
                "message": "Empty-room baseline saved.",
                "samples": len(samples),
            }
        else:
            observation = DistanceObservation(
                distance_m=float(active["distance_m"]),
                band_energy=float(np.median([sample["band_energy"] for sample in samples])),
                snr_db=float(np.median([sample["snr_db"] for sample in samples])),
                samples=len(samples),
            )
            # Re-recording a distance replaces its old sample instead of
            # silently overweighting that marker in the mapping.
            self.observations = [
                item
                for item in self.observations
                if not math.isclose(item.distance_m, observation.distance_m, abs_tol=0.01)
            ]
            self.observations.append(observation)
            self.distance_model = DistanceModel.fit(self.observations)
            self.last_result = {
                "ok": True,
                "message": "Distance point saved.",
                "distance_m": observation.distance_m,
                "samples": observation.samples,
                "mapping_ready": self.distance_model is not None,
            }
        self.active = None
        self.save()
        return self.status()

    def status(self) -> dict[str, Any]:
        active: dict[str, Any] | None = None
        if self.active:
            active = {
                "kind": self.active["kind"],
                "distance_m": self.active["distance_m"],
                "duration_seconds": self.active["duration_seconds"],
                "elapsed_seconds": round(self._elapsed(), 1),
                "samples": len(self.active["samples"]),
            }
        return {
            "active": active,
            "baseline_ready": self.baseline is not None,
            "distance_points_m": sorted(item.distance_m for item in self.observations),
            "mapping_ready": self.distance_model is not None,
            "last_result": self.last_result,
        }


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
        max_frames = int((self.config.window_seconds + 10.0) * self.config.sample_rate_hz * 2)
        self._timestamps: deque[float] = deque(maxlen=max_frames)
        self._amplitudes: deque[np.ndarray] = deque(maxlen=max_frames)
        self._rssis: deque[float] = deque(maxlen=max_frames)
        self._candidate_bpms: deque[float] = deque(maxlen=max(2, self.config.bpm_stability_windows))
        self._channel_count: int | None = None
        self._last_timestamp: float | None = None
        self._lock = threading.RLock()
        self.presence_model: Any | None = None
        self.simulated_distance_m = simulated_distance_m

    def set_presence_model(self, model: Any) -> None:
        self.presence_model = model

    def add_frame(self, frame: CSIFrame) -> bool:
        amplitudes = np.asarray(frame.amplitudes, dtype=float).ravel()
        if amplitudes.size < 8 or not np.isfinite(amplitudes).any():
            return False
        timestamp = float(frame.timestamp_s)
        if not math.isfinite(timestamp):
            return False
        with self._lock:
            if self._channel_count is not None and amplitudes.size != self._channel_count:
                # A packet-format change cannot share a PCA window with the
                # prior format.  Start a fresh window instead of mixing bins.
                self._timestamps.clear()
                self._amplitudes.clear()
                self._rssis.clear()
                self._candidate_bpms.clear()
            self._channel_count = int(amplitudes.size)
            if self._last_timestamp is not None and timestamp <= self._last_timestamp:
                # Duplicate local timestamps are harmless, but interpolation
                # needs strict order.  Retain the packet with a tiny offset.
                timestamp = self._last_timestamp + 1.0e-6
            self._last_timestamp = timestamp
            self._timestamps.append(timestamp)
            self._amplitudes.append(amplitudes.astype(np.float32, copy=False))
            self._rssis.append(float(frame.rssi))
        return True

    def telemetry(self) -> dict[str, Any]:
        with self._lock:
            timestamps = np.asarray(self._timestamps, dtype=float)
            amplitudes = list(self._amplitudes)
            rssis = np.asarray(self._rssis, dtype=float)
        result = self._analyse(timestamps, amplitudes, rssis)
        self.calibration.record(result)
        result["calibration"] = self.calibration.status()
        return result

    def _analyse(
        self, timestamps: np.ndarray, amplitudes: list[np.ndarray], rssis: np.ndarray
    ) -> dict[str, Any]:
        if timestamps.size < 32 or not amplitudes:
            return self._empty_telemetry("waiting for CSI frames")
        duration = float(timestamps[-1] - timestamps[0])
        if duration < self.config.min_analysis_seconds:
            return self._empty_telemetry(
                f"warming up ({duration:.0f}/{self.config.window_seconds:.0f} s)",
                rssi=float(np.median(rssis)) if rssis.size else None,
            )

        # Keep the newest continuous segment, then the requested 30-second
        # sliding window.  Do not interpolate through a long serial outage.
        gaps = np.flatnonzero(np.diff(timestamps) > self.config.max_gap_seconds)
        if gaps.size:
            start_index = int(gaps[-1] + 1)
            timestamps = timestamps[start_index:]
            amplitudes = amplitudes[start_index:]
            rssis = rssis[start_index:]
        if timestamps.size < 32 or timestamps[-1] - timestamps[0] < self.config.min_analysis_seconds:
            return self._empty_telemetry("waiting after CSI gap")

        wanted_start = max(float(timestamps[0]), float(timestamps[-1] - self.config.window_seconds))
        keep = timestamps >= wanted_start
        timestamps = timestamps[keep]
        rssis = rssis[keep]
        rows = np.asarray([amplitudes[index] for index, include in enumerate(keep) if include], dtype=float)
        if rows.ndim != 2 or rows.shape[0] < 32:
            return self._empty_telemetry("invalid CSI frame shape")

        carrier_mask = self._carrier_mask(rows)
        rows = rows[:, carrier_mask]
        if rows.shape[1] < 3:
            return self._empty_telemetry("too few usable subcarriers")

        grid, uniform = self._resample(timestamps, rows)
        if grid.size < int(self.config.min_analysis_seconds * self.config.sample_rate_hz * 0.8):
            return self._empty_telemetry("insufficient uniform CSI samples")
        uniform = _hampel_filter(uniform, self.config.hampel_window_samples, self.config.hampel_sigmas)

        # PCA needs changes, not the static multipath channel.  Linear detrend
        # also removes slow thermal/AGC drift before selecting a component.
        centered = signal.detrend(uniform, axis=0, type="linear")
        centered -= np.mean(centered, axis=0, keepdims=True)
        try:
            u, singular_values, _vh = np.linalg.svd(centered, full_matrices=False)
        except np.linalg.LinAlgError:
            return self._empty_telemetry("PCA did not converge")
        components = min(6, singular_values.size)
        if components == 0:
            return self._empty_telemetry("no PCA components")
        scores = u[:, :components] * singular_values[:components]

        candidates: list[dict[str, Any]] = []
        for index in range(components):
            candidate = self._breathing_metrics(scores[:, index])
            if candidate is not None:
                candidate["component"] = index
                candidates.append(candidate)
        if not candidates:
            return self._empty_telemetry("no breathing-band bins")
        # This satisfies the important distinction from simply taking PC1.
        # In a cluttered room, another component may contain most of the
        # respiration-band energy even when PC1 is motion or AGC noise.
        best = max(candidates, key=lambda item: item["band_energy"])

        raw_wave = signal.detrend(np.mean(uniform, axis=1), type="linear")
        candidate_presence, source, model_probability = self._presence_decision(best)
        raw_bpm = float(best["peak_hz"] * 60.0)
        stable_bpm, bpm_spread = self._stable_bpm(raw_bpm, candidate_presence)
        distance_m: float | None = None
        uncertainty_m: float | None = None
        calibrated = self.calibration.distance_model is not None
        if candidate_presence:
            if self.calibration.distance_model:
                distance_m, uncertainty_m = self.calibration.distance_model.estimate(
                    best["band_energy"], best["snr_db"]
                )
            elif self.simulated_distance_m is not None:
                # Simulation is intentionally explicit: never present this as
                # a measured physical distance in a real deployment.
                distance_m = float(np.clip(self.simulated_distance_m, 0.0, 5.0))
                uncertainty_m = 0.35

        result: dict[str, Any] = {
            "valid": True,
            "status": "ok",
            "presence": bool(candidate_presence),
            "presence_source": source,
            # Do not present a random winning spectrum bin as a respiration
            # rate.  A BPM becomes visible only after repeat windows agree.
            "bpm": round(stable_bpm, 1) if stable_bpm is not None else None,
            "bpm_raw": round(raw_bpm, 2),
            "bpm_stable": stable_bpm is not None,
            "bpm_spread_bpm": round(bpm_spread, 2) if bpm_spread is not None else None,
            "confidence": round(float(best["confidence"]), 3),
            "distance_m": round(distance_m, 2) if distance_m is not None else None,
            "distance_uncertainty_m": round(uncertainty_m, 2) if uncertainty_m is not None else None,
            "distance_calibrated": calibrated,
            "rssi": round(float(np.median(rssis)), 1) if rssis.size else None,
            "raw_waveform": _downsample_waveform(grid, raw_wave),
            "filtered_waveform": _downsample_waveform(grid, best["filtered"]),
            "spectrum": _spectrum_points(best["frequencies"], best["psd"]),
            "signal": {
                "band_energy": round(float(best["band_energy"]), 6),
                "variance": round(float(best["variance"]), 6),
                "snr_db": round(float(best["snr_db"]), 2),
                "spectral_entropy": round(float(best["spectral_entropy"]), 4),
                "peak_ratio": round(float(best["confidence"]), 4),
                "pca_component": int(best["component"]),
            },
            "model_probability": model_probability,
            "window_seconds": round(float(grid[-1] - grid[0]), 2),
        }
        return result

    def _stable_bpm(self, raw_bpm: float, presence: bool) -> tuple[float | None, float | None]:
        """Return a robust BPM only when consecutive candidate windows agree."""

        if not presence or not math.isfinite(raw_bpm):
            self._candidate_bpms.clear()
            return None, None
        self._candidate_bpms.append(raw_bpm)
        required = max(2, int(self.config.bpm_stability_windows))
        if len(self._candidate_bpms) < required:
            return None, None
        values = np.asarray(self._candidate_bpms, dtype=float)
        spread = float(np.max(values) - np.min(values))
        if spread > float(self.config.bpm_max_spread_bpm):
            return None, spread
        return float(np.median(values)), spread

    def _carrier_mask(self, rows: np.ndarray) -> np.ndarray:
        count = rows.shape[1]
        mask = np.zeros(count, dtype=bool)
        if count == 64:
            # ESP32-S3 20 MHz LLTF ordering is [0..31, -32..-1].  Only
            # +1..+26 and -26..-1 carry legacy LLTF data: exactly 52 bins.
            mask[1:27] = True
            mask[38:64] = True
            # Espressif marks the first four CSI bytes as possibly invalid.
            # Position 0 is DC already; position 1 is removed by default.
            if self.config.drop_first_word:
                mask[1] = False
        else:
            guard = min(6, max(1, count // 8))
            mask[:] = True
            mask[:guard] = False
            mask[-guard:] = False
            mask[count // 2] = False
        median_amplitude = np.nanmedian(rows, axis=0)
        mask &= np.isfinite(median_amplitude) & (median_amplitude > 1.0e-6)
        return mask

    def _resample(self, timestamps: np.ndarray, rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # np.interp assumes strictly increasing samples.  Retain the newest
        # duplicate, which also behaves well with some router timestamp quirks.
        unique_times, unique_indices = np.unique(timestamps, return_index=True)
        rows = rows[unique_indices]
        start, end = float(unique_times[0]), float(unique_times[-1])
        step = 1.0 / self.config.sample_rate_hz
        grid = np.arange(start, end + step * 0.25, step, dtype=float)
        result = np.empty((grid.size, rows.shape[1]), dtype=float)
        for column in range(rows.shape[1]):
            values = rows[:, column]
            good = np.isfinite(values)
            if np.count_nonzero(good) < 2:
                result[:, column] = 0.0
            else:
                result[:, column] = np.interp(grid, unique_times[good], values[good])
        return grid, result

    def _breathing_metrics(self, score: np.ndarray) -> dict[str, Any] | None:
        rate = self.config.sample_rate_hz
        if score.size < 20:
            return None
        normalized = [
            self.config.breathing_low_hz / (rate * 0.5),
            self.config.breathing_high_hz / (rate * 0.5),
        ]
        sos = signal.butter(3, normalized, btype="bandpass", output="sos")
        try:
            filtered = signal.sosfiltfilt(sos, score)
        except ValueError:
            filtered = signal.sosfilt(sos, score)
        nperseg = min(score.size, max(64, int(rate * 20.0)))
        frequencies, psd = signal.welch(
            filtered,
            fs=rate,
            nperseg=nperseg,
            noverlap=nperseg // 2,
            detrend="constant",
            scaling="density",
        )
        band = (frequencies >= self.config.breathing_low_hz) & (
            frequencies <= self.config.breathing_high_hz
        )
        if not np.any(band):
            return None
        band_psd = psd[band]
        band_frequencies = frequencies[band]
        peak_index = int(np.argmax(band_psd))
        peak_power = float(band_psd[peak_index])
        # Quadratic interpolation gives a fractional-bin peak estimate.  The
        # sampling window still sets the true information limit; this only
        # removes the distracting 3-BPM display quantisation of a 0.05-Hz bin.
        peak_hz = float(band_frequencies[peak_index])
        if 0 < peak_index < band_psd.size - 1 and band_frequencies.size > 1:
            left, center, right = (float(band_psd[peak_index - 1]), peak_power, float(band_psd[peak_index + 1]))
            denominator = left - 2.0 * center + right
            if abs(denominator) > EPS:
                offset = float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
                peak_hz += offset * float(band_frequencies[1] - band_frequencies[0])
        peak_hz = float(np.clip(peak_hz, self.config.breathing_low_hz, self.config.breathing_high_hz))
        total_band_power = float(np.sum(band_psd))
        confidence = peak_power / max(total_band_power, EPS)
        other = np.delete(band_psd, peak_index)
        noise_floor = float(np.median(other)) if other.size else EPS
        snr_db = 10.0 * math.log10(max(peak_power, EPS) / max(noise_floor, EPS))
        distribution = band_psd / max(total_band_power, EPS)
        entropy = -float(np.sum(distribution * np.log(distribution + EPS))) / math.log(
            max(2, distribution.size)
        )
        return {
            "filtered": filtered,
            "frequencies": frequencies,
            "psd": psd,
            "peak_hz": peak_hz,
            "confidence": float(confidence),
            "band_energy": float(np.mean(filtered**2)),
            "variance": float(np.var(score)),
            "snr_db": float(snr_db),
            "spectral_entropy": float(entropy),
        }

    def _presence_decision(self, metrics: Mapping[str, float]) -> tuple[bool, str, float | None]:
        baseline = self.calibration.baseline
        variance_threshold = self.config.default_variance_threshold
        energy_threshold = self.config.default_band_energy_threshold
        if baseline:
            variance_threshold = max(
                variance_threshold,
                baseline.variance_mean + 3.0 * max(baseline.variance_std, EPS),
            )
            energy_threshold = max(
                energy_threshold,
                baseline.band_energy_mean + 3.0 * max(baseline.band_energy_std, EPS),
            )
        heuristic = bool(
            metrics["confidence"] >= self.config.min_confidence
            and metrics["variance"] > variance_threshold
            and metrics["band_energy"] > energy_threshold
        )
        if self.presence_model is None:
            return heuristic, "heuristic", None
        features = np.asarray(
            [[
                metrics["band_energy"],
                metrics["confidence"],
                metrics["variance"],
                metrics["spectral_entropy"],
            ]],
            dtype=float,
        )
        try:
            prediction = bool(self.presence_model.predict(features)[0])
            probability: float | None = None
            if hasattr(self.presence_model, "predict_proba"):
                probabilities = self.presence_model.predict_proba(features)[0]
                classes = list(getattr(self.presence_model, "classes_", []))
                if 1 in classes:
                    probability = float(probabilities[classes.index(1)])
            # Keep the empty-room variance safety check even for a model that
            # was accidentally trained on a very different room.
            return bool(prediction and metrics["variance"] > variance_threshold), "random_forest", probability
        except Exception:
            return heuristic, "heuristic_model_error", None

    def _empty_telemetry(self, status: str, rssi: float | None = None) -> dict[str, Any]:
        return {
            "valid": False,
            "status": status,
            "presence": False,
            "presence_source": "waiting",
            "bpm": None,
            "bpm_raw": None,
            "bpm_stable": False,
            "bpm_spread_bpm": None,
            "confidence": 0.0,
            "distance_m": None,
            "distance_uncertainty_m": None,
            "distance_calibrated": self.calibration.distance_model is not None,
            "rssi": round(rssi, 1) if rssi is not None else None,
            "raw_waveform": [],
            "filtered_waveform": [],
            "spectrum": [],
            "signal": {
                "band_energy": 0.0,
                "variance": 0.0,
                "snr_db": 0.0,
                "spectral_entropy": 0.0,
                "peak_ratio": 0.0,
                "pca_component": None,
            },
            "model_probability": None,
            "window_seconds": 0.0,
        }


def _hampel_filter(values: np.ndarray, window: int, sigmas: float) -> np.ndarray:
    """Replace impulsive CSI samples with their local median per carrier."""

    window = max(3, int(window) | 1)  # median filter needs an odd window
    median = ndimage.median_filter(values, size=(window, 1), mode="nearest")
    deviation = np.abs(values - median)
    mad = ndimage.median_filter(deviation, size=(window, 1), mode="nearest")
    threshold = sigmas * 1.4826 * mad
    outlier = (deviation > threshold) & (threshold > 1.0e-9)
    return np.where(outlier, median, values)


def _downsample_waveform(times: np.ndarray, values: np.ndarray, maximum: int = 360) -> list[dict[str, float]]:
    if values.size == 0:
        return []
    indices = np.linspace(0, values.size - 1, min(maximum, values.size), dtype=int)
    end = float(times[-1])
    return [
        {"t": round(float(times[index] - end), 3), "v": round(float(values[index]), 6)}
        for index in indices
    ]


def _spectrum_points(frequencies: np.ndarray, psd: np.ndarray) -> list[dict[str, float]]:
    visible = (frequencies >= 0.05) & (frequencies <= 1.0)
    return [
        {"hz": round(float(frequency), 4), "power": round(float(power), 8)}
        for frequency, power in zip(frequencies[visible], psd[visible])
    ]

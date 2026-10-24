"""Serial parsing, ESP32 alert writes, and a deterministic demo CSI source."""

from __future__ import annotations

import asyncio
import csv
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np

from processing import CSIFrame

try:  # Keep `python app.py --simulate` usable before a board is plugged in.
    import serial  # type: ignore
except ImportError:  # pragma: no cover - exercised only without pyserial installed
    serial = None


_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
UINT32_US = float(2**32)


class TimestampNormalizer:
    """Turns ESP32 local timestamp microseconds into monotonic relative seconds."""

    def __init__(self) -> None:
        self._last_raw_us: float | None = None
        self._wrap_offset_us = 0.0

    def to_seconds(self, raw_us: float) -> float:
        # ESP32 rx_ctrl/local_timestamp is commonly an unsigned 32-bit µs
        # timer.  It wraps after ~71.6 minutes; make a 10-hour hackathon safe.
        if self._last_raw_us is not None and raw_us < self._last_raw_us - UINT32_US / 2.0:
            self._wrap_offset_us += UINT32_US
        self._last_raw_us = raw_us
        return (raw_us + self._wrap_offset_us) / 1_000_000.0


class CSIStreamParser:
    """Accepts both RescueSense compact CSI and stock Espressif CSI_DATA lines.

    Compact format emitted by the provided firmware:
        CSI,<timestamp_us>,<rssi>,<amp_sc0>,<amp_sc1>,...\n
    The S3 ESP-CSI get-started example's verbose CSV is also accepted.  Its
    final quoted field is `[imag0,real0,imag1,real1,...]`; the parser converts
    each pair to amplitude with hypot(imag, real).
    """

    def __init__(self) -> None:
        self.timestamps = TimestampNormalizer()
        self.parsed = 0
        self.dropped = 0
        self.last_error: str | None = None

    def parse_line(self, line: str | bytes) -> CSIFrame | None:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        text = line.strip().lstrip("\ufeff")
        if not text or text.startswith("#"):
            return None
        try:
            row = next(csv.reader([text], skipinitialspace=True))
            if not row:
                return None
            prefix = row[0].strip().upper()
            if prefix == "CSI":
                frame = self._parse_compact(row[1:])
            elif prefix == "CSI_DATA":
                frame = self._parse_stock_esp_csi(row)
            else:
                # Convenient for an ad-hoc firmware that omits CSI, while
                # still enforcing timestamp,rssi,amplitude... semantics.
                frame = self._parse_compact(row)
            self.parsed += 1
            return frame
        except (ValueError, IndexError, StopIteration) as exc:
            self.dropped += 1
            self.last_error = str(exc)
            return None

    def _parse_compact(self, values: list[str]) -> CSIFrame:
        if len(values) < 4:
            raise ValueError("compact CSI line needs timestamp,rssi,and at least two amplitudes")
        timestamp_us = float(values[0])
        rssi = float(values[1])
        amplitudes = np.asarray([float(value) for value in values[2:] if value.strip()], dtype=float)
        if amplitudes.size < 2:
            raise ValueError("compact CSI line has no amplitudes")
        return CSIFrame(self.timestamps.to_seconds(timestamp_us), rssi, amplitudes)

    def _parse_stock_esp_csi(self, row: list[str]) -> CSIFrame:
        # Current ESP-IDF ESP32-S3 csi_recv/csi_recv_router output puts RSSI
        # at CSV index 3 and local_timestamp at index 18.  Some older targets
        # add a MAC column, so fall back to scanning a plausible RSSI value.
        rssi = _parse_float_at(row, 3)
        timestamp_us = _parse_float_at(row, 18)
        if rssi is None:
            rssi = _first_in_range(row[1:-1], -127.0, 0.0)
        if rssi is None:
            rssi = -70.0
        if timestamp_us is None:
            # Stock prints from older targets sometimes omit local timestamp.
            # Serial arrival time remains adequate for the resampling stage.
            timestamp_s = time.monotonic()
        else:
            timestamp_s = self.timestamps.to_seconds(timestamp_us)

        payload = row[-1]
        numbers = [float(token) for token in _NUMBER.findall(payload)]
        if len(numbers) < 4:
            raise ValueError("CSI_DATA bracket payload has fewer than two IQ pairs")
        if len(numbers) % 2:
            numbers = numbers[:-1]
        iq = np.asarray(numbers, dtype=float).reshape(-1, 2)
        # ESP-IDF prints `[imag,real,...]`, not `[real,imag,...]`.  Hypot is
        # symmetric, but spelling it out documents the wire contract.
        amplitudes = np.hypot(iq[:, 0], iq[:, 1])
        return CSIFrame(timestamp_s, rssi, amplitudes)


def _parse_float_at(values: list[str], index: int) -> float | None:
    if index >= len(values):
        return None
    try:
        return float(values[index])
    except ValueError:
        return None


def _first_in_range(values: list[str], lower: float, upper: float) -> float | None:
    for value in values:
        try:
            number = float(value)
        except ValueError:
            continue
        if lower <= number <= upper:
            return number
    return None


class SerialCSIReader:
    """Async bridge around pyserial that also sends the ESP32 alert command."""

    def __init__(self, port: str, baud: int = 921600, timeout: float = 0.25):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.parser = CSIStreamParser()
        self._serial: object | None = None
        self._write_lock = threading.Lock()
        self._running = False

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is missing. Run: pip install -r requirements.txt")
        self._serial = serial.Serial(self.port, self.baud, timeout=self.timeout)
        self._running = True

    async def run(self, on_frame: Callable[[CSIFrame], None]) -> None:
        if self._serial is None:
            self.open()
        assert self._serial is not None
        while self._running:
            try:
                raw = await asyncio.to_thread(self._serial.readline)  # type: ignore[attr-defined]
            except Exception as exc:
                self._running = False
                raise RuntimeError(f"Serial read failed on {self.port}: {exc}") from exc
            if not raw:
                continue
            frame = self.parser.parse_line(raw)
            if frame is not None:
                on_frame(frame)

    def send_alert(self, active: bool) -> None:
        """Firmware contract: `ALERT,1\\n` enables buzzer/RGB; 0 disables it."""

        if self._serial is None:
            return
        command = b"ALERT,1\n" if active else b"ALERT,0\n"
        with self._write_lock:
            self._serial.write(command)  # type: ignore[attr-defined]
            self._serial.flush()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._running = False
        if self._serial is not None:
            try:
                self._serial.close()  # type: ignore[attr-defined]
            finally:
                self._serial = None


@dataclass
class SimulatedCSIProducer:
    """Synthetic 64-bin LLTF source for an end-to-end demo without hardware."""

    sample_rate_hz: float = 100.0
    bpm: float = 15.0
    distance_m: float = 2.5
    present: bool = True
    seed: int = 7

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        self._base = self._rng.uniform(35.0, 115.0, 64)
        self._coupling = self._rng.normal(0.0, 1.0, 64)
        self._coupling[0] = 0.0
        self._coupling[27:38] = 0.0
        self._timestamp = time.monotonic()
        self._sample_index = 0

    def prime(self, seconds: float = 30.0) -> list[CSIFrame]:
        """Generate a historical 30-second buffer so --simulate is demo-ready."""

        count = max(1, int(seconds * self.sample_rate_hz))
        self._timestamp = time.monotonic() - count / self.sample_rate_hz
        return [self.next_frame() for _ in range(count)]

    def next_frame(self) -> CSIFrame:
        t = self._sample_index / self.sample_rate_hz
        self._sample_index += 1
        self._timestamp += 1.0 / self.sample_rate_hz
        # A coherent channel perturbation models chest motion through a wall;
        # independent receiver noise is deliberately left in so PCA matters.
        breath = math.sin(2.0 * math.pi * (self.bpm / 60.0) * t)
        breath += 0.18 * math.sin(4.0 * math.pi * (self.bpm / 60.0) * t + 0.4)
        strength = 2.4 / max(0.7, self.distance_m) ** 1.10 if self.present else 0.0
        noise = self._rng.normal(0.0, 0.26, 64)
        slow_drift = 0.06 * math.sin(2.0 * math.pi * 0.025 * t)
        amplitudes = np.abs(self._base + strength * breath * self._coupling + noise + slow_drift)
        rssi = -39.0 - 3.0 * self.distance_m + float(self._rng.normal(0.0, 1.1))
        return CSIFrame(self._timestamp, rssi, amplitudes)

    def configure(
        self,
        *,
        present: bool | None = None,
        bpm: float | None = None,
        distance_m: float | None = None,
    ) -> None:
        if present is not None:
            self.present = bool(present)
        if bpm is not None:
            self.bpm = float(np.clip(bpm, 6.0, 30.0))
        if distance_m is not None:
            self.distance_m = float(np.clip(distance_m, 0.5, 5.0))

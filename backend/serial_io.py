"""Serial transport, ESP32 port discovery, wire parsing, and a CSI simulator.

The reader runs in its own thread so a slow dashboard or a heavy analysis
step can never starve the 100 Hz serial stream, and it reconnects by itself
when the board is unplugged, reset, or re-flashed.
"""

from __future__ import annotations

import csv
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from processing import CSIFrame

try:  # Keep `python app.py --simulate` usable before pyserial is installed.
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover - exercised only without pyserial installed
    serial = None
    list_ports = None


_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")

# USB-to-UART bridges used on ESP32-S3 dev boards, plus the S3's own USB.
USB_BRIDGES = {
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH34x / CH9102",
    0x0403: "FTDI",
    0x303A: "ESP32-S3 native USB",
}
NATIVE_USB_VID = 0x303A

# Espressif's wifi_err_reason_t values most worth explaining on screen.
WIFI_REASONS = {
    2: "authentication expired",
    8: "left the network",
    15: "4-way handshake timeout (wrong password?)",
    201: "network not found (wrong SSID, or hotspot is 5 GHz only)",
    202: "authentication failed (wrong password?)",
    203: "association failed",
    204: "handshake timeout (wrong password?)",
    205: "connection failed",
    210: "no AP with acceptable security found",
    211: "no AP with acceptable security found",
}


# --------------------------------------------------------------------------
# Port discovery
# --------------------------------------------------------------------------


def list_serial_ports() -> list[dict[str, Any]]:
    """All serial ports, with a hint about which ones can be an ESP32."""

    if list_ports is None:
        return []
    ports: list[dict[str, Any]] = []
    for info in list_ports.comports():
        hwid = (info.hwid or "").upper()
        description = info.description or ""
        bluetooth = "BTHENUM" in hwid or "BLUETOOTH" in description.upper()
        vid = info.vid
        ports.append(
            {
                "device": info.device,
                "description": description,
                "vid": f"{vid:04X}" if vid is not None else None,
                "pid": f"{info.pid:04X}" if info.pid is not None else None,
                "bluetooth": bluetooth,
                "candidate": (not bluetooth) and vid in USB_BRIDGES,
                "chip": USB_BRIDGES.get(vid) if vid is not None else None,
            }
        )
    return ports


def find_esp32_port() -> str | None:
    """Best guess at the ESP32 port; Bluetooth virtual ports are never chosen.

    A UART bridge is preferred over the S3's native USB because the default
    firmware console is UART0 (the connector usually labelled UART/COM).
    """

    candidates = [port for port in list_serial_ports() if port["candidate"]]
    if not candidates:
        return None
    candidates.sort(key=lambda port: (port["vid"] == f"{NATIVE_USB_VID:04X}", port["device"]))
    return str(candidates[0]["device"])


# --------------------------------------------------------------------------
# Wire parsing
# --------------------------------------------------------------------------


class ClockAligner:
    """Maps the ESP32's microsecond clock onto this machine's monotonic clock.

    Device timestamps are far less jittery than serial arrival times (USB
    delivers lines in bursts), so they are kept for spacing.  The offset is
    re-anchored whenever the two clocks disagree by more than `max_skew_s`,
    which happens when the board reboots (its clock restarts at zero).  The
    previous code instead stacked every post-reboot packet onto the last
    pre-reboot timestamp, corrupting minutes of data.
    """

    def __init__(self, wrap_bits: int | None = None, max_skew_s: float = 1.5):
        self.wrap = float(2**wrap_bits) if wrap_bits else None
        self.max_skew_s = max_skew_s
        self._offset: float | None = None
        self._last_raw: float | None = None
        self._unwrap = 0.0
        self.resyncs = 0

    def to_host(self, device_us: float, host_now: float | None = None) -> float:
        host_now = time.monotonic() if host_now is None else host_now
        raw = float(device_us)
        if self.wrap is not None and self._last_raw is not None and raw < self._last_raw - self.wrap / 2:
            self._unwrap += self.wrap
        self._last_raw = raw
        device_s = (raw + self._unwrap) / 1_000_000.0
        if self._offset is None or abs(device_s + self._offset - host_now) > self.max_skew_s:
            if self._offset is not None:
                self.resyncs += 1
            self._offset = host_now - device_s
        else:
            # Follow crystal drift slowly; serial latency jitter averages out.
            self._offset += 0.002 * (host_now - device_s - self._offset)
        return device_s + self._offset


class CSIStreamParser:
    """Accepts RescueSense compact CSI and stock Espressif CSI_DATA lines.

    Compact (this firmware):  CSI,<timestamp_us>,<rssi>,<amp0>,<amp1>,...
    Stock esp-csi examples:   CSI_DATA,...,<rssi at 3>,...,<local_timestamp at 18>,...,"[i,r,...]"
    Lines starting with '#' and ESP-IDF log lines are kept as device messages.
    """

    def __init__(self, on_message: Callable[[str], None] | None = None) -> None:
        self.compact_clock = ClockAligner()
        self.stock_clock = ClockAligner(wrap_bits=32)
        self.on_message = on_message
        self.parsed = 0
        self.dropped = 0
        self.other_lines = 0
        self.last_error: str | None = None
        self.device: dict[str, Any] = {}

    def parse_line(self, line: str | bytes, host_now: float | None = None) -> CSIFrame | None:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        text = line.strip().lstrip("﻿")
        if not text:
            return None
        if text.startswith("CSI,") or text.startswith("CSI_DATA,"):
            try:
                row = next(csv.reader([text], skipinitialspace=True))
                if row[0] == "CSI":
                    frame = self._parse_compact(row[1:], host_now)
                else:
                    frame = self._parse_stock(row, host_now)
                self.parsed += 1
                return frame
            except (ValueError, IndexError, StopIteration) as exc:
                self.dropped += 1
                self.last_error = str(exc)
                return None
        self.other_lines += 1
        if text.startswith("#"):
            self._parse_status(text)
        if self.on_message is not None and len(text) < 400:
            self.on_message(text)
        return None

    def _parse_compact(self, values: list[str], host_now: float | None) -> CSIFrame:
        if len(values) < 10:
            raise ValueError("compact CSI line is truncated")
        timestamp_us = float(values[0])
        rssi = float(values[1])
        amplitudes = np.asarray([float(value) for value in values[2:] if value.strip()], dtype=float)
        return CSIFrame(self.compact_clock.to_host(timestamp_us, host_now), rssi, amplitudes)

    def _parse_stock(self, row: list[str], host_now: float | None) -> CSIFrame:
        rssi = _float_at(row, 3)
        timestamp_us = _float_at(row, 18)
        if rssi is None or not -127.0 <= rssi <= 0.0:
            rssi = -70.0
        if timestamp_us is None:
            timestamp_s = time.monotonic() if host_now is None else host_now
        else:
            timestamp_s = self.stock_clock.to_host(timestamp_us, host_now)
        numbers = [float(token) for token in _NUMBER.findall(row[-1])]
        if len(numbers) < 8:
            raise ValueError("CSI_DATA payload has too few IQ pairs")
        if len(numbers) % 2:
            numbers = numbers[:-1]
        iq = np.asarray(numbers, dtype=float).reshape(-1, 2)
        # ESP-IDF prints [imag, real, ...]; hypot is symmetric anyway.
        return CSIFrame(timestamp_s, rssi, np.hypot(iq[:, 0], iq[:, 1]))

    def _parse_status(self, text: str) -> None:
        """Remember the firmware's own counters (#STAT, #RESCUESENSE, #WIFI...)."""

        parts = text[1:].split(",")
        kind = parts[0].upper()
        values: dict[str, str] = {}
        flags: list[str] = []
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                values[key.strip()] = value.strip()
            elif part.strip():
                flags.append(part.strip())
        if kind == "STAT":
            self.device["stats"] = values
            self.device["stats_at"] = time.monotonic()
        elif kind == "RESCUESENSE":
            self.device["firmware"] = values
        elif kind == "WIFI":
            state = flags[0] if flags else "unknown"
            wifi = {"state": state, **values}
            if state == "disconnected":
                try:
                    reason = int(values.get("reason", "0"))
                except ValueError:
                    reason = 0
                wifi["reason_text"] = WIFI_REASONS.get(reason, f"reason {reason}")
            self.device["wifi"] = wifi
        elif kind == "GAIN":
            self.device["gain"] = values
        elif kind == "ALERT":
            self.device["alert"] = flags[0] if flags else None


def _float_at(values: list[str], index: int) -> float | None:
    if index >= len(values):
        return None
    try:
        return float(values[index])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Serial reader thread
# --------------------------------------------------------------------------


class SerialCSIReader:
    """Owns the COM port: reads, parses, reconnects, and writes alert commands."""

    HEARTBEAT_SECONDS = 5.0

    def __init__(
        self,
        port: str = "auto",
        baud: int = 921600,
        on_frame: Callable[[CSIFrame], Any] | None = None,
    ) -> None:
        self.requested_port = port
        self.baud = baud
        self.on_frame = on_frame
        self.messages: deque[dict[str, Any]] = deque(maxlen=60)
        self.parser = CSIStreamParser(on_message=self._remember_message)
        self.state = "starting"
        self.active_port: str | None = None
        self.error: str | None = None
        self.bytes_read = 0
        self.frames = 0
        self.rate_hz = 0.0
        self.last_frame_at: float | None = None
        self.opened_at: float | None = None
        self._alert_wanted = False
        self._serial: Any | None = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rate_count = 0
        self._rate_started = time.monotonic()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="serial-csi", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -- public API ------------------------------------------------------
    def send_alert(self, active: bool) -> bool:
        """Firmware contract: `ALERT,1\\n` starts the buzzer, `ALERT,0\\n` stops it."""

        self._alert_wanted = bool(active)
        return self._write(b"ALERT,1\n" if active else b"ALERT,0\n")

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "state": self.state,
            "port": self.active_port,
            "requested_port": self.requested_port,
            "baud": self.baud,
            "error": self.error,
            "packet_rate_hz": round(self.rate_hz, 1),
            "frames": self.frames,
            "parse_errors": self.parser.dropped,
            "last_frame_age_s": round(now - self.last_frame_at, 1) if self.last_frame_at else None,
            "clock_resyncs": self.parser.compact_clock.resyncs + self.parser.stock_clock.resyncs,
            "device": self.parser.device,
            "messages": list(self.messages)[-12:],
        }

    # -- internals -------------------------------------------------------
    def _remember_message(self, text: str) -> None:
        self.messages.append({"t": time.time(), "text": text})

    def _write(self, payload: bytes) -> bool:
        with self._write_lock:
            if self._serial is None:
                return False
            try:
                self._serial.write(payload)
                self._serial.flush()
                return True
            except Exception as exc:  # port vanished mid-write
                self.error = f"write failed: {exc}"
                return False

    def _open(self) -> bool:
        if serial is None:
            self.state = "error"
            self.error = "pyserial is missing. Run: pip install -r requirements.txt"
            return False
        port = self.requested_port
        if not port or port.lower() == "auto":
            port = find_esp32_port()
            if port is None:
                self.state = "searching"
                self.error = "No ESP32 USB serial port found. Plug the board into the UART/COM connector."
                return False
        device = serial.Serial()
        device.port = port
        device.baudrate = self.baud
        device.timeout = 0.2
        device.write_timeout = 0.5
        # Opening with DTR/RTS released avoids the auto-reset circuit
        # rebooting the board every time the backend starts.
        device.dtr = False
        device.rts = False
        try:
            device.open()
        except Exception as exc:
            self.state = "error"
            self.error = f"Could not open {port}: {exc}"
            return False
        with self._write_lock:
            self._serial = device
        self.active_port = port
        self.state = "open"
        self.error = None
        self.opened_at = time.monotonic()
        self._remember_message(f"[backend] opened {port} @ {self.baud} baud")
        return True

    def _close(self) -> None:
        with self._write_lock:
            device, self._serial = self._serial, None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._serial is None and not self._open():
                self._stop.wait(1.5)
                continue
            try:
                self._read_loop()
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.state = "reconnecting"
                self.error = f"Serial link lost on {self.active_port}: {exc}"
                self._remember_message(f"[backend] {self.error}")
                self._close()
                self._stop.wait(1.0)

    def _read_loop(self) -> None:
        buffer = bytearray()
        last_heartbeat = 0.0
        assert self._serial is not None
        while not self._stop.is_set():
            device = self._serial
            if device is None:
                return
            chunk = device.read(max(1, min(device.in_waiting, 65536)))
            now = time.monotonic()
            if chunk:
                self.bytes_read += len(chunk)
                buffer.extend(chunk)
                if len(buffer) > 262144:  # garbage without newlines (wrong baud)
                    buffer.clear()
                    self.error = "Unreadable serial data. Is the baud rate 921600?"
                *lines, rest = buffer.split(b"\n")
                buffer = bytearray(rest)
                for raw in lines:
                    frame = self.parser.parse_line(raw, now)
                    if frame is not None:
                        self.frames += 1
                        self._rate_count += 1
                        self.last_frame_at = now
                        if self.on_frame is not None:
                            self.on_frame(frame)
            if now - self._rate_started >= 1.0:
                self.rate_hz = self._rate_count / (now - self._rate_started)
                self._rate_count = 0
                self._rate_started = now
                self.state = "streaming" if self.rate_hz > 0 else "open"
            if now - last_heartbeat >= self.HEARTBEAT_SECONDS:
                # Re-sending the wanted state keeps the firmware's alert
                # watchdog fed and re-applies it after a board reset.
                self._write(b"ALERT,1\n" if self._alert_wanted else b"ALERT,0\n")
                last_heartbeat = now


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------


@dataclass
class SimulatedCSIProducer:
    """Synthetic ESP32-S3 LLTF stream with realistic impairments.

    It reproduces what makes real CSI hard: per-subcarrier noise, frequency
    selective coupling of the chest motion, irregular packet timing, short
    outages, the odd wrong-width packet, slow drift, residual gain steps and
    gross body movement.  Absent mode keeps every impairment but no breathing.
    """

    sample_rate_hz: float = 100.0
    bpm: float = 15.0
    distance_m: float = 2.5
    present: bool = True
    seed: int = 7
    impairments: bool = True
    noise_std: float = 0.9
    breathing_gain: float = 2.2
    auto_motion: bool = False
    _rng: np.random.Generator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        index = np.arange(64)
        valid = np.zeros(64, dtype=bool)
        valid[1:27] = True
        valid[38:64] = True
        self._valid = valid
        # Frequency-selective static channel: a few multipath taps.
        profile = np.full(64, 38.0)
        for _ in range(4):
            profile += self._rng.uniform(4.0, 14.0) * np.cos(
                2 * np.pi * index * self._rng.uniform(0.01, 0.06) + self._rng.uniform(0, 2 * np.pi)
            )
        self._base = np.where(valid, np.clip(profile, 8.0, None), 0.4)
        # Chest motion couples into each subcarrier with a smooth random
        # gain and sign (the multipath geometry decides which tones grow).
        coupling = np.zeros(64)
        for _ in range(3):
            coupling += self._rng.normal() * np.cos(
                2 * np.pi * index * self._rng.uniform(0.005, 0.04) + self._rng.uniform(0, 2 * np.pi)
            )
        self._coupling = np.where(valid, coupling / max(np.std(coupling[valid]), 1e-6), 0.0)
        self._clock = time.monotonic()
        self._t = 0.0
        self._phase = 0.0
        self._rate_wander = 0.0
        self._depth_wander = 1.0
        self._gain = 1.0
        self._drift = np.zeros(64)
        self._motion_until = -1.0
        self._motion_state = np.zeros(64)
        self._outage_until = -1.0
        self.pending_motion_seconds = 0.0

    # -- controls ----------------------------------------------------------
    def configure(
        self,
        *,
        present: bool | None = None,
        bpm: float | None = None,
        distance_m: float | None = None,
        motion_seconds: float | None = None,
    ) -> None:
        if present is not None:
            self.present = bool(present)
        if bpm is not None:
            self.bpm = float(np.clip(bpm, 6.0, 30.0))
        if distance_m is not None:
            self.distance_m = float(np.clip(distance_m, 0.5, 5.0))
        if motion_seconds:
            self.pending_motion_seconds = float(np.clip(motion_seconds, 1.0, 15.0))

    # -- generation --------------------------------------------------------
    def prime(self, seconds: float = 30.0) -> list[CSIFrame]:
        """Generate a historical buffer so --simulate is demo-ready at once."""

        self._clock = time.monotonic() - seconds
        return self.frames_until(time.monotonic())

    def frames_until(self, host_now: float) -> list[CSIFrame]:
        frames: list[CSIFrame] = []
        while self._clock < host_now:
            frame = self.next_frame()
            if frame is not None:
                frames.append(frame)
        return frames

    def next_frame(self) -> CSIFrame | None:
        rng = self._rng
        nominal = 1.0 / self.sample_rate_hz
        step = nominal
        if self.impairments:
            step = nominal * float(np.clip(1.0 + 0.30 * rng.standard_normal(), 0.4, 2.5))
        self._clock += step
        self._t += step
        t = self._t

        # Breathing continues during outages; only the packets are missing.
        self._rate_wander += 0.002 * rng.standard_normal() - 0.0005 * self._rate_wander
        self._depth_wander += 0.002 * rng.standard_normal() - 0.001 * (self._depth_wander - 1.0)
        rate_hz = max(0.08, (self.bpm + 1.2 * self._rate_wander) / 60.0)
        self._phase += 2.0 * math.pi * rate_hz * step

        if self.impairments and t >= self._outage_until and rng.random() < 0.0008:
            self._outage_until = t + rng.uniform(0.2, 1.2)  # ICMP replies paused
        if t < self._outage_until:
            return None

        chest = math.sin(self._phase) + 0.22 * math.sin(2.0 * self._phase + 0.6)
        strength = self.breathing_gain / max(0.5, self.distance_m) ** 1.4 if self.present else 0.0
        breathing = strength * max(0.3, self._depth_wander) * chest * self._coupling

        amplitudes = self._base * self._gain + breathing
        if self.impairments:
            self._drift += 0.004 * rng.standard_normal(64)
            self._drift *= 0.9995
            amplitudes = amplitudes + self._drift
            if rng.random() < 0.00025:  # residual gain step after AGC compensation
                self._gain *= float(rng.choice([0.985, 1.015]))
            if self.auto_motion and rng.random() < 0.0002:
                self.pending_motion_seconds = rng.uniform(2.0, 6.0)
        if self.pending_motion_seconds > 0:
            self._motion_until = t + self.pending_motion_seconds
            self.pending_motion_seconds = 0.0
        if t < self._motion_until:
            # A person shifting position rewrites the multipath; afterwards the
            # channel settles at a new static value rather than snapping back.
            self._motion_state += 0.9 * rng.standard_normal(64) * self._valid
        amplitudes = amplitudes + self._motion_state

        amplitudes = amplitudes + self.noise_std * rng.standard_normal(64) * self._valid
        amplitudes = np.round(np.clip(amplitudes, 0.0, None), 1)
        rssi = -38.0 - 3.2 * self.distance_m + float(rng.normal(0.0, 1.2))
        if self.impairments and rng.random() < 0.004:
            # A stray HT40 packet (128 subcarriers) must not reset the window.
            amplitudes = np.round(np.abs(rng.normal(30.0, 10.0, 128)), 1)
        return CSIFrame(self._clock, rssi, amplitudes)

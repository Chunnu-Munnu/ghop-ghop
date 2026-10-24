"""FastAPI/WebSocket entry point for the RescueSense laptop backend.

Examples:
    python app.py --simulate
    python app.py --serial-port COM5 --baud 921600
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from processing import CSIProcessor, ProcessorConfig
from serial_io import CSIStreamParser, SerialCSIReader, SimulatedCSIProducer


@dataclass
class RuntimeOptions:
    simulate: bool = False
    serial_port: str | None = None
    baud: int = 921600
    sample_rate_hz: float = 100.0
    calibration_file: str = "calibration.json"
    simulate_bpm: float = 15.0
    simulate_distance_m: float = 2.5
    simulate_absent: bool = False
    auto_alert: bool = False
    model_path: str | None = None


class RescueRuntime:
    """Owns sources, alert commands, calibration, and all WS subscribers."""

    def __init__(self, options: RuntimeOptions):
        self.options = options
        self.processor = CSIProcessor(
            ProcessorConfig(sample_rate_hz=options.sample_rate_hz),
            calibration_path=options.calibration_file,
            simulated_distance_m=options.simulate_distance_m if options.simulate else None,
        )
        self.clients: set[WebSocket] = set()
        self.serial_reader: SerialCSIReader | None = None
        self.simulator: SimulatedCSIProducer | None = None
        self.source_task: asyncio.Task[None] | None = None
        self.broadcast_task: asyncio.Task[None] | None = None
        self.latest: dict[str, Any] = self._initial_telemetry()
        self.alert_active = False
        self.source_error: str | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._load_optional_model()
        if self.options.simulate:
            self.simulator = SimulatedCSIProducer(
                sample_rate_hz=self.options.sample_rate_hz,
                bpm=self.options.simulate_bpm,
                distance_m=self.options.simulate_distance_m,
                present=not self.options.simulate_absent,
            )
            # Fill a historic 30 s window immediately.  This makes a demo
            # usable on first page load without weakening real-device timing.
            for frame in self.simulator.prime(30.0):
                self.processor.add_frame(frame)
            self.source_task = asyncio.create_task(self._run_simulator(), name="simulated-csi")
        else:
            assert self.options.serial_port is not None
            self.serial_reader = SerialCSIReader(self.options.serial_port, self.options.baud)
            self.source_task = asyncio.create_task(self._run_serial(), name="serial-csi")
        self.broadcast_task = asyncio.create_task(self._broadcast_loop(), name="telemetry-broadcast")

    async def stop(self) -> None:
        self._running = False
        # Close dashboard clients first so Uvicorn does not wait for browser
        # reconnect loops when the user stops the local server with Ctrl+C.
        for client in tuple(self.clients):
            with contextlib.suppress(Exception):
                await client.close(code=1001)
        self.clients.clear()
        if self.serial_reader is not None:
            self.serial_reader.close()
        for task in (self.source_task, self.broadcast_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _load_optional_model(self) -> None:
        if not self.options.model_path:
            return
        try:
            import joblib  # installed by requirements-ml.txt

            self.processor.set_presence_model(joblib.load(self.options.model_path))
        except Exception as exc:
            self.source_error = f"Could not load model: {exc}"

    async def _run_serial(self) -> None:
        assert self.serial_reader is not None
        try:
            await self.serial_reader.run(self.processor.add_frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.source_error = str(exc)

    async def _run_simulator(self) -> None:
        assert self.simulator is not None
        interval = 1.0 / self.options.sample_rate_hz
        try:
            while self._running:
                self.processor.add_frame(self.simulator.next_frame())
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _broadcast_loop(self) -> None:
        try:
            while self._running:
                telemetry = self.processor.telemetry()
                telemetry.update(
                    {
                        "type": "telemetry",
                        "timestamp": time.time(),
                        "alert_active": self.alert_active,
                        "source": "simulation" if self.simulator else "serial",
                        "source_error": self.source_error,
                    }
                )
                self.latest = telemetry
                if self.options.auto_alert and telemetry["presence"] != self.alert_active:
                    await self.set_alert(bool(telemetry["presence"]))
                    telemetry["alert_active"] = self.alert_active
                await self.broadcast(telemetry)
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise

    async def set_alert(self, active: bool) -> dict[str, Any]:
        self.alert_active = bool(active)
        if self.serial_reader is not None:
            try:
                self.serial_reader.send_alert(self.alert_active)
            except Exception as exc:
                self.source_error = f"Alert serial write failed: {exc}"
        acknowledgement = {
            "type": "alert_ack",
            "active": self.alert_active,
            "command": "ALERT,1" if self.alert_active else "ALERT,0",
        }
        await self.broadcast(acknowledgement)
        return acknowledgement

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for client in tuple(self.clients):
            try:
                await client.send_json(payload)
            except Exception:
                stale.append(client)
        for client in stale:
            self.clients.discard(client)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.clients.add(websocket)
        await websocket.send_json(self.latest)

    def status(self) -> dict[str, Any]:
        parser_status: dict[str, Any] | None = None
        if self.serial_reader is not None:
            parser = self.serial_reader.parser
            parser_status = {
                "parsed": parser.parsed,
                "dropped": parser.dropped,
                "last_error": parser.last_error,
            }
        return {
            "ok": self.source_error is None,
            "source": "simulation" if self.simulator else "serial",
            "alert_active": self.alert_active,
            "source_error": self.source_error,
            "serial_parser": parser_status,
            "calibration": self.processor.calibration.status(),
        }

    def configure_simulation(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.simulator is None:
            raise ValueError("Simulation controls are available only with --simulate")
        present = _as_bool(payload.get("present")) if "present" in payload else None
        bpm = _as_float(payload.get("bpm")) if "bpm" in payload else None
        distance = _as_float(payload.get("distance_m")) if "distance_m" in payload else None
        self.simulator.configure(present=present, bpm=bpm, distance_m=distance)
        self.processor.simulated_distance_m = self.simulator.distance_m
        return {
            "present": self.simulator.present,
            "bpm": self.simulator.bpm,
            "distance_m": self.simulator.distance_m,
        }

    @staticmethod
    def _initial_telemetry() -> dict[str, Any]:
        return {
            "type": "telemetry",
            "valid": False,
            "status": "backend starting",
            "presence": False,
            "bpm": None,
            "bpm_raw": None,
            "bpm_stable": False,
            "bpm_spread_bpm": None,
            "confidence": 0.0,
            "distance_m": None,
            "distance_uncertainty_m": None,
            "rssi": None,
            "raw_waveform": [],
            "filtered_waveform": [],
            "spectrum": [],
            "alert_active": False,
        }


def create_app(options: RuntimeOptions) -> FastAPI:
    runtime = RescueRuntime(options)
    app = FastAPI(title="RescueSense CSI Backend", version="0.1.0")
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dashboard can be opened by a simple local web server
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def startup() -> None:
        await runtime.start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await runtime.stop()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return runtime.status()

    @app.get("/telemetry")
    async def telemetry() -> dict[str, Any]:
        return runtime.latest

    @app.post("/alert")
    async def alert(payload: dict[str, Any]) -> dict[str, Any]:
        active = _extract_alert_state(payload)
        if active is None:
            raise HTTPException(400, "Body needs active/enabled boolean or an ALERT command")
        return await runtime.set_alert(active)

    @app.get("/calibration/status")
    async def calibration_status() -> dict[str, Any]:
        return runtime.processor.calibration.status()

    @app.post("/calibration/start")
    async def calibration_start(payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", payload.get("mode", ""))).lower()
        duration = _as_float(payload.get("duration_seconds", 30.0))
        distance = _as_float(payload.get("distance_m")) if "distance_m" in payload else None
        if duration is None or not 5.0 <= duration <= 120.0:
            raise HTTPException(400, "duration_seconds must be between 5 and 120")
        try:
            return runtime.processor.calibration.start(kind, duration, distance)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/calibration/stop")
    async def calibration_stop() -> dict[str, Any]:
        return runtime.processor.calibration.stop()

    @app.post("/simulate")
    async def simulate(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return runtime.configure_simulation(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await runtime.connect(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Expected JSON"})
                    continue
                if not isinstance(payload, dict):
                    await websocket.send_json({"type": "error", "message": "Expected an object"})
                    continue
                message_type = str(payload.get("type", "")).lower()
                if message_type in {"alert", "command"} or any(
                    key in payload for key in ("active", "enabled", "command")
                ):
                    active = _extract_alert_state(payload)
                    if active is None:
                        await websocket.send_json(
                            {"type": "error", "message": "Unknown alert command"}
                        )
                    else:
                        await runtime.set_alert(active)
                elif message_type == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})
                else:
                    await websocket.send_json({"type": "error", "message": "Unsupported message"})
        except WebSocketDisconnect:
            pass
        finally:
            runtime.clients.discard(websocket)

    return app


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "enable", "enabled", "alert_on"}:
            return True
        if normalized in {"0", "false", "off", "disable", "disabled", "alert_off"}:
            return False
    return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_alert_state(payload: dict[str, Any]) -> bool | None:
    for key in ("active", "enabled"):
        if key in payload:
            parsed = _as_bool(payload[key])
            if parsed is not None:
                return parsed
    command = str(payload.get("command", "")).strip().upper().replace(" ", "")
    if command in {"ALERT,1", "ALERT=1", "ALERT_ON", "ON"}:
        return True
    if command in {"ALERT,0", "ALERT=0", "ALERT_OFF", "OFF"}:
        return False
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RescueSense Wi-Fi CSI backend")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--simulate", action="store_true", help="run an instant, hardware-free CSI demo")
    source.add_argument("--serial-port", help="ESP32 serial port, for example COM5 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=921600, help="ESP32 serial baud rate (default: 921600)")
    parser.add_argument("--sample-rate", type=float, default=100.0, help="expected CSI packet rate")
    parser.add_argument("--calibration-file", default="calibration.json", help="saved room calibration JSON")
    parser.add_argument("--simulate-bpm", type=float, default=15.0, help="synthetic respiration BPM")
    parser.add_argument("--simulate-distance", type=float, default=2.5, help="synthetic proximity in metres")
    parser.add_argument("--simulate-absent", action="store_true", help="start simulation with no person")
    parser.add_argument("--auto-alert", action="store_true", help="send ALERT,1/0 automatically on presence changes")
    parser.add_argument("--model", help="optional joblib RandomForest model from train_presence_model.py")
    parser.add_argument("--host", default="127.0.0.1", help="server bind host")
    parser.add_argument("--port", type=int, default=8000, help="server port")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    options = RuntimeOptions(
        simulate=args.simulate,
        serial_port=args.serial_port,
        baud=args.baud,
        sample_rate_hz=args.sample_rate,
        calibration_file=args.calibration_file,
        simulate_bpm=args.simulate_bpm,
        simulate_distance_m=args.simulate_distance,
        simulate_absent=args.simulate_absent,
        auto_alert=args.auto_alert,
        model_path=args.model,
    )
    uvicorn.run(create_app(options), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

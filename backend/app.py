"""Starlette/WebSocket entry point for the RescueSense laptop backend.

Examples:
    python app.py                     # auto-detect the ESP32 port
    python app.py --serial-port COM9  # explicit port
    python app.py --simulate          # hardware-free demo
    python app.py --list-ports

Then open http://127.0.0.1:8000 (the backend serves the dashboard).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from processing import CSIProcessor, ProcessorConfig
from serial_io import SerialCSIReader, SimulatedCSIProducer, list_serial_ports

BACKEND_DIR = Path(__file__).resolve().parent
DASHBOARD = BACKEND_DIR.parent / "dashboard" / "index.html"
STATE_TEXT = {
    "breathing": "Breathing detected",
    "empty": "No breathing detected",
    "motion": "Movement detected",
    "warming_up": "Collecting signal",
    "no_signal": "No CSI signal",
}


@dataclass
class RuntimeOptions:
    simulate: bool = False
    serial_port: str = "auto"
    baud: int = 921600
    calibration_file: str = str(BACKEND_DIR / "calibration.json")
    simulate_bpm: float = 15.0
    simulate_distance_m: float = 2.5
    simulate_absent: bool = False
    simulate_motion: bool = False
    auto_alert: bool = False
    model_path: str | None = None


@dataclass
class Event:
    kind: str
    text: str
    t: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "t": round(self.t, 1)}


class RescueRuntime:
    """Owns the CSI source, alert output, calibration and all dashboard clients."""

    def __init__(self, options: RuntimeOptions):
        self.options = options
        self.processor = CSIProcessor(
            ProcessorConfig(),
            calibration_path=options.calibration_file,
            simulated_distance_m=options.simulate_distance_m if options.simulate else None,
        )
        self.clients: set[WebSocket] = set()
        self.reader: SerialCSIReader | None = None
        self.simulator: SimulatedCSIProducer | None = None
        self.tasks: list[asyncio.Task[None]] = []
        self.latest: dict[str, Any] = {"type": "telemetry", "valid": False, "state": "no_signal", "state_detail": "Backend starting."}
        self.alert_active = False
        self.alert_delivered: bool | None = None
        self.model_error: str | None = None
        self.events: deque[Event] = deque(maxlen=40)
        self._last_state: str | None = None
        self._last_calibration_result: Any = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._load_optional_model()
        if self.options.simulate:
            self.simulator = SimulatedCSIProducer(
                bpm=self.options.simulate_bpm,
                distance_m=self.options.simulate_distance_m,
                present=not self.options.simulate_absent,
                auto_motion=self.options.simulate_motion,
            )
            # Fill a historic window so the demo is meaningful on first load.
            for frame in self.simulator.prime(self.processor.config.window_seconds):
                self.processor.add_frame(frame)
            self.tasks.append(asyncio.create_task(self._run_simulator(), name="simulated-csi"))
            self.log("source", "Simulation started (no hardware is being read).")
        else:
            self.reader = SerialCSIReader(self.options.serial_port, self.options.baud, on_frame=self.processor.add_frame)
            self.reader.start()
            self.log("source", f"Listening for the ESP32 on {self.options.serial_port} @ {self.options.baud} baud.")
        self.tasks.append(asyncio.create_task(self._broadcast_loop(), name="telemetry-broadcast"))

    async def stop(self) -> None:
        for client in tuple(self.clients):
            with contextlib.suppress(Exception):
                await client.close(code=1001)
        self.clients.clear()
        if self.reader is not None:
            self.reader.send_alert(False)
            self.reader.stop()
        for task in self.tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _load_optional_model(self) -> None:
        if not self.options.model_path:
            return
        try:
            import joblib  # installed by requirements-ml.txt

            self.processor.set_presence_model(joblib.load(self.options.model_path))
            self.log("model", f"Loaded presence model {self.options.model_path}.")
        except Exception as exc:
            self.model_error = f"Could not load model: {exc}"
            self.log("error", self.model_error)

    def log(self, kind: str, text: str) -> None:
        self.events.append(Event(kind, text))

    # -- sources -------------------------------------------------------------
    async def _run_simulator(self) -> None:
        assert self.simulator is not None
        while True:
            for frame in self.simulator.frames_until(time.monotonic()):
                self.processor.add_frame(frame)
            await asyncio.sleep(0.05)

    # -- telemetry -----------------------------------------------------------
    async def _broadcast_loop(self) -> None:
        while True:
            started = time.monotonic()
            telemetry = await asyncio.to_thread(self.processor.telemetry)
            self._note_changes(telemetry)
            if self.options.auto_alert and telemetry["presence"] != self.alert_active:
                await self.set_alert(bool(telemetry["presence"]), origin="auto")
            telemetry.update(self._envelope())
            self.latest = telemetry
            await self.broadcast(telemetry)
            await asyncio.sleep(max(0.05, 1.0 - (time.monotonic() - started)))

    def _envelope(self) -> dict[str, Any]:
        return {
            "type": "telemetry",
            "server_time": time.time(),
            "source": "simulation" if self.simulator else "serial",
            "serial": self.reader.status() if self.reader else None,
            "simulation": (
                {
                    "present": self.simulator.present,
                    "bpm": self.simulator.bpm,
                    "distance_m": self.simulator.distance_m,
                }
                if self.simulator
                else None
            ),
            "alert_active": self.alert_active,
            "alert_delivered": self.alert_delivered,
            "auto_alert": self.options.auto_alert,
            "model_error": self.model_error,
            "events": [event.as_dict() for event in list(self.events)[-20:]],
        }

    def _note_changes(self, telemetry: dict[str, Any]) -> None:
        state = telemetry.get("state")
        if state != self._last_state:
            text = STATE_TEXT.get(state, str(state))
            if state == "breathing" and telemetry.get("bpm"):
                text += f" ({telemetry['bpm']:.1f} BPM)"
            self.log("state", text)
            self._last_state = state
        result = telemetry.get("calibration", {}).get("last_result")
        if result and result != self._last_calibration_result:
            self.log("calibration" if result.get("ok") else "error", result.get("message", "Calibration finished."))
        self._last_calibration_result = result

    # -- commands ------------------------------------------------------------
    async def set_alert(self, active: bool, origin: str = "dashboard") -> dict[str, Any]:
        changed = self.alert_active != bool(active)
        self.alert_active = bool(active)
        delivered = self.reader.send_alert(self.alert_active) if self.reader else None
        self.alert_delivered = delivered
        if changed:
            where = "ESP32 buzzer" if delivered else ("simulation only" if self.simulator else "not delivered: no serial link")
            self.log("alert", f"Alert {'ON' if active else 'OFF'} ({origin}; {where}).")
        acknowledgement = {
            "type": "alert_ack",
            "active": self.alert_active,
            "delivered": delivered,
            "command": "ALERT,1" if self.alert_active else "ALERT,0",
        }
        await self.broadcast(acknowledgement)
        return acknowledgement

    def calibration(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "start")).lower()
        manager = self.processor.calibration
        if action == "stop":
            return manager.stop()
        if action == "clear":
            return manager.clear()
        kind = str(payload.get("kind", payload.get("mode", ""))).lower()
        duration = _as_float(payload.get("duration_seconds", 30.0))
        distance = _as_float(payload.get("distance_m")) if payload.get("distance_m") is not None else None
        if duration is None:
            raise ValueError("duration_seconds must be a number")
        status = manager.start(kind, duration, distance)
        label = "empty-room baseline" if kind == "empty" else f"{distance:g} m anchor"
        self.log("calibration", f"Recording {label} for {duration:.0f} s.")
        return status

    def configure_simulation(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.simulator is None:
            raise ValueError("Simulation controls are available only with --simulate")
        present = _as_bool(payload.get("present")) if "present" in payload else None
        bpm = _as_float(payload.get("bpm")) if "bpm" in payload else None
        distance = _as_float(payload.get("distance_m")) if "distance_m" in payload else None
        motion = _as_float(payload.get("motion_seconds")) if "motion_seconds" in payload else None
        self.simulator.configure(present=present, bpm=bpm, distance_m=distance, motion_seconds=motion)
        self.processor.simulated_distance_m = self.simulator.distance_m
        if motion:
            self.log("simulation", f"Simulated {motion:.0f} s of body movement.")
        return {"present": self.simulator.present, "bpm": self.simulator.bpm, "distance_m": self.simulator.distance_m}

    # -- clients -------------------------------------------------------------
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

    async def handle_message(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type", "")).lower()
        try:
            if kind in {"alert", "command"} or "active" in payload or "enabled" in payload:
                active = _extract_alert_state(payload)
                if active is None:
                    raise ValueError("Unknown alert command")
                await self.set_alert(active)
            elif kind == "calibration":
                status = self.calibration(payload)
                await websocket.send_json({"type": "ack", "for": "calibration", "ok": True, "calibration": status})
            elif kind == "simulate":
                await websocket.send_json({"type": "ack", "for": "simulate", "ok": True, "simulation": self.configure_simulation(payload)})
            elif kind == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})
            elif kind == "hello":
                pass
            else:
                raise ValueError(f"Unsupported message type '{kind}'")
        except ValueError as exc:
            await websocket.send_json({"type": "ack", "for": kind, "ok": False, "message": str(exc)})

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "source": "simulation" if self.simulator else "serial",
            "state": self.latest.get("state"),
            "alert_active": self.alert_active,
            "serial": self.reader.status() if self.reader else None,
            "link": self.latest.get("link"),
            "calibration": self.processor.calibration.status(),
            "model_error": self.model_error,
        }


def create_app(options: RuntimeOptions) -> Starlette:
    """Plain Starlette (no FastAPI): fewer moving parts and no version coupling."""

    runtime = RescueRuntime(options)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    async def body(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def error(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"detail": message}, status_code=status)

    async def dashboard(_request: Request) -> Response:
        if not DASHBOARD.exists():
            return error(f"Dashboard not found at {DASHBOARD}", 404)
        return FileResponse(DASHBOARD, headers={"Cache-Control": "no-store"})

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(runtime.status())

    async def telemetry(_request: Request) -> JSONResponse:
        return JSONResponse(runtime.latest)

    async def ports(_request: Request) -> JSONResponse:
        return JSONResponse(list_serial_ports())

    async def alert(request: Request) -> JSONResponse:
        active = _extract_alert_state(await body(request))
        if active is None:
            return error("Body needs an active/enabled boolean or an ALERT,1 / ALERT,0 command")
        return JSONResponse(await runtime.set_alert(active, origin="api"))

    async def calibration_status(_request: Request) -> JSONResponse:
        return JSONResponse(runtime.processor.calibration.status())

    async def calibration_start(request: Request) -> JSONResponse:
        try:
            return JSONResponse(runtime.calibration({**(await body(request)), "action": "start"}))
        except ValueError as exc:
            return error(str(exc))

    async def calibration_stop(_request: Request) -> JSONResponse:
        return JSONResponse(runtime.calibration({"action": "stop"}))

    async def calibration_clear(_request: Request) -> JSONResponse:
        return JSONResponse(runtime.calibration({"action": "clear"}))

    async def simulate(request: Request) -> JSONResponse:
        try:
            return JSONResponse(runtime.configure_simulation(await body(request)))
        except ValueError as exc:
            return error(str(exc))

    async def websocket_endpoint(websocket: WebSocket) -> None:
        await runtime.connect(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "ack", "ok": False, "message": "Expected JSON"})
                    continue
                if isinstance(payload, dict):
                    await runtime.handle_message(websocket, payload)
        except WebSocketDisconnect:
            pass
        finally:
            runtime.clients.discard(websocket)

    routes = [
        Route("/", dashboard),
        Route("/health", health),
        Route("/telemetry", telemetry),
        Route("/ports", ports),
        Route("/alert", alert, methods=["POST"]),
        Route("/calibration/status", calibration_status),
        Route("/calibration/start", calibration_start, methods=["POST"]),
        Route("/calibration/stop", calibration_stop, methods=["POST"]),
        Route("/calibration/clear", calibration_clear, methods=["POST"]),
        Route("/simulate", simulate, methods=["POST"]),
        WebSocketRoute("/ws", websocket_endpoint),
    ]
    # CORS: the dashboard may also be opened straight from disk (file://).
    middleware = [Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.runtime = runtime
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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--simulate", action="store_true", help="run a hardware-free CSI simulation")
    source.add_argument("--serial-port", default="auto", help="ESP32 serial port (default: auto-detect, e.g. COM9)")
    source.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    parser.add_argument("--baud", type=int, default=921600, help="ESP32 serial baud rate (default: 921600)")
    parser.add_argument(
        "--calibration-file",
        help="saved room calibration (default: calibration.json, or calibration.simulated.json with --simulate)",
    )
    parser.add_argument("--simulate-bpm", type=float, default=15.0, help="synthetic respiration rate")
    parser.add_argument("--simulate-distance", type=float, default=2.5, help="synthetic proximity in metres")
    parser.add_argument("--simulate-absent", action="store_true", help="start the simulation with nobody present")
    parser.add_argument("--simulate-motion", action="store_true", help="add occasional random movement to the simulation")
    parser.add_argument("--auto-alert", action="store_true", help="drive the buzzer automatically from detections")
    parser.add_argument("--model", help="optional joblib model from train_presence_model.py")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 to allow other devices)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No serial ports found.")
        for port in ports:
            tag = "ESP32 candidate" if port["candidate"] else ("Bluetooth (ignored)" if port["bluetooth"] else "other")
            print(f"{port['device']:>8}  {tag:<20} {port['description']}  VID:PID={port['vid']}:{port['pid']}")
        return
    # A baseline recorded against simulated data must never leak into a live
    # session, so simulation keeps its own calibration file.
    calibration_file = args.calibration_file or str(
        BACKEND_DIR / ("calibration.simulated.json" if args.simulate else "calibration.json")
    )
    options = RuntimeOptions(
        simulate=args.simulate,
        serial_port=args.serial_port,
        baud=args.baud,
        calibration_file=calibration_file,
        simulate_bpm=args.simulate_bpm,
        simulate_distance_m=args.simulate_distance,
        simulate_absent=args.simulate_absent,
        simulate_motion=args.simulate_motion,
        auto_alert=args.auto_alert,
        model_path=args.model,
    )
    url = f"http://{'127.0.0.1' if args.host in {'0.0.0.0', '::'} else args.host}:{args.port}"
    mode = "SIMULATION" if args.simulate else f"ESP32 serial ({args.serial_port})"
    print(f"\n  RescueSense backend  |  source: {mode}\n  Dashboard: {url}\n  Stop with Ctrl+C\n")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(create_app(options), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

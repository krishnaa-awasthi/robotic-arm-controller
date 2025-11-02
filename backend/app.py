# backend/app.py
"""
Safe Simulated Control Server for TechExpo demo.
- WebSocket control channel
- Simulated arm model with limits, safety, estop, and telemetry
- Mission logging to mission_logs/ (created automatically)
- Demo token: demo-token-123
"""

import asyncio
import json
import os
import logging
import math
import random
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

LOG_DIR = "mission_logs"
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="TechExpo Sim Control")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

VALID_TOKENS = {"demo-token-123"}
SIM_LOOP_INTERVAL = 0.12

class SimulatedArm:
    JOINTS = ("base", "shoulder", "elbow", "wrist", "gripper")
    LIMITS = {
        "base": (-180, 180),
        "shoulder": (-90, 90),
        "elbow": (-90, 90),
        "wrist": (-90, 90),
        "gripper": (0, 100),
    }
    MAX_SPEED = {"base": 60.0, "shoulder": 40.0, "elbow": 40.0, "wrist": 60.0, "gripper": 120.0}

    def __init__(self):
        self.pos = {j: 0.0 for j in self.JOINTS}
        self.pos["gripper"] = 10.0
        self.targets = self.pos.copy()
        self.health = {"battery": 100.0, "temp_c": 35.0, "comm_ok": True}
        self.estopped = False
        self.frame_id = 0
        self.events = []

    def request_move(self, joint, value):
        if self.estopped:
            return False, "estopped"
        if joint not in self.JOINTS:
            return False, "invalid_joint"
        lo, hi = self.LIMITS[joint]
        if value < lo or value > hi:
            return False, f"out_of_range({lo},{hi})"
        self.targets[joint] = float(value)
        self.events.append({"ev": "move_req", "joint": joint, "value": value})
        return True, "accepted"

    def request_grip(self, v):
        if self.estopped:
            return False, "estopped"
        if v == "open":
            self.targets["gripper"] = float(self.LIMITS["gripper"][0])
        else:
            self.targets["gripper"] = float(self.LIMITS["gripper"][1])
        self.events.append({"ev": "grip_req", "val": v})
        return True, "accepted"

    def emergency_stop(self):
        self.estopped = True
        self.targets = self.pos.copy()
        self.events.append({"ev": "estop"})
        return True

    def clear_estop(self):
        self.estopped = False
        self.events.append({"ev": "estop_clear"})
        return True

    def simulate_step(self, dt):
        if self.estopped:
            # cooling while stopped
            self.health["temp_c"] = max(30.0, self.health["temp_c"] - 0.02)
            self.frame_id += 1
            return

        # simulate battery drain and temp noise
        self.health["battery"] = max(0.0, self.health["battery"] - 0.005 * dt)
        self.health["temp_c"] += (random.random() - 0.5) * 0.03

        for j in self.JOINTS:
            cur = self.pos[j]
            tgt = self.targets[j]
            if abs(tgt - cur) < 1e-3:
                continue
            max_move = self.MAX_SPEED[j] * dt
            delta = tgt - cur
            if abs(delta) <= max_move:
                self.pos[j] = tgt
            else:
                self.pos[j] = cur + math.copysign(max_move, delta)

        # comm_ok is placeholder for future checks
        self.health["comm_ok"] = True
        self.frame_id += 1

    def state(self):
        return {
            "positions": self.pos.copy(),
            "targets": self.targets.copy(),
            "health": self.health.copy(),
            "estopped": self.estopped,
            "frame_id": self.frame_id,
            "events": self.events[-10:],
        }

# Global simulated arm and client tracking
arm = SimulatedArm()
clients = set()  # set of WebSocket objects
write_lock = asyncio.Lock()  # for mission log file writes

async def mission_log(entry: dict):
    """Append a JSON line to today's mission log file."""
    async with write_lock:
        fname = os.path.join(LOG_DIR, f"mission_{datetime.utcnow().strftime('%Y%m%d')}.log")
        line = json.dumps({"ts": datetime.utcnow().isoformat(), "entry": entry})
        with open(fname, "a") as f:
            f.write(line + "\n")
    logging.info("Mission log: %s", entry)

@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """
    WebSocket protocol:
    - Client must send: {"type":"auth","token":"..."} as first message
    - Then send commands as: {"type":"cmd","command": {...}}
    - Server periodically sends telemetry: {"type":"telemetry", "state": ...}
    - And simulated video frames: {"type":"video_frame", "frame_id": ...}
    """
    await ws.accept()
    try:
        # short auth handshake
        raw = await asyncio.wait_for(ws.receive_text(), timeout=6.0)
        try:
            j = json.loads(raw)
        except Exception:
            await ws.send_text(json.dumps({"type": "error", "message": "invalid_json"}))
            await ws.close()
            return

        if j.get("type") != "auth" or j.get("token") not in VALID_TOKENS:
            await ws.send_text(json.dumps({"type": "error", "message": "invalid_auth"}))
            await ws.close()
            return

        # register client
        clients.add(ws)
        logging.info("Client authenticated and added. Total clients: %d", len(clients))
        await ws.send_text(json.dumps({"type": "auth_ok"}))
        await mission_log({"ev": "client_connected"})

        # start consumer/producer tasks
        reader_task = asyncio.create_task(consume_messages(ws))
        writer_task = asyncio.create_task(produce_messages(ws))

        done, pending = await asyncio.wait([reader_task, writer_task], return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()

    except (asyncio.TimeoutError, WebSocketDisconnect):
        logging.info("Websocket auth timeout or disconnect during handshake.")
    except Exception as e:
        logging.exception("ws err %s", e)
    finally:
        # unregister client
        if ws in clients:
            clients.discard(ws)
        logging.info("Client disconnected. Remaining clients: %d", len(clients))

        # if no clients remain, place arm in safe state
        if not clients:
            logging.info("No clients connected — triggering emergency stop on simulated arm.")
            arm.emergency_stop()
            await mission_log({"ev": "autostop_no_clients"})

        try:
            await ws.close()
        except Exception:
            pass

async def consume_messages(ws: WebSocket):
    """Read incoming messages from client and process commands."""
    try:
        while True:
            txt = await ws.receive_text()
            try:
                msg = json.loads(txt)
            except Exception:
                await ws.send_text(json.dumps({"type": "error", "message": "invalid_json"}))
                continue

            if msg.get("type") == "cmd":
                cmd = msg.get("command", {})
                await handle_cmd(cmd, ws)
            else:
                await ws.send_text(json.dumps({"type": "error", "message": "unknown"}))

    except WebSocketDisconnect:
        logging.info("consume_messages: client disconnected (WebSocketDisconnect).")
    except Exception as e:
        logging.exception("consume_messages error: %s", e)

async def handle_cmd(cmd: dict, ws: WebSocket):
    """Process a command dict sent by client."""
    action = cmd.get("action")
    if not action:
        await ws.send_text(json.dumps({"type": "error", "message": "no_action"}))
        return

    if action == "estop":
        arm.emergency_stop()
        await mission_log({"ev": "estop", "by": "client"})
        await ws.send_text(json.dumps({"type": "ack", "msg": "estop_ok"}))
        return

    if action == "clear_estop":
        arm.clear_estop()
        await mission_log({"ev": "estop_cleared"})
        await ws.send_text(json.dumps({"type": "ack", "msg": "estop_cleared"}))
        return

    if action == "move":
        joint = cmd.get("joint")
        value = cmd.get("value")
        try:
            value_f = float(value)
        except Exception:
            await ws.send_text(json.dumps({"type": "error", "message": "invalid_value"}))
            return
        ok, reason = arm.request_move(joint, value_f)
        await mission_log({"ev": "move_req", "joint": joint, "value": value_f, "ok": ok})
        if ok:
            await ws.send_text(json.dumps({"type": "ack", "msg": "move_accepted", "joint": joint, "value": value_f}))
        else:
            await ws.send_text(json.dumps({"type": "error", "message": reason}))
        return

    if action == "grip":
        val = cmd.get("value")
        if val not in ("open", "close"):
            await ws.send_text(json.dumps({"type": "error", "message": "invalid_grip"}))
            return
        ok, reason = arm.request_grip(val)
        await mission_log({"ev": "grip_req", "val": val, "ok": ok})
        if ok:
            await ws.send_text(json.dumps({"type": "ack", "msg": "grip_accepted", "value": val}))
        else:
            await ws.send_text(json.dumps({"type": "error", "message": reason}))
        return

    await ws.send_text(json.dumps({"type": "error", "message": "unsupported"}))

async def produce_messages(ws: WebSocket):
    """Periodically update simulation and push telemetry + simulated video frames."""
    try:
        while True:
            arm.simulate_step(SIM_LOOP_INTERVAL)
            t_msg = {"type": "telemetry", "state": arm.state(), "timestamp": datetime.utcnow().isoformat()}
            try:
                await ws.send_text(json.dumps(t_msg))
                v_msg = {"type": "video_frame", "frame_id": arm.frame_id, "note": "simulated_frame"}
                await ws.send_text(json.dumps(v_msg))
            except Exception:
                # sending failed (client likely disconnected)
                break
            await asyncio.sleep(SIM_LOOP_INTERVAL)
    except WebSocketDisconnect:
        logging.info("produce_messages: client disconnected (WebSocketDisconnect).")
    except Exception as e:
        logging.exception("produce_messages error: %s", e)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8765, log_level="info")

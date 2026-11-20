"""Adrishta API: hardware ingest + live viewer WebSockets + operator controls."""
import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse

from .dataset import Store, clean
from .engine import CMDS, LABELS, Engine

log = logging.getLogger("adrishta")
INGEST_TOKEN = os.environ.get("ADRISHTA_INGEST_TOKEN", "")
ADMIN_KEY = os.environ.get("ADRISHTA_ADMIN_KEY", "")
DATA_DIR = os.environ.get("ADRISHTA_DATA_DIR", "./data")
ALLOWED_ORIGIN = os.environ.get("ADRISHTA_ALLOWED_ORIGIN", "")
MAX_VIEWERS = int(os.environ.get("ADRISHTA_MAX_VIEWERS", "100"))
PUBLIC_HEATMAP = os.environ.get("ADRISHTA_PUBLIC_HEATMAP", "1") == "1"
STATIC_DIR = os.environ.get("ADRISHTA_SERVE_STATIC", "")      # local testing only
if len(INGEST_TOKEN) < 16 or len(ADMIN_KEY) < 12:
    raise RuntimeError("Set ADRISHTA_INGEST_TOKEN (16+ chars) and ADRISHTA_ADMIN_KEY (12+ chars)")

store = Store(DATA_DIR)
engine = Engine(store)
viewers = set()
gateway = None                 # legacy serial_bridge.py WebSocket
browser_gateway = None         # authenticated browser Web Serial owner


def safe_eq(a, b):
    return hmac.compare_digest(str(a).encode(), str(b).encode())


class Viewer:
    __slots__ = ("ws", "view", "delta", "admin", "fails")

    def __init__(self, ws):
        self.ws, self.view, self.delta, self.admin, self.fails = ws, "0", False, False, 0

    async def send(self, text):
        await asyncio.wait_for(self.ws.send_text(text), 1.0)


async def broadcaster():
    nxt = time.monotonic()
    while True:
        nxt += 0.1
        try:
            now = time.time()
            engine.tick(now)
            out = [json.dumps(engine.snapshot(now, gateway is not None or browser_gateway is not None))]
            out += [json.dumps(e) for e in engine.drain_events()]
            ds_msg = None
            if engine.dataset_dirty:
                engine.dataset_dirty = False
                ds_msg = json.dumps({"type": "dataset", "items": store.summary()})
            send_heat, cache = engine.tick_n % 2 == 0, {}

            async def push(v, msgs):
                try:
                    for m in msgs:
                        await v.send(m)
                except Exception:
                    viewers.discard(v)

            jobs = []
            for v in list(viewers):
                msgs = list(out)
                if ds_msg and v.admin:
                    msgs.append(ds_msg)
                if send_heat and (PUBLIC_HEATMAP or v.admin):
                    key = (v.view, v.delta)
                    if key not in cache:
                        h = engine.heat(*key)
                        cache[key] = json.dumps(h) if h else None
                    if cache[key]:
                        msgs.append(cache[key])
                jobs.append(push(v, msgs))
            if jobs:
                await asyncio.gather(*jobs)
        except Exception:
            log.exception("broadcast error")
        await asyncio.sleep(max(0.0, nxt - time.monotonic()))


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(broadcaster())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/health")
def health():
    return {"ok": True}


def require_admin(key):
    if not key or not safe_eq(key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/api/dataset/export.json")
def export_json(x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    return Response(store.export_json(), media_type="application/json")


@app.get("/api/dataset/raw.csv")
def export_raw(x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    return StreamingResponse(store.raw_csv_iter(), media_type="text/csv")


@app.get("/api/dataset/features.csv")
def export_features(x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    return Response(store.features_csv(), media_type="text/csv")


@app.websocket("/ws/ingest")
async def ws_ingest(ws: WebSocket):
    global gateway
    if browser_gateway is not None:
        await ws.close(code=4009)
        return
    auth = ws.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not safe_eq(auth[7:].strip(), INGEST_TOKEN):
        await ws.close(code=4401)
        return
    await ws.accept()
    if gateway is not None:
        try:
            await gateway.close(code=4000)
        except Exception:
            pass
    gateway = ws
    log.info("gateway connected")
    try:
        while True:
            msg = await ws.receive_text()
            if len(msg) > 262144:
                break
            now = time.time()
            for line in msg.splitlines():
                engine.handle_line(line.strip(), now)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ingest error")
    finally:
        if gateway is ws:
            gateway = None
        log.info("gateway disconnected")


async def handle_client(v, m, now):
    global browser_gateway
    t = m.get("type")
    if t == "view":
        view = str(m.get("view", "0"))
        v.view, v.delta = (view if view in ("0", "1", "2", "fused") else "0"), bool(m.get("delta"))
        return
    if t == "auth":
        if v.fails >= 5:
            return
        if safe_eq(m.get("key", ""), ADMIN_KEY):
            v.admin = True
            await v.send(json.dumps({"type": "auth", "ok": True}))
            await v.send(json.dumps({"type": "dataset", "items": store.summary()}))
        else:
            v.fails += 1
            await asyncio.sleep(1)
            await v.send(json.dumps({"type": "auth", "ok": False}))
        return
    if not v.admin:
        await v.send(json.dumps({"type": "event", "kind": "toast", "msg": "Admin login required", "level": "warn"}))
        return

    # Browser-based UART ingestion. The browser must be operator-authenticated;
    # the permanent ADRISHTA_INGEST_TOKEN is never exposed to JavaScript.
    if t == "ingest_start":
        if gateway is not None:
            await v.send(json.dumps({"type": "ingest_status", "ok": False,
                                     "message": "A serial_bridge gateway is already connected"}))
        elif browser_gateway is not None and browser_gateway is not v:
            await v.send(json.dumps({"type": "ingest_status", "ok": False,
                                     "message": "Another browser already owns the hardware connection"}))
        else:
            browser_gateway = v
            await v.send(json.dumps({"type": "ingest_status", "ok": True, "message": "Browser UART gateway active"}))
        return
    if t == "ingest_stop":
        if browser_gateway is v:
            browser_gateway = None
        return
    if t == "ingest":
        if browser_gateway is not v:
            await v.send(json.dumps({"type": "ingest_status", "ok": False,
                                     "message": "Start the RX Hub connection before sending data"}))
            return
        lines = m.get("lines")
        if not isinstance(lines, list) or len(lines) > 16:
            return
        for line in lines:
            if not isinstance(line, str) or len(line) > 1024:
                continue
            line = line.strip()
            if line.startswith(("CSI_DATA,", "ACK,", "DBG,")):
                engine.handle_line(line, now)
        return

    if t == "calibrate":
        engine.start_calibration(now)
    elif t == "walk_cal":
        engine.start_walk(now)
    elif t == "fall_reset":
        engine.fall.reset(clear_cooldown=True)
    elif t == "cmd":
        cmd = str(m.get("cmd", ""))
        if cmd not in CMDS:
            engine.emit("toast", msg="Command not allowed", level="warn")
        elif gateway is not None:
            await gateway.send_text(cmd)
        elif browser_gateway is not None:
            await browser_gateway.send(json.dumps({"type": "hardware_cmd", "cmd": cmd}))
        else:
            engine.emit("toast", msg="Hardware gateway offline", level="warn")
    elif t == "set":
        if "threshold" in m:
            engine.set_threshold(min(8.0, max(0.3, float(m["threshold"]))))
        if "spike" in m:
            engine.spike = min(12.0, max(2.0, float(m["spike"])))
        if "min_agree" in m:
            engine.min_agree = min(3, max(1, int(m["min_agree"])))
        if "auto_event" in m:
            engine.auto_event = bool(m["auto_event"])
    elif t == "rec_start":
        label = m.get("label")
        if label not in LABELS:
            return
        secs = int(m.get("seconds", 6))
        engine.start_recording(now, label, clean(m.get("subject"), "subj_01"), clean(m.get("room"), "roomA"),
                               secs if secs in (0, 4, 6, 10) else 6, bool(m.get("countdown", True)))
    elif t == "rec_stop":
        engine.stop_recording(now)
    elif t == "ds_delete":
        store.delete(str(m.get("id", "")))
        engine.dataset_dirty = True
    elif t == "ds_clear":
        store.clear()
        engine.dataset_dirty = True


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    global browser_gateway
    if ALLOWED_ORIGIN and ws.headers.get("origin", "") != ALLOWED_ORIGIN:
        await ws.close(code=4403)
        return
    if len(viewers) >= MAX_VIEWERS:
        await ws.close(code=4429)
        return
    await ws.accept()
    v = Viewer(ws)
    viewers.add(v)
    try:
        await v.send(json.dumps(engine.init_payload()))
        while True:
            raw = await ws.receive_text()
            if len(raw) > 16384:
                break
            try:
                m = json.loads(raw)
                if isinstance(m, dict):
                    await handle_client(v, m, time.time())
            except (ValueError, TypeError):
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if browser_gateway is v:
            browser_gateway = None
        viewers.discard(v)


if STATIC_DIR:                                            # local testing only
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

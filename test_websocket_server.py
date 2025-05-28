# test_websocket_server.py – telemetry simulator with start/stop control
# ----------------------------------------------------------------------------
# Usage summary
#   $ uvicorn test_websocket_server:app --reload --port 8000
#   POST /start  {"cmd":"start"}  → begin emitting SSE telemetry on /stream
#   POST /stop   {"cmd":"stop"}   → pause telemetry
#   GET  /stream                 → text/event-stream compatible with dashboard
#   GET  /status                 → {started: true|false}
# ----------------------------------------------------------------------------

from __future__ import annotations
import asyncio, json, math, os
from datetime import datetime
from typing import List

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

# ----------------------- configuration constants --------------------------- #
CSV_PATH          = os.getenv("CSV_PATH", "sample_stops.csv")
TRUCK_SPEED_MPH   = float(os.getenv("TRUCK_SPEED_MPH", 20))
DRONE_SPEED_MPH   = float(os.getenv("DRONE_SPEED_MPH", 35))
TICK_SEC          = float(os.getenv("TICK_SEC", 1))          # seconds between frames
RANGE_MILES       = float(os.getenv("RANGE_MILES", 3))       # drone range limit
NUM_DRONES        = int(os.getenv("NUM_DRONES", 4))

# ----------------------- geo utility functions ---------------------------- #
EARTH_MI = 3958.8  # mean Earth radius in miles

def haversine_miles(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_MI * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def interpolate(p0, p1, frac):
    lat = p0[0] + (p1[0] - p0[0]) * frac
    lon = p0[1] + (p1[1] - p0[1]) * frac
    return lat, lon

def heading_deg(p0, p1):
    dlon = math.radians(p1[1] - p0[1])
    lat1 = math.radians(p0[0])
    lat2 = math.radians(p1[0])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

# ----------------------- routing identical to dashboard ------------------- #

def load_stops() -> List[dict]:
    df = pd.read_csv(CSV_PATH)
    df["lat"] = df["lat"].astype(float)
    df["lon"] = df["lon"].astype(float)
    return df.to_dict("records")

def plan_routes(stops: List[dict]):
    truck_set = {0, len(stops) - 1}
    prev_cache, next_cache = {}, {}

    def prev_truck(i):
        if i in prev_cache:
            return prev_cache[i]
        prev_cache[i] = max(j for j in truck_set if j < i)
        return prev_cache[i]

    def next_truck(i):
        if i in next_cache:
            return next_cache[i]
        next_cache[i] = min(j for j in truck_set if j > i)
        return next_cache[i]

    drone_assign = []
    for cust_idx in range(1, len(stops) - 1):
        l, r = prev_truck(cust_idx), next_truck(cust_idx)
        d = (
            haversine_miles(stops[l]["lat"], stops[l]["lon"], stops[cust_idx]["lat"], stops[cust_idx]["lon"]) +
            haversine_miles(stops[cust_idx]["lat"], stops[cust_idx]["lon"], stops[r]["lat"], stops[r]["lon"])
        )
        if d <= RANGE_MILES:
            drone_assign.append((cust_idx, l, r))
        else:
            truck_set.add(cust_idx)

    truck_path = [[stops[idx]["lat"], stops[idx]["lon"]] for idx in sorted(truck_set)]

    bins = [[] for _ in range(NUM_DRONES)]
    for n, triple in enumerate(drone_assign):
        bins[n % NUM_DRONES].append(triple)

    drone_paths = []
    for bucket in bins:
        poly = []
        for cust, l, r in bucket:
            launch   = [stops[l]["lat"], stops[l]["lon"]]
            customer = [stops[cust]["lat"], stops[cust]["lon"]]
            recovery = [stops[r]["lat"], stops[r]["lon"]]
            if poly and poly[-1] == launch:
                poly.extend([customer, recovery])
            else:
                poly.extend([launch, customer, recovery])
        drone_paths.append(poly)
    return truck_path, drone_paths

# ----------------------- animator factory --------------------------------- #

def make_animator(polyline, speed_mph):
    if len(polyline) < 2:
        async def _idle():
            p = polyline[0]
            while True:
                yield p[0], p[1], 0.0
                await asyncio.sleep(TICK_SEC)
        return _idle()

    seg_len = [
        haversine_miles(polyline[i][0], polyline[i][1], polyline[i + 1][0], polyline[i + 1][1])
        for i in range(len(polyline) - 1)
    ]
    seg_time = [l / speed_mph * 3600 for l in seg_len]

    async def _anim():
        idx, t = 0, 0.0
        while True:
            p0, p1 = polyline[idx], polyline[idx + 1]
            alpha = t / seg_time[idx] if seg_time[idx] else 1.0
            lat, lon = interpolate(p0, p1, alpha)
            hdg = heading_deg(p0, p1)
            yield lat, lon, hdg
            await asyncio.sleep(TICK_SEC)
            t += TICK_SEC
            if t >= seg_time[idx]:
                t, idx = 0.0, (idx + 1) % (len(polyline) - 1)
    return _anim()

# ----------------------- FastAPI application ------------------------------ #

app = FastAPI(title="Delivery Telemetry Simulator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared start/stop flag
STARTED = asyncio.Event()

# Build animators once on startup
stops = load_stops()
truck_poly, drone_polys = plan_routes(stops)
ANIMATORS = {
    "truck-1": make_animator(truck_poly, TRUCK_SPEED_MPH),
    **{f"drone-{i+1}": make_animator(p, DRONE_SPEED_MPH) for i, p in enumerate(drone_polys) if p},
}

# ----------------------- SSE endpoint ------------------------------------- #

async def sse_iter():
    await STARTED.wait()
    while True:
        for vid, gen in ANIMATORS.items():
            lat, lon, hdg = await gen.__anext__()
            payload = {
                "id": vid,
                "type": "truck" if vid.startswith("truck") else "drone",
                "lat": lat,
                "lon": lon,
                "heading": hdg,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            yield f"data: {json.dumps(payload)}\n\n"

@app.get("/stream")
async def stream(_req: Request):
    return StreamingResponse(sse_iter(), media_type="text/event-stream")

# ----------------------- control endpoints --------------------------------- #

@app.post("/start")
@app.get("/start")
async def start_sim(request: Request, body: dict | None = None):
    """Toggle simulation on – accepts POST with JSON {cmd:'start'} **or** simple GET."""
    if request.method == "POST":
        if not body or body.get("cmd") != "start":
            raise HTTPException(400, "Expected JSON {'cmd':'start'} in POST body")
    STARTED.set()
    return {"started": True}

@app.post("/stop")
@app.get("/stop")
async def stop_sim(request: Request, body: dict | None = None):
    """Pause simulation – accepts POST {cmd:'stop'} or simple GET."""
    if request.method == "POST":
        if not body or body.get("cmd") != "stop":
            raise HTTPException(400, "Expected JSON {'cmd':'stop'} in POST body")
    STARTED.clear()
    return {"started": False}

@app.get("/status")
async def status():
    return JSONResponse({"started": STARTED.is_set()})

@app.get("/")
async def root():
    return {"msg": "POST /start to emit SSE, /stop to pause"}

# ----------------------- entrypoint --------------------------------------- #

if __name__ == "__main__":
    uvicorn.run("test_websocket_server:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)

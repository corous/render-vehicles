# test_websocket_server.py – telemetry simulator (truck + drones)
# -----------------------------------------------------------------------------
# • Reads stops from CSV (warehouse first row) and reproduces the same 3‑mile
#   routing logic used by the Streamlit dashboard.
# • Emits Server‑Sent Events (SSE) on /stream that animate:
#       truck‑1  – along its blue polyline at 20 mph
#       drone‑n  – along their purple polylines at 35 mph
# • NEW: waits 30 seconds after startup **before** the first SSE event so
#   front‑end services have time to come online.
# -----------------------------------------------------------------------------

from __future__ import annotations
import asyncio, json, math, os
from datetime import datetime
from typing import List

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import uvicorn

# ----------------------- configuration constants --------------------------- #
CSV_PATH          = os.getenv("CSV_PATH", "sample_stops.csv")
TRUCK_SPEED_MPH   = float(os.getenv("TRUCK_SPEED_MPH", 20))
DRONE_SPEED_MPH   = float(os.getenv("DRONE_SPEED_MPH", 35))
TICK_SEC          = float(os.getenv("TICK_SEC", 1))          # seconds per frame
RANGE_MILES       = float(os.getenv("RANGE_MILES", 3))       # drone range cap
NUM_DRONES        = int(os.getenv("NUM_DRONES", 4))
START_DELAY_SEC   = float(os.getenv("START_DELAY_SEC", 30))  # <-- 30‑second wait

# ----------------------- geo helpers -------------------------------------- #
EARTH_MI = 3958.8

def haversine_miles(lat1, lon1, lat2, lon2):
    phi1, phi2 = map(math.radians, (lat1, lat2))
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
    lat1, lat2 = map(math.radians, (p0[0], p1[0]))
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

# ----------------------- load stops & plan routes ------------------------- #

def load_stops() -> List[dict]:
    df = pd.read_csv(CSV_PATH)
    df["lat"], df["lon"] = df["lat"].astype(float), df["lon"].astype(float)
    return df.to_dict("records")

def plan_routes(stops: List[dict]):
    truck_set = {0, len(stops) - 1}
    prev_cache, next_cache = {}, {}

    def prev_truck(i: int) -> int:
        if i not in prev_cache:
            prev_cache[i] = max(j for j in truck_set if j < i)
        return prev_cache[i]

    def next_truck(i: int) -> int:
        if i not in next_cache:
            next_cache[i] = min(j for j in truck_set if j > i)
        return next_cache[i]

    drone_candidates = []
    for cid in range(1, len(stops) - 1):
        l, r = prev_truck(cid), next_truck(cid)
        dist = (
            haversine_miles(stops[l]["lat"], stops[l]["lon"], stops[cid]["lat"], stops[cid]["lon"]) +
            haversine_miles(stops[cid]["lat"], stops[cid]["lon"], stops[r]["lat"], stops[r]["lon"])
        )
        if dist <= RANGE_MILES:
            drone_candidates.append((cid, l, r))
        else:
            truck_set.add(cid)

    truck_path = [[stops[i]["lat"], stops[i]["lon"]] for i in sorted(truck_set)]

    bins = [[] for _ in range(NUM_DRONES)]
    for n, triple in enumerate(drone_candidates):
        bins[n % NUM_DRONES].append(triple)

    drone_paths = []
    for bucket in bins:
        poly = []
        for cid, l, r in bucket:
            launch   = [stops[l]["lat"], stops[l]["lon"]]
            customer = [stops[cid]["lat"], stops[cid]["lon"]]
            recovery = [stops[r]["lat"], stops[r]["lon"]]
            if poly and poly[-1] == launch:
                poly.extend([customer, recovery])
            else:
                poly.extend([launch, customer, recovery])
        drone_paths.append(poly)
    return truck_path, drone_paths

# ----------------------- animator factory --------------------------------- #

def make_animator(path, speed_mph):
    if len(path) < 2:
        async def idle():
            lat, lon = path[0]
            while True:
                yield lat, lon, 0.0
                await asyncio.sleep(TICK_SEC)
        return idle()

    seg_len = [haversine_miles(*path[i], *path[i + 1]) for i in range(len(path) - 1)]
    seg_time = [l / speed_mph * 3600 for l in seg_len]

    async def anim():
        idx, t = 0, 0.0
        while True:
            p0, p1 = path[idx], path[idx + 1]
            frac = t / seg_time[idx] if seg_time[idx] else 1.0
            lat, lon = interpolate(p0, p1, frac)
            hdg = heading_deg(p0, p1)
            yield lat, lon, hdg
            await asyncio.sleep(TICK_SEC)
            t += TICK_SEC
            if t >= seg_time[idx]:
                t, idx = 0.0, (idx + 1) % (len(path) - 1)
    return anim()

# ----------------------- FastAPI setup ------------------------------------ #

app = FastAPI(title="Telemetry Simulator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

stops = load_stops()
truck_path, drone_paths = plan_routes(stops)
ANIMATORS = {
    "truck-1": make_animator(truck_path, TRUCK_SPEED_MPH),
    **{f"drone-{i+1}": make_animator(poly, DRONE_SPEED_MPH) for i, poly in enumerate(drone_paths) if poly},
}

# ----------------------- SSE stream with start delay ---------------------- #

async def sse_stream():
    await asyncio.sleep(START_DELAY_SEC)  # 30‑second warm‑up
    while True:
        for vid, gen in list(ANIMATORS.items()):
            try:
                lat, lon, hdg = await gen.__anext__()
            except StopAsyncIteration:
                # Rebuild animator in the rare event it ends
                if vid.startswith("truck"):
                    ANIMATORS[vid] = make_animator(truck_path, TRUCK_SPEED_MPH)
                else:
                    idx = int(vid.split("-")[1]) - 1
                    if idx < len(drone_paths) and drone_paths[idx]:
                        ANIMATORS[vid] = make_animator(drone_paths[idx], DRONE_SPEED_MPH)
                    continue
                lat, lon, hdg = await ANIMATORS[vid].__anext__()
            payload = {
                "id": vid,
                "type": "truck" if vid.startswith("truck") else "drone",
                "lat": lat,
                "lon": lon,
                "heading": hdg,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            yield f"data: {json.dumps(payload)}"

@app.get("/stream")
async def stream(_req: Request):
    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable Nginx/Render buffering
        },
    )

@app.get("/")
async def root():
    return {"msg": "Telemetry will start 30 s after server launch – connect to /stream"}

if __name__ == "__main__":
    uvicorn.run("test_websocket_server:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

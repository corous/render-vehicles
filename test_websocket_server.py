# test_websocket_server.py – simulates one truck + four drones
# -----------------------------------------------------------------------------
# • Reads the same CSV used by the Streamlit UI (warehouse must be first row).
# • Re‑runs the 3‑mile‑range routing algorithm to decide which stops each
#   agent services.
# • Generates Server‑Sent Events (SSE) that animate every vehicle along its
#   polyline at a fixed ground speed (truck 20 mph, drones 35 mph).
# • FastAPI + uvicorn ready for Render deployment.
# -----------------------------------------------------------------------------

from __future__ import annotations
import asyncio, json, math, os, time
from datetime import datetime
from typing import List

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import uvicorn

CSV_PATH = os.getenv("CSV_PATH", "sample_stops.csv")
TRUCK_SPEED_MPH = 20  # animation speed controls
DRONE_SPEED_MPH = 35
TICK_SEC = 1          # emit every second
RANGE_MILES = 3.0
NUM_DRONES = 4

# ----------------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------------
EARTH_MI = 3958.8

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
    """Linear in lat/lon – fine for small segments (< few km)."""
    lat = p0[0] + (p1[0] - p0[0]) * frac
    lon = p0[1] + (p1[1] - p0[1]) * frac
    return lat, lon

def heading_deg(p0, p1):
    dLon = math.radians(p1[1] - p0[1])
    lat1 = math.radians(p0[0])
    lat2 = math.radians(p1[0])
    y = math.sin(dLon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dLon)
    brng = math.degrees(math.atan2(y, x))
    return (brng + 360) % 360

# ----------------------------------------------------------------------------
# Load stops + plan routes (mirrors Streamlit logic)
# ----------------------------------------------------------------------------

def load_stops() -> List[dict]:
    df = pd.read_csv(CSV_PATH)
    df["lat"] = df["lat"].astype(float)
    df["lon"] = df["lon"].astype(float)
    return df.to_dict("records")


def plan_routes(stops: List[dict]):
    truck_indices = {0, len(stops) - 1}
    prev_cache, next_cache = {}, {}

    def prev_truck(idx):
        if idx in prev_cache:
            return prev_cache[idx]
        prev_cache[idx] = max(j for j in truck_indices if j < idx)
        return prev_cache[idx]

    def next_truck(idx):
        if idx in next_cache:
            return next_cache[idx]
        next_cache[idx] = min(j for j in truck_indices if j > idx)
        return next_cache[idx]

    drone_assign = []
    for cust_idx in range(1, len(stops) - 1):
        launch_idx, recov_idx = prev_truck(cust_idx), next_truck(cust_idx)
        l, c, r = stops[launch_idx], stops[cust_idx], stops[recov_idx]
        if (
            haversine_miles(l["lat"], l["lon"], c["lat"], c["lon"]) +
            haversine_miles(c["lat"], c["lon"], r["lat"], r["lon"])
        ) <= RANGE_MILES:
            drone_assign.append((cust_idx, launch_idx, recov_idx))
        else:
            truck_indices.add(cust_idx)

    truck_path = [[stops[i]["lat"], stops[i]["lon"]] for i in sorted(truck_indices)]

    bins = [[] for _ in range(NUM_DRONES)]
    for n, tri in enumerate(drone_assign):
        bins[n % NUM_DRONES].append(tri)

    drone_paths = []
    for b in bins:
        if not b:
            drone_paths.append([])
            continue
        poly = []
        for cust, launch, recov in b:
            l = [stops[launch]["lat"], stops[launch]["lon"]]
            c = [stops[cust]["lat"], stops[cust]["lon"]]
            r = [stops[recov]["lat"], stops[recov]["lon"]]
            if poly and poly[-1] == l:
                poly.extend([c, r])
            else:
                poly.extend([l, c, r])
        drone_paths.append(poly)
    return truck_path, drone_paths

# ----------------------------------------------------------------------------
# Build animation iterators
# ----------------------------------------------------------------------------

def build_animators(truck_path, drone_paths):
    vehicles = {}
    if truck_path:
        vehicles["truck-1"] = make_animator(truck_path, TRUCK_SPEED_MPH)
    for i, path in enumerate(drone_paths):
        if path:
            vehicles[f"drone-{i+1}"] = make_animator(path, DRONE_SPEED_MPH)
    return vehicles


def make_animator(polyline, speed_mph):
    # Pre‑compute segment lengths & cumulative distances
    seg_len = [
        haversine_miles(polyline[i][0], polyline[i][1], polyline[i+1][0], polyline[i+1][1])
        for i in range(len(polyline) - 1)
    ]
    seg_time = [l / speed_mph * 3600 for l in seg_len]  # seconds per segment

    async def animator():
        idx, t_in_seg = 0, 0.0
        while True:
            p0, p1 = polyline[idx], polyline[idx + 1]
            frac = t_in_seg / seg_time[idx] if seg_time[idx] > 0 else 1.0
            lat, lon = interpolate(p0, p1, frac)
            hdg = heading_deg(p0, p1)
            yield lat, lon, hdg
            await asyncio.sleep(TICK_SEC)
            t_in_seg += TICK_SEC
            if t_in_seg >= seg_time[idx]:
                t_in_seg = 0
                idx += 1
                if idx >= len(polyline) - 1:
                    idx = 0  # loop forever
    return animator()

# ----------------------------------------------------------------------------
# FastAPI SSE server
# ----------------------------------------------------------------------------

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

stops_data = load_stops()
truck_poly, drone_polys = plan_routes(stops_data)
ANIMATORS = build_animators(truck_poly, drone_polys)

async def sse_stream():
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
    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/")
async def root():
    return {"status": "ok", "msg": "Telemetry SSE – use /stream"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

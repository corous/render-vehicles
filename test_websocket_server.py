import asyncio
import json
import random
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn

app = FastAPI()

async def generate_vehicle_data():
    """Generate random vehicle telemetry data."""
    vehicle_id = random.choice(["truck-1", "drone-1", "drone-2", "drone-3", "drone-4"])
    return {
        "id": vehicle_id,
        "type": "truck" if "truck" in vehicle_id else "drone",
        "lat": 33.6846 + random.uniform(-0.01, 0.01),
        "lon": -117.8265 + random.uniform(-0.01, 0.01),
        "heading": random.uniform(0, 360),
        "timestamp": datetime.now().isoformat()
    }

async def event_generator():
    """Generate SSE events."""
    while True:
        data = generate_vehicle_data()
        yield f"data: {json.dumps(data)}\n\n"
        await asyncio.sleep(1)

@app.get("/stream")
async def stream_data(request: Request):
    """Stream vehicle data using Server-Sent Events."""
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 
import asyncio
import json
import random
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

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
    print("Starting event generator")  # Debug print
    while True:
        try:
            data = await generate_vehicle_data()
            # Format the SSE message properly
            message = f"data: {json.dumps(data)}\n\n"
            print(f"Sending: {message.strip()}")  # Debug print
            yield message
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Error in event generator: {e}")  # Debug print
            continue

@app.get("/stream")
async def stream_data(request: Request):
    """Stream vehicle data using Server-Sent Events."""
    print("New client connected to /stream")  # Debug print
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@app.get("/")
async def root():
    """Root endpoint for health check."""
    return {"status": "ok", "message": "Server is running. Use /stream for SSE endpoint."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 
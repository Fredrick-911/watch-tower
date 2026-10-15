"""
The ESCALATE hook for the tower's "cell brain".

When your detection / face-match code decides a situation is serious, call:

    from pi_cell.escalate import escalate
    await escalate(event="Face match / person of interest", track_id="trk_9f2a", confidence=0.87)

It does, in order:
  1. POST /trigger-emergency on the communications API
     -> the API creates the WebRTC room, sends WhatsApp + Africa's Talking call
  2. Starts streaming both cameras into that room until it expires.

Command-line test:
  python -m pi_cell.escalate --event "Test alert" --confidence 0.9 --test-camera
"""
import argparse
import asyncio
import logging
import os
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from .camera import FrameGrabber
from .publisher import Publisher, grabbers_from_env

load_dotenv()
log = logging.getLogger("escalate")

COMMS_API_URL = os.getenv("COMMS_API_URL", "http://localhost:8000").rstrip("/")
COMMS_API_KEY = os.getenv("COMMS_API_KEY", "change-me")
TOWER_ID = os.getenv("TOWER_ID", "WT-A3")
TOWER_NAME = os.getenv("TOWER_NAME", "")


async def trigger_emergency(event: str, track_id: Optional[str], confidence: Optional[float]) -> dict:
    payload = {"tower_id": TOWER_ID, "tower_name": TOWER_NAME, "event": event,
               "track_id": track_id, "confidence": confidence}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{COMMS_API_URL}/trigger-emergency", json=payload,
                          headers={"X-API-Key": COMMS_API_KEY}, timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            return await r.json()


async def escalate(event: str, track_id: Optional[str] = None, confidence: Optional[float] = None,
                   grabbers: Optional[list[FrameGrabber]] = None, max_seconds: int = 1800) -> None:
    result = await trigger_emergency(event, track_id, confidence)
    log.info("alert sent: room=%s whatsapp=%s voice=%s", result["room_id"],
             result["notifications"].get("whatsapp"), result["notifications"].get("voice"))
    grabbers = grabbers or grabbers_from_env()
    await Publisher(result["signaling_url"], result["room_id"], result["publisher_token"],
                    grabbers, max_seconds).run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Raise an alert and stream this tower's cameras")
    p.add_argument("--event", default="Manual test alert")
    p.add_argument("--track-id")
    p.add_argument("--confidence", type=float)
    p.add_argument("--test-camera", action="store_true")
    p.add_argument("--max-seconds", type=int, default=1800)
    a = p.parse_args()
    asyncio.run(escalate(a.event, a.track_id, a.confidence, grabbers_from_env(a.test_camera), a.max_seconds))


if __name__ == "__main__":
    main()

"""
WebRTC publisher that runs on the tower's Raspberry Pi.

It joins a room on the signaling server as the "publisher". Every time a
call-centre viewer opens the link, it builds a peer connection for that
viewer, attaches both camera tracks, and sends an offer.

Manual run (for testing):
  python -m pi_cell.publisher --room rm_xxx --token PUBLISHER_TOKEN --test-camera
"""
import argparse
import asyncio
import logging
import os
import time

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from dotenv import load_dotenv

from .camera import FrameGrabber, GrabberTrack

load_dotenv()
log = logging.getLogger("publisher")


def grabbers_from_env(test_camera: bool = False) -> list[FrameGrabber]:
    """CAMERA_URLS=http://192.168.4.11:81/stream,http://192.168.4.12:81/stream"""
    width = int(os.getenv("STREAM_WIDTH", "640"))
    height = int(os.getenv("STREAM_HEIGHT", "480"))
    fps = int(os.getenv("STREAM_FPS", "12"))
    urls = ["test", "test"] if test_camera else [u.strip() for u in os.getenv("CAMERA_URLS", "").split(",") if u.strip()]
    if not urls:
        raise SystemExit("Set CAMERA_URLS in .env or use --test-camera")
    return [FrameGrabber(u, f"CAM-{i + 1}", width, height, fps).start() for i, u in enumerate(urls)]


class Publisher:
    def __init__(self, signaling_url: str, room_id: str, token: str, grabbers: list[FrameGrabber],
                 max_seconds: int = 1800):
        self.base = signaling_url.rstrip("/")
        self.room_id, self.token = room_id, token
        self.grabbers = grabbers
        self.deadline = time.time() + max_seconds
        self.pcs: dict[str, RTCPeerConnection] = {}
        self.ice: list[RTCIceServer] = []
        self.ws = None

    async def run(self) -> None:
        """Stay connected (reconnecting if needed) until the room expires."""
        async with aiohttp.ClientSession() as session:
            while time.time() < self.deadline:
                try:
                    await self._session(session)
                except _RoomGone:
                    log.info("room closed or expired, stopping publisher")
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("signaling connection problem: %s; retrying in 3s", exc)
                await self._close_all()
                await asyncio.sleep(3)
        await self._close_all()

    async def _session(self, session: aiohttp.ClientSession) -> None:
        async with session.get(f"{self.base}/ice-config", params={"room": self.room_id, "token": self.token}) as r:
            if r.status in (403, 404):
                raise _RoomGone()
            r.raise_for_status()
            cfg = await r.json()
        self.ice = [RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
                    for s in cfg["iceServers"]]
        ws_url = self.base.replace("https://", "wss://").replace("http://", "ws://")
        ws_url += f"/ws/{self.room_id}?role=publisher&token={self.token}"
        remaining = max(1, self.deadline - time.time())
        async with session.ws_connect(ws_url, heartbeat=20) as ws:
            self.ws = ws
            log.info("publisher connected to room %s", self.room_id)
            async with asyncio.timeout(remaining):
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    await self._handle(msg.json())
            if ws.close_code in (4403, 4409, 4410):
                raise _RoomGone()

    async def _handle(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "viewer-joined":
            await self._offer_to(data["viewer_id"])
        elif kind == "answer":
            pc = self.pcs.get(data.get("from", ""))
            if pc is not None:
                await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type="answer"))
        elif kind == "viewer-left":
            await self._close(data["viewer_id"])

    async def _offer_to(self, viewer_id: str) -> None:
        await self._close(viewer_id)
        pc = RTCPeerConnection(RTCConfiguration(iceServers=self.ice))
        self.pcs[viewer_id] = pc
        for g in self.grabbers:
            pc.addTransceiver(GrabberTrack(g), direction="sendonly")

        @pc.on("connectionstatechange")
        async def _state():
            log.info("viewer %s: %s", viewer_id, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self._close(viewer_id)

        await pc.setLocalDescription(await pc.createOffer())  # aiortc gathers ICE here
        await self.ws.send_json({"type": "offer", "to": viewer_id, "sdp": pc.localDescription.sdp})
        log.info("offer sent to viewer %s", viewer_id)

    async def _close(self, viewer_id: str) -> None:
        pc = self.pcs.pop(viewer_id, None)
        if pc is not None:
            await pc.close()

    async def _close_all(self) -> None:
        for vid in list(self.pcs):
            await self._close(vid)


class _RoomGone(Exception):
    pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Publish tower cameras into a WebRTC room")
    p.add_argument("--room", required=True)
    p.add_argument("--token", required=True, help="publisher_token for the room")
    p.add_argument("--signaling-url", default=os.getenv("SIGNALING_URL", "http://localhost:8080"))
    p.add_argument("--test-camera", action="store_true", help="use moving test patterns instead of ESP32-CAMs")
    p.add_argument("--max-seconds", type=int, default=1800)
    a = p.parse_args()
    grabbers = grabbers_from_env(a.test_camera)
    asyncio.run(Publisher(a.signaling_url, a.room, a.token, grabbers, a.max_seconds).run())


if __name__ == "__main__":
    main()

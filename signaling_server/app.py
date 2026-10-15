"""
Watchtower WebRTC signaling server.

What it does (in plain words):
  * Creates short-lived "rooms" - one room per escalated incident.
  * Gives each room two secret tokens:
        publisher_token -> only the tower's Raspberry Pi may use it
        viewer_token    -> goes inside the WhatsApp link for the call centre
  * Relays WebRTC "offer"/"answer" messages between the Pi and the viewers
    over WebSockets. Video itself does NOT pass through this server - it goes
    directly peer-to-peer, or through the TURN server when networks block that.
  * Hands out STUN/TURN settings (with short-lived TURN passwords).
  * Serves the viewer web page at /viewer.

Run:  uvicorn signaling_server.app:app --host 0.0.0.0 --port 8080
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()
log = logging.getLogger("signaling")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------- settings
API_KEY = os.getenv("SIGNALING_API_KEY", "change-me")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
STUN_URLS = [u.strip() for u in os.getenv("STUN_URLS", "stun:stun.l.google.com:19302").split(",") if u.strip()]
TURN_URLS = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
TURN_SECRET = os.getenv("TURN_SECRET", "")
ROOM_TTL_SECONDS = int(os.getenv("ROOM_TTL_SECONDS", "1800"))
MAX_VIEWERS_PER_ROOM = int(os.getenv("MAX_VIEWERS_PER_ROOM", "3"))  # Pi encodes once per viewer
STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------- room state
@dataclass
class Room:
    room_id: str
    tower_id: str
    track_id: Optional[str]
    event: Optional[str]
    publisher_token: str
    viewer_token: str
    expires_at: float
    publisher: Optional[WebSocket] = None
    viewers: dict = field(default_factory=dict)  # viewer_id -> WebSocket

    def expired(self) -> bool:
        return time.time() > self.expires_at


rooms: dict[str, Room] = {}


async def safe_send(ws: Optional[WebSocket], message: dict) -> None:
    """Send JSON without crashing if the other side already went away."""
    if ws is None:
        return
    try:
        await ws.send_json(message)
    except Exception:  # noqa: BLE001
        pass


async def cleanup_loop() -> None:
    """Every 30 s, delete rooms whose time is up and close their sockets."""
    while True:
        await asyncio.sleep(30)
        for room_id in [r for r, room in rooms.items() if room.expired()]:
            room = rooms.pop(room_id)
            log.info("room %s expired", room_id)
            for ws in [room.publisher, *room.viewers.values()]:
                if ws is not None:
                    try:
                        await ws.close(code=4410)
                    except Exception:  # noqa: BLE001
                        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Watchtower WebRTC signaling", lifespan=lifespan)


# ---------------------------------------------------------------- REST API
class CreateRoomRequest(BaseModel):
    tower_id: str
    track_id: Optional[str] = None
    event: Optional[str] = None
    ttl_seconds: Optional[int] = None


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(rooms)}


@app.post("/rooms")
def create_room(body: CreateRoomRequest, x_api_key: str = Header(default="")):
    """Called by the communications API when a tower escalates."""
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="bad API key")
    room_id = "rm_" + secrets.token_hex(6)
    ttl = min(body.ttl_seconds or ROOM_TTL_SECONDS, 6 * 3600)
    room = Room(
        room_id=room_id,
        tower_id=body.tower_id,
        track_id=body.track_id,
        event=body.event,
        publisher_token=secrets.token_urlsafe(24),
        viewer_token=secrets.token_urlsafe(24),
        expires_at=time.time() + ttl,
    )
    rooms[room_id] = room
    log.info("room %s created for tower %s (ttl %ss)", room_id, body.tower_id, ttl)
    return {
        "room_id": room_id,
        "publisher_token": room.publisher_token,
        "viewer_url": f"{PUBLIC_BASE_URL}/viewer?room={room_id}&token={room.viewer_token}",
        "expires_at": int(room.expires_at),
    }


@app.get("/rooms/{room_id}")
def room_status(room_id: str, x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="bad API key")
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="no such room")
    return {
        "room_id": room_id,
        "tower_id": room.tower_id,
        "publisher_online": room.publisher is not None,
        "viewers": len(room.viewers),
        "expires_at": int(room.expires_at),
    }


def _turn_credentials(ttl: int = 3600) -> tuple[str, str]:
    """coturn 'use-auth-secret' style temporary username/password."""
    username = f"{int(time.time()) + ttl}:watchtower"
    digest = hmac.new(TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


def ice_servers() -> list[dict]:
    servers: list[dict] = []
    if STUN_URLS:
        servers.append({"urls": STUN_URLS})
    if TURN_URLS and TURN_SECRET:
        user, cred = _turn_credentials()
        servers.append({"urls": TURN_URLS, "username": user, "credential": cred})
    return servers


def _room_for_token(room_id: str, token: str) -> Room:
    room = rooms.get(room_id)
    if not room or room.expired():
        raise HTTPException(status_code=404, detail="room not found or expired")
    if not (hmac.compare_digest(token, room.viewer_token) or hmac.compare_digest(token, room.publisher_token)):
        raise HTTPException(status_code=403, detail="bad token")
    return room


@app.get("/ice-config")
def ice_config(room: str, token: str):
    r = _room_for_token(room, token)
    return {"iceServers": ice_servers(), "tower_id": r.tower_id, "track_id": r.track_id, "event": r.event}


@app.get("/viewer")
def viewer_page():
    return FileResponse(STATIC_DIR / "viewer.html", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- WebSocket relay
@app.websocket("/ws/{room_id}")
async def signaling_ws(websocket: WebSocket, room_id: str, role: str = Query(...), token: str = Query(...)):
    room = rooms.get(room_id)
    expected = None
    if room and not room.expired():
        expected = room.publisher_token if role == "publisher" else room.viewer_token if role == "viewer" else None
    if expected is None or not hmac.compare_digest(token, expected):
        await websocket.close(code=4403)
        return
    if role == "viewer" and len(room.viewers) >= MAX_VIEWERS_PER_ROOM:
        await websocket.close(code=4429)
        return

    await websocket.accept()
    if role == "publisher":
        await _publisher_session(room, websocket)
    else:
        await _viewer_session(room, websocket)


async def _publisher_session(room: Room, ws: WebSocket) -> None:
    if room.publisher is not None:  # a reconnecting Pi replaces the old socket
        try:
            await room.publisher.close(code=4409)
        except Exception:  # noqa: BLE001
            pass
    room.publisher = ws
    log.info("publisher joined %s", room.room_id)
    await safe_send(ws, {"type": "welcome", "room_id": room.room_id, "expires_at": int(room.expires_at)})
    for viewer_id, vws in room.viewers.items():  # viewers who were already waiting
        await safe_send(ws, {"type": "viewer-joined", "viewer_id": viewer_id})
        await safe_send(vws, {"type": "publisher-joined"})
    try:
        while True:
            msg = await ws.receive_json()
            target = room.viewers.get(msg.get("to", ""))
            if msg.get("type") in ("offer", "bye") and target is not None:
                await safe_send(target, {"type": msg["type"], "sdp": msg.get("sdp"), "from": "publisher"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if room.publisher is ws:
            room.publisher = None
            for vws in room.viewers.values():
                await safe_send(vws, {"type": "publisher-left"})
        log.info("publisher left %s", room.room_id)


async def _viewer_session(room: Room, ws: WebSocket) -> None:
    viewer_id = "v_" + secrets.token_hex(4)
    room.viewers[viewer_id] = ws
    log.info("viewer %s joined %s", viewer_id, room.room_id)
    await safe_send(ws, {
        "type": "welcome",
        "viewer_id": viewer_id,
        "tower_id": room.tower_id,
        "publisher_online": room.publisher is not None,
    })
    await safe_send(room.publisher, {"type": "viewer-joined", "viewer_id": viewer_id})
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "answer":
                await safe_send(room.publisher, {"type": "answer", "sdp": msg.get("sdp"), "from": viewer_id})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.viewers.pop(viewer_id, None)
        await safe_send(room.publisher, {"type": "viewer-left", "viewer_id": viewer_id})
        log.info("viewer %s left %s", viewer_id, room.room_id)

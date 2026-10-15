"""
Communications API ("fastapi_backend") - the ESCALATE endpoint.

POST /trigger-emergency   (called by a tower's Pi)
   1. asks the signaling server for a new WebRTC room
   2. sends the WhatsApp alert (Twilio) with the live-view link
   3. rings the call-centre phones (Africa's Talking voice)
   4. returns room_id + publisher_token so the Pi can start streaming

POST /at/voice-callback  (called by Africa's Talking when an operator answers)
   returns the XML that tells Africa's Talking what to say.

Set DRY_RUN=true to test everything without Twilio/Africa's Talking accounts:
messages are printed to the log instead of being sent.

Run:  uvicorn comms_api.app:app --host 0.0.0.0 --port 8000
"""
import hmac
import logging
import os
import time
from typing import Optional
from xml.sax.saxutils import escape

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

load_dotenv()
log = logging.getLogger("comms")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

COMMS_API_KEY = os.getenv("COMMS_API_KEY", "change-me")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# signaling server: internal address used by this API, and the public one given to the Pi
SIGNALING_INTERNAL_URL = os.getenv("SIGNALING_INTERNAL_URL", "http://localhost:8080").rstrip("/")
SIGNALING_URL_FOR_PI = os.getenv("SIGNALING_URL", SIGNALING_INTERNAL_URL).rstrip("/")
SIGNALING_API_KEY = os.getenv("SIGNALING_API_KEY", "change-me")

# Twilio WhatsApp
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
CALL_CENTRE_WHATSAPP = [n.strip() for n in os.getenv("CALL_CENTRE_WHATSAPP", "").split(",") if n.strip()]

# Africa's Talking voice
AT_USERNAME = os.getenv("AT_USERNAME", "")
AT_API_KEY = os.getenv("AT_API_KEY", "")
AT_VOICE_NUMBER = os.getenv("AT_VOICE_NUMBER", "")  # your AT voice number, e.g. +254711082XXX
AT_VOICE_URL = os.getenv("AT_VOICE_URL", "https://voice.africastalking.com/call")
CALL_CENTRE_PHONES = [n.strip() for n in os.getenv("CALL_CENTRE_PHONES", "").split(",") if n.strip()]

app = FastAPI(title="Watchtower communications API")

# What the voice call should say. Africa's Talking calls our callback when the
# operator picks up; we answer with the most recent alert's text.
latest_voice_text = {"text": "Security alert from the watchtower network. Open WhatsApp for live video."}


class EmergencyRequest(BaseModel):
    tower_id: str
    tower_name: Optional[str] = ""
    event: str
    track_id: Optional[str] = None
    confidence: Optional[float] = None


def build_whatsapp_text(req: EmergencyRequest, viewer_url: str) -> str:
    tower = f"{req.tower_id} ({req.tower_name})" if req.tower_name else req.tower_id
    lines = ["ALERT – Watchtower network", f"Tower: {tower}", f"Event: {req.event}"]
    if req.track_id:
        lines.append(f"Track ID: {req.track_id}")
    if req.confidence is not None:
        lines.append(f"Confidence: {req.confidence:.2f}")
    lines += [f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}", f"Live view: {viewer_url}"]
    return "\n".join(lines)


async def create_room(client: httpx.AsyncClient, req: EmergencyRequest) -> dict:
    r = await client.post(f"{SIGNALING_INTERNAL_URL}/rooms", headers={"X-API-Key": SIGNALING_API_KEY},
                          json={"tower_id": req.tower_id, "track_id": req.track_id, "event": req.event})
    r.raise_for_status()
    return r.json()


async def send_whatsapp(client: httpx.AsyncClient, text: str) -> str:
    if DRY_RUN or not (TWILIO_ACCOUNT_SID and CALL_CENTRE_WHATSAPP):
        log.info("[DRY RUN] WhatsApp to %s:\n%s", CALL_CENTRE_WHATSAPP or "(no numbers set)", text)
        return "dry-run"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    sent = 0
    for number in CALL_CENTRE_WHATSAPP:
        to = number if number.startswith("whatsapp:") else f"whatsapp:{number}"
        r = await client.post(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                              data={"From": TWILIO_WHATSAPP_FROM, "To": to, "Body": text})
        if r.status_code >= 300:
            log.error("Twilio error for %s: %s %s", to, r.status_code, r.text)
        else:
            sent += 1
    return f"sent {sent}/{len(CALL_CENTRE_WHATSAPP)}"


async def call_operators(client: httpx.AsyncClient, req: EmergencyRequest) -> str:
    latest_voice_text["text"] = (f"Security alert. Tower {req.tower_id}. {req.event}. "
                                 "Open WhatsApp now for the live video.")
    if DRY_RUN or not (AT_API_KEY and AT_VOICE_NUMBER and CALL_CENTRE_PHONES):
        log.info("[DRY RUN] Voice call to %s: %s", CALL_CENTRE_PHONES or "(no numbers set)", latest_voice_text["text"])
        return "dry-run"
    r = await client.post(AT_VOICE_URL, headers={"apiKey": AT_API_KEY, "Accept": "application/json"},
                          data={"username": AT_USERNAME, "from": AT_VOICE_NUMBER, "to": ",".join(CALL_CENTRE_PHONES)})
    if r.status_code >= 300:
        log.error("Africa's Talking error: %s %s", r.status_code, r.text)
        return f"error {r.status_code}"
    body = r.json()
    if body.get("errorMessage") not in (None, "None"):
        log.error("Africa's Talking: %s", body["errorMessage"])
    return ", ".join(f"{e.get('phoneNumber')}:{e.get('status')}" for e in body.get("entries", []))


@app.post("/trigger-emergency")
async def trigger_emergency(req: EmergencyRequest, x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key, COMMS_API_KEY):
        raise HTTPException(status_code=401, detail="bad API key")
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            room = await create_room(client, req)
        except httpx.HTTPError as exc:
            log.error("could not create WebRTC room: %s", exc)
            raise HTTPException(status_code=502, detail="signaling server unreachable")
        text = build_whatsapp_text(req, room["viewer_url"])
        # Notification failures must never stop the Pi from streaming, so we catch them.
        try:
            whatsapp = await send_whatsapp(client, text)
        except httpx.HTTPError as exc:
            whatsapp = f"error: {exc}"
        try:
            voice = await call_operators(client, req)
        except httpx.HTTPError as exc:
            voice = f"error: {exc}"
    return {
        "room_id": room["room_id"],
        "publisher_token": room["publisher_token"],
        "signaling_url": SIGNALING_URL_FOR_PI,
        "viewer_url": room["viewer_url"],
        "notifications": {"whatsapp": whatsapp, "voice": voice},
    }


@app.post("/at/voice-callback")
async def at_voice_callback(request: Request):
    """Set this URL as the callback for your voice number in the Africa's Talking dashboard."""
    form = await request.form()
    log.info("AT voice callback: %s", dict(form))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<Response><Say>{escape(latest_voice_text["text"])}</Say>'
           f'<Say>{escape(latest_voice_text["text"])}</Say></Response>')
    return Response(content=xml, media_type="application/xml")


@app.get("/health")
def health():
    return {"ok": True, "dry_run": DRY_RUN}

# Watchtower WebRTC – live video from a tower to the call centre

This project is the "live view" part of the rural watchtower network. When a tower's
Raspberry Pi decides something is serious (**ESCALATE**), this code:

1. creates a private video "room" for that incident,
2. sends a WhatsApp alert with a link to the room, and rings the operators with Africa's Talking,
3. streams **both ESP32-CAMs** from the Pi into the room,
4. lets the operator tap the WhatsApp link and watch live in any phone or laptop browser – no app to install.

---

## 1. The big picture (read this first)

```
 ESP32-CAM #1 ─┐  MJPEG over Wi-Fi                      ┌──────────── Call centre phone/laptop
 ESP32-CAM #2 ─┤                                        │   opens WhatsApp link → /viewer page
               ▼                                        │
      Raspberry Pi (tower)                              │
      pi_cell/escalate.py ──(1) POST /trigger-emergency──► comms_api  (port 8000)
                                                        │     ├─(2) POST /rooms ──► signaling_server (port 8080)
                                                        │     ├─(3) Twilio WhatsApp  (link)
                                                        │     └─(4) Africa's Talking voice call
      pi_cell/publisher.py ──(5) WebSocket "publisher"──► signaling_server ◄── WebSocket "viewer"
                │                                                         (only small text messages)
                └══════════(6) VIDEO, peer-to-peer or via TURN relay══════════► viewer
```

Three words you will see a lot:

| Word | What it means here |
|---|---|
| **Signaling server** | A small "matchmaker". The Pi and the viewer tell it who they are; it passes setup messages ("offer"/"answer") between them. Video does **not** go through it. |
| **STUN** | A free public service that tells a device its public internet address. Enough on simple networks. |
| **TURN** | A relay server that carries the video when the two sides can't reach each other directly (very common when the Pi is on Airtel/Safaricom 4G). **You need one for real field use.** |

> **Important about ngrok:** ngrok can carry the signaling server (the web page and
> WebSocket), but it **cannot carry the WebRTC video**. Video goes directly, or through
> TURN. That's why section 7 sets up a TURN server.

### What each folder is

| Folder / file | Runs on | Job |
|---|---|---|
| `signaling_server/app.py` | Server/laptop | Rooms, secret tokens, TURN passwords, WebSocket relay, serves the viewer page |
| `signaling_server/static/viewer.html` | Operator's browser | Shows the live video from both cameras |
| `comms_api/app.py` | Server/laptop | `/trigger-emergency`: creates the room, sends WhatsApp + voice call |
| `pi_cell/camera.py` | Pi | Reads the ESP32-CAM streams (or a test pattern) |
| `pi_cell/publisher.py` | Pi | Sends the camera video into the room |
| `pi_cell/escalate.py` | Pi | The one function your detection code calls: `escalate(...)` |
| `turn/turnserver.conf` | Cloud VPS | TURN relay settings (coturn) |
| `tests/smoke_test.py` | Anywhere | Full test with fake cameras – no hardware needed |

---

## 2. Step 1 – Test everything on one laptop (no hardware, no accounts)

You need **Python 3.11 or newer**. Check with `python3 --version`.

1. Open a terminal and go into the project folder:
   ```bash
   cd watchtower-webrtc
   ```
2. Make a virtual environment (a private box for this project's Python packages) and switch it on:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   ```
   Your prompt now starts with `(.venv)`. Do this "activate" line every time you open a new terminal.
3. Install the packages:
   ```bash
   pip install -r requirements-server.txt -r requirements-pi.txt
   ```
4. Create your settings file:
   ```bash
   cp .env.example .env
   ```
   Leave it as it is for now (`DRY_RUN=true` means alerts are printed, not sent).
5. **Terminal 1** – start the signaling server:
   ```bash
   uvicorn signaling_server.app:app --host 0.0.0.0 --port 8080
   ```
6. **Terminal 2** (activate the venv again) – start the communications API:
   ```bash
   uvicorn comms_api.app:app --host 0.0.0.0 --port 8000
   ```
7. **Terminal 3** – run the automatic test:
   ```bash
   python -m tests.smoke_test
   ```
   You should see `frames received per camera track: [20, 20]` and `PASS`.
8. Now watch it yourself. In Terminal 3 run:
   ```bash
   python -m pi_cell.escalate --event "Test alert" --confidence 0.9 --test-camera
   ```
   Look in **Terminal 2**: it prints the WhatsApp text with a `Live view:` link.
   Open that link in your browser. You'll see two moving test patterns and a green **Live** badge.
   Press `Ctrl+C` in Terminal 3 to stop.

If that works, the whole software chain works. Everything after this is swapping fake parts for real ones.

---

## 3. Step 2 – Make the ESP32-CAMs stream

1. In Arduino IDE, install the **esp32** boards package (Boards Manager → "esp32 by Espressif").
2. Open **File → Examples → ESP32 → Camera → CameraWebServer**.
3. In the sketch: pick `#define CAMERA_MODEL_AI_THINKER`, and type your Wi-Fi name and password.
4. Upload, open Serial Monitor at 115200 baud, press the reset button. It prints an IP like `192.168.4.11`.
5. Test in a browser: `http://192.168.4.11:81/stream` should show live video.
6. Repeat for camera #2.
7. In your router, give each camera a **fixed IP** (DHCP reservation) so the address never changes.

> Your TinyML person/face detection firmware can keep this `/stream` endpoint – the Pi
> only needs it when it is actually streaming to the call centre.

---

## 4. Step 3 – Set up the Raspberry Pi

Use **Raspberry Pi OS 64-bit (Bookworm)** – it ships Python 3.11. A Pi 4 or Pi 5 is recommended;
the Pi encodes video in software for each viewer.

1. Copy the project to the Pi (e.g. `scp -r watchtower-webrtc pi@<pi-ip>:~/` or `git clone`).
2. On the Pi:
   ```bash
   sudo apt update
   sudo apt install -y python3-venv libavdevice-dev libavfilter-dev libopus-dev libvpx-dev pkg-config libsrtp2-dev
   cd ~/watchtower-webrtc
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements-pi.txt
   cp .env.example .env
   nano .env
   ```
3. In `.env` on the Pi, set at least:
   ```
   TOWER_ID=WT-A3
   TOWER_NAME=Lokiriama road
   COMMS_API_URL=http://<laptop-ip>:8000
   COMMS_API_KEY=<same value as on the server>
   CAMERA_URLS=http://192.168.4.11:81/stream,http://192.168.4.12:81/stream
   ```
4. Test with the real cameras (servers from Step 1 must be running on the laptop, and the laptop's `.env` must have `SIGNALING_URL=http://<laptop-ip>:8080` and `PUBLIC_BASE_URL=http://<laptop-ip>:8080`):
   ```bash
   python -m pi_cell.escalate --event "Pi camera test"
   ```
   Open the printed link on your phone (same Wi-Fi). You should see both real cameras.

**If the video is choppy:** lower `STREAM_FPS` to 8–10 or `STREAM_WIDTH/HEIGHT` to `480/360`.
Keep `MAX_VIEWERS_PER_ROOM` small (2–3): every extra viewer is another encode on the Pi.

---

## 5. Step 4 – Connect it to your detection code (the cell brain)

Your Pi program that does person/face matching only needs one line when it decides to escalate:

```python
import asyncio
from pi_cell.escalate import escalate
from pi_cell.publisher import grabbers_from_env

grabbers = grabbers_from_env()        # start once; your detector can read grabbers[i].latest() too

async def on_face_match(track_id: str, score: float):
    # don't await it if your detector must keep running - run it as a background task
    asyncio.create_task(escalate(event="Face match / person of interest",
                                 track_id=track_id, confidence=score, grabbers=grabbers))
```

Tips:
- Sharing the same `grabbers` between detection and streaming means each ESP32-CAM has **one** connection (ESP32-CAMs handle only one stream client well).
- Add a cool-down (e.g. no new escalation for the same `track_id` within 5 minutes) so one person doesn't trigger ten calls.
- To run it automatically at boot, use `deploy/watchtower-cell.service` (instructions inside the file).

---

## 6. Step 5 – Make it reachable from anywhere with ngrok

1. Create a free account at ngrok.com, install ngrok, and run its `ngrok config add-authtoken ...` command once.
2. On the server/laptop:
   ```bash
   ngrok http 8080
   ```
   Copy the `https://....ngrok-free.app` address.
3. In the **server's** `.env`:
   ```
   PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
   SIGNALING_URL=https://xxxx.ngrok-free.app
   ```
   Restart both uvicorn commands.
4. The Pi must also reach the comms API. Either run a second tunnel for port 8000, or (simpler) put the
   comms API behind the same machine and use a VPN such as Tailscale between Pi and server, setting
   `COMMS_API_URL=http://<tailscale-ip>:8000` on the Pi.

> Free ngrok addresses change every restart – for the field, use a reserved ngrok domain or a VPS with your own domain.

---

## 7. Step 6 – TURN relay (needed for 4G towers)

Without TURN, video often fails with the red badge **"Video blocked by network (check TURN)"**,
especially when the Pi is on mobile data.

1. Rent a small Linux VPS with a public IP (1 vCPU is enough to start).
2. Install coturn:
   ```bash
   sudo apt install -y coturn
   ```
3. Copy `turn/turnserver.conf` to `/etc/turnserver.conf`. Change `static-auth-secret` (make up a long random string) and `external-ip` (the VPS's public IP).
4. Enable and start it:
   ```bash
   sudo sed -i 's/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn
   sudo systemctl enable --now coturn
   ```
5. Open the firewall: UDP+TCP **3478** and UDP **49160–49200**.
6. In the **signaling server's** `.env`:
   ```
   TURN_URLS=turn:<VPS-IP>:3478?transport=udp,turn:<VPS-IP>:3478?transport=tcp
   TURN_SECRET=<the same string as static-auth-secret>
   ```
   Restart the signaling server. It now hands out passwords that expire after one hour – no permanent TURN password is ever sent to a phone.
7. Check: open https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/ , add your TURN URL with a username/password taken from `GET /ice-config` of a live room, and click "Gather candidates". A line with type **relay** means TURN works.

---

## 8. Step 7 – Turn on real WhatsApp and voice alerts

**WhatsApp (Twilio sandbox):**
1. Create a Twilio account → Messaging → Try it out → **Send a WhatsApp message**.
2. From each operator's phone, send the shown "join ..." code to the sandbox number.
3. Put `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `CALL_CENTRE_WHATSAPP` (operators' numbers, `+2547...`) in the server's `.env`.

**Voice (Africa's Talking):**
1. Create an Africa's Talking account and an app; copy the **username** and **API key**.
2. Get a **voice number** in the dashboard (Voice → Phone numbers). Outbound calls need this number and a funded account – check the dashboard for current requirements.
3. Set that number's **callback URL** to `https://<your-public-comms-address>/at/voice-callback`. This is what tells the call what to say when the operator picks up.
4. Put `AT_USERNAME`, `AT_API_KEY`, `AT_VOICE_NUMBER`, `CALL_CENTRE_PHONES` in `.env`.

Finally set `DRY_RUN=false` and restart the comms API. Run `python -m pi_cell.escalate --event "Live test" --test-camera` – phones should get the WhatsApp message and ring.

---

## 9. If you already have `webrtc_server` and `fastapi_backend`

- **Communications API:** copy the functions `create_room`, `send_whatsapp`, `call_operators`, and the two routes (`/trigger-emergency`, `/at/voice-callback`) into your existing FastAPI app. Keep the response fields the same (`room_id`, `publisher_token`, `signaling_url`) because `pi_cell/escalate.py` reads them.
- **WebRTC server:** if your existing one uses a different message format, either replace it with `signaling_server/` or change the message names in `publisher.py` and `viewer.html` to match. Message types used here: `welcome`, `viewer-joined`, `viewer-left`, `publisher-joined`, `publisher-left`, `offer`, `answer`.

---

## 10. How the security works (short version)

- Creating a room needs `SIGNALING_API_KEY`; triggering an emergency needs `COMMS_API_KEY`.
- Each room has two random tokens. The WhatsApp link only contains the **viewer** token, which can watch but never publish.
- Rooms expire after `ROOM_TTL_SECONDS` (30 min by default); old links stop working.
- Video is encrypted end-to-end by WebRTC (DTLS-SRTP), including when it passes through TURN.
- Nothing is recorded by this code. If you add recording, set retention rules first (Kenya Data Protection Act, partner MoUs).

---

## 11. Troubleshooting

| You see | Likely cause | Fix |
|---|---|---|
| "This alert link has expired" | Room older than TTL, or server restarted (rooms live in memory) | Trigger a new alert |
| Stuck on "Waiting for tower…" | Pi publisher not running or can't reach signaling | Check the Pi log; check `SIGNALING_URL` |
| "Video blocked by network (check TURN)" | Direct connection impossible | Set up TURN (Step 6) |
| Black box with "CAM-1: no signal" | Pi can't read the ESP32-CAM | Open the camera URL in a browser on the Pi's network; check `CAMERA_URLS` |
| "Tap to play" button | Phone blocked autoplay | Tap it |
| `401 bad API key` | Keys differ between `.env` files | Copy the same values to every machine |

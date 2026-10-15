"""
End-to-end test with simulated cameras. Start both servers first, then:
    python -m tests.smoke_test

It plays the Pi (escalate + publish test patterns) AND a call-centre viewer,
and passes when the viewer receives video frames from both cameras.
"""
import asyncio
import sys
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from pi_cell.escalate import trigger_emergency
from pi_cell.publisher import Publisher, grabbers_from_env


async def run_viewer(viewer_url: str, need_frames: int = 20) -> dict:
    u = urlparse(viewer_url)
    q = parse_qs(u.query)
    room, token = q["room"][0], q["token"][0]
    base = f"{u.scheme}://{u.netloc}"
    counts: dict[str, int] = {}
    done = asyncio.Event()
    async with aiohttp.ClientSession() as s:
        cfg = await (await s.get(f"{base}/ice-config", params={"room": room, "token": token})).json()
        ice = [RTCIceServer(urls=x["urls"], username=x.get("username"), credential=x.get("credential"))
               for x in cfg["iceServers"]]
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))

        @pc.on("track")
        def on_track(track):
            async def pull():
                while True:
                    try:
                        await track.recv()
                    except Exception:  # noqa: BLE001 - track ended when we hang up
                        return
                    counts[track.id] = counts.get(track.id, 0) + 1
                    if len(counts) >= 2 and all(c >= need_frames for c in counts.values()):
                        done.set()
            asyncio.ensure_future(pull())

        ws_url = base.replace("http", "ws") + f"/ws/{room}?role=viewer&token={token}"
        async with s.ws_connect(ws_url) as ws:
            async for msg in ws:
                data = msg.json()
                if data["type"] == "offer":
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type="offer"))
                    await pc.setLocalDescription(await pc.createAnswer())
                    await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})
                    await asyncio.wait_for(done.wait(), timeout=30)
                    break
        await pc.close()
    return counts


async def main() -> int:
    result = await trigger_emergency("Smoke test alert", "trk_test", 0.9)
    print("trigger-emergency ->", {k: v for k, v in result.items() if k != "publisher_token"})
    pub = Publisher(result["signaling_url"], result["room_id"], result["publisher_token"],
                    grabbers_from_env(test_camera=True), max_seconds=60)
    pub_task = asyncio.create_task(pub.run())
    await asyncio.sleep(1)
    counts = await run_viewer(result["viewer_url"])
    pub_task.cancel()
    print("frames received per camera track:", list(counts.values()))
    ok = len(counts) == 2 and all(c >= 20 for c in counts.values())
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

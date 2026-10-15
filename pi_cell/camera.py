"""
Camera input for the tower's Raspberry Pi.

FrameGrabber  - keeps ONE connection to an ESP32-CAM MJPEG stream (or a test
                pattern) and always holds the newest frame. Your detection code
                (person/face) can read from the same grabber.
GrabberTrack  - turns a grabber into a WebRTC video track. Each viewer gets
                its own track object, all sharing the same grabber.
"""
import asyncio
import fractions
import logging
import threading
import time
from typing import Optional

import av
import cv2
import numpy as np
from aiortc import VideoStreamTrack

log = logging.getLogger("camera")
VIDEO_CLOCK_RATE = 90000
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)


class FrameGrabber:
    def __init__(self, source: str, name: str, width: int = 640, height: int = 480, fps: int = 12):
        self.source, self.name = source, name
        self.width, self.height, self.fps = width, height, fps
        self._frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"grab-{name}", daemon=True)
        self.connected = False

    def start(self) -> "FrameGrabber":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def _store(self, frame: np.ndarray) -> None:
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height))
        with self._lock:
            self._frame = frame

    def _run(self) -> None:
        if self.source == "test":
            self._run_test_pattern()
            return
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.source)
            if not cap.isOpened():
                self.connected = False
                log.warning("%s: cannot open %s, retrying in 3s", self.name, self.source)
                time.sleep(3)
                continue
            log.info("%s: connected to %s", self.name, self.source)
            self.connected = True
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    log.warning("%s: stream dropped, reconnecting", self.name)
                    break
                self._store(frame)
            self.connected = False
            cap.release()
            time.sleep(1)

    def _run_test_pattern(self) -> None:
        """Moving bar + clock, so you can test everything without real cameras."""
        self.connected = True
        x = 0
        while not self._stop.is_set():
            img = np.full((self.height, self.width, 3), (40, 60, 40), np.uint8)
            cv2.rectangle(img, (x, 0), (x + 40, self.height), (60, 180, 255), -1)
            cv2.putText(img, f"{self.name}  {time.strftime('%H:%M:%S')}", (16, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            self._store(img)
            x = (x + 8) % self.width
            time.sleep(1 / self.fps)


class GrabberTrack(VideoStreamTrack):
    """WebRTC video track fed by a FrameGrabber, paced at the grabber's fps."""

    def __init__(self, grabber: FrameGrabber):
        super().__init__()
        self.grabber = grabber
        self._start: Optional[float] = None
        self._ts = 0
        self._placeholder = np.zeros((grabber.height, grabber.width, 3), np.uint8)
        cv2.putText(self._placeholder, f"{grabber.name}: no signal", (16, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)

    async def recv(self) -> av.VideoFrame:
        if self._start is None:
            self._start = time.time()
        else:
            self._ts += int(VIDEO_CLOCK_RATE / self.grabber.fps)
            wait = self._start + self._ts / VIDEO_CLOCK_RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        img = self.grabber.latest()
        frame = av.VideoFrame.from_ndarray(img if img is not None else self._placeholder, format="bgr24")
        frame.pts, frame.time_base = self._ts, VIDEO_TIME_BASE
        return frame

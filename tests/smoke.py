"""End-to-end check against a running SoundDetect server on localhost."""

import base64
import json
import math
import ssl
import struct
import sys
import time
import urllib.request
import wave
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path


root = Path(__file__).resolve().parents[1]
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
base = f"https://127.0.0.1:{port}"
key = (root / "data/admin-key.txt").read_text().strip()
context = ssl.create_default_context(cafile=str(root / "data/tls/SoundDetect-CA.crt"))


def call(path, method="GET", body=None, headers=None):
    request = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers or {})
    with urllib.request.urlopen(request, context=context, timeout=35) as response:
        raw = response.read()
        return response.status, json.loads(raw) if response.headers.get_content_type() == "application/json" else raw


def sample_wav():
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        samples = []
        for i in range(16000 * 4):
            t = i / 16000
            pulse = sum(math.exp(-((t - k) / .008) ** 2) for k in (.5, 1.1, 1.7, 2.3, 2.9))
            samples.append(int(18000 * pulse * math.sin(2 * math.pi * 700 * t)))
        output.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return buffer.getvalue()


status, page = call("/dashboard")
assert status == 200 and b"SoundDetect" in page
status, state = call("/api/state?key=" + key)
assert status == 200 and state["classifier"] == "Apple Sound Analysis ready"
status, sensor = call("/api/sensors", "POST", {}, {"X-Admin-Key": key, "Content-Type": "application/json"})
assert status == 201
sid, token = sensor["id"], sensor["token"]
try:
    status, config = call(f"/api/sensor-config?sid={sid}&token={token}")
    assert status == 200 and config["lastSeen"]
    raw = sample_wav()
    payload = {"sensorId": sid, "capturedAt": datetime.now(timezone.utc).isoformat(),
               "peakDbfs": -19, "audioBase64": base64.b64encode(raw).decode()}
    status, result = call("/api/observations", "POST", payload,
                          {"X-Sensor-Token": token, "Content-Type": "application/json"})
    assert status == 202
    for _ in range(35):
        time.sleep(1)
        _, state = call("/api/state?key=" + key)
        event = next(e for e in state["events"] if e["id"] == result["eventId"])
        if event["label"] != "Analyzing…":
            break
    assert event["label"] not in ("Analyzing…", "Analysis failed", "Unclassified"), event
    observation = event["observations"][0]
    status, audio = call(f"/api/audio?event={event['id']}&observation={observation['id']}&key={key}")
    assert status == 200 and audio == raw
    print("PASS: HTTPS dashboard, state auth, pairing, heartbeat, WAV upload, Mac classification, playback")
finally:
    call(f"/api/sensors/{sid}", "DELETE", headers={"X-Admin-Key": key})

"""SoundDetect local gateway and cloud processor (Python standard library only)."""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
MAX_JSON = 9_000_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def limited(value, low, high, fallback):
    try:
        number = float(value)
        return max(low, min(high, number)) if math.isfinite(number) else fallback
    except (ValueError, TypeError):
        return fallback


def text_value(value, limit=80):
    return str(value or "").strip()[:limit]


def public_ip() -> str:
    try:
        output = subprocess.check_output(["/sbin/ifconfig"], text=True, stderr=subprocess.DEVNULL)
        ips = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", output)
        return next((ip for ip in ips if not ip.startswith("127.")), "127.0.0.1")
    except Exception:
        return "127.0.0.1"


def ensure_certificate(addresses: list[str], regenerate=False):
    cert_dir = DATA / "tls"
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca = cert_dir / "SoundDetect-CA.crt"
    leaf = cert_dir / "server.crt"
    key = cert_dir / "server.key"
    if regenerate or not (ca.exists() and leaf.exists() and key.exists()):
        ca_key = cert_dir / "ca.key"
        csr = cert_dir / "server.csr"
        ext = cert_dir / "server.ext"
        names = ["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in addresses if ip != "127.0.0.1"]
        ext.write_text("basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join(names) + "\n")
        commands = [
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "3650", "-subj", "/CN=SoundDetect Local CA", "-keyout", str(ca_key), "-out", str(ca), "-addext", "basicConstraints=critical,CA:TRUE"],
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=SoundDetect Mac", "-keyout", str(key), "-out", str(csr)],
            ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(leaf), "-days", "365", "-sha256", "-extfile", str(ext)],
        ]
        for command in commands:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(ca_key, 0o600)
        os.chmod(key, 0o600)
    return ca, leaf, key


class Store:
    def __init__(self, root=DATA):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "audio").mkdir(exist_ok=True)
        self.path = self.root / "state.json"
        self.key_path = self.root / "admin-key.txt"
        self.lock = threading.RLock()
        if not self.key_path.exists():
            self.key_path.write_text(secrets.token_urlsafe(24))
            os.chmod(self.key_path, 0o600)
        self.admin_key = self.key_path.read_text().strip()
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {
                "room": {"name": "Living room", "width": 6, "depth": 5, "photo": None},
                "furniture": [
                    {"id": "sofa-demo", "type": "sofa", "x": 1.0, "y": 0.65, "w": 2.1, "d": 0.8},
                    {"id": "table-demo", "type": "table", "x": 2.15, "y": 2.05, "w": 1.25, "d": 0.8},
                    {"id": "plant-demo", "type": "plant", "x": 4.75, "y": 0.7, "w": 0.5, "d": 0.5},
                ],
                "sensors": [],
                "events": [],
            }
            self.save()

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2))
        temp.replace(self.path)

    def sensor(self, sid):
        return next((s for s in self.state["sensors"] if s["id"] == sid), None)

    def snapshot(self, base_url, classifier_status):
        with self.lock:
            data = json.loads(json.dumps(self.state))
        for sensor in data["sensors"]:
            sensor["pairingUrl"] = f"{base_url}/sensor?sid={sensor['id']}&token={sensor['token']}"
        data["classifier"] = classifier_status
        data["baseUrl"] = base_url
        return data

    def add_observation(self, sensor, payload, raw):
        captured = text_value(payload.get("capturedAt"), 48)
        try:
            timestamp = datetime.fromisoformat(captured.replace("Z", "+00:00")).timestamp()
            if abs(timestamp - time.time()) > 300:
                timestamp = time.time()
                captured = now_iso()
        except ValueError:
            timestamp = time.time()
            captured = now_iso()
        with self.lock:
            event = next((e for e in reversed(self.state["events"][-30:]) if abs(e["timestamp"] - timestamp) < 2.5), None)
            if event is None:
                event = {"id": uuid.uuid4().hex[:12], "capturedAt": captured, "timestamp": timestamp,
                         "label": "Analyzing…", "confidence": 0, "candidates": [], "observations": [],
                         "location": None, "locationMethod": "", "createdAt": now_iso()}
                self.state["events"].append(event)
                self.state["events"] = self.state["events"][-300:]
            obs_id = uuid.uuid4().hex[:12]
            file_name = f"{event['id']}-{obs_id}.wav"
            (self.root / "audio" / file_name).write_bytes(raw)
            observation = {"id": obs_id, "sensorId": sensor["id"], "sensorName": sensor["name"],
                           "sensorPosition": {"x": sensor["x"], "y": sensor["y"]},
                           "capturedAt": captured, "receivedAt": now_iso(),
                           "peakDbfs": limited(payload.get("peakDbfs"), -120, 0, -120),
                           "duration": limited(payload.get("duration"), 0, 10, 0),
                           "file": file_name, "label": "Analyzing…", "confidence": 0}
            event["observations"].append(observation)
            self._locate(event)
            self.save()
            return event["id"], obs_id, self.root / "audio" / file_name

    def _locate(self, event):
        observations = event["observations"]
        distinct = {o["sensorId"]: o for o in observations}
        items = list(distinct.values())
        if len(items) == 1:
            event["location"] = items[0]["sensorPosition"]
            event["locationMethod"] = "Near reporting sensor; source location unknown"
        else:
            # Relative microphone levels provide only a rough estimate; phones are not calibrated.
            weights = [max(0.001, 10 ** (o["peakDbfs"] / 20)) for o in items]
            total = sum(weights)
            event["location"] = {"x": round(sum(o["sensorPosition"]["x"] * w for o, w in zip(items, weights)) / total, 2),
                                 "y": round(sum(o["sensorPosition"]["y"] * w for o, w in zip(items, weights)) / total, 2)}
            event["locationMethod"] = "Approximate level-weighted estimate; phones are uncalibrated"

    def finish_observation(self, event_id, obs_id, result):
        with self.lock:
            event = next((e for e in self.state["events"] if e["id"] == event_id), None)
            if not event:
                return
            observation = next((o for o in event["observations"] if o["id"] == obs_id), None)
            if not observation:
                return
            observation["label"] = result["label"]
            observation["confidence"] = result["confidence"]
            observation["candidates"] = result.get("candidates", [])
            best = max(event["observations"], key=lambda o: o.get("confidence", 0))
            event["label"] = best["label"]
            event["confidence"] = best["confidence"]
            event["candidates"] = best.get("candidates", [])
            self.save()


def display_label(identifier):
    lower = identifier.lower().replace("_", " ").replace("-", " ")
    if "speech" in lower or "conversation" in lower:
        return "Talking"
    if "clap" in lower:
        return "Clapping"
    if "thump" in lower or "thud" in lower or "crash" in lower:
        return "Impact / possible fall"
    return lower.title()


class Classifier:
    def __init__(self):
        self.binary = DATA / "bin" / "classify"
        self.status = "unavailable"
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        try:
            xcode = Path("/Applications/Xcode.app/Contents/Developer")
            swiftc = xcode / "Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc"
            sdk = xcode / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
            command = [str(swiftc), "-sdk", str(sdk)] if swiftc.exists() and sdk.exists() else ["swiftc"]
            cache = DATA / "build-cache"
            cache.mkdir(exist_ok=True)
            environment = os.environ.copy()
            environment["CLANG_MODULE_CACHE_PATH"] = str(cache / "clang")
            environment["SWIFT_MODULE_CACHE_PATH"] = str(cache / "swift")
            subprocess.run(command + [str(ROOT / "classify.swift"), "-o", str(self.binary)],
                           check=True, capture_output=True, text=True, timeout=120, env=environment)
            self.status = "Apple Sound Analysis ready"
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            self.status = f"Classifier unavailable: {detail[-180:]}"

    def classify(self, path):
        if not self.binary.exists():
            return {"label": "Unclassified", "confidence": 0, "candidates": []}
        try:
            completed = subprocess.run([str(self.binary), str(path)], capture_output=True, text=True, timeout=35, check=True)
            candidates = json.loads(completed.stdout)
            if not candidates:
                return {"label": "Unknown sound", "confidence": 0, "candidates": []}
            top = candidates[0]
            return {"label": display_label(top["identifier"]), "confidence": round(top["confidence"], 3),
                    "candidates": [{"label": display_label(c["identifier"]), "rawLabel": c["identifier"],
                                    "confidence": round(c["confidence"], 3)} for c in candidates[:5]]}
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
            return {"label": "Analysis failed", "confidence": 0, "candidates": [], "error": str(exc)[:180]}


class Handler(BaseHTTPRequestHandler):
    server_version = "SoundDetect/1.0"

    def log_message(self, format, *args):
        # Pairing tokens live in URLs, so omit ordinary access logs.
        pass

    def respond(self, status, data=None, content_type="application/json", extra=None):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def query(self):
        return parse_qs(urlparse(self.path).query)

    def admin(self):
        key = self.headers.get("X-Admin-Key") or self.query().get("key", [""])[0]
        return secrets.compare_digest(key, self.server.store.admin_key)

    def sensor_auth(self, sid):
        sensor = self.server.store.sensor(sid)
        token = self.headers.get("X-Sensor-Token") or self.query().get("token", [""])[0]
        return sensor if sensor and secrets.compare_digest(token, sensor["token"]) else None

    def json_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > MAX_JSON:
            raise ValueError("Request size outside allowed range")
        return json.loads(self.rfile.read(size))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            if not self.admin():
                return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid dashboard key"})
            return self.respond(HTTPStatus.OK, self.server.store.snapshot(self.server.base_url, self.server.classifier.status))
        if path == "/api/sensor-config":
            sid = self.query().get("sid", [""])[0]
            sensor = self.sensor_auth(sid)
            if not sensor:
                return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid sensor link"})
            with self.server.store.lock:
                sensor["lastSeen"] = now_iso()
                sensor["active"] = self.headers.get("X-Sensor-Active") == "true"
                self.server.store.save()
                config = {k: v for k, v in sensor.items() if k != "token"}
            return self.respond(HTTPStatus.OK, config)
        if path == "/api/audio":
            if not self.admin():
                return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid dashboard key"})
            event_id = self.query().get("event", [""])[0]
            obs_id = self.query().get("observation", [""])[0]
            with self.server.store.lock:
                event = next((e for e in self.server.store.state["events"] if e["id"] == event_id), None)
                obs = next((o for o in event["observations"] if o["id"] == obs_id), None) if event else None
            if not obs:
                return self.respond(HTTPStatus.NOT_FOUND, {"error": "Recording not found"})
            return self.respond(HTTPStatus.OK, (self.server.store.root / "audio" / obs["file"]).read_bytes(), "audio/wav")
        if path == "/api/photo":
            if not self.admin():
                return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid dashboard key"})
            with self.server.store.lock:
                photo = self.server.store.state["room"].get("photo")
            if not photo:
                return self.respond(HTTPStatus.NOT_FOUND, {"error": "No photo"})
            mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[photo.split(".")[-1]]
            return self.respond(HTTPStatus.OK, (self.server.store.root / photo).read_bytes(), mime)
        files = {"/": "dashboard.html", "/dashboard": "dashboard.html", "/sensor": "sensor.html",
                 "/styles.css": "styles.css", "/dashboard.js": "dashboard.js", "/sensor.js": "sensor.js",
                 "/pcm-processor.js": "pcm-processor.js"}
        if path in files:
            name = files[path]
            mime = "text/html; charset=utf-8" if name.endswith(".html") else "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
            return self.respond(HTTPStatus.OK, (STATIC / name).read_bytes(), mime,
                                {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; media-src 'self'"})
        if path == "/ca.crt":
            return self.respond(HTTPStatus.OK, self.server.ca_path.read_bytes(), "application/x-x509-ca-cert")
        self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.json_body()
            if path == "/api/observations":
                sid = text_value(payload.get("sensorId"), 40)
                sensor = self.sensor_auth(sid)
                if not sensor:
                    return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid sensor token"})
                raw = base64.b64decode(payload.get("audioBase64", ""), validate=True)
                if len(raw) > 2_000_000:
                    raise ValueError("Audio clip is too large")
                with wave.open(io.BytesIO(raw), "rb") as wav:
                    if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                        raise ValueError("Expected mono 16-bit PCM WAV")
                    duration = wav.getnframes() / wav.getframerate()
                    if not 0.2 <= duration <= 10 or not 8000 <= wav.getframerate() <= 96000:
                        raise ValueError("Invalid audio duration or sample rate")
                payload["duration"] = duration
                event_id, obs_id, file_path = self.server.store.add_observation(sensor, payload, raw)
                threading.Thread(target=self.server.analyze, args=(event_id, obs_id, file_path), daemon=True).start()
                return self.respond(HTTPStatus.ACCEPTED, {"eventId": event_id, "observationId": obs_id})
            if not self.admin():
                return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid dashboard key"})
            store = self.server.store
            with store.lock:
                if path == "/api/room":
                    room = store.state["room"]
                    room["name"] = text_value(payload.get("name"), 60) or room["name"]
                    room["width"] = limited(payload.get("width"), 2, 30, room["width"])
                    room["depth"] = limited(payload.get("depth"), 2, 30, room["depth"])
                    for s in store.state["sensors"]:
                        s["x"] = min(s["x"], room["width"])
                        s["y"] = min(s["y"], room["depth"])
                    store.save()
                    return self.respond(HTTPStatus.OK, room)
                if path == "/api/sensors":
                    room = store.state["room"]
                    corners = [(0.3, 0.3), (room["width"] - 0.3, 0.3),
                               (room["width"] - 0.3, room["depth"] - 0.3), (0.3, room["depth"] - 0.3)]
                    x, y = corners[len(store.state["sensors"]) % 4]
                    sensor = {"id": uuid.uuid4().hex[:10], "token": secrets.token_urlsafe(24),
                              "name": f"Sensor {len(store.state['sensors']) + 1:02}", "x": round(x, 2), "y": round(y, 2),
                              "thresholdDbfs": -28, "watchFor": "", "lastSeen": None, "active": False}
                    store.state["sensors"].append(sensor)
                    store.save()
                    return self.respond(HTTPStatus.CREATED, sensor)
                if path == "/api/furniture":
                    item = {"id": uuid.uuid4().hex[:10], "type": text_value(payload.get("type"), 20),
                            "x": limited(payload.get("x"), 0, room_max(store, "width"), 1),
                            "y": limited(payload.get("y"), 0, room_max(store, "depth"), 1),
                            "w": limited(payload.get("w"), 0.25, 4, 1), "d": limited(payload.get("d"), 0.25, 4, 0.7)}
                    if item["type"] not in ("sofa", "table", "bed", "desk", "chair", "cabinet", "plant"):
                        raise ValueError("Unknown furniture type")
                    store.state["furniture"].append(item)
                    store.save()
                    return self.respond(HTTPStatus.CREATED, item)
                if path == "/api/photo":
                    mime = payload.get("mime")
                    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime)
                    if not extension:
                        raise ValueError("Use a JPEG, PNG, or WebP photo")
                    photo = base64.b64decode(payload.get("base64", ""), validate=True)
                    if len(photo) > 6_000_000:
                        raise ValueError("Photo exceeds 6 MB")
                    name = f"room-photo.{extension}"
                    (store.root / name).write_bytes(photo)
                    store.state["room"]["photo"] = name
                    store.save()
                    return self.respond(HTTPStatus.OK, {"photo": name})
            return self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except (ValueError, json.JSONDecodeError, base64.binascii.Error, wave.Error) as exc:
            return self.respond(HTTPStatus.BAD_REQUEST, {"error": str(exc)[:160]})

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not self.admin():
            return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid dashboard key"})
        try:
            payload = self.json_body()
            store = self.server.store
            with store.lock:
                match = re.fullmatch(r"/api/(sensors|furniture)/([a-zA-Z0-9-]+)", path)
                if not match:
                    return self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                collection, item_id = match.groups()
                item = next((x for x in store.state[collection] if x["id"] == item_id), None)
                if not item:
                    return self.respond(HTTPStatus.NOT_FOUND, {"error": "Item not found"})
                if collection == "sensors":
                    if "name" in payload:
                        item["name"] = text_value(payload["name"], 40) or item["name"]
                    if "thresholdDbfs" in payload:
                        item["thresholdDbfs"] = limited(payload["thresholdDbfs"], -60, -8, item["thresholdDbfs"])
                    if "watchFor" in payload:
                        item["watchFor"] = text_value(payload["watchFor"], 100)
                for axis, bound in (("x", "width"), ("y", "depth")):
                    if axis in payload:
                        item[axis] = round(limited(payload[axis], 0, room_max(store, bound), item[axis]), 2)
                store.save()
                return self.respond(HTTPStatus.OK, item)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.respond(HTTPStatus.BAD_REQUEST, {"error": str(exc)[:160]})

    def do_DELETE(self):
        if not self.admin():
            return self.respond(HTTPStatus.FORBIDDEN, {"error": "Invalid dashboard key"})
        path = urlparse(self.path).path
        store = self.server.store
        with store.lock:
            match = re.fullmatch(r"/api/(sensors|furniture)/([a-zA-Z0-9-]+)", path)
            if match:
                collection, item_id = match.groups()
                before = len(store.state[collection])
                store.state[collection] = [x for x in store.state[collection] if x["id"] != item_id]
                if len(store.state[collection]) != before:
                    store.save()
                    return self.respond(HTTPStatus.OK, {"deleted": item_id})
        self.respond(HTTPStatus.NOT_FOUND, {"error": "Item not found"})


def room_max(store, dimension):
    return store.state["room"][dimension]


class SoundServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, classifier, base_url, ca_path):
        super().__init__(address, Handler)
        self.store = store
        self.classifier = classifier
        self.base_url = base_url
        self.ca_path = ca_path

    def analyze(self, event_id, obs_id, path):
        self.store.finish_observation(event_id, obs_id, self.classifier.classify(path))


def main():
    parser = argparse.ArgumentParser(description="SoundDetect Mac gateway")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--ip", help="LAN address advertised in phone pairing links")
    parser.add_argument("--regenerate-cert", action="store_true")
    args = parser.parse_args()
    lan_ip = args.ip or public_ip()
    ca, cert, key = ensure_certificate([lan_ip], args.regenerate_cert)
    store = Store()
    print("Preparing Apple Sound Analysis classifier...", flush=True)
    classifier = Classifier()
    server = SoundServer(("0.0.0.0", args.port), store, classifier, f"https://{lan_ip}:{args.port}", ca)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"Dashboard: https://localhost:{args.port}/dashboard?key={store.admin_key}", flush=True)
    print(f"Phone address: https://{lan_ip}:{args.port}", flush=True)
    print(f"Classifier: {classifier.status}", flush=True)
    print("Keep this terminal open while sensors are connected.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

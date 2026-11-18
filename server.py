"""SoundDetect local gateway and cloud processor (Python standard library only)."""

from __future__ import annotations

import argparse
import base64
import contextlib
import ipaddress
import io
import json
import math
import os
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
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
# Peak level a typical sound produces 1 m from a sensor; tune per sensor in the dashboard.
DEFAULT_REF_DBFS_AT_1M = -20.0


def estimate_distance(peak_dbfs, ref_dbfs_at_1m, max_distance):
    """Free-field inverse-distance law: level falls 6 dB per doubling of distance.

    Only a rough range: real rooms add reverberation, and the true source loudness is unknown.
    """
    distance = 10 ** ((ref_dbfs_at_1m - peak_dbfs) / 20)
    return round(max(0.1, min(max_distance, distance)), 2)


def trilaterate(ranges, width, depth, step=0.05):
    """Point in the room whose distances to the sensors best match the estimated ranges."""
    step = max(step, max(width, depth) / 150)  # bound the search for large rooms
    best, best_error = None, float("inf")
    nx, ny = int(width / step) + 1, int(depth / step) + 1
    for i in range(nx):
        x = i * step
        for j in range(ny):
            y = j * step
            error = sum((math.hypot(x - r["x"], y - r["y"]) - r["distance"]) ** 2 * r["weight"] for r in ranges)
            if error < best_error:
                best, best_error = (x, y), error
    return {"x": round(best[0], 2), "y": round(best[1], 2)}


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


def usable_lan_ip(value) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
        return not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast)
    except (ipaddress.AddressValueError, TypeError):
        return False


def choose_lan_ip(wifi_ip=None, route_ip=None, fallback_ips=()) -> str:
    """Prefer the active Wi-Fi address, then the routed and other local addresses."""
    for candidate in (wifi_ip, route_ip, *fallback_ips):
        if usable_lan_ip(candidate):
            return str(ipaddress.IPv4Address(candidate))
    return "127.0.0.1"


def wifi_ipv4() -> str | None:
    """Return the current macOS Wi-Fi interface address when one is assigned."""
    try:
        output = subprocess.check_output(
            ["/usr/sbin/networksetup", "-listallhardwareports"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for block in re.split(r"\n(?=Hardware Port:)", output):
        if not re.search(r"Hardware Port:\s*(?:Wi-Fi|AirPort)\s*$", block, re.IGNORECASE | re.MULTILINE):
            continue
        device = re.search(r"^Device:\s*(\S+)", block, re.MULTILINE)
        if not device:
            continue
        try:
            address = subprocess.check_output(
                ["/usr/sbin/ipconfig", "getifaddr", device.group(1)],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if usable_lan_ip(address):
            return address
    return None


def routed_ipv4() -> str | None:
    """Ask the kernel which IPv4 interface would carry an off-link UDP packet.

    UDP connect only performs route selection; it sends no packet.
    """
    for destination in ("192.0.2.1", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((destination, 9))
                address = probe.getsockname()[0]
            if usable_lan_ip(address):
                return address
        except OSError:
            continue
    return None


def interface_ipv4s() -> list[tuple[str, str]]:
    """List usable IPv4 addresses by interface, with hardware interfaces first."""
    try:
        output = subprocess.check_output(
            ["/sbin/ifconfig"], text=True, stderr=subprocess.DEVNULL, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for block in re.split(r"(?m)^(?=\S[^:\n]*: flags=)", output):
        header = re.match(r"([^\s:]+):\s+flags=", block)
        if not header:
            continue
        interface = header.group(1)
        for address in re.findall(r"\binet (\d+(?:\.\d+){3})\b", block):
            if usable_lan_ip(address):
                found.append((interface, address))
    found.sort(key=lambda pair: (not re.fullmatch(r"en\d+", pair[0]), pair[0]))
    return found


def public_ip() -> str:
    """Return the best current local address for sensor links."""
    wifi = wifi_ipv4()
    if usable_lan_ip(wifi):
        return str(ipaddress.IPv4Address(wifi))
    route = routed_ipv4()
    if usable_lan_ip(route):
        return str(ipaddress.IPv4Address(route))
    fallback = [address for _, address in interface_ipv4s()]
    return choose_lan_ip(fallback_ips=fallback)


def ensure_certificate(addresses: list[str], regenerate=False, cert_dir=None):
    cert_dir = Path(cert_dir) if cert_dir else DATA / "tls"
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca = cert_dir / "SoundDetect-CA.crt"
    leaf = cert_dir / "server.crt"
    key = cert_dir / "server.key"
    ca_key = cert_dir / "ca.key"
    csr = cert_dir / "server.csr"
    ext = cert_dir / "server.ext"
    address_record = cert_dir / "server-addresses.txt"
    san_addresses = sorted({"127.0.0.1", *(ip for ip in addresses if usable_lan_ip(ip))})
    names = ["DNS:localhost"] + [f"IP:{ip}" for ip in san_addresses]

    if regenerate or not (ca.exists() and ca_key.exists()):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "3650",
             "-subj", "/CN=SoundDetect Local CA", "-keyout", str(ca_key), "-out", str(ca),
             "-addext", "basicConstraints=critical,CA:TRUE"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.chmod(ca_key, 0o600)

    addresses_changed = not address_record.exists() or address_record.read_text().splitlines() != san_addresses
    if regenerate or addresses_changed or not (leaf.exists() and key.exists()):
        if regenerate or not key.exists():
            make_key_and_csr = ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=SoundDetect Mac",
                                 "-keyout", str(key), "-out", str(csr)]
        else:
            make_key_and_csr = ["openssl", "req", "-new", "-key", str(key), "-subj", "/CN=SoundDetect Mac", "-out", str(csr)]
        subprocess.run(make_key_and_csr, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ext.write_text(
            "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join(names) + "\n"
        )
        subprocess.run(
            ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key),
             "-CAcreateserial", "-out", str(leaf), "-days", "365", "-sha256", "-extfile", str(ext)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.chmod(key, 0o600)
        address_record.write_text("\n".join(san_addresses) + "\n")
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
                "eventRules": [],
                "incidents": [],
            }
            self.save()
        self.state.setdefault("eventRules", [])
        self.state.setdefault("incidents", [])
        for sensor in self.state.get("sensors", []):
            sensor.setdefault("nodeType", "mobile")
            sensor.setdefault("refDbfsAt1m", DEFAULT_REF_DBFS_AT_1M)

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2))
        temp.replace(self.path)

    def sensor(self, sid):
        return next((s for s in self.state["sensors"] if s["id"] == sid), None)

    def add_sensor(self, node_type="mobile"):
        node_type = text_value(node_type, 32) or "mobile"
        if node_type not in ("mobile", "esp32_inmp441"):
            raise ValueError("nodeType must be mobile or esp32_inmp441")
        with self.lock:
            room = self.state["room"]
            corners = [(0.3, 0.3), (room["width"] - 0.3, 0.3),
                       (room["width"] - 0.3, room["depth"] - 0.3), (0.3, room["depth"] - 0.3)]
            x, y = corners[len(self.state["sensors"]) % 4]
            number = len(self.state["sensors"]) + 1
            node_name = "ESP32 + INMP441" if node_type == "esp32_inmp441" else "Mobile sensor"
            sensor = {"id": uuid.uuid4().hex[:10], "token": secrets.token_urlsafe(24),
                      "name": f"{node_name} {number:02}", "nodeType": node_type,
                      "x": round(x, 2), "y": round(y, 2),
                      "thresholdDbfs": -28, "refDbfsAt1m": DEFAULT_REF_DBFS_AT_1M, "watchFor": "", "lastSeen": None, "active": False}
            self.state["sensors"].append(sensor)
            self.save()
            return sensor

    def snapshot(self, base_url, classifier_status, network_mode="automatic"):
        with self.lock:
            data = json.loads(json.dumps(self.state))
        for sensor in data["sensors"]:
            sensor.setdefault("nodeType", "mobile")
            sensor["pairingUrl"] = f"{base_url}/sensor?sid={sensor['id']}&token={sensor['token']}"
        data.setdefault("eventRules", [])
        data.setdefault("incidents", [])
        for rule in data["eventRules"]:
            rule.pop("createdAtEpoch", None)
        data["classifier"] = classifier_status
        data["baseUrl"] = base_url
        data["networkMode"] = network_mode
        return data

    def create_event_rule(self, payload):
        name = text_value(payload.get("name"), 60)
        first_match = text_value(payload.get("firstMatch"), 80)
        second_match = text_value(payload.get("secondMatch"), 80)
        if not name or not first_match or not second_match:
            raise ValueError("Enter an event name and both sound-label matches")
        scope = text_value(payload.get("scope"), 24) or "any_sensor"
        if scope not in ("any_sensor", "same_sensor"):
            raise ValueError("Scope must be any_sensor or same_sensor")
        window = int(limited(payload.get("windowSeconds"), 5, 600, 60))
        rule = {
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "firstMatch": first_match,
            "secondMatch": second_match,
            "windowSeconds": window,
            "scope": scope,
            "enabled": True,
            "createdAt": now_iso(),
            "createdAtEpoch": time.time(),
        }
        with self.lock:
            self.state.setdefault("eventRules", []).append(rule)
            self.save()
        return {key: value for key, value in rule.items() if key != "createdAtEpoch"}

    def update_event_rule(self, rule_id, payload):
        with self.lock:
            rule = next((item for item in self.state.setdefault("eventRules", []) if item["id"] == rule_id), None)
            if not rule:
                return None
            name = text_value(payload.get("name", rule["name"]), 60)
            first_match = text_value(payload.get("firstMatch", rule["firstMatch"]), 80)
            second_match = text_value(payload.get("secondMatch", rule["secondMatch"]), 80)
            if not name or not first_match or not second_match:
                raise ValueError("Enter an event name and both sound-label matches")
            scope = text_value(payload.get("scope", rule["scope"]), 24)
            if scope not in ("any_sensor", "same_sensor"):
                raise ValueError("Scope must be any_sensor or same_sensor")
            rule.update({
                "name": name,
                "firstMatch": first_match,
                "secondMatch": second_match,
                "windowSeconds": int(limited(payload.get("windowSeconds", rule["windowSeconds"]), 5, 600, rule["windowSeconds"])),
                "scope": scope,
                "enabled": bool(payload.get("enabled", rule["enabled"])),
                "createdAt": now_iso(),
                "createdAtEpoch": time.time(),
            })
            self.save()
            return {key: value for key, value in rule.items() if key != "createdAtEpoch"}

    def delete_event_rule(self, rule_id):
        with self.lock:
            rules = self.state.setdefault("eventRules", [])
            before = len(rules)
            self.state["eventRules"] = [rule for rule in rules if rule["id"] != rule_id]
            if len(self.state["eventRules"]) == before:
                return False
            self.save()
            return True

    @staticmethod
    def _event_matches(event, fragment):
        label = text_value(event.get("label"), 100).casefold()
        return label != "analyzing…" and text_value(fragment, 80).casefold() in label

    @staticmethod
    def _event_sensors(event):
        return {observation.get("sensorId") for observation in event.get("observations", []) if observation.get("sensorId")}

    @staticmethod
    def _incident_step(event):
        sensor_names = sorted({observation.get("sensorName", "Sensor") for observation in event.get("observations", [])})
        return {
            "eventId": event["id"],
            "label": event.get("label", "Unknown sound"),
            "capturedAt": event.get("capturedAt"),
            "confidence": event.get("confidence", 0),
            "sensorNames": sensor_names,
        }

    def _evaluate_event_rules(self):
        """Build named incidents from ordered classifier labels inside configured time windows."""
        with self.lock:
            self._evaluate_event_rules_locked()

    def _evaluate_event_rules_locked(self):
        events = sorted(
            (event for event in self.state.get("events", []) if event.get("label") != "Analyzing…"),
            key=lambda event: event.get("timestamp", 0),
        )
        incidents = self.state.setdefault("incidents", [])
        existing = {(incident.get("ruleId"), incident.get("triggerEventId")) for incident in incidents}
        for rule in self.state.get("eventRules", []):
            if not rule.get("enabled", True):
                continue
            earliest = rule.get("createdAtEpoch", 0)
            window = rule.get("windowSeconds", 60)
            for second_index, second in enumerate(events):
                second_time = second.get("timestamp", 0)
                if second_time < earliest or not self._event_matches(second, rule["secondMatch"]):
                    continue
                if (rule["id"], second["id"]) in existing:
                    continue
                for first in reversed(events[:second_index]):
                    delta = second_time - first.get("timestamp", 0)
                    if delta > window:
                        break
                    if delta <= 0 or first.get("timestamp", 0) < earliest:
                        continue
                    if not self._event_matches(first, rule["firstMatch"]):
                        continue
                    if rule.get("scope") == "same_sensor" and not (self._event_sensors(first) & self._event_sensors(second)):
                        continue
                    incident = {
                        "id": uuid.uuid4().hex[:12],
                        "ruleId": rule["id"],
                        "ruleName": rule["name"],
                        "triggerEventId": second["id"],
                        "sourceEventIds": [first["id"], second["id"]],
                        "startedAt": first.get("capturedAt"),
                        "triggeredAt": second.get("capturedAt"),
                        "timestamp": second_time,
                        "windowSeconds": window,
                        "sequence": [self._incident_step(first), self._incident_step(second)],
                        "createdAt": now_iso(),
                    }
                    incidents.append(incident)
                    existing.add((rule["id"], second["id"]))
                    break
        incidents.sort(key=lambda incident: incident.get("timestamp", 0))
        if len(incidents) > 300:
            self.state["incidents"] = incidents[-300:]

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
                           "refDbfsAt1m": sensor.get("refDbfsAt1m", DEFAULT_REF_DBFS_AT_1M),
                           "capturedAt": captured, "receivedAt": now_iso(),
                           "peakDbfs": limited(payload.get("peakDbfs"), -120, 0, -120),
                           "duration": limited(payload.get("duration"), 0, 10, 0),
                           "sampleRate": int(limited(payload.get("sampleRate"), 0, 96000, 0)),
                           "firmware": text_value(payload.get("firmware"), 48),
                           "file": file_name, "label": "Analyzing…", "confidence": 0}
            event["observations"].append(observation)
            self._locate(event)
            self.save()
            return event["id"], obs_id, self.root / "audio" / file_name

    def _locate(self, event):
        room = self.state["room"]
        diagonal = math.hypot(room["width"], room["depth"])
        distinct = {}
        for o in event["observations"]:
            # Keep the loudest observation per sensor.
            if o["sensorId"] not in distinct or o["peakDbfs"] > distinct[o["sensorId"]]["peakDbfs"]:
                distinct[o["sensorId"]] = o
        ranges = []
        for o in distinct.values():
            distance = estimate_distance(o["peakDbfs"], o.get("refDbfsAt1m", DEFAULT_REF_DBFS_AT_1M), diagonal)
            o["estimatedDistance"] = distance
            ranges.append({"sensorId": o["sensorId"], "sensorName": o["sensorName"],
                           "x": o["sensorPosition"]["x"], "y": o["sensorPosition"]["y"],
                           "distance": distance, "weight": 1 / max(distance, 0.3) ** 2})
        event["ranges"] = [{k: v for k, v in r.items() if k != "weight"} for r in ranges]
        if len(ranges) == 1:
            r = ranges[0]
            event["location"] = {"x": r["x"], "y": r["y"]}
            event["locationMethod"] = (f"About {r['distance']:.1f} m from {r['sensorName']}; direction unknown "
                                       "with one sensor (level-based, uncalibrated)")
        elif len(ranges) == 2:
            event["location"] = trilaterate(ranges, room["width"], room["depth"])
            event["locationMethod"] = ("Approximate position from two level-based ranges; "
                                       "a mirror position is also possible")
        else:
            event["location"] = trilaterate(ranges, room["width"], room["depth"])
            event["locationMethod"] = (f"Approximate position from {len(ranges)} level-based ranges; "
                                       "microphones are uncalibrated")

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
            classified = [item for item in event["observations"] if item.get("label") != "Analyzing…"]
            if not classified:
                self.save()
                return
            best = max(classified, key=lambda o: o.get("confidence", 0))
            event["label"] = best["label"]
            event["confidence"] = best["confidence"]
            event["candidates"] = best.get("candidates", [])
            if all(item.get("label") != "Analyzing…" for item in event["observations"]):
                self._evaluate_event_rules()
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


MIN_ANALYSIS_SECONDS = 3.0


@contextlib.contextmanager
def padded_for_analysis(path, min_seconds=MIN_ANALYSIS_SECONDS):
    """Yield a WAV path at least min_seconds long.

    Apple's sound classifier analyses fixed-length windows and can return no result at
    all for short clips (such as the 1.2 s ESP32 uploads). Short clips are centred in
    silence in a temporary copy; the stored recording is not changed.
    """
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        frames = source.readframes(params.nframes)
    needed = int(min_seconds * params.framerate)
    if params.nframes >= needed:
        yield path
        return
    width = params.sampwidth * params.nchannels
    lead = ((needed - params.nframes) // 2) * width
    tail = needed * width - lead - len(frames)
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.close()
    try:
        with wave.open(handle.name, "wb") as target:
            target.setnchannels(params.nchannels)
            target.setsampwidth(params.sampwidth)
            target.setframerate(params.framerate)
            target.writeframes(b"\0" * lead + frames + b"\0" * tail)
        yield Path(handle.name)
    finally:
        os.unlink(handle.name)


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
            with padded_for_analysis(path) as analysis_path:
                completed = subprocess.run([str(self.binary), str(analysis_path)], capture_output=True, text=True, timeout=35, check=True)
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

    def send_audio(self, data):
        """Serve a WAV clip with HTTP byte-range support (Safari requires it to play media)."""
        size = len(data)
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if not match or (not match.group(1) and not match.group(2)):
            return self.respond(HTTPStatus.OK, data, "audio/wav", {"Accept-Ranges": "bytes"})
        start_text, end_text = match.groups()
        if start_text:
            start = int(start_text)
            end = min(int(end_text), size - 1) if end_text else size - 1
        else:
            start = max(0, size - int(end_text))
            end = size - 1
        if start >= size or start > end:
            return self.respond(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", "audio/wav",
                                {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        return self.respond(HTTPStatus.PARTIAL_CONTENT, data[start:end + 1], "audio/wav",
                            {"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"})

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
            return self.respond(HTTPStatus.OK, self.server.store.snapshot(
                self.server.current_base_url(), self.server.classifier.status, self.server.network_mode
            ))
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
            return self.send_audio((self.server.store.root / "audio" / obs["file"]).read_bytes())
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
                    payload["sampleRate"] = wav.getframerate()
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
                    sensor = store.add_sensor(payload.get("nodeType", "mobile"))
                    return self.respond(HTTPStatus.CREATED, sensor)
                if path == "/api/event-rules":
                    rule = store.create_event_rule(payload)
                    return self.respond(HTTPStatus.CREATED, rule)
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
                rule_match = re.fullmatch(r"/api/event-rules/([a-zA-Z0-9-]+)", path)
                if rule_match:
                    rule = store.update_event_rule(rule_match.group(1), payload)
                    if not rule:
                        return self.respond(HTTPStatus.NOT_FOUND, {"error": "Event rule not found"})
                    return self.respond(HTTPStatus.OK, rule)
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
                    if "refDbfsAt1m" in payload:
                        item["refDbfsAt1m"] = limited(payload["refDbfsAt1m"], -70, 0, item.get("refDbfsAt1m", DEFAULT_REF_DBFS_AT_1M))
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
            rule_match = re.fullmatch(r"/api/event-rules/([a-zA-Z0-9-]+)", path)
            if rule_match and store.delete_event_rule(rule_match.group(1)):
                return self.respond(HTTPStatus.OK, {"deleted": rule_match.group(1)})
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

    def __init__(self, address, store, classifier, lan_ip, ca_path, cert_path, key_path, tls_context, network_mode):
        super().__init__(address, Handler)
        self.store = store
        self.classifier = classifier
        self.lan_ip = lan_ip
        self.port = address[1]
        self.base_url = f"https://{lan_ip}:{self.port}"
        self.ca_path = ca_path
        self.cert_path = cert_path
        self.key_path = key_path
        self.tls_context = tls_context
        self.network_mode = network_mode
        self.network_lock = threading.RLock()

    def current_base_url(self):
        with self.network_lock:
            return self.base_url

    def update_network_address(self, address):
        if not usable_lan_ip(address):
            return False
        address = str(ipaddress.IPv4Address(address))
        with self.network_lock:
            if address == self.lan_ip:
                return False
            # Keep the trusted local CA, but issue a leaf certificate for the new Wi-Fi IP.
            ensure_certificate([address])
            self.tls_context.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
            self.lan_ip = address
            self.base_url = f"https://{address}:{self.port}"
            return True

    def analyze(self, event_id, obs_id, path):
        self.store.finish_observation(event_id, obs_id, self.classifier.classify(path))


def main():
    parser = argparse.ArgumentParser(description="SoundDetect Mac gateway")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--ip", help="LAN address advertised in phone pairing links")
    parser.add_argument("--regenerate-cert", action="store_true")
    args = parser.parse_args()
    lan_ip = args.ip or public_ip()
    if args.ip and not usable_lan_ip(args.ip):
        parser.error("--ip must be a usable IPv4 address assigned to this Mac")
    ca, cert, key = ensure_certificate([lan_ip], args.regenerate_cert)
    store = Store()
    print("Preparing Apple Sound Analysis classifier...", flush=True)
    classifier = Classifier()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    server = SoundServer(("0.0.0.0", args.port), store, classifier, lan_ip, ca, cert, key, context,
                         "manual" if args.ip else "automatic")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"Dashboard: https://localhost:{args.port}/dashboard?key={store.admin_key}", flush=True)
    print(f"Phone address ({server.network_mode}): https://{lan_ip}:{args.port}", flush=True)
    if lan_ip == "127.0.0.1":
        print("No Wi-Fi IPv4 address found yet; the phone address will appear when Wi-Fi connects.", flush=True)
    print(f"Classifier: {classifier.status}", flush=True)
    print("Keep this terminal open while sensors are connected.", flush=True)
    stop_monitor = threading.Event()
    if not args.ip:
        threading.Thread(target=monitor_network_address, args=(server, stop_monitor), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor.set()
        server.server_close()


def monitor_network_address(server, stop_event):
    """Refresh pairing links and the TLS certificate when Wi-Fi changes."""
    while not stop_event.wait(3):
        address = public_ip()
        if not usable_lan_ip(address) or address == server.lan_ip:
            continue
        try:
            if server.update_network_address(address):
                print(f"Wi-Fi changed; refreshed phone address: {server.base_url}", flush=True)
        except (OSError, subprocess.SubprocessError, ssl.SSLError) as exc:
            print(f"Could not refresh the phone address: {exc}", flush=True)


if __name__ == "__main__":
    main()

# SoundDetect

A local IoT sound detection prototype: phones act as edge sensors, and a Mac acts as the gateway, storage server, and sound classifier. The dashboard shows a live isometric room map, sensor settings, sound events, and playable recordings.

## Start on the Mac

Requires macOS with Xcode or Command Line Tools, Python 3, Swift, and OpenSSL. No Python or JavaScript packages need installing. Apple Sound Analysis is used for sound classification.

```bash
cd /Users/sohampatil/SoundDetect
python3 server.py
```

The first start generates a private local certificate and compiles the Swift classifier. Open the **Dashboard** URL printed in the terminal. Keep the terminal running. The dashboard key is saved in `data/admin-key.txt`; recordings, room settings, and certificates are also stored under `data/`.

By default, SoundDetect selects the Mac's active Wi-Fi IPv4 address and watches for network changes. The dashboard address and newly copied phone links update automatically; the server also refreshes its TLS certificate while keeping the same local CA, so phones that already trust that CA do not need to be paired again after a Wi-Fi change.

If automatic detection chooses the wrong interface, set a fixed address with `python3 server.py --ip YOUR_MAC_LAN_IP`. A fixed `--ip` disables automatic switching until the server is restarted without that option. Use `--regenerate-cert` only when you intentionally want to replace the local CA; phones must trust the replacement CA.

## Pair a phone

1. In the dashboard, click **Add sensor**, choose **Mobile device**, then click **Copy pairing link**.
2. Trust the local certificate on each phone. The CA certificate is in `data/tls/SoundDetect-CA.crt`; you can AirDrop it or open `https://CURRENT_MAC_WIFI_IP:8443/ca.crt` on the phone and accept the initial browser warning. On iPhone, install the downloaded profile in Settings, then enable full trust in **Settings → General → About → Certificate Trust Settings**. Android certificate installation menus vary by device. Trust the CA once; its signed server certificate can update when the Mac changes Wi-Fi.
3. Open the copied pairing link in the phone's browser and tap **Start listening**. Grant microphone access. Keep the page open in the foreground; mobile browsers can suspend capture when locked or backgrounded.
4. Position the phone at the corresponding corner. Drag its marker in the map if needed.

## Composite event rules

Create a rule in **Event rules** by naming the incident, entering the first and second sound-label text, setting a time window, and choosing whether the sounds can come from any nodes or must share a node. For example, “Breaking and Entering” can match *lock picking* followed by *opening door* within 45 seconds. The **Compound events** panel records the triggered time and links back to both sound detections.

Rules match case-insensitive text in each sound event's primary classifier label. The current classifier is a general sound model and may not identify specific actions such as lock picking; use labels it actually reports. These rules correlate recognized sounds and do not establish intent or confirm a crime.

## Add an ESP32 + INMP441 sensor

Firmware, wiring, certificate setup, and PlatformIO instructions for classic ESP32 DevKit/WROOM and ESP32-S3 DevKitC-1 boards are in [`docs/ESP32_INMP441_SETUP.md`](docs/ESP32_INMP441_SETUP.md) and [`firmware/esp32_inmp441`](firmware/esp32_inmp441). The sensor applies its dashboard threshold locally, then uploads a short HTTPS WAV clip to the same observation API used by phone sensors. The current `server.py` backend still performs classification on the Mac.

The mobile sensor page and ESP32 firmware send a mono PCM WAV clip with time, measured peak level, and sensor identity after the configured threshold is exceeded. The Mac associates the node with its configured room position, stores the clip, classifies it, and updates the dashboard. Clips remain on the Mac.

## Map and configure

- Set the room name, width, and depth. The first four sensors are placed near the four corners; markers can be dragged.
- Add and drag furniture blocks. Upload a room photo as a visual reference while arranging furniture. A single photo is **not** automatically converted to a measured 3D room or furniture layout.
- Set each sensor's name, trigger threshold, and comma-separated sound watchlist. Watchlist matches are highlighted **after** the Mac classifies a recording; a watchlist does not suppress other recordings.
- Open an event to see classifier candidates, sensor readings, an approximate map position, and playable clips from each reporting phone.

## Measurement and location limits

The trigger uses **dBFS**, a level relative to each node's digital microphone signal. It is not calibrated acoustic **dB SPL**. Different phones, microphone boards, gain controls, and distances can yield different values. Tune each threshold in its actual position.

One reporting sensor identifies only its own position. With two or more sensors reporting the same event within about 2.5 seconds, the map shows a level-weighted approximate position. Phone and ESP32 microphones are not calibrated or synchronized for accurate acoustic triangulation. The UI labels these estimates accordingly.

Classification uses Apple's built-in general sound model. Its labels and confidence are estimates, and an impact cannot prove that an object fell. The UI may show “Impact / possible fall.” If the classifier is unavailable, recordings are still stored and shown as unclassified.

## Architecture

```text
Mobile or ESP32 microphone → local threshold detector → HTTPS WAV + metadata
       → Mac gateway → stored observation → Apple Sound Analysis
       → grouped event + rough spatial estimate → dashboard
```

The server has no external cloud dependency. The dashboard URL contains a private administrator key; phone links contain per-sensor pairing tokens. Share them only with trusted people on the local network. To stop all sensors, stop the server. To revoke one phone, remove its sensor in the dashboard.

### College Wi-Fi and device isolation

The Mac and phones must be on the same reachable local network, and incoming connections to port 8443 must be allowed by the Mac firewall. Some campus Wi-Fi networks block devices from connecting directly to one another. If the phone cannot open the current address shown in the dashboard, use a personal hotspot or a travel router for the Mac and sensor phones, or ask the campus network administrator to allow client-to-client traffic. Automatic IP selection cannot bypass network isolation.

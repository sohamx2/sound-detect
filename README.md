# SoundDetect

A local IoT sound detection prototype: phones act as edge sensors, and a Mac acts as the gateway, storage server, and sound classifier. The dashboard shows a live isometric room map, sensor settings, sound events, and playable recordings.

## Start on the Mac

Requires macOS with Xcode or Command Line Tools, Python 3, Swift, and OpenSSL. No Python or JavaScript packages need installing. Apple Sound Analysis is used for sound classification.

```bash
cd /Users/sohampatil/SoundDetect
python3 server.py
```

The first start generates a private local certificate and compiles the Swift classifier. Open the **Dashboard** URL printed in the terminal. Keep the terminal running. The dashboard key is saved in `data/admin-key.txt`; recordings, room settings, and certificates are also stored under `data/`.

If the printed phone address is wrong, use `python3 server.py --ip YOUR_MAC_LAN_IP --regenerate-cert`. The Mac and phones must be on the same network, with incoming connections to port 8443 allowed by the Mac firewall.

## Pair a phone

1. In the dashboard, click **Add phone sensor**, select the sensor, and click **Copy phone link**.
2. Trust the local certificate on each phone. The CA certificate is in `data/tls/SoundDetect-CA.crt`; you can AirDrop it or open `https://YOUR_MAC_LAN_IP:8443/ca.crt` on the phone and accept the initial browser warning. On iPhone, install the downloaded profile in Settings, then enable full trust in **Settings → General → About → Certificate Trust Settings**. Android certificate installation menus vary by device.
3. Open the copied pairing link in the phone's browser and tap **Start listening**. Grant microphone access. Keep the page open in the foreground; mobile browsers can suspend capture when locked or backgrounded.
4. Position the phone at the corresponding corner. Drag its marker in the map if needed.

The sensor page sends a mono PCM WAV clip with time, measured peak level, and sensor identity only after its threshold is exceeded. The Mac associates the trusted sensor with its configured room position, stores the clip, classifies it, and updates the dashboard. Clips remain on the Mac.

## Map and configure

- Set the room name, width, and depth. The first four sensors are placed near the four corners; markers can be dragged.
- Add and drag furniture blocks. Upload a room photo as a visual reference while arranging furniture. A single photo is **not** automatically converted to a measured 3D room or furniture layout.
- Set each sensor's name, trigger threshold, and comma-separated sound watchlist. Watchlist matches are highlighted **after** the Mac classifies a recording; a watchlist does not suppress other recordings.
- Open an event to see classifier candidates, sensor readings, an approximate map position, and playable clips from each reporting phone.

## Measurement and location limits

The trigger uses **dBFS**, a level relative to each phone's digital microphone signal. It is not calibrated acoustic **dB SPL**. Different phones, gain controls, and distances can yield different values. Tune each threshold in its actual position.

One reporting sensor identifies only its own position. With two or more phones reporting the same event within about 2.5 seconds, the map shows a level-weighted approximate position. Phone microphones are not calibrated or synchronized for accurate acoustic triangulation. The UI labels these estimates accordingly.

Classification uses Apple's built-in general sound model. Its labels and confidence are estimates, and an impact cannot prove that an object fell. The UI may show “Impact / possible fall.” If the classifier is unavailable, recordings are still stored and shown as unclassified.

## Architecture

```text
Phone microphone → local threshold detector → HTTPS WAV + metadata
       → Mac gateway → stored observation → Apple Sound Analysis
       → grouped event + rough spatial estimate → dashboard
```

The server has no external cloud dependency. The dashboard URL contains a private administrator key; phone links contain per-sensor pairing tokens. Share them only with trusted people on the local network. To stop all sensors, stop the server. To revoke one phone, remove its sensor in the dashboard.

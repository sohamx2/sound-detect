# ESP32 + INMP441 SoundDetect sensor

This firmware turns one ESP32 plus one INMP441 I²S microphone into an event sensor. It measures short-window RMS level on the ESP32, keeps a 200 ms pre-trigger buffer, records about 1.2 seconds after a threshold crossing, and sends the mono 16 kHz PCM WAV clip plus timestamp, peak dBFS, sample rate, and firmware ID to SoundDetect over HTTPS.

The firmware uses the existing `/api/sensor-config` and `/api/observations` endpoints. The current repository runs that gateway and Apple Sound Analysis classifier in `server.py` on the Mac. The ESP32 can point at another host only if that host runs a compatible SoundDetect API. A Raspberry Pi 5 backend with the Mac limited to UI is a separate deployment change; this firmware alone does not move classification off the Mac.

## Hardware and wiring

The included PlatformIO profiles target a classic ESP32 DevKit/WROOM board and ESP32-S3 DevKitC-1. Connect the INMP441 like this for the classic ESP32 profile:

| INMP441 | ESP32 DevKit/WROOM |
| --- | --- |
| VDD | 3V3 |
| GND | GND |
| SCK / BCLK | GPIO 26 |
| WS / LRCL | GPIO 25 |
| SD / DOUT | GPIO 33 |
| L/R | GND (selects the left I²S channel) |

Power the microphone from **3.3 V only**. Connect the microphone's SD/DOUT output to the ESP32's data-in pin, and share ground. Use USB to power and program the ESP32. Audio transfer to the gateway is wireless over Wi-Fi; the microphone needs no separate network connection.

For ESP32-S3 DevKitC-1, the `esp32-s3-devkitc-1` PlatformIO environment uses GPIO 4 (BCLK), GPIO 5 (WS), and GPIO 6 (DATA). Other ESP32 boards, including XIAO ESP32-S3, may use different available pins. Change `SOUNDDETECT_I2S_BCLK_PIN`, `SOUNDDETECT_I2S_WS_PIN`, and `SOUNDDETECT_I2S_DATA_PIN` build flags in `platformio.ini` to match your board, and avoid GPIOs reserved for flash or PSRAM.

## Configure the sensor

1. Start SoundDetect on the Mac with `python3 server.py` and open the Dashboard URL printed in the terminal.
2. In **Sensor network**, click **Add sensor** and choose **ESP32 + INMP441**. This creates the server-side node record and gives it a room position. In its editor, copy the ESP32 credentials or copy the pairing link and read its `sid` and `token` query values.
3. Find the Mac's current sensor address in the dashboard. It should look like `https://192.168.x.x:8443`. The ESP32 and Mac must be able to reach each other on the same Wi-Fi network.
4. Install VS Code and the PlatformIO IDE extension. Open `firmware/esp32_inmp441` as the PlatformIO project. Select `esp32dev` for a classic ESP32 DevKit/WROOM or `esp32-s3-devkitc-1` for that specific S3 DevKit board.
5. Copy `include/secrets.h.example` to `include/secrets.h`. Set the Wi-Fi name/password, gateway URL, sensor ID, and sensor token. Keep `secrets.h` private; it is ignored by Git.
6. Copy the complete PEM certificate from `data/tls/SoundDetect-CA.crt` into `SOUNDDETECT_CA` in `secrets.h`, replacing the placeholder text. This lets the ESP32 validate the local HTTPS server certificate. Keep `ALLOW_INSECURE_TLS_FOR_LAB` set to `0` for normal use.
7. Connect the ESP32 to the computer by USB, choose the serial port in PlatformIO, then run **Upload**. Open the PlatformIO Serial Monitor at **115200 baud**.
8. The monitor should report Wi-Fi connection, the sensor name and threshold, and live dBFS readings. Make a short sound above the threshold. The monitor should report an upload, and the event should appear in the dashboard.

The board synchronizes its clock with NTP so HTTPS certificate dates can be checked. If the network blocks NTP, configure an NTP server reachable on that network. `ALLOW_INSECURE_TLS_FOR_LAB=1` can help diagnose a certificate/time problem on a trusted, isolated lab network, but it disables server identity checks and should not be used for normal operation.

## Tune and test

- The firmware polls the dashboard every 8 seconds and applies the sensor's `thresholdDbfs` setting. Start with `-28 dBFS`, watch the serial level readings, then raise or lower the threshold for your room and mic placement.
- dBFS is a digital signal level, not calibrated acoustic dB SPL. Different microphone boards and gain paths produce different readings.
- The firmware needs two consecutive 20 ms frames above threshold. It then sends a clip with up to 200 ms of pre-roll and about one second after detection. Upload is synchronous, so the sensor can miss another event while it is sending a clip.
- The app will classify a received clip and show it under the sensor's configured room location. A single sensor does not triangulate source position.

## Network limits

Some college Wi-Fi networks block device-to-device traffic or require registering each device. Automatic IP selection in the Mac dashboard cannot bypass that policy. If the ESP32 joins Wi-Fi but cannot connect to the gateway, use a personal hotspot or travel router for both the Mac and ESP32, or ask the campus network administrator whether the ESP32 and Mac can communicate on port 8443.

The dashboard currently shows the server-side sensor as active when the ESP32 polls its configuration. The sensor uses its own status in serial output; it does not use a browser pairing page.
